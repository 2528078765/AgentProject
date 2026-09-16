"""ComfyUI 客户端：把 ComfyUI 当成「出图」和「数字人」的统一生成后端。

为什么值得单独做一层：

1. **ComfyUI 是 AMD 用户唯一现实的本地生成后端**。
   借助 `comfyui-rocm`（AMD 官方 ROCm + PyTorch，支持 RDNA1~RDNA4），
   你的 RX 6750 GRE 能跑真正的 ROCm，而不是 ZLUDA 转译层。
   ComfyUI 社区里已经有 Wav2Lip / MuseTalk / Sonic 等数字人工作流。

2. **工作流由用户提供，代码不做假设**。
   我们用 `{{占位符}}` 注入参数（图片、音频、宽高、种子），
   所以换一个数字人工作流不用改一行代码 —— 换一个 JSON 就行。

3. **它是纯粹的编排层**，和上层用什么编排（自研引擎 / LangGraph / n8n）无关。
   一个节点要做的就是「提交任务 -> 轮询 -> 取回文件」。

ComfyUI 的接口很小，一共就四个：
    POST /upload/image      上传输入文件（multipart）
    POST /prompt            提交工作流，返回 prompt_id
    GET  /history/{id}      查询执行结果
    GET  /view?...          下载产物
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import media as M
from .errors import AutoVidError

VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".gif"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

# 这些占位符的值必须是数字。用户在 JSON 里很容易写成 "{{WIDTH}}"（带引号），
# 那样替换出来就是字符串 "1080"，ComfyUI 会报参数类型错误。
# 所以这里做一次自动纠正 —— 属于「能自动填的坑就别让用户踩」。
NUMERIC_PLACEHOLDERS = {"WIDTH", "HEIGHT", "FPS", "SEED", "DURATION",
                        "BATCH", "STEPS", "CFG", "LENGTH", "FRAMES"}


class ComfyError(AutoVidError):
    """ComfyUI 通信或执行失败。"""


def _http(url: str, payload: dict | None = None, raw: bytes | None = None,
          headers: dict[str, str] | None = None, timeout: int = 120) -> bytes:
    data = raw if raw is not None else (
        json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None)
    merged = dict(headers or {})
    if payload is not None and raw is None:
        merged.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, headers=merged,
                                    method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        # 502/503/504 说明请求根本没到 ComfyUI（网关/代理层就挡了，
        # 或者服务正在重启）。这类要按「连不上」提示，而不是甩一句 HTTP 错误码
        # —— 否则用户看到 "HTTP 502" 完全不知道该怎么办。
        if exc.code in (502, 503, 504):
            raise ComfyError(
                f"ComfyUI 服务不可用（HTTP {exc.code}）<- {url}\n"
                "  请求没有到达 ComfyUI：可能是服务没启动/正在重启，"
                "或中间有代理挡住了。\n"
                "  确认 ComfyUI 已经启动，且地址端口和 providers.comfy.url 一致。\n"
                f"  原始响应：{detail[:200]}"
            ) from exc
        raise ComfyError(f"HTTP {exc.code} <- {url}\n{detail}") from exc
    except urllib.error.URLError as exc:
        raise ComfyError(
            f"连不上 ComfyUI（{url}）：{exc.reason}\n"
            "  确认 ComfyUI 已经启动，且地址端口和 providers.comfy.url 一致。"
        ) from exc


def _http_json(url: str, payload: dict | None = None, timeout: int = 120) -> dict:
    body = _http(url, payload=payload, timeout=timeout)
    if not body.strip():
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ComfyError(f"响应不是合法 JSON <- {url}\n{body[:300]!r}") from exc


def _coerce_numbers(node: Any, numeric_literals: set[str]) -> Any:
    """把「本该是数字却成了字符串」的值转回数字。"""
    if isinstance(node, dict):
        return {k: _coerce_numbers(v, numeric_literals) for k, v in node.items()}
    if isinstance(node, list):
        return [_coerce_numbers(v, numeric_literals) for v in node]
    if isinstance(node, str) and node in numeric_literals:
        try:
            return int(node)
        except ValueError:
            try:
                return float(node)
            except ValueError:
                return node
    return node


def render_workflow(template: str, context: dict[str, Any]) -> dict:
    """把工作流 JSON 里的 {{占位符}} 替换掉。

    直接在 JSON 文本层面替换，然后解析 —— 这样用户只要把 ComfyUI 里
    「导出 (API 格式)」得到的工作流丢进来、把对应字段改成占位符即可，
    不需要理解任何内部结构。

    数字类占位符（WIDTH/HEIGHT/FPS/SEED/DURATION 等）即使被写进引号里，
    也会被自动转回数字 —— ComfyUI 对参数类型很严格，这个坑不该让用户踩。
    """
    text = template
    for key, value in context.items():
        text = text.replace("{{" + key + "}}", str(value))
    try:
        graph = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ComfyError(
            f"替换占位符后工作流不是合法 JSON：{exc}\n"
            "  常见原因：占位符把引号截断了。\n"
            "  正确写法：字符串字段写成 \"image\": \"{{IMAGE}}\"（占位符在引号内），\n"
            "            数字字段写成 \"width\": {{WIDTH}}（占位符在引号外）。"
        ) from exc
    numeric_literals = {
        str(context[key]) for key in NUMERIC_PLACEHOLDERS if key in context
    }
    return _coerce_numbers(graph, numeric_literals)


class ComfyClient:
    def __init__(self, base_url: str, timeout_s: int = 600, poll_s: float = 2.0,
                 log=print):
        if not base_url:
            raise ComfyError("providers.comfy.url 未配置")
        self.base = base_url.rstrip("/")
        self.timeout_s = int(timeout_s)
        self.poll_s = max(0.2, float(poll_s))
        self.log = log
        self.client_id = f"autovid-{int(time.time())}"

    # ---------------------------------------------------------------- 探测
    def ping(self) -> dict:
        """确认 ComfyUI 在线，并返回它的系统信息。"""
        return _http_json(f"{self.base}/system_stats", timeout=20)

    # ---------------------------------------------------------------- 上传
    def upload(self, path: Path, endpoint: str = "/upload/image", field: str = "image",
               subfolder: str = "autovid", content_type: str = "application/octet-stream") -> str:
        """上传一个输入文件，返回它在 ComfyUI 里可被引用的文件名。"""
        payload, ctype = M.build_multipart(
            {"subfolder": subfolder, "type": "input", "overwrite": "true"},
            field, Path(path).name, Path(path).read_bytes(), content_type,
        )
        data = _http(f"{self.base}{endpoint}", raw=payload,
                     headers={"Content-Type": ctype}, timeout=180)
        try:
            result = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ComfyError(
                f"上传 {Path(path).name} 失败，返回的不是 JSON：{data[:200]!r}\n"
                f"  如果这是音频文件，ComfyUI 的 /upload/image 可能拒绝非图片；\n"
                f"  把 providers.comfy.upload.audio.endpoint 改成你那个音频节点\n"
                f"  实际使用的上传接口即可。"
            ) from None
        name = result.get("name") or Path(path).name
        self.log(f"    已上传 {Path(path).name} -> {name}")
        return name

    # ---------------------------------------------------------------- 执行
    def submit(self, workflow: dict) -> str:
        data = _http_json(f"{self.base}/prompt",
                          {"prompt": workflow, "client_id": self.client_id}, timeout=120)
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            node_errors = data.get("node_errors") or data.get("error") or data
            raise ComfyError(
                "ComfyUI 没有返回 prompt_id，工作流很可能不合法：\n"
                f"{json.dumps(node_errors, ensure_ascii=False)[:600]}"
            )
        return str(prompt_id)

    def wait(self, prompt_id: str) -> dict:
        """轮询直到执行结束，返回该次执行的 history 条目。

        注意：**执行完成 ≠ 有产物**。工作流没接 SaveImage / VHS_VideoCombine
        这类输出节点时会「成功完成但没有任何输出」。这种情况必须立刻返回，
        让上层报出「检查输出节点」，而不是傻等到超时 —— 数字人工作流超时
        动辄 30 分钟，让用户白等是最糟糕的体验。
        """
        deadline = time.time() + self.timeout_s
        while time.time() < deadline:
            history = _http_json(f"{self.base}/history/{prompt_id}", timeout=60)
            entry = history.get(prompt_id)
            if entry:
                status = entry.get("status") or {}
                state = str(status.get("status_str") or "").lower()
                if state == "error":
                    raise ComfyError(
                        "ComfyUI 执行出错："
                        f"{json.dumps(status.get('messages'), ensure_ascii=False)[:600]}"
                    )
                finished = (
                    bool(entry.get("outputs"))
                    or status.get("completed") is True
                    or state in ("success", "completed")
                )
                if finished:
                    return entry
            time.sleep(self.poll_s)
        raise ComfyError(
            f"等待 ComfyUI 执行超时（{self.timeout_s}s）。\n"
            "  数字人工作流很慢，可以调大 providers.comfy.avatar_timeout_s；\n"
            "  也可能是工作流卡在某个节点上，去 ComfyUI 界面看它的执行进度。"
        )

    # ---------------------------------------------------------------- 取产物
    @staticmethod
    def _collect(outputs: dict, want: str) -> list[dict]:
        wanted = VIDEO_EXTS if want == "video" else IMAGE_EXTS
        fallback: list[dict] = []
        for node in (outputs or {}).values():
            for key in ("images", "gifs", "videos", "audio", "files"):
                for item in node.get(key) or []:
                    if not isinstance(item, dict) or not item.get("filename"):
                        continue
                    suffix = Path(item["filename"]).suffix.lower()
                    if suffix in wanted:
                        fallback.insert(0, item)
                    else:
                        fallback.append(item)
        return fallback

    def download(self, item: dict, out_path: Path) -> Path:
        query = urllib.parse.urlencode({
            "filename": item.get("filename", ""),
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        })
        blob = _http(f"{self.base}/view?{query}", timeout=600)
        out_path.write_bytes(blob)
        return out_path

    def run(self, workflow: dict, out_dir: Path, want: str = "video",
            prefix: str = "out") -> list[Path]:
        """提交工作流 -> 等结果 -> 把产物下载下来。"""
        out_dir.mkdir(parents=True, exist_ok=True)
        prompt_id = self.submit(workflow)
        self.log(f"    ComfyUI 任务已提交：{prompt_id}")
        entry = self.wait(prompt_id)
        items = self._collect(entry.get("outputs") or {}, want)
        if not items:
            raise ComfyError(
                "ComfyUI 执行完成但没有任何产物。\n"
                f"  原始 outputs：{json.dumps(entry.get('outputs'), ensure_ascii=False)[:500]}\n"
                "  检查工作流最后是不是接了 SaveImage / VHS_VideoCombine 之类的输出节点。"
            )
        saved: list[Path] = []
        for index, item in enumerate(items):
            suffix = Path(item["filename"]).suffix or (".mp4" if want == "video" else ".png")
            target = out_dir / f"{prefix}_{index:02d}{suffix}"
            saved.append(self.download(item, target))
        self.log(f"    取回 {len(saved)} 个产物")
        return saved
