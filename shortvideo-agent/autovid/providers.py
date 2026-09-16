"""可插拔 Provider 层：LLM 改写 / TTS 音色 / 出图 / 数字人 / 发布。

每个环节都是「协议 + 多个实现」，通过 config/pipeline.json 的 `provider` 字段切换。
离线实现（offline / ffmpeg_gradient / still / package）保证整条链路在
**没有 GPU、没有外网、没有 API Key** 的机器上也能端到端跑通。

⚠️ 标注 [未实测] 的 provider 是云服务实现：本机开发环境没有外网，无法验证。
   首次使用请先单步调试（`python -m autovid step voice --run <id>`），
   并对照官方文档确认请求/响应字段。
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import random
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from . import media as M
from . import provider_caps as CAP
from .assets import AssetStore
from .errors import AutoVidError


def _ctype(headers: Any) -> str:
    """安全地取 Content-Type（HTTPError.headers 可能是 None）。"""
    if headers is None:
        return ""
    try:
        return str(headers.get("Content-Type") or "")
    except Exception:  # noqa: BLE001
        return ""

# --------------------------------------------------------------------------- #
# HTTP 小工具（标准库，无 requests 依赖）
# --------------------------------------------------------------------------- #
class ProviderError(AutoVidError):
    """Provider 调用失败。继承 AutoVidError，保证上层用统一类型就能兜住所有可预期错误。"""

    def __init__(
        self,
        message: str,
        *,
        transient: bool = False,
        status: int | None = None,
        content_type: str = "",
        body: bytes = b"",
        headers: Any = None,
    ) -> None:
        super().__init__(message)
        # transient=True 表示「这次失败是网络/服务端抖动，重试有意义」。
        # 关键区别：4xx（参数错、Key 错）重试一万次也没用，必须立刻报出来；
        # 5xx / 429 / SSL 断流只是这一次不走运，重试就能过。
        self.transient = transient
        # 证据。带着它，上层才能拿**厂商声明的失败签名**去判定这次到底算哪种，
        # 而不是靠异常文本做正则。硅基流动的 trace-id、D-ID 的 {"kind": ...}
        # 都从这里流出去。
        self.status = status
        self.content_type = content_type
        self.body = body or b""
        self.headers = headers


# 值得重试的 HTTP 状态码：限流 + 各类服务端临时故障（含 Cloudflare 522/524）。
_TRANSIENT_CODES = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 522, 523, 524})


def _is_transient(exc: BaseException) -> bool:
    """这次失败到底值不值得重试。"""
    if isinstance(exc, ProviderError):
        return bool(exc.transient)
    if isinstance(exc, (ssl.SSLError, socket.timeout, TimeoutError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        return True                       # DNS 抖动 / 连接被重置 / 代理断流
    if isinstance(exc, http.client.HTTPException):
        return True                       # IncompleteRead / RemoteDisconnected
    if isinstance(exc, ConnectionError):
        return True
    return False


def _worth_retrying(exc: BaseException, cfg: dict | None = None) -> bool:
    """这次失败值不值得重试。**先看厂商声明的签名，再看通用网络语义。**

    顺序很重要：签名是厂商的行为契约（比如硅基流动的「200 + 空体」虽然看着
    像抖动，实际是明确拒收），比通用规则更准。没有签名覆盖时才退回
    `_is_transient` 那套 SSL/超时/5xx 判断。
    """
    if cfg:
        rej = CAP.classify(
            getattr(exc, "status", None),
            getattr(exc, "content_type", ""),
            len(getattr(exc, "body", b"") or b""),
            getattr(exc, "body", b"") or b"",
            cfg,
        )
        if rej is not None:
            return rej.worth_retrying
    return _is_transient(exc)


def _retry(
    fn: Callable[[], Any],
    *,
    what: str,
    log: Callable[[str], None] = print,
    attempts: int = 6,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    cfg: dict | None = None,
) -> Any:
    """带指数退避的重试。

    起因是一次真实事故：D-ID 分片 3 轮询时 SSL 断了（UNEXPECTED_EOF_WHILE_READING），
    整条工作流被中止 —— 前两个分片白跑。网络抖动不该终结一次跑了十几分钟的任务。

    `cfg` 传进来后，重试与否由**该厂商声明的失败签名**决定：声明成 rejected 的
    一次都不重试（快速失败、换下一家），声明成 transient 的才退避重试。
    """
    last: BaseException | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return fn()
        except BaseException as exc:          # noqa: BLE001 - 分类交给 _worth_retrying
            last = exc
            if attempt >= attempts or not _worth_retrying(exc, cfg):
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            log(f"    ⚠ {what} 第 {attempt} 次失败"
                f"（{type(exc).__name__}: {str(exc)[:90]}），{delay:.0f}s 后重试…")
            time.sleep(delay)
    raise last if last else ProviderError(f"{what} 重试耗尽")


def _http(
    url: str,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: int = 120,
    method: str | None = None,
) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    merged = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=merged, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise ProviderError(f"HTTP {exc.code} <- {url}\n{detail}",
                            transient=exc.code in _TRANSIENT_CODES,
                            status=exc.code, content_type=_ctype(exc.headers),
                            body=detail.encode("utf-8", "replace"),
                            headers=exc.headers) from exc
    except urllib.error.URLError as exc:
        raise ProviderError(
            f"无法连接 {url}: {exc.reason}\n"
            "  - 若是云服务，检查网络/代理；本机沙箱环境默认禁止外网。",
            transient=True,
        ) from exc
    except (ssl.SSLError, http.client.HTTPException, socket.timeout, TimeoutError) as exc:
        raise ProviderError(f"连接 {url} 时中断：{type(exc).__name__}: {exc}",
                            transient=True) from exc
    if not body.strip():
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"响应不是合法 JSON <- {url}\n{body[:400]}") from exc


def _http_download(url: str, out: Path, headers: dict[str, str] | None = None, timeout_s: int = 600) -> Path:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            out.write_bytes(resp.read())
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"下载失败 {url}: {exc}",
                            transient=exc.code in _TRANSIENT_CODES) from exc
    except (urllib.error.URLError, ssl.SSLError, http.client.HTTPException,
            socket.timeout, TimeoutError) as exc:
        raise ProviderError(f"下载失败 {url}: {type(exc).__name__}: {exc}",
                            transient=True) from exc
    return out


def _auth_header(api_key: str | None) -> dict[str, str]:
    if not api_key:
        raise ProviderError("缺少 API Key。请在 config/secrets.json 或环境变量里配置。")
    return {"Authorization": f"Bearer {api_key}"}


# --------------------------------------------------------------------------- #
# Provider 可用性自检
# --------------------------------------------------------------------------- #
@dataclass
class ProviderStatus:
    kind: str
    name: str
    available: bool
    detail: str


def _has_module(name: str) -> bool:
    return shutil.which(name) is not None


def _python_module_available(module: str) -> bool:
    try:
        __import__(module)
        return True
    except Exception:
        return False


def provider_statuses(config: Any) -> list[ProviderStatus]:
    statuses: list[ProviderStatus] = []
    llm_key = config.secret_for("llm", "AUTOVID_LLM_API_KEY")
    statuses.append(ProviderStatus("llm", "offline", True, "内置模板，无需网络"))
    statuses.append(ProviderStatus("llm", "openai_compat", bool(llm_key),
                                   "已配置 Key" if llm_key else "缺少 API Key（config/secrets.json）"))

    cloud_cfg = config.provider_cfg("cloud_tts")
    cloud_key = config.secret_for("cloud_tts", "AUTOVID_CLOUD_TTS_KEY")
    cloud_model = str(cloud_cfg.get("model") or "未指定模型")
    statuses.append(ProviderStatus("tts", "edge_native", True,
                                   "零依赖的 Edge 在线 TTS（推荐；需要外网）"))
    statuses.append(ProviderStatus("tts", "sapi", True,
                                   "Windows 内置；但受限/非交互会话下会被系统拒绝，本项目实测经常失败"))
    edge_ok = _python_module_available("edge_tts") or _has_module("edge-tts")
    statuses.append(ProviderStatus("tts", "edge", edge_ok,
                                   "已安装 edge-tts" if edge_ok
                                   else "未安装，且本机 pip 装不了：改用 edge_native"))
    cloud_cfg2 = config.provider_cfg("cloud_tts")
    cloud_ready = bool(cloud_key) and bool(cloud_cfg2.get("url"))
    statuses.append(ProviderStatus(
        "tts", "cloud_tts", cloud_ready,
        f"云端克隆已就绪（{cloud_model}）" if cloud_ready
        else "需要 Key + providers.cloud_tts.url（能用你的音色）"))
    http_tts_url = str(config.provider_cfg("http_tts").get("url") or "")
    statuses.append(ProviderStatus(
        "tts", "http_json", bool(http_tts_url),
        "自建 TTS 已配地址" if http_tts_url
        else "需要 providers.http_tts.url（自建 GPT-SoVITS，需 NVIDIA 卡）"))
    try:
        from .qwen3tts import model_ready as qwen3tts_ready  # noqa: PLC0415
        qwen_ok = bool(qwen3tts_ready(config))
        qwen_detail = ("本地 Qwen3-TTS：你的音色（克隆一次反复用）" if qwen_ok
                       else "还没部署：python scripts/deploy_qwen3tts.py")
    except Exception as exc:  # noqa: BLE001
        qwen_ok = False
        qwen_detail = f"运行依赖不可用：{type(exc).__name__}: {exc}"
    statuses.append(ProviderStatus(
        "tts", "local_qwen_tts", qwen_ok, qwen_detail))

    # 用户在设置面板里配的付费 API —— 让它们也出现在下拉框里
    from .providers_registry import availability, entry_name, load_entries  # noqa: PLC0415
    for entry in load_entries(config):
        kind = str(entry.get("kind") or "")
        if kind not in ("voice", "avatar") or not entry.get("enabled", True):
            continue
        ok, note = availability(config, entry)
        statuses.append(ProviderStatus(
            "tts" if kind == "voice" else "avatar",
            entry_name(entry), ok,
            f"{entry.get('label')}（{entry.get('preset') or '自定义'}）：{note}"))
    statuses.append(ProviderStatus("tts", "silent", True,
                                   "静音占位，不出声；只用于验证链路"))

    statuses.append(ProviderStatus("image", "ffmpeg_gradient", M.has_ffmpeg(), "离线渐变背景"))
    comfy_cfg = config.provider_cfg("comfy")
    comfy_img = str(comfy_cfg.get("workflow") or "")
    comfy_img_ok = bool(comfy_img) and config.path(comfy_img).exists()
    statuses.append(ProviderStatus(
        "image", "comfy", comfy_img_ok,
        "ComfyUI 出图（工作流已就绪）" if comfy_img_ok
        else "需要一份出图工作流：providers.comfy.workflow"))
    img_key = config.secret_for("openai_images", "AUTOVID_IMAGE_API_KEY")
    statuses.append(ProviderStatus("image", "openai_images", bool(img_key),
                                   "已配置 Key" if img_key else "缺少 API Key"))

    statuses.append(ProviderStatus("avatar", "still", M.has_ffmpeg(), "离线：照片合成到背景（图文口播）"))
    try:
        from .wav2lip import models_ready as wav2lip_ready  # noqa: PLC0415
        wav2lip_ok = bool(wav2lip_ready())
        wav2lip_detail = ("本地 Wav2Lip：嘴会跟着声音动（照片驱动）" if wav2lip_ok
                          else "还没部署：python scripts/deploy_wav2lip.py")
    except Exception as exc:  # noqa: BLE001
        wav2lip_ok = False
        wav2lip_detail = f"运行依赖不可用：{type(exc).__name__}: {exc}"
    statuses.append(ProviderStatus(
        "avatar", "local_wav2lip", wav2lip_ok, wav2lip_detail))
    comfy_av = str(comfy_cfg.get("avatar_workflow") or "")
    comfy_av_ok = bool(comfy_av) and config.path(comfy_av).exists()
    statuses.append(ProviderStatus(
        "avatar", "comfy", comfy_av_ok,
        "ComfyUI 数字人（工作流已就绪，会动嘴）" if comfy_av_ok
        else "需要一份数字人工作流：providers.comfy.avatar_workflow"))
    avatar_http_url = str(config.provider_cfg("avatar_http").get("submit_url") or "")
    statuses.append(ProviderStatus(
        "avatar", "http_job", bool(avatar_http_url),
        "数字人服务已配地址" if avatar_http_url
        else "需要 providers.avatar_http.submit_url（云端数字人，会动嘴）"))
    mm_key = config.secret_for("minimax_h3", "AUTOVID_MINIMAX_API_KEY")
    statuses.append(ProviderStatus(
        "avatar", "minimax_h3", bool(mm_key),
        "云端 Key 已配置（权重本身需 NVIDIA 大显存，跑不了）" if mm_key
        else "需要 MiniMax API Key"))

    statuses.append(ProviderStatus("publish", "package", True, "生成发布包，人工上传（推荐）"))
    statuses.append(ProviderStatus("publish", "playwright_douyin", False, "P1 未实现：有封号风险"))
    return statuses


# =========================================================================== #
# 1. LLM —— 文案改写
# =========================================================================== #
SCRIPT_SYSTEM_PROMPT = """你是一位专精抖音竖屏口播的文案操盘手，擅长老百姓听得懂、愿意听完的表达。

硬性要求：
1. 前 3 秒必须抛出钩子，直接给冲突/结论/反常识，禁止「大家好我是XX」式开场。
2. 全文口语化：短句、主语明确、不用书面连接词（因此/综上所述/首先其次）。
3. 每个句号或问号处都必须是能独立成镜头的完整语义单元，句子控制在 25 字以内。
4. 禁止编造数据、禁止绝对化承诺（最/第一/保证收益）。
5. 结尾给一个自然的互动引导，不要硬广式逼单。

只输出 JSON，不要任何解释、不要 Markdown 代码块。JSON 结构：
{
  "hook": "开头钩子，<=30字",
  "segments": [
    {"headline": "该段上屏关键词，<=10字", "text": "该段口播文本", "visual_prompt": "该段背景画面的英文描述"}
  ],
  "title_options": ["标题候选1", "标题候选2", "标题候选3"],
  "description": "发布简介，120字以内",
  "tags": ["话题标签，不带#", "..."],
  "cta": "结尾互动引导"
}"""


def llm_rewrite(
    config: Any,
    topic: str,
    source_text: str = "",
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """按配置选择 LLM provider 产出结构化口播稿。"""
    provider = str(config.provider_of("script"))
    target_words = int(config.get("steps.script.target_words", 260))
    segment_count = int(config.get("steps.script.segments", 5))

    if provider == "openai_compat":
        return _llm_openai_compat(config, topic, source_text, target_words, segment_count, log)
    return _llm_offline(config, topic, source_text, target_words, segment_count, log)


def _llm_openai_compat(
    config: Any,
    topic: str,
    source_text: str,
    target_words: int,
    segment_count: int,
    log,
) -> dict[str, Any]:
    """[云] 任何 OpenAI 兼容端点：DeepSeek / 通义 / 智谱 / 本地 vLLM。"""
    cfg = config.provider_cfg("llm")
    base_url = str(cfg.get("base_url", "")).rstrip("/")
    if not base_url:
        raise ProviderError("providers.llm.base_url 未配置")
    api_key = config.secret_for("llm", "AUTOVID_LLM_API_KEY")

    user_parts = [
        f"选题：{topic}",
        f"目标总字数：{target_words} 字左右",
        f"分段数量：{segment_count} 段",
    ]
    if source_text.strip():
        user_parts.append(
            "以下是需要你消化后重新表达的原始素材。"
            "注意：必须用自己的语言重构，不得逐句照搬，不得保留原文的独特措辞：\n"
            f"<素材>\n{source_text.strip()[:6000]}\n</素材>"
        )
    user_parts.append("请按系统提示的 JSON 结构输出。")

    payload = {
        "model": cfg.get("model", "deepseek-chat"),
        "temperature": float(cfg.get("temperature", 0.8)),
        "messages": [
            {"role": "system", "content": SCRIPT_SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ],
    }
    log(f"调用 LLM: {base_url} model={payload['model']}")
    data = _http(
        f"{base_url}/chat/completions",
        payload,
        headers=_auth_header(api_key),
        timeout_s=int(cfg.get("timeout_s", 120)),
    )
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"LLM 响应结构异常：{json.dumps(data, ensure_ascii=False)[:500]}") from exc

    parsed = _extract_json(content)
    parsed["provider"] = "openai_compat"
    return _normalize_script(parsed, topic, target_words, log)


def _extract_json(text: str) -> dict[str, Any]:
    """LLM 经常裹着 ```json 或前后废话，这里做容错提取。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ProviderError(f"LLM 没返回 JSON：{text[:400]}")
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ProviderError(f"LLM 返回的 JSON 解析失败：{exc}\n{cleaned[start:end + 1][:400]}") from exc


def _llm_offline(
    config: Any,
    topic: str,
    source_text: str,
    target_words: int,
    segment_count: int,
    log,
) -> dict[str, Any]:
    """离线占位实现。

    它的价值不是「写得好」，而是让整条流水线在无网络时也能跑通、可测。
    真正的文案质量必须接 LLM（把 steps.script.provider 改成 openai_compat）。
    如果你手里已经有写好的文案，用 `--script-file` 传入，会被自动切段。
    """
    if source_text.strip():
        log("离线模式：按 --script-file 提供的文案切段（不做改写）")
        return _script_from_text(source_text, topic, target_words, segment_count, log)

    log("离线模式：生成结构化占位稿。要真正改写请配置 steps.script.provider=openai_compat")
    body_parts = [
        f"先说结论：{topic}这件事，大多数人第一步就做错了。",
        f"第一，别急着上手。先把{topic}的目标拆成一个本周就能验证的小动作。",
        "第二，把过程记录下来。不是为了发朋友圈，是为了让下一次的自己有据可依。",
        "第三，只优化一个变量。一次改一个地方，你才知道到底是哪一步起了作用。",
        "第四，给自己设一个止损线。到点没结果就换方向，别用坚持掩盖方向错误。",
    ]
    while len(body_parts) - 1 < segment_count:
        body_parts.insert(-1, f"还有一个细节常被忽略：{topic}真正拉开差距的是执行密度，不是方法本身。")

    segments = [
        {"headline": f"{topic}别踩坑", "text": body_parts[0],
         "visual_prompt": f"clean minimal background about {topic}"}
    ]
    for index, text in enumerate(body_parts[1:], start=1):
        segments.append(
            {
                "headline": f"要点{index}",
                "text": text,
                "visual_prompt": f"abstract gradient background, concept {index} of {topic}",
            }
        )
    return _normalize_script(
        {
            "hook": body_parts[0],
            "segments": segments,
            "title_options": [
                f"{topic}，90%的人第一步就错了",
                f"关于{topic}，我只说4句话",
                f"{topic}做对这件事，效率翻倍",
            ],
            "description": f"关于{topic}的四个关键动作，看完就能用。",
            "tags": [topic, "干货分享", "认知提升", "经验分享", "自我提升", "方法"],
            "cta": "你现在卡在哪一步？评论区说一句，我挑几个展开讲。",
            "provider": "offline",
        },
        topic,
        target_words,
        log,
    )


def _script_from_text(text: str, topic: str, target_words: int, segment_count: int, log) -> dict[str, Any]:
    """把已有文案切成镜头段：按标点断句 -> 聚合成 N 段。"""
    sentences = _split_sentences(text)
    if not sentences:
        raise ProviderError("提供的文案为空或无法切分")
    groups = _group_sentences(sentences, segment_count)
    segments = [
        {
            "headline": _headline_of(group),
            "text": "".join(group).strip(),
            "visual_prompt": f"background about {topic}",
        }
        for group in groups
    ]
    return _normalize_script(
        {
            "hook": segments[0]["text"] if segments else topic,
            "segments": segments,
            "title_options": [topic, f"{topic}的完整思路", f"一次讲清{topic}"],
            "description": text.strip()[:120],
            "tags": [topic, "干货分享", "经验分享"],
            "cta": "有用的话点个赞，下条继续讲。",
            "provider": "offline-from-file",
        },
        topic,
        target_words,
        log,
    )


_SENTENCE_END = "。！？!?；;…"


def _split_sentences(text: str) -> list[str]:
    out: list[str] = []
    current = ""
    for char in text.replace("\r", ""):
        if char == "\n":
            if current.strip():
                out.append(current.strip())
            current = ""
            continue
        current += char
        if char in _SENTENCE_END:
            out.append(current.strip())
            current = ""
    if current.strip():
        out.append(current.strip())
    return [s for s in out if s]


def _group_sentences(sentences: list[str], groups: int) -> list[list[str]]:
    """把句子按字数均衡地聚成 groups 组。"""
    groups = max(1, min(groups, len(sentences)))
    total = sum(len(s) for s in sentences)
    target = total / groups
    result: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for index, sentence in enumerate(sentences):
        current.append(sentence)
        current_len += len(sentence)
        remaining_groups = groups - len(result) - 1
        remaining_sentences = len(sentences) - index - 1
        if remaining_groups > 0 and current_len >= target and remaining_sentences >= remaining_groups:
            result.append(current)
            current, current_len = [], 0
    if current:
        result.append(current)
    return result


def _headline_of(group: Sequence[str]) -> str:
    first = group[0] if group else ""
    for char in "，。！？；：,.:;":
        first = first.split(char)[0]
    return first[:10] if first else "要点"


def _normalize_script(raw: dict[str, Any], topic: str, target_words: int, log) -> dict[str, Any]:
    """把 LLM/模板输出规整成稳定契约，任何一步都不允许下游拿到畸形结构。"""
    segments_raw = raw.get("segments") or []
    segments: list[dict[str, Any]] = []
    for index, item in enumerate(segments_raw):
        if isinstance(item, str):
            item = {"text": item}
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        segments.append(
            {
                "id": f"s{index + 1:02d}",
                "index": index,
                "headline": str(item.get("headline", "")).strip() or _headline_of([text]),
                "text": text,
                "visual_prompt": str(item.get("visual_prompt", "")).strip() or f"background about {topic}",
                "chars": len(text),
            }
        )
    if not segments:
        raise ProviderError("口播稿没有任何有效分段，请检查 LLM 输出")

    word_count = sum(s["chars"] for s in segments)
    cps = float(4.6)
    result = {
        "topic": topic,
        "hook": str(raw.get("hook", segments[0]["text"])).strip(),
        "segments": segments,
        "title_options": [str(t).strip() for t in (raw.get("title_options") or []) if str(t).strip()],
        "description": str(raw.get("description", "")).strip(),
        "tags": [str(t).strip().lstrip("#") for t in (raw.get("tags") or []) if str(t).strip()],
        "cta": str(raw.get("cta", "")).strip(),
        "provider": raw.get("provider", "unknown"),
        "word_count": word_count,
        "target_words": target_words,
        "estimated_duration_s": round(word_count / cps, 2),
    }
    if not result["title_options"]:
        result["title_options"] = [topic]
    log(f"口播稿完成：{len(segments)} 段 / {word_count} 字 / 预估 {result['estimated_duration_s']}s")
    return result


# =========================================================================== #
# 2. TTS —— 音色克隆 / 语音合成
# =========================================================================== #
@dataclass
class TTSResult:
    provider: str
    parts: list[Path]
    note: str = ""
    cloned: bool = False          # 是否真的用到了用户上传的音色
    voice_id: str | None = None
    voice_name: str = ""


def tts_synthesize(
    config: Any,
    segments: Sequence[dict[str, Any]],
    out_dir: Path,
    log: Callable[[str], None] = print,
    voice_asset: Any = None,
) -> TTSResult:
    """按 provider + fallback 链合成每段语音。

    逐段合成而不是整篇合成，是为了让字幕时间轴精确到句 ——
    这样不需要 ASR 反推时间戳，字幕天然与口型对齐。

    `voice_asset` 是用户上传的音色资产。只有支持克隆的 provider 会真正用到它；
    其余 provider 会**明确告知「本次没有用你的音色」**，而不是默默用内置音色糊弄过去。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    primary = str(config.provider_of("voice"))
    fallback = list(config.step_cfg("voice").get("fallback", []) or [])
    strict = bool(config.step_cfg("voice").get("strict", False))
    chain = [primary] + ([] if strict else [p for p in fallback if p != primary])

    if voice_asset is not None:
        has_ref = bool(getattr(voice_asset, "ref_audio", None))
        log(f"已选音色资产：{voice_asset.name}（{voice_asset.id}）"
            f"{f'，参考音频 {voice_asset.duration_s}s' if has_ref else '，但还没有参考音频'}")
    elif str(config.get("project.voice_id", "")).strip():
        log(f"⚠ 配置指定了 voice_id={config.get('project.voice_id')}，但资产库里找不到")

    errors: list[str] = []
    last_error: Exception | None = None
    for name in chain:
        handler = _TTS_PROVIDERS.get(name)
        kwargs: dict[str, Any] = {}
        if handler is None and name.startswith("voice:"):
            # 用户在「设置」页配的付费 API：复用同一套模板引擎，
            # 只是把该条目的 cfg / key / 名字传进去，不必为每家写代码。
            from .providers_registry import resolve as _resolve_custom  # noqa: PLC0415
            custom = _resolve_custom(config, name)
            if custom is None:
                errors.append(f"{name}: 设置里找不到这个提供商（可能已删除）")
                continue
            handler = _tts_cloud
            kwargs = {"cfg": custom["cfg"], "api_key": custom["api_key"],
                      "provider_name": name}
            log(f"TTS provider = {name}（{custom['label']}）")
        if handler is None:
            errors.append(f"{name}: 未知 provider")
            continue
        try:
            if not kwargs:
                log(f"TTS provider = {name}")
            result = handler(config, segments, out_dir, log, voice_asset, **kwargs)
            if voice_asset is not None:
                result.voice_id = voice_asset.id
                result.voice_name = voice_asset.name
                if not result.cloned:
                    result.note += (
                        f" | ⚠ 本次**没有**使用你的音色「{voice_asset.name}」："
                        f"{name} 不支持音色克隆"
                    )
            if result.note:
                log(f"  {result.note}")
            if errors:
                result.note += " | 之前失败: " + "; ".join(errors)
            return result
        except Exception as exc:  # noqa: BLE001 - 回退链需要吞掉所有异常
            last_error = exc
            message = f"{name}: {type(exc).__name__}: {exc}"
            errors.append(message)
            log(f"  ✗ {name} 失败：{str(exc).splitlines()[0][:160]}")
    if strict and len(chain) == 1 and last_error is not None:
        first = str(last_error).strip().splitlines()[0]
        raise ProviderError(f"语音 API「{primary}」失败：{first}") from last_error
    raise ProviderError("所有 TTS provider 都失败：\n  - " + "\n  - ".join(errors))


def _tts_sapi(config: Any, segments, out_dir: Path, log, voice_asset=None) -> TTSResult:
    """Windows SAPI。离线、免费，但在受限/非交互会话下可能被系统拒绝。"""
    cfg = config.step_cfg("voice")
    parts = [out_dir / f"seg_{i:02d}.wav" for i in range(len(segments))]
    jobs = [(p, str(s["text"])) for p, s in zip(parts, segments)]
    # voice 是 Edge 音色名（如 zh-CN-XiaoxiaoNeural），SAPI 用的是完全不同的
    # Windows 音色名（如 Microsoft Huihui Desktop），所以单独一个字段。
    sapi_voice = str(cfg.get("sapi_voice") or cfg.get("voice") or "")
    M.sapi_synthesize(
        jobs,
        voice=sapi_voice,
        rate=int(cfg.get("rate", 0)),
        volume=int(cfg.get("volume", 100)),
        sample_rate=int(cfg.get("sample_rate", 24000)),
        log=log,
    )
    return TTSResult("sapi", parts, f"voice={sapi_voice or '默认'}")


def _tts_edge(config: Any, segments, out_dir: Path, log, voice_asset=None) -> TTSResult:
    """[未实测] edge-tts：免费、无需 Key、中文自然度好。需要 pip install edge-tts。"""
    if not (_python_module_available("edge_tts") or _has_module("edge-tts")):
        raise ProviderError("未安装 edge-tts。安装：pip install edge-tts")
    cfg = config.provider_cfg("edge_tts")
    voice = str(cfg.get("voice", "zh-CN-XiaoxiaoNeural"))
    rate = str(cfg.get("rate", "+0%"))
    sample_rate = int(config.step_cfg("voice").get("sample_rate", 24000))

    parts: list[Path] = []
    for index, segment in enumerate(segments):
        mp3 = out_dir / f"seg_{index:02d}.mp3"
        wav = out_dir / f"seg_{index:02d}.wav"
        cmd = [
            sys.executable, "-m", "edge_tts",
            "--voice", voice, "--rate", rate,
            "--text", str(segment["text"]),
            "--write-media", str(mp3),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=300)
        if proc.returncode != 0 or not mp3.exists():
            raise ProviderError(f"edge-tts 第 {index} 段失败：{(proc.stderr or '').strip()[:300]}")
        M.run_ffmpeg(["-i", str(mp3), "-ac", "1", "-ar", str(sample_rate),
                      "-c:a", "pcm_s16le", str(wav)], desc="edge-tts 转 WAV")
        mp3.unlink(missing_ok=True)
        parts.append(wav)
    return TTSResult("edge", parts, f"voice={voice}")


def _tts_http_json(config: Any, segments, out_dir: Path, log, voice_asset=None) -> TTSResult:
    """[未实测] 通用自建 TTS：GPT-SoVITS / CosyVoice 等服务。

    约定：POST {url}  body={"text": "...", "ref_audio": "...", "prompt_text": "..."}
          返回音频二进制流。
    不同项目的字段名不一样，请按你的部署改这里。
    """
    cfg = config.provider_cfg("http_tts")
    url = str(cfg.get("url", ""))
    if not url:
        raise ProviderError("providers.http_tts.url 未配置")

    # 用户上传的音色优先于配置里写死的参考音频 —— 这才是「上传自己的音色」的落点
    ref_audio = cfg.get("ref_audio", "")
    prompt_text = cfg.get("prompt_text", "")
    cloned = False
    if voice_asset is not None and getattr(voice_asset, "ref_audio", None):
        ref_path = AssetStore(config).voice_reference(voice_asset.id)
        if ref_path is not None:
            ref_audio = str(ref_path)
            prompt_text = voice_asset.ref_text or prompt_text
            cloned = True
            log(f"使用你的音色「{voice_asset.name}」作为参考音频："
                f"{voice_asset.duration_s}s 样本")

    payload = {"text": "", "ref_audio": ref_audio, "prompt_text": prompt_text}
    parts: list[Path] = []
    for index, segment in enumerate(segments):
        payload["text"] = str(segment["text"])
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        out = out_dir / f"seg_{index:02d}.wav"
        try:
            with urllib.request.urlopen(req, timeout=int(cfg.get("timeout_s", 300))) as resp:
                out.write_bytes(resp.read())
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"自建 TTS 第 {index} 段失败：{exc}") from exc
        parts.append(out)
    note = f"url={url}"
    if cloned:
        note = f"已用你的音色克隆（{ref_audio}） | {note}"
    return TTSResult("http_json", parts, note, cloned=cloned)


def _tts_edge_native(config: Any, segments, out_dir: Path, log, voice_asset=None) -> TTSResult:
    """零依赖的 Edge 在线 TTS（自实现 WebSocket 传输，见 edge_tts_native.py）。

    免费、无需 API Key、中文自然度好，而且不依赖 aiohttp —— 在装不了包的机器上
    这是最现实的一条「能真正出声」的路。需要外网。
    """
    from . import edge_tts_native as E

    cfg = config.provider_cfg("edge_tts")
    voice = str(cfg.get("voice") or E.DEFAULT_VOICE)
    rate = str(cfg.get("rate", "+0%"))
    volume = str(cfg.get("volume", "+0%"))
    pitch = str(cfg.get("pitch", "+0Hz"))
    sample_rate = int(config.step_cfg("voice").get("sample_rate", 24000))

    parts: list[Path] = []
    for index, segment in enumerate(segments):
        result = E.synthesize(str(segment["text"]), voice=voice,
                              rate=rate, volume=volume, pitch=pitch)
        mp3 = out_dir / f"seg_{index:02d}.mp3"
        mp3.write_bytes(result["audio"])
        wav = out_dir / f"seg_{index:02d}.wav"
        M.run_ffmpeg(
            ["-i", str(mp3), "-ac", "1", "-ar", str(sample_rate),
             "-c:a", "pcm_s16le", str(wav)],
            desc=f"Edge TTS 第 {index + 1} 段转 WAV",
        )
        mp3.unlink(missing_ok=True)
        parts.append(wav)
    return TTSResult("edge_native", parts,
                     f"voice={voice}（微软 Edge 在线 TTS，免费无需 Key）")


def _render_template(value: Any, ctx: dict[str, Any]) -> Any:
    """递归替换模板里的 {{var}}。

    请求体/请求头用模板描述，是为了让「换一家云厂商」变成改 JSON 而不是改代码。
    """
    if isinstance(value, dict):
        return {k: _render_template(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_template(v, ctx) for v in value]
    if isinstance(value, str):
        out = value
        for key, replacement in ctx.items():
            out = out.replace("{{" + key + "}}", "" if replacement is None else str(replacement))
        return out
    return value


def _dig(data: Any, path: str) -> Any:
    """按 'a.b.0.c' 取值；path 为空表示取整个响应体。"""
    if not path:
        return data
    node = data
    for part in path.split("."):
        if part == "":
            continue
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            return None
        if node is None:
            return None
    return node


def _signed_error(
    exc: BaseException,
    cfg: dict | None,
    provider_name: str,
    stage: str,
) -> ProviderError:
    """把一次失败翻译成「这家在说什么」，返回**该抛出去的那个异常**。

    调用方写成 `raise _signed_error(exc, cfg, name, stage)`。
    （早先这里写成「内部 raise + 返回」，调用方 `raise` 了原异常，
    结果签名信息被丢掉 —— 测试逮到的。）

    返回的 ProviderError 带 `transient`，这个值由签名决定：
    声明成 `transient` 的会被 _retry 重试，声明成 `rejected` / `fatal` 的
    **立刻抛出**，不再硬磕。这就是「快速失败并切换下一家」的实现点。
    """
    if not isinstance(exc, ProviderError):
        return exc  # type: ignore[return-value]
    rej = CAP.classify(
        getattr(exc, "status", None),
        getattr(exc, "content_type", ""),
        len(getattr(exc, "body", b"") or b""),
        getattr(exc, "body", b"") or b"",
        cfg,
    )
    if rej is None:
        return exc
    evidence = CAP.describe_evidence(
        getattr(exc, "status", None),
        getattr(exc, "content_type", ""),
        len(getattr(exc, "body", b"") or b""),
        headers=getattr(exc, "headers", None),
    )
    lines = [f"「{provider_name}」{stage}被拒：{rej.label}", f"  证据：{evidence}"]
    if rej.hint:
        lines.append(f"  怎么办：{rej.hint}")
    if rej.kind == "fatal":
        lines.append("  （这类错误重试和改输入都没用，会直接换下一家）")
    return ProviderError(
        "\n".join(lines),
        transient=rej.worth_retrying,
        status=getattr(exc, "status", None),
        content_type=getattr(exc, "content_type", ""),
        body=getattr(exc, "body", b"") or b"",
        headers=getattr(exc, "headers", None),
    )


def _http_raw_meta(
    url: str, data: bytes | None, headers: dict[str, str] | None,
    timeout_s: int = 300, method: str = "POST",
) -> dict[str, Any]:
    """请求原始字节，并把这次的**全部证据**带回来。

    为什么不能只返回字节：硅基流动最要命的一次失败是「HTTP 200 + 0 字节」——
    从字节上看是空，从状态码上看是成功。要判断它到底是「拒绝」还是「抖动」，
    必须同时拿到 状态码 / Content-Type / 耗时 / 响应头里的 trace-id。
    之前只返回字节，等于把证据全扔了，导致连查三轮都没定论。

    返回 {"blob", "status", "content_type", "headers", "elapsed"}。
    """
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            blob = resp.read()
            return {"blob": blob, "status": getattr(resp, "status", 200),
                    "content_type": _ctype(resp.headers), "headers": resp.headers,
                    "elapsed": time.time() - started}
    except urllib.error.HTTPError as exc:
        body = exc.read()
        detail = body.decode("utf-8", errors="replace")[:400]
        raise ProviderError(f"HTTP {exc.code} <- {url[:120]}\n{detail}",
                            transient=exc.code in _TRANSIENT_CODES,
                            status=exc.code, content_type=_ctype(exc.headers),
                            body=body, headers=exc.headers) from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"无法连接 {url[:120]}: {exc.reason}", transient=True) from exc
    except (ssl.SSLError, http.client.HTTPException, socket.timeout, TimeoutError) as exc:
        raise ProviderError(f"连接 {url[:120]} 时中断：{type(exc).__name__}: {exc}",
                            transient=True) from exc


def _http_raw(url: str, data: bytes | None, headers: dict[str, str] | None,
              timeout_s: int = 300, method: str = "POST") -> bytes:
    """请求原始字节并拿回原始响应字节。

    method 必须可指定：下载成片要用 GET —— S3 的预签名 URL 是按 GET 签的，
    用 POST 去打会直接 403 SignatureDoesNotMatch（实测踩过）。
    """
    return _http_raw_meta(url, data, headers, timeout_s, method)["blob"]


def _build_multipart(
    fields: dict[str, Any], file_field: str, filename: str,
    file_bytes: bytes, file_ctype: str = "audio/wav",
) -> tuple[bytes, str]:
    """转发到 media.build_multipart（云端上传与 ComfyUI 上传共用同一份实现）。"""
    return M.build_multipart(fields, file_field, filename, file_bytes, file_ctype)


def _dashscope_oss_upload(
    spec: dict[str, Any], blob: bytes, filename: str, content_type: str,
    context: dict[str, Any],
) -> str:
    """按百炼官方流程把本地文件上传为短期 ``oss://`` 地址。

    百炼的图生视频接口不接 data URI，也没有固定上传地址：每次要先用 Key
    获取临时 policy，再把 policy 返回的动态字段提交给 OSS。把这段封装为上传
    模式后，数字人主流程仍然只认识 image_url / audio_url。
    """
    policy_url = str(_render_template(
        spec.get("policy_url")
        or ("https://dashscope.aliyuncs.com/api/v1/uploads"
            "?action=getPolicy&model={{model}}"),
        context,
    ))
    policy_headers = _render_template(
        spec.get("policy_headers")
        or {"Authorization": "Bearer {{api_key}}"},
        context,
    )
    response = _http(policy_url, None, headers=policy_headers, timeout_s=60, method="GET")
    data = response.get("data") if isinstance(response.get("data"), dict) else response
    required = ("policy", "signature", "upload_dir", "upload_host",
                "oss_access_key_id")
    missing = [name for name in required if not data.get(name)]
    if missing:
        raise ProviderError(
            "百炼没有返回完整的临时上传凭证，缺少 " + "、".join(missing) +
            "。\n  原始响应：" + json.dumps(response, ensure_ascii=False)[:400])

    upload_dir = str(data["upload_dir"]).rstrip("/")
    # policy 的目录本身是临时且隔离的；追加内容摘要同时避免同批同名覆盖。
    stem = Path(filename).stem or "autovid"
    suffix = Path(filename).suffix
    digest = hashlib.sha1(blob).hexdigest()[:12]
    object_key = f"{upload_dir}/{stem}-{digest}{suffix}"
    fields = {
        "OSSAccessKeyId": str(data["oss_access_key_id"]),
        "Signature": str(data["signature"]),
        "policy": str(data["policy"]),
        "x-oss-object-acl": str(data.get("x_oss_object_acl") or "private"),
        "x-oss-forbid-overwrite": str(data.get("x_oss_forbid_overwrite") or "true"),
        "key": object_key,
        "success_action_status": "200",
    }
    payload, multipart_type = M.build_multipart(
        fields, "file", filename, blob, content_type)
    _http_raw(str(data["upload_host"]), payload,
              {"Content-Type": multipart_type}, timeout_s=300, method="POST")
    return "oss://" + object_key


def register_voice(config: Any, voice_asset: Any, log=print) -> dict[str, Any]:
    """把音色资产「准备好」：云端则注册克隆，本地则校验参考音频。

    这是**音色克隆**这一步的公开入口，供编排层直接调用，不必了解各家厂商差异。

    返回：
        {
          "voice_id", "name", "ref_audio", "duration_s",
          "provider",          # 本次使用的 TTS provider
          "can_clone",         # 该 provider 是否支持克隆
          "cloned",            # 是否真的完成/将完成克隆
          "cloud_voice_id",    # 云端分配的音色 ID（本地则为 None）
          "note",
        }
    """
    if voice_asset is None:
        raise ProviderError("没有音色资产，无法执行音色克隆")

    provider = str(config.provider_of("voice"))
    ref = AssetStore(config).voice_reference(voice_asset.id)
    info: dict[str, Any] = {
        "voice_id": voice_asset.id,
        "name": voice_asset.name,
        "ref_audio": str(ref) if ref else None,
        "duration_s": voice_asset.duration_s,
        "provider": provider,
        "can_clone": provider in _CLONE_CAPABLE,
        "cloned": False,
        "cloud_voice_id": None,
        "note": "",
    }

    if ref is None:
        raise ProviderError(
            f"音色「{voice_asset.name}」没有参考音频，无法克隆。\n"
            "  请先在「音色库」里录音或上传一段（建议 10 秒以上）。"
        )

    if provider.startswith("voice:"):
        # 设置面板里配的付费 API
        from .providers_registry import resolve as _resolve_custom  # noqa: PLC0415
        custom = _resolve_custom(config, provider)
        if custom is None:
            raise ProviderError(f"设置里找不到提供商 {provider}（可能已被删除）")
        info["can_clone"] = True
        cloud_id = _cloud_register_voice(config, custom["cfg"], log, voice_asset,
                                         api_key=custom["api_key"],
                                         provider_name=provider)
        info["cloud_voice_id"] = cloud_id
        info["cloned"] = bool(cloud_id)
        info["note"] = (
            f"{custom['label']}：云端克隆完成，音色 ID = {cloud_id}" if cloud_id
            else f"{custom['label']}：模板里没配克隆步骤，将用参考音频或固定音色合成")
    elif provider == "cloud_tts":
        cloud_id = _cloud_register_voice(config, config.provider_cfg("cloud_tts"),
                                          log, voice_asset)
        info["cloud_voice_id"] = cloud_id
        info["cloned"] = bool(cloud_id)
        info["note"] = f"云端克隆完成，音色 ID = {cloud_id}"
    elif provider == "http_json":
        info["cloned"] = True
        info["note"] = "将用参考音频做本地零样本克隆（自建服务）"
    elif provider == "local_qwen_tts":
        # 本地 Qwen3-TTS：提取「说话人嵌入 + 音频码」存成无损锚点，一次算好反复用
        from .qwen3tts import Qwen3TTSBackend  # noqa: PLC0415
        store = AssetStore(config)
        anchor = store.voice_dir(voice_asset.id) / "anchor.json"
        Qwen3TTSBackend.get(config).prepare_anchor(ref, anchor, log)
        info["cloned"] = True
        info["note"] = "本地 Qwen3-TTS：音色锚点已提取（anchor.json，无损）"
    elif provider in ("edge_native", "sapi", "edge"):
        info["note"] = (
            f"{provider} 不支持音色克隆，本次会用内置音色；"
            "参考音频已就绪，切到 cloud_tts / local_qwen_tts / http_json 即可用上它"
        )
    elif provider == "silent":
        info["note"] = (
            "silent 是静音占位，不会出声也不会克隆；"
            "参考音频已就绪，切到 edge_native / cloud_tts / local_qwen_tts / http_json 才会用上它"
        )
    else:
        info["note"] = f"{provider} 的能力未知，按不支持克隆处理"

    log(f"音色克隆：{info['note']}")
    return info


# 支持「用参考音频克隆音色」的 provider
_CLONE_CAPABLE = {"cloud_tts", "http_json", "local_qwen_tts"}


def can_clone(provider: str, config: Any = None) -> bool:
    """这个语音引擎会不会真的用你的音色。

    必须走这个函数，别直接查 _CLONE_CAPABLE —— 用户自己在设置里配的
    voice:xxx 不在那个集合里，会被误判成「不支持克隆」并报出与实际相反的
    警告（实测踩过：硅基流动明明克隆成功，前置判断却说不会是你的音色）。
    """
    name = str(provider or "")
    if name in _CLONE_CAPABLE:
        return True
    if not name.startswith("voice:"):
        return False
    if config is None:
        return True
    from .providers_registry import resolve  # noqa: PLC0415
    custom = resolve(config, name)
    if custom is None:
        return False
    # 模板里配了克隆步骤（建音色接口或上传接口）才算支持
    clone = (custom["cfg"].get("clone") or {})
    upload = (clone.get("upload") or {})
    return bool(clone.get("enabled")) and bool(clone.get("url") or upload.get("url"))


def _cloud_register_voice(config: Any, cfg: dict, log, voice_asset: Any,
                          api_key: str | None = None,
                          provider_name: str = "cloud_tts") -> str | None:
    """按配置把参考音频注册成云端音色，返回云端音色 ID。

    注册结果会缓存进资产（cloud_voice_ids，按 provider 分开存），
    **绝不会每次生成视频都重新克隆一遍** —— 那既慢又费钱。
    换参考音频后用 clear_cloud_voice_ids 清缓存即可。
    """
    clone = cfg.get("clone") or {}
    # 有的厂商（如硅基流动）没有单独的「建音色」接口：上传音频返回的 uri
    # 本身就是音色标识。所以只要配了 upload.url 就算支持克隆。
    upload_cfg = clone.get("upload") or {}
    if not clone.get("enabled") or not (clone.get("url") or upload_cfg.get("url")):
        return None

    cached = (getattr(voice_asset, "cloud_voice_ids", None) or {}).get(provider_name)
    if cached:
        log(f"  复用已注册的云端音色 ID：{cached}（不会重复克隆）")
        return str(cached)

    store = AssetStore(config)
    ref = store.voice_reference(voice_asset.id)
    if ref is None:
        raise ProviderError(
            f"音色「{voice_asset.name}」还没有参考音频，无法注册云端克隆"
        )
    # 有些厂商限制参考音频长度（硅基流动：≤30 秒，实测报
    # "audio longer than 30s is not supported"）。超了就裁一段再说 ——
    # 克隆只需要音色特征，几十秒足够，用户不必为此重录。
    # 参考文本：硅基流动等厂商的上传接口要求 text 与音频内容对应。
    # 之前 context 里压根没有 prompt_text，占位符渲染成空串（等于没给文本）。
    ref_text = str(getattr(voice_asset, "ref_text", "")
                   or getattr(voice_asset, "prompt_text", "") or "")

    max_audio_s = float(clone.get("max_audio_s") or 0)
    if max_audio_s > 0:
        try:
            import soundfile as sf  # noqa: PLC0415
            data, rate = sf.read(str(ref), dtype="float32", always_2d=True)
            limit = int(max_audio_s * rate)
            if len(data) > limit:
                ratio = limit / len(data)
                trimmed = ref.parent / f"{ref.stem}_first{int(max_audio_s)}s.wav"
                if not trimmed.exists():
                    sf.write(str(trimmed), data[:limit], rate)
                log(f"  参考音频 {len(data) / rate:.0f}s 超过上限 {max_audio_s:.0f}s，"
                    f"已裁前 {max_audio_s:.0f}s 用于克隆")
                ref = trimmed
                if ref_text:
                    # 文本也要按同样比例裁，否则文本与音频对不上，克隆质量会差
                    ref_text = ref_text[:max(1, int(len(ref_text) * ratio))]
                    log(f"  参考文本同步裁到 {len(ref_text)} 字")
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"  ⚠ 裁剪参考音频失败（{type(exc).__name__}），按原样上传")

    api_key = api_key or config.secret_for("cloud_tts", "AUTOVID_CLOUD_TTS_KEY")
    context = {
        "api_key": api_key or "",
        "voice_name": voice_asset.name,
        "prompt_text": ref_text,
        "ref_audio": str(ref),
        "audio_base64": base64.b64encode(ref.read_bytes()).decode("ascii"),
        "audio_format": ref.suffix.lstrip(".") or "wav",
        "voice_id": "",
        "model": str(cfg.get("model", "")),
        # 有些厂商要求自己指定一个新音色 ID（如 MiniMax 的 voice_id），给个合法默认值
        "new_voice_id": f"autovid{random.randint(10 ** 7, 10 ** 8 - 1)}",
        "upload_id": "",
    }
    timeout_s = int(cfg.get("timeout_s", 300))

    # 可选的第一步：先把参考音频上传拿到 file_id，再用它建音色。
    # MiniMax、火山这类都是两步流程，只支持一步的适配器接不上。
    upload = clone.get("upload") or {}
    if upload.get("url"):
        upload_headers = _render_template(
            upload.get("headers") or clone.get("headers") or {}, context)
        log("  上传参考音频到云端…")
        if str(upload.get("mode", "")).lower() == "multipart":
            # 表单上传（硅基流动等）：字段 + 文件
            fields = _render_template(upload.get("fields") or {}, context)
            payload, ctype = _build_multipart(
                fields,
                str(upload.get("file_field") or "file"),
                str(upload.get("filename") or "ref.wav"),
                ref.read_bytes(),
                str(upload.get("file_ctype") or "audio/wav"),
            )
            upload_headers = {k: v for k, v in upload_headers.items()
                              if k.lower() != "content-type"}
            upload_headers["Content-Type"] = ctype
            raw = _http_raw(str(upload["url"]), payload, upload_headers, timeout_s)
            uploaded = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        else:
            upload_body = _render_template(upload.get("body") or {}, context)
            uploaded = _http(str(upload["url"]), upload_body, headers=upload_headers,
                             timeout_s=timeout_s)
        upload_id = _dig(uploaded, str(upload.get("id_path") or "file_id"))
        if not upload_id:
            raise ProviderError(
                f"上传参考音频后没拿到文件 ID（配置路径 '{upload.get('id_path')}'）。"
                f"原始响应：\n{json.dumps(uploaded, ensure_ascii=False)[:500]}"
            )
        context["upload_id"] = str(upload_id)
        log(f"  ✓ 参考音频已上传，file_id = {upload_id}")

    # 「一步式」厂商：有些服务（如硅基流动）没有单独的建音色接口，
    # 上传返回的 uri 本身就是音色标识 —— clone.url 留空即表示这种流程。
    # 不处理的话会往空字符串发请求，报 unknown url type（实测踩过）。
    if not str(clone.get("url") or "").strip():
        if not context["upload_id"]:
            raise ProviderError(
                "clone.url 为空，且没有可用的上传步骤，拿不到音色 ID。\n"
                "  这个模板需要配 clone.url（建音色接口）或 clone.upload.url（上传接口）。")
        cloud_id = context["upload_id"]
        store.set_cloud_voice_id(voice_asset.id, provider_name, cloud_id)
        log(f"  ✓ 音色 ID = {cloud_id}（上传即得，已缓存，后续不再重复上传）")
        return str(cloud_id)

    headers = _render_template(clone.get("headers") or {}, context)
    body = _render_template(clone.get("body") or {}, context)
    log(f"  正在向云端注册音色「{voice_asset.name}」…")
    data = _http(str(clone["url"]), body, headers=headers, timeout_s=timeout_s)
    cloud_id = _dig(data, str(clone.get("voice_id_path") or "voice_id"))
    if not cloud_id:
        raise ProviderError(
            f"云端克隆没有返回音色 ID（配置的路径是 "
            f"'{clone.get('voice_id_path')}'）。原始响应：\n"
            f"{json.dumps(data, ensure_ascii=False)[:500]}"
        )
    store.set_cloud_voice_id(voice_asset.id, provider_name, str(cloud_id))
    log(f"  ✓ 云端克隆完成，音色 ID = {cloud_id}（已缓存，后续不再重复注册）")
    return str(cloud_id)


def _looks_like_audio(blob: bytes) -> bool:
    """粗略判断响应体是不是音频。

    为什么必须校验：硅基流动的合成接口**会间歇性返回 HTTP 200 + 0 字节**
    （实测连打三次，第三次 text/plain 空响应）。不校验就会把一个空文件
    当成配音交给下游，用户听到的是「静音」，而且全程没有任何报错。
    """
    if not blob or len(blob) < 512:
        return False
    head = blob[:16]
    return (head[:4] == b"RIFF" or head[:4] == b"fLaC" or head[:4] == b"OggS"
            or head[:3] == b"ID3" or head[:2] == b"\xff\xfb" or head[:2] == b"\xff\xf3")


def _audio_sane(blob: bytes) -> tuple[bool, str]:
    """判断响应音频是否可用：文件头 + 时长 + 音量。

    只查文件头不够 —— 实测云端会返回「合法 RIFF 头但只有 0.32 秒、几乎静音」
    的响应（RMS 0.0037），头校验放过去了，用户听到的就是「静音」。
    """
    if not _looks_like_audio(blob):
        return False, f"不是音频（{len(blob)} 字节）"
    try:
        import io  # noqa: PLC0415

        import numpy as np  # noqa: PLC0415
        import soundfile as sf  # noqa: PLC0415
        data, rate = sf.read(io.BytesIO(blob), dtype="float32", always_2d=True)
        duration = len(data) / rate if rate else 0.0
        rms = float(np.sqrt(np.mean(np.square(data)))) if data.size else 0.0
    except Exception as exc:  # noqa: BLE001
        return False, f"解码失败（{type(exc).__name__}）"
    if duration < 0.5:
        return False, f"只有 {duration:.2f}s"
    if rms < 0.005:
        return False, f"几乎静音（RMS={rms:.4f}）"
    return True, f"{duration:.2f}s RMS={rms:.4f}"


def _tts_cloud(config: Any, segments, out_dir: Path, log, voice_asset=None,
               cfg: dict | None = None, api_key: str | None = None,
               provider_name: str = "cloud_tts") -> TTSResult:
    """厂商无关的云端 TTS / 音色克隆。

    请求与响应都由模板描述（url / headers / body / 音频在响应的哪个字段），
    所以接一家新厂商只需要给一份模板 —— 设置面板里加条目就是这么走的：
    传入该条目的 cfg / api_key / provider_name 即可，代码不用动。
    支持四种音频返回形式：hex / base64 / url / 原始字节流。
    """
    cfg = cfg if cfg is not None else config.provider_cfg("cloud_tts")
    out_dir.mkdir(parents=True, exist_ok=True)
    url = str(cfg.get("url") or "")
    if not url:
        raise ProviderError(
            "providers.cloud_tts.url 未配置。\n"
            "  用 `python scripts/probe_cloud.py --help` 查看主流厂商的预设模板。"
        )
    api_key = api_key or config.secret_for("cloud_tts", "AUTOVID_CLOUD_TTS_KEY")
    if not api_key:
        raise ProviderError(
            f"「{provider_name}」缺少 API Key。\n"
            "  在「设置」页填到对应提供商里即可（存 config/secrets.json）。"
        )

    # 音色：优先用资产注册出来的克隆音色，否则用配置里写死的
    voice_id = str(cfg.get("voice_id") or "")
    cloned = False
    if voice_asset is not None:
        registered = _cloud_register_voice(config, cfg, log, voice_asset,
                                           api_key=api_key,
                                           provider_name=provider_name)
        if registered:
            voice_id = registered
            cloned = True
    if not voice_id:
        raise ProviderError(
            "没有可用的音色 ID：请设置 providers.cloud_tts.voice_id，"
            "或在页面上选一个音色资产并开启 clone.enabled"
        )

    encoding = str(cfg.get("audio_encoding", "base64")).lower()
    audio_path = str(cfg.get("audio_path") or "")
    timeout_s = int(cfg.get("timeout_s", 300))
    sample_rate = int(config.step_cfg("voice").get("sample_rate", 24000))

    parts: list[Path] = []
    # 段间节流：一条视频有几十个气口句，连着猛打会被云端限流（实测 59 个
    # 气句时连续 4 次拿到空响应，整条链路被迫回退到别的引擎、声音就不是你的了）。
    delay_s = float(cfg.get("request_delay_s", 0.4) or 0.0)
    for index, segment in enumerate(segments):
        if index and delay_s > 0:
            time.sleep(delay_s)
        context = {
            "api_key": api_key,
            "text": str(segment["text"]),
            "voice_id": voice_id,
            "model": str(cfg.get("model", "")),
            "voice_name": getattr(voice_asset, "name", ""),
        }
        headers = {k: v for k, v in
                   (_render_template(cfg.get("headers") or {}, context)).items()
                   if v not in ("", None)}
        body = _render_template(cfg.get("body") or {}, context)

        raw_out = out_dir / f"seg_{index:02d}.raw"
        # 云端会间歇性返回 HTTP 200 + 空响应。但「间歇」不等于「随便磕」：
        # 实测硅基流动 8 次同请求 6 成 2 败，失败的那次是 **0.18s 秒回**
        # （成功要 0.6s 才出首字节）—— 说明它根本没合成，是明确拒收，
        # 只是用 200 伪装了。
        #
        # 所以这里做两件事：
        #   1. 每次都把证据（状态码/Content-Type/字节数/耗时/trace-id）记下来，
        #      并拿厂商声明的签名去判定它到底算哪种失败；
        #   2. 花一个**预算**（连续拒收次数 + 总耗时），超了就承认这家现在不可用，
        #      立刻抛出去让回退链接手 —— 而不是在一个坏窗口里磕到十几分钟。
        attempts = int(cfg.get("retries", 12) or 12)
        max_streak = int(cfg.get("reject_streak_limit", 6) or 6)
        budget_s = float(cfg.get("reject_budget_s", 60) or 0)
        blob = b""
        last_note = ""
        rejection: CAP.Rejection | None = None
        evidence = ""
        streak = 0
        started_at = time.time()
        for attempt in range(1, attempts + 1):
            try:
                if encoding == "raw" and not audio_path:
                    # 贵在「读字节」这一步，所以重试也包在 try 里
                    meta = _http_raw_meta(
                        url, json.dumps(body, ensure_ascii=False).encode("utf-8"),
                        headers, timeout_s)
                    blob = meta["blob"]
                    evidence = CAP.describe_evidence(
                        meta["status"], meta["content_type"], len(blob),
                        meta["elapsed"], meta["headers"])
                    # 200 也算失败的情况只有在这里能发现：状态码是成功的，
                    # 但载荷根本不是音频。签名必须基于完整证据来判。
                    rejection = CAP.classify(meta["status"], meta["content_type"],
                                             len(blob), blob, cfg)
                else:
                    break
            except ProviderError as exc:
                blob = b""
                evidence = CAP.describe_evidence(
                    getattr(exc, "status", None), getattr(exc, "content_type", ""),
                    len(getattr(exc, "body", b"") or b""), headers=getattr(exc, "headers", None))
                rejection = CAP.classify(
                    getattr(exc, "status", None), getattr(exc, "content_type", ""),
                    len(getattr(exc, "body", b"") or b""),
                    getattr(exc, "body", b"") or b"", cfg)
                last_note = f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
            except Exception as exc:  # noqa: BLE001
                last_note = f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
                blob = b""
                evidence = ""
                rejection = None

            ok, why = _audio_sane(blob) if blob else (False, "空响应")
            if ok:
                last_note = why
                break
            last_note = why
            streak += 1
            spent = time.time() - started_at

            # 签名说是 rejected / fatal -> 重试改变不了结果，但实测这种
            # 「成簇随机失败」确实会在几次之后自己好转，所以给一个小额度，
            # 超出就不再客气。fatal（认证/额度）连额度都不给。
            if rejection is not None and rejection.kind == "fatal":
                raise ProviderError(
                    f"「{provider_name}」不可用：{rejection.label}\n"
                    f"  证据：{evidence}\n"
                    + (f"  怎么办：{rejection.hint}\n" if rejection.hint else ""),
                    transient=False)
            if rejection is not None and not rejection.worth_retrying:
                if streak >= max_streak or (budget_s and spent >= budget_s):
                    raise ProviderError(
                        f"「{provider_name}」连续 {streak} 次被拒"
                        f"（累计 {spent:.0f}s），判定这家当前不可用，立即交给下一家。\n"
                        f"  签名：{rejection.label}\n"
                        f"  证据：{evidence}\n"
                        + (f"  怎么办：{rejection.hint}\n" if rejection.hint else ""),
                        transient=False)

            if attempt < attempts:
                wait = min(30.0, 2.0 * attempt)
                tag = f"，签名：{rejection.label}" if rejection else ""
                log(f"  ⚠ 第 {attempt} 次响应不可用（{why}）{tag}，等待 {wait:.0f}s 重试…")
                if evidence:
                    log(f"      证据：{evidence}")
                # 退避要狠一点：实测云端空响应是**成簇出现**的（一段坏窗口，
                # 窗口内几乎全空），间隔短了永远打在同一个坏窗口里。
                time.sleep(wait)
        if encoding == "raw" and not audio_path:
            ok, why = _audio_sane(blob) if blob else (False, "空响应")
            if not ok:
                lines = [f"「{provider_name}」连续 {attempts} 次都没拿到可用音频"
                         f"（最后一次：{why}）。"]
                if rejection is not None:
                    lines.append(f"  签名：{rejection.label}")
                if evidence:
                    lines.append(f"  证据：{evidence}")
                lines.append("  这是接口端不稳定（HTTP 200 但返回空/极短/静音），"
                             "稍后重试通常就好了 —— 已自动交给回退链的下一个引擎。")
                raise ProviderError("\n".join(lines), transient=False)

        if not (encoding == "raw" and not audio_path):
            data = _http(url, body, headers=headers, timeout_s=timeout_s)
            payload = _dig(data, audio_path)
            if payload is None:
                raise ProviderError(
                    f"响应里找不到音频字段 '{audio_path}'。原始响应：\n"
                    f"{json.dumps(data, ensure_ascii=False)[:500]}"
                )
            if encoding == "hex":
                blob = bytes.fromhex(str(payload).strip())
            elif encoding == "base64":
                blob = base64.b64decode(str(payload))
            elif encoding == "url":
                _http_download(str(payload), raw_out, timeout_s=timeout_s)
                blob = raw_out.read_bytes()
            else:
                raise ProviderError(f"未知的 audio_encoding: {encoding}（应为 hex/base64/url/raw）")

        raw_out.write_bytes(blob)
        wav = out_dir / f"seg_{index:02d}.wav"
        try:
            M.run_ffmpeg(["-i", str(raw_out), "-ac", "1", "-ar", str(sample_rate),
                          "-c:a", "pcm_s16le", str(wav)],
                         desc=f"云端音频第 {index + 1} 段转 WAV")
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(
                f"云端返回的第 {index + 1} 段不是可解码的音频（{len(blob)} 字节）。"
                f"大概率是 audio_path / audio_encoding 配错了：{exc}"
            ) from exc
        finally:
            raw_out.unlink(missing_ok=True)
        parts.append(wav)

    note = f"云端克隆（音色 ID {voice_id}）" if cloned else f"云端合成（音色 ID {voice_id}）"
    return TTSResult(provider_name, parts, note, cloned=cloned)


def _tts_silent(config: Any, segments, out_dir: Path, log, voice_asset=None) -> TTSResult:
    """生成等长静音。永远可用 —— 用于验证链路、字幕排版和后期流程。"""
    cfg = config.step_cfg("voice")
    sample_rate = int(cfg.get("sample_rate", 24000))
    cps = float(cfg.get("chars_per_second", 4.6))
    import wave as _wave

    parts: list[Path] = []
    for index, segment in enumerate(segments):
        text = str(segment["text"])
        duration = max(0.8, len(text) / cps)
        frames = int(duration * sample_rate)
        out = out_dir / f"seg_{index:02d}.wav"
        with _wave.open(str(out), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(sample_rate)
            writer.writeframes(b"\x00" * (frames * 2))
        parts.append(out)
    log(f"  已生成静音轨道（{cps} 字/秒估算时长）。换真实音色请配置 voice.provider")
    return TTSResult("silent", parts, "静音占位：字幕时间轴仍精确")


def _tts_local_qwen(config: Any, segments, out_dir: Path, log, voice_asset=None) -> TTSResult:
    """本地 Qwen3-TTS：Talker/Predictor 走 GGUF-Vulkan，Decoder 走 ONNX-DML。

    用 voice_asset 的音色锚点（anchor.json）克隆合成；没有资产就报错，
    绝不默默用内置音色糊弄 —— 用户上传参考音频就是要自己的声音。
    """
    from .qwen3tts import Qwen3TTSBackend  # noqa: PLC0415

    if voice_asset is None:
        raise ProviderError(
            "本地 Qwen3-TTS 需要音色资产（要克隆就得先录一段参考音频）。"
            "请先在「音色库」上传/录音。")
    store = AssetStore(config)
    ref = store.voice_reference(voice_asset.id)
    if ref is None:
        raise ProviderError(f"音色「{voice_asset.name}」没有参考音频，无法克隆。")
    anchor = store.voice_dir(voice_asset.id) / "anchor.json"

    backend = Qwen3TTSBackend.get(config)
    backend.prepare_anchor(ref, anchor, log)
    parts = backend.synthesize(anchor, list(segments), out_dir, log)
    return TTSResult(
        "local_qwen_tts", parts,
        f"本地 Qwen3-TTS：你的音色「{voice_asset.name}」",
        cloned=True)


_TTS_PROVIDERS: dict[str, Callable[..., TTSResult]] = {
    "edge_native": _tts_edge_native,
    "cloud_tts": _tts_cloud,
    "sapi": _tts_sapi,
    "edge": _tts_edge,
    "http_json": _tts_http_json,
    "local_qwen_tts": _tts_local_qwen,
    "silent": _tts_silent,
}


def known_tts_providers(config: Any) -> set[str]:
    """当前可用的语音引擎名 = 内置的 + 用户在设置里配的。

    **唯一数据源**。之前「哪些引擎算数」这件事散落在三处
    （_TTS_PROVIDERS 字典、前端 CLONE_ENGINES 列表、试音接口的白名单），
    加了「设置里配的付费 API」之后三处都漏改，用户连续撞到
    「选不到」「试音报未知引擎」。以后只改这一个地方。
    """
    from .providers_registry import entry_name, load_entries  # noqa: PLC0415
    names = set(_TTS_PROVIDERS)
    for entry in load_entries(config):
        if str(entry.get("kind")) == "voice" and entry.get("enabled", True):
            names.add(entry_name(entry))
    return names


def known_avatar_providers(config: Any) -> set[str]:
    """当前可用的数字人引擎名（内置的 + 设置里配的）。"""
    from .providers_registry import entry_name, load_entries  # noqa: PLC0415
    names = {"still", "comfy", "http_job", "minimax_h3", "local_wav2lip"}
    for entry in load_entries(config):
        if str(entry.get("kind")) == "avatar" and entry.get("enabled", True):
            names.add(entry_name(entry))
    return names


# =========================================================================== #
# 3. Image —— 背景图 / 封面底图
# =========================================================================== #
# 一组经过挑选的深色系配色，配合白色描边字幕对比度足够
PALETTES: list[list[str]] = [
    ["0x0f2027", "0x203a43", "0x2c5364"],   # 深海蓝
    ["0x1a1a2e", "0x16213e", "0x0f3460"],   # 午夜蓝
    ["0x2b1055", "0x7597de", "0x1b1b3a"],   # 紫蓝
    ["0x0b0b0f", "0x2d1b3d", "0x4a1f3d"],   # 暗酒红
    ["0x102a1e", "0x1d4a34", "0x0b1f16"],   # 墨绿
    ["0x241023", "0x4a1e2b", "0x12121c"],   # 暗棕
]


def image_generate(
    config: Any,
    prompts: Sequence[str],
    out_dir: Path,
    width: int,
    height: int,
    log: Callable[[str], None] = print,
) -> tuple[list[Path], list[str]]:
    """返回 (图片路径列表, 每个的说明)。首张用于封面底图。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    provider = str(config.provider_of("visuals"))
    rng = random.Random(str(config.get("project.channel_seed", "autovid")))
    seed_base = rng.randint(0, 9999)
    # 整条视频用同一套配色（只变 seed 换渐变走向），保证成片视觉统一；
    # 每段换配色会让视频看起来像幻灯片拼盘。
    palette = PALETTES[seed_base % len(PALETTES)]

    paths: list[Path] = []
    notes: list[str] = []

    for index, prompt in enumerate(prompts):
        out = out_dir / f"visual_{index:02d}.png"
        if provider == "comfy":
            _image_comfy(config, prompt, out, width, height, index, log)
            notes.append("comfy")
        elif provider == "openai_images":
            _image_openai(config, prompt, out, index, log)
            notes.append("openai_images")
        else:
            M.make_gradient(out, width, height, palette, seed=seed_base + index * 13)
            notes.append("ffmpeg_gradient")
        paths.append(out)
    log(f"背景图完成：{len(paths)} 张（provider={provider}，配色 {palette[0]}）")
    return paths, notes


def _image_comfy(config: Any, prompt: str, out: Path, width: int, height: int,
                 index: int, log) -> None:
    """[未实测] ComfyUI 出图。

    需要一份 API 格式的工作流（ComfyUI 界面里「导出 (API)」），
    约定用 {{PROMPT}} / {{WIDTH}} / {{HEIGHT}} / {{SEED}} 占位。
    """
    from .comfy import ComfyClient, render_workflow

    cfg = config.provider_cfg("comfy")
    workflow_path = config.path(str(cfg.get("workflow", "")))
    if not workflow_path.exists():
        raise ProviderError(
            f"找不到 ComfyUI 工作流文件：{workflow_path}\n"
            "  用 ComfyUI 的「导出 (API)」保存一份，再设 providers.comfy.workflow"
        )
    template = workflow_path.read_text(encoding="utf-8")
    client = ComfyClient(str(cfg.get("url", "")),
                         timeout_s=int(cfg.get("timeout_s", 600)),
                         poll_s=float(cfg.get("poll_interval_s", 2)), log=log)
    workflow = render_workflow(template, {
        # prompt 注入的是 JSON 字符串内部的转义内容，所以要掐掉外层的引号
        "PROMPT": json.dumps(prompt, ensure_ascii=False)[1:-1],
        "WIDTH": width, "HEIGHT": height,
        "SEED": random.randint(1, 2 ** 31 - 1),
    })
    saved = client.run(workflow, out.parent, want="image", prefix=out.stem)
    saved[0].replace(out)
    log(f"  ComfyUI 产出 {out.name}")


def _image_openai(config: Any, prompt: str, out: Path, index: int, log) -> None:
    """[未实测] OpenAI 兼容的 images/generations 端点（SiliconFlow、通义万相等）。"""
    cfg = config.provider_cfg("openai_images")
    base = str(cfg.get("base_url", "")).rstrip("/")
    api_key = config.secret_for("openai_images", "AUTOVID_IMAGE_API_KEY")
    size = str(cfg.get("size", "768x1344"))
    payload = {
        "model": cfg.get("model", "black-forest-labs/FLUX.1-schnell"),
        "prompt": prompt,
        "n": 1,
        "size": size,
    }
    data = _http(f"{base}/images/generations", payload, headers=_auth_header(api_key),
                 timeout_s=int(cfg.get("timeout_s", 300)))
    items = data.get("data") or data.get("images") or []
    if not items:
        raise ProviderError(f"出图接口无结果：{json.dumps(data, ensure_ascii=False)[:300]}")
    first = items[0]
    if first.get("b64_json"):
        out.write_bytes(base64.b64decode(first["b64_json"]))
    elif first.get("url"):
        _http_download(first["url"], out, timeout_s=300)
    else:
        raise ProviderError(f"无法识别的出图响应字段：{list(first.keys())}")
    log(f"  云端出图 {out.name}")


# =========================================================================== #
# 4. Avatar —— 数字人
# =========================================================================== #
def avatar_image_limits(config: Any, provider: str) -> Any:
    """这家数字人引擎对上传图片的硬约束。

    内置引擎（local_wav2lip / still / comfy）在本地处理，不吃厂商限制，
    所以返回空的 ImageLimits —— 含义是「确认没有额外限制」，
    而不是「没查过」（没查过的那些会在 preset 里写明「尚未核实」）。
    """
    if provider.startswith("avatar:"):
        from .providers_registry import resolve as _resolve  # noqa: PLC0415
        custom = _resolve(config, provider)
        if custom is not None:
            return CAP.image_limits(custom["cfg"])
        return CAP.ImageLimits(note="设置里找不到这个提供商")
    return CAP.ImageLimits(note="本地渲染，无厂商限制")


def avatar_audio_limits(config: Any, provider: str) -> Any:
    """这家数字人引擎对输入音频的硬约束。"""
    if provider.startswith("avatar:"):
        from .providers_registry import resolve as _resolve  # noqa: PLC0415
        custom = _resolve(config, provider)
        if custom is not None:
            return CAP.audio_limits(custom["cfg"])
        return CAP.AudioLimits(note="设置里找不到这个提供商")
    return CAP.AudioLimits(note="本地渲染，无厂商限制")


def avatar_output_spec(
    config: Any,
    provider: str,
    width: int = 0,
    height: int = 0,
    fps: int = 0,
) -> Any:
    """这家数字人引擎实际能吐出什么规格。

    **三种情况要分开，不能混为一谈：**

    1. 设置面板里配的付费厂商（`avatar:<id>`）：规格是**厂商定的**，
       我们只能协商。D-ID 实测就是 512×512 @25fps，要更大得看它给不给。
    2. 本地引擎（local_wav2lip / still）：规格是**我们要的**。
       本地渲染直接按目标画布出图，所以它「支持」目标分辨率。
    3. 未知/未声明：如实返回 0，让上层按「不知道」处理，
       而不是假装成目标规格（那会把「放大」伪装成「原生」）。
    """
    if provider.startswith("avatar:"):
        from .providers_registry import resolve as _resolve  # noqa: PLC0415
        custom = _resolve(config, provider)
        if custom is not None:
            return CAP.output_spec(custom["cfg"])
        return CAP.OutputSpec(note="设置里找不到这个提供商，规格未知")
    if provider in ("local_wav2lip", "still"):
        # 本地渲染的规格是**我们决定的**，不是从厂商文档抄来的 ——
        # 标成未实测会让预检报一条假警告（实测踩过）。
        return CAP.OutputSpec(
            width=width, height=height, fps=fps, aspect="",
            note="本地渲染，直接按目标画布出图",
            verified=True, source="本框架自己渲染，尺寸可控")
    if provider == "comfy":
        return CAP.OutputSpec(
            note="规格由 comfy 工作流决定，框架无法预先声明")
    return CAP.OutputSpec(note=f"「{provider}」未声明输出规格")


def avatar_render(
    config: Any,
    segments: Sequence[dict[str, Any]],
    visuals: Sequence[Path],
    out_dir: Path,
    width: int,
    height: int,
    fps: int,
    log: Callable[[str], None] = print,
    portrait: Path | None = None,
) -> tuple[list[Path], str]:
    """返回 (每段的镜头片段, 实际使用的 provider)。

    `portrait` 是用户上传的形象主图。still provider 会把它合成到背景上，
    所以即使还没有接数字人模型，成片里也**已经是你自己的脸**。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    provider = str(config.provider_of("avatar"))
    clips: list[Path] = []

    if provider == "comfy":
        clips = _avatar_comfy(config, segments, visuals, out_dir, log, portrait)
    elif provider == "local_wav2lip":
        clips = _avatar_local_wav2lip(config, segments, visuals, out_dir,
                                      width, height, fps, log)
    elif provider.startswith("avatar:"):
        # 设置面板里配的付费数字人 —— 走模板引擎
        from .providers_registry import resolve as _resolve_custom  # noqa: PLC0415
        custom = _resolve_custom(config, provider)
        if custom is None:
            raise ProviderError(f"设置里找不到提供商 {provider}（可能已被删除）")
        log(f"数字人 provider = {provider}（{custom['label']}）")
        source_portrait = None if custom["cfg"].get("use_scene_photo") else portrait
        clips = _avatar_template(config, segments, visuals, out_dir, log,
                                 cfg=custom["cfg"], api_key=custom["api_key"],
                                 provider_name=provider, portrait=source_portrait)
    elif provider == "http_job":
        clips = _avatar_http_job(config, segments, visuals, out_dir, log, portrait)
    elif provider == "minimax_h3":
        clips = _avatar_minimax(config, segments, visuals, out_dir, log, portrait)
    else:
        clips = _avatar_still(config, segments, visuals, out_dir, width, height, fps, log, portrait)
        provider = "still"
    log(f"镜头片段完成：{len(clips)} 个（provider={provider}）")
    return clips, provider


def _avatar_local_wav2lip(config: Any, segments, visuals, out_dir: Path,
                          width: int, height: int, fps: int, log) -> list[Path]:
    """本地 Wav2Lip：把场景照片的嘴驱动起来（真·数字人，不是静态推镜）。

    逐段渲染：每段音频配同一张场景照片出一个片段，下游合成逻辑不用改。
    画面与背景仍来自场景照片，只是嘴会跟着声音动。
    """
    from .wav2lip import Wav2LipRunner  # noqa: PLC0415

    if not visuals:
        raise ProviderError("数字人缺少画面底图（本次场景照片）")
    scene = Path(visuals[0])
    if not scene.exists():
        raise ProviderError(f"场景照片不存在：{scene}")

    runner = Wav2LipRunner.get(config)
    jobs: list[dict[str, str]] = []
    for index, segment in enumerate(segments):
        audio = Path(str(segment.get("audio_path") or ""))
        if not audio.exists():
            raise ProviderError(f"第 {index + 1} 段的音频不存在：{audio}")
        jobs.append({
            "photo": str(scene),
            "audio": str(audio),
            "out": str(out_dir / f"avatar_{index:02d}.mp4"),
        })
    # 一次子进程渲染完所有片段：模型只加载一次，且与 llama.cpp 的
    # OpenMP 运行时隔离（同进程会 OMP Error #15 直接崩）。
    runner.render_batch(jobs, log, fps=fps, width=width, height=height)
    clips = [Path(job["out"]) for job in jobs]
    missing = [c.name for c in clips if not c.exists() or c.stat().st_size == 0]
    if missing:
        raise ProviderError("口型同步没产出这些片段：" + "、".join(missing))
    return clips


def _avatar_still(
    config: Any, segments, visuals, out_dir: Path, width: int, height: int, fps: int, log,
    portrait: Path | None = None,
) -> list[Path]:
    """离线实现：对「本次场景照片」做缓慢推镜。

    设计变更：画面与背景**来自本次拍摄的场景照片**（里面本来就有你本人），
    所以默认不再叠加身份照片 —— 那会让你在画面里出现两次。
    形象资产在 still 引擎里只用于「谁」这个身份标识，不参与合成。

    需要旧行为（把形象照叠到背景上）时，把 `avatar.use_identity_overlay` 设为 true。
    """
    cfg = config.step_cfg("avatar")
    zoom = float(cfg.get("zoom", 0.12))
    ratio = float(cfg.get("person_width_ratio", 0.62))
    bottom = float(cfg.get("person_bottom_ratio", 0.28))
    overlay = bool(cfg.get("use_identity_overlay", False)) and portrait is not None

    if not visuals:
        raise ProviderError("数字人缺少画面底图（本次场景照片）")
    log(f"  画面来自场景照片：{Path(visuals[0]).name}"
        + (f"　并叠加身份照 {portrait.name}" if overlay else ""))

    clips: list[Path] = []
    for index, segment in enumerate(segments):
        background = Path(visuals[index % len(visuals)])
        clip = out_dir / f"avatar_{index:02d}.mp4"
        if overlay:
            M.person_over_background(
                background, portrait, clip, float(segment["clip_duration"]),
                width, height, fps=fps, zoom=zoom,
                person_width_ratio=ratio, bottom_ratio=bottom, cwd=out_dir,
            )
        else:
            M.kenburns_clip(
                background, clip, float(segment["clip_duration"]), width, height,
                fps=fps, zoom=zoom, cwd=out_dir,
            )
        clips.append(clip)
    return clips


def _avatar_comfy(
    config: Any, segments, visuals, out_dir: Path, log, portrait: Path | None = None
) -> list[Path]:
    """[未实测] 用 ComfyUI 数字人工作流生成「会动嘴」的人物片段。

    这是 AMD 用户唯一现实的本地数字人路径：ComfyUI 通过 comfyui-rocm
    能用上 AMD 官方 ROCm + PyTorch（含 Triton / Flash Attention），
    社区里已有 Wav2Lip / MuseTalk / Sonic 等工作流。

    工作流由用户提供（ComfyUI 里「导出 (API)」），我们用占位符注入输入：
        {{IMAGE}}           本次场景照片（画面与背景的来源，已上传）
        {{IDENTITY_IMAGE}}  形象参考图（身份一致用，已上传；没选形象时为空）
        {{AUDIO}}           该段语音 wav（已上传）
        {{WIDTH}} {{HEIGHT}} {{FPS}} {{DURATION}} {{SEED}}
    换一个数字人工作流不用改代码，只要换 JSON。
    """
    from .comfy import ComfyClient, render_workflow

    cfg = config.provider_cfg("comfy")
    workflow_raw = str(cfg.get("avatar_workflow") or "")
    if not workflow_raw:
        raise ProviderError(
            "没有配置 ComfyUI 数字人工作流。\n"
            "  1. 在 ComfyUI 里搭好数字人流程（形象图 + 音频 -> 视频）\n"
            "  2. 用「导出 (API)」保存成 JSON\n"
            "  3. 把输入图片改成 \"{{IMAGE}}\"、输入音频改成 \"{{AUDIO}}\"\n"
            "  4. 在 config/pipeline.json 设 providers.comfy.avatar_workflow 指向它"
        )
    workflow_path = config.path(workflow_raw)
    if not workflow_path.exists():
        raise ProviderError(f"找不到 ComfyUI 数字人工作流：{workflow_path}")
    template = workflow_path.read_text(encoding="utf-8")

    if not visuals:
        raise ProviderError("数字人缺少画面底图（本次场景照片）")

    platform = config.platform
    width = int(platform.get("width", 1080))
    height = int(platform.get("height", 1920))
    fps = int(platform.get("fps", 30))
    upload_cfg = cfg.get("upload") or {}
    image_cfg = upload_cfg.get("image") or {}
    audio_cfg = upload_cfg.get("audio") or {}

    client = ComfyClient(str(cfg.get("url", "")),
                         timeout_s=int(cfg.get("avatar_timeout_s")
                                       or cfg.get("timeout_s", 1800)),
                         poll_s=float(cfg.get("poll_interval_s", 3)), log=log)
    rng = random.Random(str(config.get("project.channel_seed", "autovid")))

    def _upload(path: Path) -> str:
        return client.upload(
            path,
            endpoint=str(image_cfg.get("endpoint", "/upload/image")),
            field=str(image_cfg.get("field", "image")),
            subfolder=str(image_cfg.get("subfolder", "autovid")),
            content_type="image/png" if path.suffix.lower() == ".png" else "image/jpeg",
        )

    # 场景照片和身份照都只上传一次，所有片段复用
    scene = Path(visuals[0])
    scene_name = _upload(scene)
    identity_name = _upload(portrait) if portrait is not None else ""
    if portrait is None:
        log("  ⚠ 没有身份参考图 —— 工作流里 {{IDENTITY_IMAGE}} 会是空字符串")

    clips: list[Path] = []
    for index, segment in enumerate(segments):
        audio_path = Path(str(segment.get("audio_path") or ""))
        if not audio_path.exists():
            raise ProviderError(f"第 {index + 1} 段的音频文件不存在：{audio_path}")
        audio_name = client.upload(
            audio_path,
            endpoint=str(audio_cfg.get("endpoint", "/upload/image")),
            field=str(audio_cfg.get("field", "image")),
            subfolder=str(audio_cfg.get("subfolder", "autovid")),
            content_type="audio/wav",
        )
        workflow = render_workflow(template, {
            "IMAGE": scene_name,              # 画面与背景来自本次场景照片
            "IDENTITY_IMAGE": identity_name,  # 身份参考（形象库）
            "AUDIO": audio_name,
            "WIDTH": width, "HEIGHT": height, "FPS": fps,
            "DURATION": round(float(segment.get("clip_duration", 0.0)), 3),
            "SEED": rng.randint(1, 2 ** 31 - 1),
        })
        produced = client.run(workflow, out_dir, want="video", prefix=f"avatar_{index:02d}")
        clips.append(produced[0])
        log(f"  数字人片段 {index + 1}/{len(segments)} 就绪：{produced[0].name}")
    return clips


def _prepare_cloud_image(
    path: Path,
    max_side: int = 1280,
    limits: Any = None,
) -> tuple[bytes, str, Any]:
    """把上传给云端数字人的图压到「该厂商允许、且不浪费」的尺寸。

    返回 `(字节, 格式, ImagePlan)`。第三项带着尺寸协商的结论和问题清单，
    调用方据此决定要不要提前报错 —— 这就是 D-ID 那条「上传 201 通过、
    提交 400 才说 file size exceeded 10 MB」的正面解法：**上传前就算清楚**。

    原图动辄十几 MB（手机照片 3072×4096），base64 后更夸张，直接导致云端 500。
    1280 长边对「照片说话」绰绰有余，体积能掉到几百 KB。
    """
    from .provider_caps import ImageLimits, plan_image  # noqa: PLC0415

    active: ImageLimits = limits if limits is not None else ImageLimits()
    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415

        blob = np.fromfile(str(path), dtype=np.uint8)
        image = cv2.imdecode(blob, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("解码失败")
        height, width = image.shape[:2]
        plan = plan_image(width, height, active, prefer_long_side=max_side)
        if plan.scale < 1.0:
            image = cv2.resize(image, (plan.width, plan.height),
                               interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok:
            raise ValueError("编码失败")
        return encoded.tobytes(), "jpeg", plan
    except Exception:  # noqa: BLE001
        # 压缩失败就按原样上传，并保留原后缀（别让图都传不上去）。
        # 但**必须**把「我没能压缩」这件事报出去 —— 否则原图可能正好超过
        # 该厂商的像素上限，而上传接口又不查，错误会拖到提交任务时才出现。
        suffix = (path.suffix.lstrip(".") or "jpeg").lower()
        size = _read_image_size(path)
        if size:
            plan = plan_image(size[0], size[1], active, prefer_long_side=max_side)
            plan.issues.append("图片解码失败，只能原样上传（尺寸未被压缩）")
        else:
            plan = _make_plan(len(path.read_bytes()), active)
        return path.read_bytes(), suffix, plan


def _read_image_size(path: Path) -> tuple[int, int] | None:
    """不依赖 cv2 读图片宽高（读文件头就够）。读不到返回 None。"""
    try:
        import struct  # noqa: PLC0415

        with path.open("rb") as handle:
            head = handle.read(32)
            if head[:2] == b"\xff\xd8":                      # JPEG
                handle.seek(2)
                while True:
                    marker = handle.read(2)
                    if len(marker) < 2:
                        return None
                    if marker[0] != 0xFF:
                        return None
                    code = marker[1]
                    if code in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        chunk = handle.read(5)
                        if len(chunk) < 5:
                            return None
                        height, width = struct.unpack(">HH", chunk[1:5])
                        return (width, height)
                    length = struct.unpack(">H", handle.read(2))[0]
                    handle.seek(length - 2, 1)
            if head[:8] == b"\x89PNG\r\n\x1a\n":             # PNG
                return struct.unpack(">II", head[16:24])
    except Exception:  # noqa: BLE001
        return None
    return None


def _make_plan(size_bytes: int, limits: Any) -> Any:
    from .provider_caps import ImagePlan  # noqa: PLC0415

    return ImagePlan(0, 0, 1.0, [f"无法判断图片尺寸（{size_bytes / 1024:.0f}KB），未做校验"])


def _segment_audio(segment: Any) -> Path:
    """取片段音频路径。编排层用 audio_path，voice.json 里叫 audio_file，两个都认。"""
    if isinstance(segment, dict):
        return Path(str(segment.get("audio_path") or segment.get("audio_file") or ""))
    return Path(str(segment))


def _safe_wav_duration(path: Any) -> float:
    """读 WAV 时长，读不到就当 0（只用于生成断点续跑指纹，不值得因此报错）。"""
    try:
        return M.wav_duration(Path(str(path)))
    except Exception:                            # noqa: BLE001
        return 0.0


def _merge_avatar_jobs(segments, cfg: dict, out_dir: Path, log) -> list[dict]:
    """把相邻的气口句合并成尽量少的云端任务。

    为什么要合并：一次口播有几十个气口句，逐句提交就是几十次上传 + 几十次
    轮询 —— 任何一次网络抖动都会毁掉整条链路（实测第 3 次的 SSL 断流），
    而且按「生成量」计费的厂商会按次数收费（D-ID 试用账号只有 12 个额度）。

    合并后每段音频在成片里的时间位置不变：整段音频是按时序拼出来的，
    下游字幕用的还是 voice 阶段那张绝对时间轴，所以字幕照样逐句对齐。

    `merge_max_s`（秒）为 0 或不配则不合并，保持逐句一任务。
    """
    merge_max_s = float(cfg.get("merge_max_s", 0) or 0)
    # 厂商声明的音频上限必须能压过我们自己的合并上限。
    # 否则「merge_max_s=150」配上「某家只收 60s」就会稳定撞墙 ——
    # 声明了能力却不照它做，等于没声明。
    declared_max = CAP.audio_limits(cfg).max_s
    if declared_max:
        merge_max_s = min(merge_max_s, declared_max) if merge_max_s else declared_max

    if merge_max_s <= 0 or len(segments) <= 1:
        return [{**dict(s), "audio_path": str(_segment_audio(s))} for s in segments]

    out_dir.mkdir(parents=True, exist_ok=True)
    groups: list[list[dict]] = []
    current: list[dict] = []
    total = 0.0
    for segment in segments:
        path = _segment_audio(segment)
        duration = _safe_wav_duration(path)
        if current and total + duration > merge_max_s:
            groups.append(current)
            current, total = [], 0.0
        current.append(dict(segment))
        total += duration
    if current:
        groups.append(current)

    jobs: list[dict] = []
    for index, group in enumerate(groups):
        if len(group) == 1:
            jobs.append({**group[0], "audio_path": str(_segment_audio(group[0]))})
            continue
        parts = [_segment_audio(s) for s in group]
        merged = out_dir / f"talk_{index:02d}.wav"
        M.concat_wav_pcm(parts, merged, gap_ms=0, trailing_gap=False)
        jobs.append({**group[0], "audio_path": str(merged)})
        log(f"    合并 {len(group)} 句 -> {merged.name}"
            f"（{M.wav_duration(merged):.1f}s，少提交 {len(group) - 1} 次）")
    if len(jobs) < len(segments):
        log(f"  云端任务数：{len(segments)} 句 -> {len(jobs)} 个（上限 {merge_max_s:.0f}s/个）")
    # 单句本身就超过厂商上限 —— 合并救不了，只能明确报出来
    if declared_max:
        for job in jobs:
            got = _safe_wav_duration(job.get("audio_path"))
            if got > declared_max:
                raise ProviderError(
                    f"有一段音频 {got:.1f}s 超过该厂商的上限 {declared_max:.0f}s，"
                    f"拆任务也救不了。\n"
                    f"  请减少内容量，或换一家支持更长音频的厂商。")
    return jobs


def _avatar_template(
    config: Any, segments, visuals, out_dir: Path, log,
    cfg: dict, api_key: str, provider_name: str,
    portrait: Path | None = None,
) -> list[Path]:
    """模板驱动的云端数字人：提交任务 → 轮询 → 下载成片。

    和语音那套同一个思路：各家只差三件事 —— 提交怎么拼、任务 ID 在响应哪个字段、
    成片 URL 在哪。用模板描述，加一家厂商 = 加一份模板，不用改代码。

    模板变量：
        {{api_key}} {{model}} {{job_id}}
        {{image_base64}} {{image_data_uri}} {{image_format}}
        {{audio_base64}} {{audio_data_uri}} {{audio_format}}
    可用变量：submit.url / submit.method / submit.headers / submit.body /
             submit.job_id_path
             query.url（含 {{job_id}}）/ query.method / query.headers /
             query.status_path / query.done_values / query.failed_values /
             query.result_path；可选 result.url / result.method / result.headers /
             result.body / result.result_path（状态与结果分开的队列 API）
             poll_interval_s / timeout_s / merge_max_s
    """
    submit = cfg.get("submit") or {}
    query = cfg.get("query") or {}
    result_cfg = cfg.get("result") or {}
    submit_url = str(submit.get("url") or "")
    query_url = str(query.get("url") or "")
    out_dir.mkdir(parents=True, exist_ok=True)
    if not submit_url or not query_url:
        raise ProviderError(
            f"「{provider_name}」还缺提交/查询地址。在「设置」页填好再试。")
    timeout_s = int(cfg.get("timeout_s", 1800))
    interval = float(cfg.get("poll_interval_s", 10))
    done_values = [str(v).lower() for v in
                   (query.get("done_values") or ["done", "success", "succeeded"])]
    failed_values = [str(v).lower() for v in
                     (query.get("failed_values") or ["error", "failed", "rejected"])]

    clips: list[Path] = []
    jobs = _merge_avatar_jobs(segments, cfg, out_dir, log)
    # 断点续跑的安全阀：上一轮下好的片段只有在「任务划分完全一致」时才能复用。
    # 否则会拿旧的「第 1 句」冒充新的「整段」，成片直接少掉大半内容。
    plan = hashlib.sha1(json.dumps(
        [[Path(str(j.get("audio_path") or "")).name,
          round(_safe_wav_duration(j.get("audio_path")), 3)] for j in jobs],
        ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    plan_file = out_dir / ".avatar_plan.json"
    try:
        previous = str(json.loads(plan_file.read_text(encoding="utf-8")).get("plan") or "")
    except Exception:                            # noqa: BLE001 - 没有/坏了都当首次
        previous = ""
    existing = sorted(out_dir.glob("avatar_*.mp4"))
    # previous 为空也算「变了」：没有划分记录就无从证明这些片段的身份，
    # 宁可重做也不能拿错片段去拼。
    if existing and previous != plan:
        for path in existing:
            path.unlink(missing_ok=True)
        log(f"  任务划分与旧片段不匹配（{previous or '无记录'} -> {plan}），"
            f"作废 {len(existing)} 个旧片段")
    plan_file.write_text(json.dumps({"plan": plan, "jobs": len(jobs)}),
                         encoding="utf-8")
    # 上传结果按「插槽 + 文件」缓存。形象主图每个分片都是同一张，
    # 之前每段重传一次（3072×4096 压完也有几百 KB），纯浪费。
    upload_cache: dict[tuple[str, str], str] = {}

    for index, segment in enumerate(jobs):
        audio_path = Path(str(segment.get("audio_path") or ""))
        if not audio_path.exists():
            raise ProviderError(f"第 {index + 1} 段的音频不存在：{audio_path}")
        image_path = portrait if portrait is not None else visuals[index % len(visuals)]

        # 断点续跑：上一轮已经下好的片段直接复用。
        # 云端一次任务要花钱也要等，重跑时不该为已经拿到的结果再付一次。
        done_clip = out_dir / f"avatar_{index:02d}.mp4"
        if done_clip.exists() and done_clip.stat().st_size > 10_000:
            log(f"    ↻ 复用上一轮已下好的片段 {done_clip.name}"
                f"（{done_clip.stat().st_size / 1e6:.1f} MB）")
            clips.append(done_clip)
            continue

        # 图片必须先压小。用户的照片是 3072×4096，原样 base64 接近 19MB，
        # 塞进 JSON body 云端直接 500（实测 D-ID 就报 Internal Server Error）。
        # 具体压到多大由**该厂商声明的能力**决定，不再拍脑袋写 1280。
        max_side = int(cfg.get("image_max_side", 1280) or 1280)
        img_limits = CAP.image_limits(cfg)
        image_blob, img_fmt, plan = _prepare_cloud_image(image_path, max_side, img_limits)
        if img_limits.min_side or img_limits.max_pixels or img_limits.formats:
            log(f"    图片规格：{img_limits.describe()}")
        if not plan.ok:
            # 上传前就拦住。D-ID 的 /images 不查像素上限、纯色图也收，
            # 等发现不对时额度已经花了。
            raise ProviderError(
                f"「{provider_name}」不接受这张图：\n  - " + "\n  - ".join(plan.issues) +
                f"\n  该厂商要求：{img_limits.describe()}")
        image_b64 = base64.b64encode(image_blob).decode("ascii")
        audio_b64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
        aud_fmt = (audio_path.suffix.lstrip(".") or "wav").lower()
        log(f"    上传图 {len(image_blob) / 1024:.0f} KB（{img_fmt}）"
            f"　音频 {len(audio_b64) / 1024:.0f} KB(base64)")
        context = {
            "api_key": api_key,
            "model": str(cfg.get("model", "")),
            "segment_text": str(segment.get("text") or ""),
            "motion_prompt": str(cfg.get("motion_prompt") or ""),
            "image_base64": image_b64,
            "audio_base64": audio_b64,
            "image_format": img_fmt,
            "audio_format": aud_fmt,
            # 不少厂商（D-ID / HeyGen）要的是 URL 或 data URI，不是裸 base64
            "image_data_uri": f"data:image/{img_fmt};base64,{image_b64}",
            "audio_data_uri": f"data:audio/{aud_fmt};base64,{audio_b64}",
        }
        # 厂商专属的标量配置也可用于 URL/请求体模板，例如百炼的 workspace_id。
        # 通用展开比在这里逐家新增变量更不容易漏。
        context.update({str(k): v for k, v in cfg.items()
                        if isinstance(v, (str, int, float, bool))})

        # 「上传前置」：有些厂商（D-ID / HeyGen）**只收公网可访问的 URL**，
        # 明确拒绝 data URI（实测 D-ID 报 "must be a valid image URL"）。
        # 它们同时提供了资产上传接口，所以先传文件拿 URL，再带着 URL 提交。
        # 配置形如：
        #   upload.image = {url, field, filename, content_type, url_path, headers}
        #   upload.audio = {...}
        # 上传成功后 {{image_url}} / {{audio_url}} 才可用。
        upload = cfg.get("upload") or {}
        for slot, blob, filename, content_type in (
            ("image", image_blob, "autovid.jpg", "image/jpeg"),
            ("audio", audio_path.read_bytes(), f"autovid{audio_path.suffix or '.wav'}",
             "audio/wav"),
        ):
            context[f"{slot}_url"] = ""
            spec = upload.get(slot) or {}
            mode = str(spec.get("mode") or "multipart").lower()
            if not spec.get("url") and mode != "dashscope_oss":
                continue
            cache_key = (slot, str(image_path if slot == "image" else audio_path))
            if cache_key in upload_cache:
                context[f"{slot}_url"] = upload_cache[cache_key]
                log(f"    ✓ {slot} 复用已上传的 URL：{upload_cache[cache_key][:70]}")
                continue
            log(f"    上传{('图' if slot == 'image' else '音频')}到云端…")
            if mode == "dashscope_oss":
                try:
                    value = _retry(
                        lambda: _dashscope_oss_upload(
                            spec, blob,
                            str(spec.get("filename") or filename),
                            str(spec.get("content_type") or content_type),
                            context,
                        ),
                        what=f"上传{slot}", log=log, cfg=cfg,
                    )
                except ProviderError as exc:
                    raise _signed_error(
                        exc, cfg, provider_name,
                        f"上传{('图片' if slot == 'image' else '音频')}") from exc
                upload_cache[cache_key] = str(value)
                context[f"{slot}_url"] = str(value)
                log(f"    ✓ {slot} 已上传：{str(value)[:70]}")
                continue
            form = _render_template(spec.get("fields") or {}, context)
            payload, multipart_type = M.build_multipart(
                form, str(spec.get("field") or slot),
                str(spec.get("filename") or filename), blob,
                str(spec.get("content_type") or content_type))
            up_headers = _render_template(spec.get("headers") or {}, context)
            up_headers = {k: v for k, v in up_headers.items()
                          if k.lower() != "content-type"}
            up_headers["Content-Type"] = multipart_type

            def _do_upload() -> dict:
                raw = _http_raw(str(spec["url"]), payload, up_headers, 300)
                return json.loads(raw.decode("utf-8", errors="replace") or "{}")

            try:
                uploaded = _retry(_do_upload, what=f"上传{slot}", log=log, cfg=cfg)
            except ProviderError as exc:
                raise _signed_error(
                    exc, cfg, provider_name,
                    f"上传{('图片' if slot == 'image' else '音频')}") from exc
            value = _dig(uploaded, str(spec.get("url_path") or "url"))
            if not value:
                raise ProviderError(
                    f"上传{slot}后没拿到 URL（配置路径 '{spec.get('url_path')}'）。\n"
                    f"  原始响应：{json.dumps(uploaded, ensure_ascii=False)[:400]}")

            # 免费的人脸闸门：D-ID 的 /images 响应里带 faces[]，而**纯色图也能
            # 上传成功**。不读它的话，「图里没人」这种错误会一路拖到生成阶段
            # 才炸 —— 那时候额度已经花了。
            if slot == "image" and img_limits.reports_faces:
                faces = uploaded.get("faces")
                if not isinstance(faces, list):
                    # 关键：不能静默跳过。实测不显式传 detect_faces 时这个字段
                    # 就是 null —— 那时闸门形同虚设，而日志上看不出任何异常。
                    log(f"    ⚠ 该厂商声明会返回人脸检测结果，但这次响应里没有 "
                        f"faces 字段（{str(uploaded.get('faces'))[:20]}）—— "
                        f"无法在提交前确认图里有脸，只能靠厂商在生成阶段报错。")
                elif not faces:
                    log(f"    ✗ 人脸检测结果：0 张脸（这张图拿去生成必然失败）")
                    raise ProviderError(
                        f"「{provider_name}」在这张图里**没检测到人脸**，生成必然失败。\n"
                        f"  图片：{image_path.name}\n"
                        f"  请换一张正脸、清晰、无遮挡的照片。\n"
                        f"  （这一步是免费的前置检查，没有产生费用）")
                else:
                    best = max(faces, key=lambda f: float(f.get("size") or 0))
                    log(f"    ✓ 检测到 {len(faces)} 张人脸："
                        f"置信度 {best.get('detect_confidence')}、"
                        f"清晰度 {best.get('sharpness')}、"
                        f"遮挡 {best.get('face_occluded')}")

            upload_cache[cache_key] = str(value)
            context[f"{slot}_url"] = str(value)
            log(f"    ✓ {slot} 已上传：{str(value)[:70]}")

        headers = _render_template(submit.get("headers") or {}, context)
        body = _render_template(submit.get("body") or {}, context)
        # URL 和 body/headers 一样都属于模板。以前只渲染后两者，像
        # queue.fal.run/{{model}} 这样的地址会把占位符原样发出去并得到 405。
        submit_target = _render_template(submit_url, context)
        submit_method = str(submit.get("method") or
                            ("POST" if body is not None else "GET")).upper()
        log(f"  提交数字人任务 {index + 1}/{len(jobs)}…")
        try:
            submitted = _retry(lambda: _http(str(submit_target), body, headers=headers,
                                              timeout_s=180, method=submit_method),
                               what="提交任务", log=log, cfg=cfg)
        except ProviderError as exc:
            # 走到这里说明重试已经放弃了。把厂商声明的签名翻出来，
            # 让人一眼知道是「图太大」而不是去怀疑 Key 或网络。
            raise _signed_error(exc, cfg, provider_name, "提交任务") from exc
        job_id = _dig(submitted, str(submit.get("job_id_path") or "id"))
        if not job_id:
            raise ProviderError(
                f"提交后没拿到任务 ID（配置路径 '{submit.get('job_id_path')}'）。\n"
                f"  原始响应：{json.dumps(submitted, ensure_ascii=False)[:500]}")
        log(f"    job_id = {job_id}，轮询结果…")

        deadline = time.time() + timeout_s
        video_url = None
        state = ""
        polls = 0
        while time.time() < deadline:
            query_context = {**context, "job_id": str(job_id)}
            # 队列服务提交后若返回自己的 status_url / response_url，优先使用；
            # 比客户端自行重拼路径更稳，也能兼容模型路由调整。
            query_url_path = str(query.get("url_path") or "")
            actual_query_url = (_dig(submitted, query_url_path)
                                if query_url_path else None) or query_url
            target = _render_template(str(actual_query_url), query_context)
            qheaders = _render_template(query.get("headers") or {}, query_context)
            qbody = (_render_template(query.get("body"), query_context)
                     if query.get("body") is not None else None)
            qmethod = str(query.get("method") or ("POST" if qbody is not None else "GET"))
            # 轮询本身必须容错：一次 SSL 抖动不该让整条工作流前功尽弃
            # （真实事故：分片 3 轮询时 UNEXPECTED_EOF，图被中止）。
            try:
                status = _retry(lambda: _http(str(target), qbody, headers=qheaders,
                                              timeout_s=60, method=qmethod),
                                what="查询任务状态", log=log, attempts=5, cfg=cfg)
            except ProviderError as exc:
                raise _signed_error(
                    exc, cfg, provider_name, "查询任务状态") from exc
            polls += 1
            state = str(_dig(status, str(query.get("status_path") or "status")) or "").lower()
            if state in done_values:
                if result_cfg.get("url"):
                    result_url_path = str(result_cfg.get("url_path") or "")
                    actual_result_url = (_dig(submitted, result_url_path)
                                         if result_url_path else None) or result_cfg["url"]
                    result_target = _render_template(
                        str(actual_result_url), query_context)
                    result_headers = _render_template(
                        result_cfg.get("headers") or {}, query_context)
                    result_body = (_render_template(result_cfg.get("body"), query_context)
                                   if result_cfg.get("body") is not None else None)
                    result_method = str(result_cfg.get("method") or
                                        ("POST" if result_body is not None else "GET"))
                    try:
                        result_data = _retry(
                            lambda: _http(str(result_target), result_body,
                                          headers=result_headers, timeout_s=120,
                                          method=result_method),
                            what="获取任务结果", log=log, attempts=5, cfg=cfg)
                    except ProviderError as exc:
                        raise _signed_error(
                            exc, cfg, provider_name, "获取任务结果") from exc
                    video_url = _dig(
                        result_data,
                        str(result_cfg.get("result_path") or "video.url"))
                else:
                    video_url = _dig(
                        status, str(query.get("result_path") or "result_url"))
                break
            if state in failed_values:
                raise ProviderError(
                    f"云端数字人任务失败（{state}）。\n"
                    f"  原始响应：{json.dumps(status, ensure_ascii=False)[:500]}")
            if polls % 3 == 0:
                left = max(0, int(deadline - time.time()))
                log(f"    等待中… 状态 '{state}'（已查 {polls} 次，剩余 {left}s）")
            time.sleep(interval)
        if not video_url:
            raise ProviderError(
                f"等了 {timeout_s}s 还没出结果（最后状态 '{state}'）。\n"
                f"  如果状态一直是初始值，多半是提交的字段不对，"
                f"对着文档核一下 submit.body。")

        clip = out_dir / f"avatar_{index:02d}.mp4"
        # 成片下载同样重试：预签名 URL 有有效期，偶发 5xx/断流要能自愈。
        try:
            blob = _retry(
                lambda: _http_raw(str(video_url), None, {}, timeout_s=600, method="GET"),
                what="下载成片", log=log, cfg=cfg)
        except ProviderError as exc:
            raise _signed_error(exc, cfg, provider_name, "下载成片") from exc
        if len(blob) < 10_000:
            raise ProviderError(f"下载到的成片太小（{len(blob)} 字节），可能不是视频")
        postprocess = str(cfg.get("postprocess") or "").strip().lower()
        if postprocess == "local_wav2lip":
            # 图生视频负责表情、头部与手势；Wav2Lip 只负责把嘴型重新对齐
            # 到本段真实配音。两层各做擅长的事，避免退化成“只有嘴在动”。
            motion = out_dir / f"motion_{index:02d}.mp4"
            motion.write_bytes(blob)
            if cfg.get("require_motion_duration_match"):
                motion_s = M.probe_duration(motion)
                audio_s = M.wav_duration(audio_path)
                if motion_s + 0.75 < audio_s:
                    raise ProviderError(
                        f"「{provider_name}」只生成了 {motion_s:.1f}s 动作，"
                        f"但配音有 {audio_s:.1f}s；继续处理会循环动作并造成突变，"
                        "已停止生成。请改用音频直接驱动的数字人模型。")
            from .wav2lip import Wav2LipRunner  # noqa: PLC0415
            runner = Wav2LipRunner.get(config)
            log(f"    动作片段已生成，正在按真实配音重做口型…")
            runner.render(
                motion, audio_path, clip, log,
                fps=int(config.platform.get("fps", 30)),
                width=int(config.platform.get("width", 1080)),
                height=int(config.platform.get("height", 1920)),
            )
        elif postprocess:
            raise ProviderError(f"未知数字人后处理：{postprocess}")
        else:
            clip.write_bytes(blob)
        log(f"    片段就绪：{clip.name}（{len(blob) / 1e6:.1f} MB）")
        clips.append(clip)
    return clips


def _avatar_http_job(
    config: Any, segments, visuals, out_dir: Path, log, portrait: Path | None = None,
    cfg: dict | None = None, api_key: str | None = None,
    provider_name: str = "http_job",
) -> list[Path]:
    """[未实测] 通用「提交任务 + 轮询结果」型数字人服务。

    约定：
        POST {submit_url}  {"image": "<base64>", "audio": "<base64>"} -> {"task_id": "..."}
        GET  {query_url}?task_id=...  -> {"status": "done|failed|running", "video_url": "..."}

    注意：这是**通用约定**，各家差异很大（HeyGen 要 talking_photo + asset_id，
    D-ID 要 source_url + Basic 认证，fal/Replicate 走队列）。设置面板里配的
    条目会走到这里，字段对不上时会把**原始响应**打出来，方便对照文档改模板。
    """
    cfg = cfg if cfg is not None else config.provider_cfg("avatar_http")
    submit_url = str(cfg.get("submit_url", ""))
    query_url = str(cfg.get("query_url", ""))
    if not submit_url or not query_url:
        raise ProviderError(
            f"「{provider_name}」缺 submit_url / query_url。在「设置」页填好再试。")
    api_key = api_key or config.secret_for("avatar_http", "AUTOVID_AVATAR_API_KEY")
    headers = _auth_header(api_key)
    timeout_s = int(cfg.get("timeout_s", 1800))
    interval = float(cfg.get("poll_interval_s", 10))

    clips: list[Path] = []
    for index, segment in enumerate(segments):
        audio_path = Path(segment["audio_path"])
        image_path = portrait if portrait is not None else visuals[index % len(visuals)]
        payload = {
            "image": base64.b64encode(image_path.read_bytes()).decode("ascii"),
            "audio": base64.b64encode(audio_path.read_bytes()).decode("ascii"),
            "audio_format": "wav",
            "image_format": image_path.suffix.lstrip("."),
        }
        submitted = _http(submit_url, payload, headers=headers, timeout_s=120)
        task_id = submitted.get("task_id") or submitted.get("id") or submitted.get("data", {}).get("task_id")
        if not task_id:
            raise ProviderError(f"数字人服务未返回 task_id：{json.dumps(submitted, ensure_ascii=False)[:300]}")

        deadline = time.time() + timeout_s
        video_url = None
        while time.time() < deadline:
            status = _http(f"{query_url}?task_id={task_id}", None, headers=headers, timeout_s=60)
            state = str(status.get("status") or status.get("state") or "").lower()
            if state in ("done", "success", "succeeded", "finished", "completed"):
                video_url = status.get("video_url") or status.get("url") or status.get("data", {}).get("video_url")
                break
            if state in ("failed", "error"):
                raise ProviderError(f"数字人任务失败：{json.dumps(status, ensure_ascii=False)[:300]}")
            time.sleep(interval)
        if not video_url:
            raise ProviderError(f"数字人任务超时（{timeout_s}s）")

        clip = out_dir / f"avatar_{index:02d}.mp4"
        _http_download(video_url, clip, timeout_s=600)
        clips.append(clip)
        log(f"  数字人片段 {index + 1}/{len(segments)} 就绪")
    return clips


def _avatar_minimax(
    config: Any, segments, visuals, out_dir: Path, log, portrait: Path | None = None
) -> list[Path]:
    """[未实测] MiniMax H3 / 海螺 视频生成 API。

    注意：MiniMax H3 的**开放权重**需要 CUDA 大显存，AMD 显卡跑不了；
    这里走的是 MiniMax 云端 API（按次计费）。
    接口字段请以 MiniMax 官方文档为准，本实现未在真实账号上验证过。
    """
    cfg = config.provider_cfg("minimax_h3")
    base = str(cfg.get("base_url", "")).rstrip("/")
    api_key = config.secret_for("minimax_h3", "AUTOVID_MINIMAX_API_KEY")
    headers = _auth_header(api_key)
    model = str(cfg.get("model", "MiniMax-Hailuo-H3"))
    timeout_s = int(cfg.get("timeout_s", 1800))
    interval = float(cfg.get("poll_interval_s", 10))

    clips: list[Path] = []
    for index, segment in enumerate(segments):
        prompt = str(segment.get("visual_prompt") or segment["text"])[:2000]
        submitted = _http(
            f"{base}/video_generation",
            {"model": model, "prompt": prompt, "duration": 6, "resolution": "1080P"},
            headers=headers, timeout_s=120,
        )
        task_id = submitted.get("task_id")
        if not task_id:
            raise ProviderError(f"MiniMax 未返回 task_id：{json.dumps(submitted, ensure_ascii=False)[:300]}")

        deadline = time.time() + timeout_s
        file_id = None
        while time.time() < deadline:
            status = _http(f"{base}/query/video_generation?task_id={task_id}", None,
                           headers=headers, timeout_s=60)
            state = str(status.get("status", "")).lower()
            if state == "success":
                file_id = status.get("file_id")
                break
            if state in ("fail", "failed"):
                raise ProviderError(f"MiniMax 任务失败：{json.dumps(status, ensure_ascii=False)[:300]}")
            time.sleep(interval)
        if not file_id:
            raise ProviderError(f"MiniMax 任务超时（{timeout_s}s）")

        info = _http(f"{base}/files/retrieve?file_id={file_id}", None, headers=headers, timeout_s=60)
        url = (info.get("file") or {}).get("download_url")
        if not url:
            raise ProviderError(f"无法取得下载地址：{json.dumps(info, ensure_ascii=False)[:300]}")
        clip = out_dir / f"avatar_{index:02d}.mp4"
        _http_download(url, clip, timeout_s=900)
        clips.append(clip)
    return clips


# =========================================================================== #
# 5. Publish —— 发布
# =========================================================================== #
def publish(
    config: Any,
    video: Path,
    cover: Path,
    cover_3x4: Path,
    metadata: dict[str, Any],
    out_dir: Path,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """生成自包含的发布包。P0 只做「一站式备好，人工上传」，不碰账号自动化。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    copy_media = bool(config.step_cfg("publish").get("copy_media", True))

    titles: list[str] = list(metadata.get("title_options") or [])
    tags: list[str] = list(metadata.get("tags") or [])

    # 把成片和封面复制进发布包，让它成为一个可以直接上传的自包含目录
    if copy_media:
        packaged_video = out_dir / "video.mp4"
        packaged_cover = out_dir / "cover.jpg"
        packaged_cover_3x4 = out_dir / "cover_3x4.jpg"
        shutil.copy2(video, packaged_video)
        shutil.copy2(cover, packaged_cover)
        shutil.copy2(cover_3x4, packaged_cover_3x4)
    else:
        packaged_video, packaged_cover, packaged_cover_3x4 = video, cover, cover_3x4

    caption_path = out_dir / "发布文案.txt"
    caption_path.write_text(
        "\n".join([
            "【标题候选】",
            *[f"{i + 1}. {t}" for i, t in enumerate(titles)],
            "",
            "【简介】",
            str(metadata.get("description", "")),
            "",
            "【话题标签】",
            " ".join(f"#{t}" for t in tags),
            "",
            "【AI 内容标识（法规要求，务必勾选）】",
            "本视频为人工智能生成合成内容，发布时需按平台要求开启「AI 生成内容」标识。",
        ]),
        encoding="utf-8",
    )

    checklist_path = out_dir / "发布清单.md"
    checklist_path.write_text(
        "\n".join([
            "# 抖音发布清单",
            "",
            f"- [ ] 成片：`{packaged_video.name}`（"
            f"{config.get('platform.width')}x{config.get('platform.height')}，H.264，AAC）",
            f"- [ ] 竖版封面：`{packaged_cover.name}`",
            f"- [ ] 3:4 封面：`{packaged_cover_3x4.name}`",
            f"- [ ] 标题：从 `{caption_path.name}` 里挑一个，控制在 30 字内",
            "- [ ] **开启「AI 生成内容」标识**"
            "（《人工智能生成合成内容标识办法》2025-09-01 施行）",
            "- [ ] 确认音色已获得本人授权，未克隆他人声音",
            "- [ ] 确认文案为原创改写，未逐句搬运他人作品",
            "- [ ] 发布时间：工作日 12:00-13:00 或 19:00-22:00",
            "",
            "## 为什么不做全自动发布",
            "抖音对自动化发布风控严格，Playwright 脚本方案存在账号封禁风险。",
            "建议优先申请抖音开放平台权限走官方接口；在拿到资质前，",
            "用「一键备好 + 人工点发布」是风险最低的做法。",
        ]),
        encoding="utf-8",
    )

    report = {
        "platform": str(config.step_cfg("publish").get("platform", "douyin")),
        "video": str(packaged_video),
        "cover": str(packaged_cover),
        "cover_3x4": str(packaged_cover_3x4),
        "titles": titles,
        "description": metadata.get("description", ""),
        "tags": tags,
        "caption_file": str(caption_path),
        "checklist_file": str(checklist_path),
        "published": False,
        "note": "P0：生成自包含发布包，需人工上传",
    }
    (out_dir / "publish_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"发布包已生成：{out_dir}")
    return report
