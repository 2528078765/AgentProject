"""厂商能力声明 + 失败签名：把「这家支持什么、会怎么拒绝」从代码里挪进配置。

## 为什么要单独一层

前两轮真机踩出来的坑，本质是同一件事：**我们把「厂商会拒绝什么」散落在代码各处猜。**

1. 硅基流动的合成接口会返回 `HTTP 200 + text/plain + 0 字节`，耗时 0.18s。
   它把「拒绝」伪装成「成功」。我们把它当成网络抖动，退避重试 12 次 ——
   对着一个已经明确拒收的接口磕了两分半钟。
2. D-ID 的 `/images` 上传**不检查像素上限**（3072×4096 也返回 201），
   到 `/talks` 提交时才报 `InvalidFileSizeError: file size exceeded 10 MB` ——
   而那张 JPEG 只有 738KB，错误信息完全是误导的。

这两条都不是「参数写错了」，而是**厂商的行为契约**。它们应该和 url/model 一样，
作为配置的一部分被声明出来、被统一识别，而不是每接一家就在 handler 里写一堆 if。

## 三层内容

- **能力（能做什么）**：输出分辨率/画幅、音频时长与体积上限、图片尺寸上下限与格式。
  合成阶段据此协商画布，而不是硬编码 1080×1920。
- **签名（会怎么拒绝）**：`when` 匹配条件 -> `kind` 分类 + 人话解释 + 修复提示。
- **判定（这次算哪种）**：`classify()` 返回 `Rejection` 或 `None`。

`kind` 的三种取值，决定了上层该怎么反应：

- `rejected`：请求被拒（参数/像素/内容不合格）。**重试没有意义**，要改输入或换厂商。
- `transient`：网络抖动 / 服务端临时故障。**重试有意义**。
- `fatal`：认证、权限、额度。**重试和改输入都没用**，必须换厂商或充值。

区分这三者就是「快速失败并切换下一家，不靠猜、不硬磕」的全部内容。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# 能力
# --------------------------------------------------------------------------- #
# D-ID 实测：128×128 / 64×64 返回 400 InvalidImageResolutionError
#   "image resolution is too low - please use an image with a at least 160X160 pixels"
# D-ID 实测：2304×3072（7.08 Mpx）通过，3072×4096（12.58 Mpx）失败，
#   阈值与「解码后按 1 字节/像素算 <= 10MB」完全吻合 -> 10 * 1024 * 1024 像素。
DEFAULT_MIN_SIDE = 160
DEFAULT_MAX_PIXELS = 10 * 1024 * 1024


@dataclass(frozen=True)
class ImageLimits:
    """图片输入的硬约束。0 表示该项不限制。"""

    min_side: int = 0
    max_side: int = 0
    max_pixels: int = 0
    formats: tuple[str, ...] = ()
    # 上传响应里是否带人脸检测结果。带了就能在上传当场判断图能不能用（免费）。
    reports_faces: bool = False
    note: str = ""
    verified: bool = False
    source: str = ""

    def describe(self) -> str:
        bits = []
        if self.min_side:
            bits.append(f"最短边 >= {self.min_side}px")
        if self.max_side:
            bits.append(f"最长边 <= {self.max_side}px")
        if self.max_pixels:
            bits.append(f"总像素 <= {self.max_pixels / 1e6:.1f}Mpx")
        if self.formats:
            bits.append("格式 " + "/".join(self.formats))
        text = "；".join(bits) or "无已知限制"
        if bits and not self.verified:
            text += "［未实测］"
        return text


@dataclass(frozen=True)
class AudioLimits:
    max_s: float = 0.0
    max_mb: float = 0.0
    formats: tuple[str, ...] = ()
    note: str = ""
    verified: bool = False
    source: str = ""

    def describe(self) -> str:
        bits = []
        if self.max_s:
            bits.append(f"时长 <= {self.max_s:.0f}s")
        if self.max_mb:
            bits.append(f"体积 <= {self.max_mb:.0f}MB")
        if self.formats:
            bits.append("格式 " + "/".join(self.formats))
        text = "；".join(bits) or "无已知限制"
        if bits and not self.verified:
            text += "［未实测］"
        return text


@dataclass(frozen=True)
class OutputSpec:
    """这家实际能吐出来的画面规格。"""

    width: int = 0
    height: int = 0
    fps: int = 0
    aspect: str = ""
    note: str = ""
    # 这组数字是我们自己实测的，还是照厂商文档抄的？
    # 目标要求「未实测的能力必须明确标注」，所以它是一等字段，不是注释。
    verified: bool = False
    source: str = ""

    def describe(self) -> str:
        if not (self.width and self.height):
            return (self.note or "未声明") + ("（未实测）" if not self.verified else "")
        text = f"{self.width}×{self.height}"
        if self.fps:
            text += f" @{self.fps}fps"
        if self.aspect:
            text += f"（{self.aspect}）"
        if not self.verified:
            text += "［未实测］"
        if self.note:
            text += f" —— {self.note}"
        return text

    @property
    def declared(self) -> bool:
        return bool(self.width and self.height)


def _caps(cfg: dict | None) -> dict:
    return (cfg or {}).get("capabilities") or {}


def _provenance(cfg: dict | None) -> tuple[bool, str]:
    caps = _caps(cfg)
    return bool(caps.get("verified", False)), str(caps.get("source") or "")


def image_limits(cfg: dict | None) -> ImageLimits:
    raw = _caps(cfg).get("image") or {}
    verified, source = _provenance(cfg)
    return ImageLimits(
        min_side=int(raw.get("min_side", 0) or 0),
        max_side=int(raw.get("max_side", 0) or 0),
        max_pixels=int(raw.get("max_pixels", 0) or 0),
        formats=tuple(str(f).lower().lstrip(".") for f in (raw.get("formats") or ())),
        reports_faces=bool(raw.get("reports_faces", False)),
        note=str(raw.get("note") or ""),
        verified=verified,
        source=source,
    )


def audio_limits(cfg: dict | None) -> AudioLimits:
    raw = _caps(cfg).get("audio") or {}
    verified, source = _provenance(cfg)
    return AudioLimits(
        max_s=float(raw.get("max_s", 0) or 0),
        max_mb=float(raw.get("max_mb", 0) or 0),
        formats=tuple(str(f).lower().lstrip(".") for f in (raw.get("formats") or ())),
        note=str(raw.get("note") or ""),
        verified=verified,
        source=source,
    )


def output_spec(cfg: dict | None) -> OutputSpec:
    raw = _caps(cfg).get("output") or {}
    verified, source = _provenance(cfg)
    return OutputSpec(
        width=int(raw.get("width", 0) or 0),
        height=int(raw.get("height", 0) or 0),
        fps=int(raw.get("fps", 0) or 0),
        aspect=str(raw.get("aspect") or ""),
        note=str(raw.get("note") or ""),
        verified=verified,
        source=source,
    )


def capability_summary(cfg: dict | None) -> str:
    """一行说清这家声明了什么、这些数字可不可信。"""
    out = output_spec(cfg)
    img = image_limits(cfg)
    aud = audio_limits(cfg)
    bits = [f"输出 {out.describe()}"]
    if img.min_side or img.max_pixels or img.formats:
        bits.append(f"图片 {img.describe()}")
    if aud.max_s or aud.max_mb or aud.formats:
        bits.append(f"音频 {aud.describe()}")
    return "；".join(bits)


# --------------------------------------------------------------------------- #
# 图片尺寸协商
# --------------------------------------------------------------------------- #
@dataclass
class ImagePlan:
    """给某家厂商上传前，把图压到多大。"""

    width: int
    height: int
    scale: float
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def describe(self, src_w: int, src_h: int) -> str:
        if self.scale >= 1.0:
            return f"{src_w}×{src_h} 原样（未超限）"
        return (f"{src_w}×{src_h} -> {self.width}×{self.height}"
                f"（{self.scale * 100:.0f}%）")


def plan_image(
    src_w: int,
    src_h: int,
    limits: ImageLimits,
    prefer_long_side: int = 1280,
) -> ImagePlan:
    """算出一个既满足厂商硬约束、又尽量省流量的上传尺寸。

    三条规则，顺序有意义：

    1. **不超过 `max_pixels`** —— 这条最要命。D-ID 上传时不查，提交时才报
       `InvalidFileSizeError`，错误信息还说「超过 10MB」，而文件只有 738KB。
    2. 长边不超过 `prefer_long_side`（默认 1280）。数字人只用到脸，
       传 3072×4096 是纯粹浪费流量和时间。
    3. **不为了满足 `min_side` 去放大**。把 128×128 拉成 160×160 能骗过校验，
       但出来一定是糊的 —— 那是把问题从「失败」变成「静默的低质量」，
       比失败更糟。这种情况记进 issues，让上层明确报出来。
    """
    issues: list[str] = []
    if src_w <= 0 or src_h <= 0:
        return ImagePlan(src_w, src_h, 1.0, ["读不到图片尺寸"])

    short_side = min(src_w, src_h)
    if limits.min_side and short_side < limits.min_side:
        issues.append(
            f"图片最短边只有 {short_side}px，低于该厂商要求的 {limits.min_side}px。"
            f"放大到 {limits.min_side}px 能骗过校验，但画面一定是糊的 —— 换一张更清晰的照片。")

    scale = 1.0
    if limits.max_pixels and src_w * src_h > limits.max_pixels:
        scale = math.sqrt(limits.max_pixels / float(src_w * src_h))
        # 留 2% 余量：厂商可能按「>= 上限」判超，卡在边界上会随机失败
        scale *= 0.98
    if prefer_long_side:
        scale = min(scale, prefer_long_side / float(max(src_w, src_h)))
    if limits.max_side:
        scale = min(scale, limits.max_side / float(max(src_w, src_h)))

    width = max(1, int(round(src_w * scale)))
    height = max(1, int(round(src_h * scale)))

    # 压完仍然超上限（取整误差/极端长宽比），再收一档
    if limits.max_pixels and width * height > limits.max_pixels:
        scale *= math.sqrt(limits.max_pixels / float(width * height)) * 0.98
        width = max(1, int(round(src_w * scale)))
        height = max(1, int(round(src_h * scale)))

    return ImagePlan(width, height, scale, issues)


def negotiate_canvas(
    platform_w: int,
    platform_h: int,
    spec: OutputSpec,
) -> tuple[int, int, list[str]]:
    """决定数字人片段要按什么规格出，并说清楚「能做到什么程度」。

    返回 `(canvas_w, canvas_h, notes)`。

    **不假装能做到厂商做不到的事。** D-ID 的照片数字人实测只出 512×512，
    那就如实说「这家最高 512×512，成片 1080×1920 是放大来的」，
    而不是默默拉伸、让用户以为是 1080p 的清晰度。
    """
    notes: list[str] = []
    if not (spec.width and spec.height):
        return platform_w, platform_h, notes

    if (spec.width, spec.height) == (platform_w, platform_h):
        return platform_w, platform_h, notes

    src_px = spec.width * spec.height
    dst_px = platform_w * platform_h
    aspect_src = spec.width / float(spec.height)
    aspect_dst = platform_w / float(platform_h)

    if abs(aspect_src - aspect_dst) > 0.02:
        notes.append(
            f"该厂商输出 {spec.width}×{spec.height}（{aspect_src:.2f}:1），"
            f"与目标画布 {platform_w}×{platform_h}（{aspect_dst:.2f}:1）画幅不同 → "
            f"会按「模糊铺底 + 原比例居中」适配，不拉伸变形。")
    if dst_px > src_px * 1.05:
        notes.append(
            f"该厂商输出 {spec.width}×{spec.height} = {src_px / 1e6:.2f}Mpx，"
            f"目标 {platform_w}×{platform_h} = {dst_px / 1e6:.2f}Mpx → "
            f"画面是被放大的（{dst_px / src_px:.1f}×），清晰度上限由该厂商决定，"
            f"不会因为导出 1080p 就变清晰。")
    return platform_w, platform_h, notes


# --------------------------------------------------------------------------- #
# 失败签名
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Rejection:
    """一次失败被判定成什么。"""

    kind: str          # rejected | transient | fatal
    label: str
    hint: str = ""
    detail: str = ""
    matched: str = ""  # 命中的签名名字（来自配置）

    @property
    def worth_retrying(self) -> bool:
        return self.kind == "transient"

    def line(self) -> str:
        text = f"{self.label}"
        if self.detail:
            text += f"（{self.detail}）"
        return text


def _match_when(
    when: dict,
    status: int | None,
    ctype: str,
    nbytes: int,
    body_text: str,
) -> bool:
    """判断一条签名是否命中。所有条件都要满足（AND）。"""
    if "status" in when and status != when["status"]:
        return False
    if "status_in" in when and status not in set(when["status_in"]):
        return False
    if "status_min" in when and (status is None or status < int(when["status_min"])):
        return False
    if "ctype_has" in when and str(when["ctype_has"]).lower() not in (ctype or "").lower():
        return False
    if "max_bytes" in when and nbytes > int(when["max_bytes"]):
        return False
    if "min_bytes" in when and nbytes < int(when["min_bytes"]):
        return False
    if ("body_has" in when
            and str(when["body_has"]).lower() not in body_text.lower()):
        return False
    # JSON 响应里某个键等于某值，比如 D-ID 的 {"kind": "InvalidFileSizeError"}
    if "body_key" in when:
        spec = when["body_key"] or {}
        key = str(spec.get("key") or "")
        try:
            parsed = json.loads(body_text or "{}")
        except Exception:  # noqa: BLE001
            return False
        actual = parsed.get(key) if isinstance(parsed, dict) else None
        if "equals" in spec and actual != spec["equals"]:
            return False
        if "contains" in spec and str(spec["contains"]).lower() not in str(actual).lower():
            return False
    return True


def classify(
    status: int | None,
    content_type: str = "",
    nbytes: int = 0,
    body: bytes | str = b"",
    cfg: dict | None = None,
) -> Rejection | None:
    """把一次响应判定成 `Rejection`，或者返回 None 表示「没看出问题」。

    先看配置里声明的签名（厂商特有），再退到通用规则（HTTP 语义）。
    """
    body_text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body or "")

    for index, sig in enumerate((cfg or {}).get("failure_signatures") or []):
        when = sig.get("when") or {}
        if _match_when(when, status, content_type, nbytes, body_text):
            return Rejection(
                kind=str(sig.get("kind") or "rejected"),
                label=str(sig.get("label") or f"命中失败签名 #{index + 1}"),
                hint=str(sig.get("hint") or ""),
                detail=str(sig.get("detail") or ""),
                matched=str(sig.get("name") or f"#{index + 1}"),
            )

    # 通用规则：HTTP 语义。签名没覆盖到的时候靠它兜底。
    if status is None:
        return None
    if status in (401, 402, 403):
        return Rejection("fatal", f"认证/权限/额度被拒（HTTP {status}）",
                         hint="换 Key、充值，或者换一家厂商。改输入没有用。")
    if status == 429:
        return Rejection("transient", "限流（HTTP 429）",
                         hint="等一会儿再试；这是官方文档明确的限流响应。")
    if status >= 500:
        return Rejection("transient", f"服务端故障（HTTP {status}）")
    if status >= 400:
        return Rejection("rejected", f"请求被拒（HTTP {status}）",
                         hint="参数/输入不合格，重试同样会失败。")
    return None


# --------------------------------------------------------------------------- #
# 抠出响应里的证据，方便写进日志
# --------------------------------------------------------------------------- #
_TRACE_HEADERS = (
    "x-siliconcloud-trace-id",
    "x-request-id",
    "x-d-id-trace-id",
    "cf-ray",
    "traceparent",
)


def trace_ids(headers: Any) -> dict[str, str]:
    """把响应头里的追踪 ID 捞出来。

    硅基流动官方文档写明 `x-siliconcloud-trace-id` 是「请求的唯一追踪标识，
    便于日志查询和问题排查」—— 也就是说，这是给他们提工单时唯一有用的凭据。
    之前我们把它扔了，导致连查三轮都没定论。
    """
    found: dict[str, str] = {}
    if not headers:
        return found
    try:
        items = headers.items()
    except AttributeError:
        return found
    for key, value in items:
        if key.lower() in _TRACE_HEADERS and value:
            found[key.lower()] = str(value)
    return found


def describe_evidence(
    status: int | None,
    content_type: str,
    nbytes: int,
    elapsed: float | None = None,
    headers: Any = None,
) -> str:
    """一行把「这次响应长什么样」说全，用于日志和报错。"""
    bits = [f"HTTP {status}", (content_type or "无 Content-Type"), f"{nbytes} 字节"]
    if elapsed is not None:
        bits.append(f"{elapsed:.2f}s")
    traces = trace_ids(headers)
    if traces:
        bits.append(" ".join(f"{k}={v}" for k, v in traces.items()))
    return " | ".join(bits)
