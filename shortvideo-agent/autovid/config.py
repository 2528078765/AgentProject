"""配置加载。

优先级（后者覆盖前者）：
    内置 DEFAULTS  ->  config/pipeline.json  ->  config/secrets.json  ->  环境变量

密钥永远不要写进 pipeline.json（那个文件会进 git）。写 secrets.json 或环境变量。
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AutoVidError

CONFIG_PATH = Path("config/pipeline.json")
SECRETS_PATH = Path("config/secrets.json")


# --------------------------------------------------------------------------- #
# 内置默认值：即使 config/pipeline.json 不存在，流水线也能按这套默认值跑起来
# --------------------------------------------------------------------------- #
DEFAULTS: dict[str, Any] = {
    "project": {
        "language": "zh-CN",
        "out_dir": "runs",
        "keep_intermediate": True,
        # 音色与形象资产库（一次采集、长期复用）
        "assets_dir": "assets",
        "voice_id": "",     # 指向 assets/voices/<id>；留空则用 provider 的默认音色
        "avatar_id": "",    # 指向 assets/avatars/<id>；留空则只用背景图
        # True = 没选音色或形象就直接拒绝生成
        # （数字人口播视频的前提就是这两样，缺了跑出来不是你要的东西）
        "require_assets": True,
    },
    "platform": {
        # 抖音竖屏规格
        "name": "douyin",
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "video_bitrate": "8M",
        "audio_bitrate": "192k",
        "audio_sample_rate": 44100,
        "min_duration_s": 15,
        "max_duration_s": 180,
    },
    "steps": {
        "topic": {
            "enabled": True,
            "provider": "offline",
        },
        "script": {
            "enabled": True,
            "provider": "offline",
            # 目标口播字数：中文口播约 4.5 字/秒，260 字 ≈ 58 秒
            "target_words": 260,
            "segments": 5,
        },
        "voice": {
            "enabled": True,
            # edge_native = 零依赖的 Edge 在线 TTS（自实现 WebSocket），免费无需 Key，
            # 是「能真正出声」里门槛最低的一条路。需要外网。
            "provider": "edge_native",
            # 回退链：前一个失败自动往下试，永远不会因为 TTS 卡死整条链路
            "fallback": ["sapi", "silent"],
            "strict": False,
            "voice": "zh-CN-XiaoxiaoNeural",
            # sapi 用的参数（切到 sapi 时才生效）
            "sapi_voice": "Microsoft Huihui Desktop",
            "rate": 0,          # -10 .. 10（sapi）
            "volume": 100,      # 0 .. 100（sapi）
            "gap_ms": 220,      # 兜底停顿（未分级时用）
            "sample_rate": 24000,
            "chars_per_second": 4.6,   # 仅 silent 占位用

            # ── 断句与气口 ──
            # 一口气说不完 40 个字，所以按标点切成 12~20 字的小句再合成；
            # 停顿按标点分级，听起来才像人说话而不是机器均匀地念。
            "breath_max_chars": 20,
            "pause_comma_ms": 180,      # 逗号
            "pause_clause_ms": 300,     # 分号 / 冒号
            "pause_sentence_ms": 430,   # 句号 / 问号 / 叹号
            "pause_none_ms": 120,       # 硬切出来的碎句
            "pause_segment_ms": 650,    # 段落之间
            "pause_jitter_ms": 40,      # 抖动，避免机械感（固定种子，时间轴可复现）
            "pause_seed": "autovid",
        },
        "visuals": {
            # 已废弃：画面与背景来自「本次场景照片」，不再生成背景图。
            # 保留这一节只是为了让老的 pipeline 路径还能跑。
            "enabled": False,
            "provider": "ffmpeg_gradient",
            "per_segment": True,
        },
        "avatar": {
            "enabled": True,
            # 画面 = 本次场景照片（带人物），语音驱动它 -> 会动的口播片段
            "provider": "still",
            "zoom": 0.12,
            # 是否把「形象照」再叠到场景照片上。
            # 默认 false —— 场景照片里已经有人了，再叠一次画面里会出现两个人。
            # 只有当场景照片里没人（纯环境照）时才需要打开。
            "use_identity_overlay": False,
            "person_width_ratio": 0.62,
            "person_bottom_ratio": 0.28,
        },
        "subtitles": {
            "enabled": True,
            "provider": "ass",
            "font": "Microsoft YaHei",
            "font_size": 76,
            "margin_v": 360,
            "primary": "&H00FFFFFF",     # ASS 是 &HAABBGGRR
            "outline_colour": "&H00202020",
            "back_colour": "&H80000000",
            "outline": 5,
            "shadow": 2,
            "max_chars_per_line": 13,
            "bold": True,
        },
        "compose": {
            "enabled": True,
            "provider": "ffmpeg",
        },
        "metadata": {
            "enabled": True,
            "provider": "offline",
            "title_count": 3,
            "tag_count": 6,
        },
        "publish": {
            "enabled": True,
            # package = 只生成发布包（成片+封面+标题+话题+清单），由人手动上传
            "provider": "package",
            "platform": "douyin",
            # True = 把成片和封面复制进发布包目录，形成一个自包含的上传目录
            "copy_media": True,
        },
    },
    "providers": {
        "llm": {
            # 任何 OpenAI 兼容端点：DeepSeek / 通义 / 智谱 / 本地 vLLM
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "api_key_env": "AUTOVID_LLM_API_KEY",
            "timeout_s": 120,
            "temperature": 0.8,
        },
        "edge_tts": {
            "voice": "zh-CN-XiaoxiaoNeural",
            "rate": "+0%",
            "volume": "+0%",
        },
        "http_tts": {
            # GPT-SoVITS / CosyVoice 等自建推理服务的通用约定
            "url": "http://127.0.0.1:9880/tts",
            "ref_audio": "",
            "prompt_text": "",
            "timeout_s": 300,
        },
        "cloud_tts": {
            # ── 厂商无关的云端 TTS / 音色克隆适配器 ──
            # 请求体和响应字段全部用模板描述，换厂商只改这里，不用改代码。
            # 用 scripts/probe_cloud.py 对着真实接口核对字段，它会打印原始响应。
            #
            # 模板变量：{{api_key}} {{text}} {{voice_id}} {{model}} {{voice_name}}
            # 响应字段路径用点号表示，支持数组下标，如 "data.audios.0.url"
            "url": "",
            "method": "POST",
            "headers": {
                "Authorization": "Bearer {{api_key}}",
                "Content-Type": "application/json",
            },
            "body": {
                "model": "{{model}}",
                "text": "{{text}}",
                "voice": "{{voice_id}}",
                "response_format": "wav",
            },
            "model": "",
            "voice_id": "",          # 不绑定资产时直接用的固定音色
            # 音频在响应里的位置；留空 + audio_encoding=raw 表示响应体本身就是音频字节
            "audio_path": "",
            # hex | base64 | url | raw
            "audio_encoding": "base64",
            "api_key_env": "AUTOVID_CLOUD_TTS_KEY",
            "timeout_s": 300,
            # 可选：先注册音色（克隆）。注册一次后会缓存进资产，之后不再重复注册。
            "clone": {
                "enabled": False,
                "url": "",
                "method": "POST",
                "headers": {
                    "Authorization": "Bearer {{api_key}}",
                    "Content-Type": "application/json",
                },
                # {{audio_base64}} = 参考音频的 base64；{{audio_format}} = wav
                "body": {
                    "audio": "{{audio_base64}}",
                    "format": "{{audio_format}}",
                    "name": "{{voice_name}}",
                },
                "voice_id_path": "voice_id",
            },
        },
        "comfy": {
            # ComfyUI 作为「出图」和「数字人」的统一生成后端。
            # AMD 显卡请用 comfyui-rocm（AMD 官方 ROCm + PyTorch，支持 RDNA1~RDNA4，
            # 自带 Triton / Flash Attention），ZLUDA 是另一条备选路线。
            "url": "http://127.0.0.1:8188",
            "workflow": "config/workflows/txt2img.json",
            # 数字人工作流（ComfyUI 里「导出 (API)」得到的 JSON）
            # 约定占位符：{{IMAGE}} {{AUDIO}} {{WIDTH}} {{HEIGHT}} {{FPS}} {{DURATION}} {{SEED}}
            "avatar_workflow": "",
            "timeout_s": 600,
            "avatar_timeout_s": 1800,   # 数字人比出图慢得多
            "poll_interval_s": 2,
            # ComfyUI 的上传接口。音频用 /upload/image 是一些环境的权宜之计，
            # 如果你的音频节点有自己的上传接口，改这里。
            "upload": {
                "image": {"endpoint": "/upload/image", "field": "image", "subfolder": "autovid"},
                "audio": {"endpoint": "/upload/image", "field": "image", "subfolder": "autovid"},
            },
        },
        "openai_images": {
            "base_url": "https://api.siliconflow.cn/v1",
            "model": "black-forest-labs/FLUX.1-schnell",
            "api_key_env": "AUTOVID_IMAGE_API_KEY",
            "size": "768x1344",
            "timeout_s": 300,
        },
        "avatar_http": {
            # 通用「提交任务 + 轮询结果」型数字人服务
            "submit_url": "",
            "query_url": "",
            "api_key_env": "AUTOVID_AVATAR_API_KEY",
            "timeout_s": 1800,
            "poll_interval_s": 10,
        },
        # 本地 Qwen3-TTS：真正的音色克隆（Talker/Predictor 走 GGUF-Vulkan，
        # Decoder 走 ONNX）。先跑 python scripts/deploy_qwen3tts.py
        "local_qwen_tts": {
            "model_dir": "vendor/qwen3-tts-export/model-base-small",
            # Decoder 的 ONNX provider。DML 在部分显卡驱动上会因动态 Reshape
            # 算子报 80070057（见 vendor/.../Experience/DML_Reshape_Fix_Guide.md），
            # 所以默认用 CPU 保证出得来；驱动靠谱时可自行改 DML 提速。
            "onnx_provider": "CPU",
            "llm_use_gpu": True,      # llama.cpp Vulkan
            "language": "chinese",
            "zero_shot": True,        # 只用说话人嵌入，不需要参考文本
            "temperature": 0.8,
            "sub_temperature": 0.8,
            "max_steps": 400,
        },
        # 本地 Wav2Lip：让照片里的嘴跟着声音动。先跑
        # python scripts/deploy_wav2lip.py 下模型（436MB + 90MB）
        "local_wav2lip": {
            "batch_size": 32,   # CPU 上 32 挺稳；显卡好可以调大
            "fps": 25,
            # 嘴部锐化强度（0 = 关）。Wav2Lip 只在 96×96 上生成嘴部，放大后偏糊，
            # 非锐化掩模把边缘提回来，且**不会改变长相**。
            # 为什么不用 CodeFormer 那类人脸修复：实测它会把你换成另一个人
            # （眉毛变粗直、眼型改变、皮肤塑料感），数字人最看重「还是本人」。
            "sharpen": 0.6,
            "sharpen_sigma": 3.0,
        },
        "minimax_h3": {
            # MiniMax H3（海螺 3.0）云 API；本地权重需要 CUDA 大显存，本机跑不动
            "base_url": "https://api.minimaxi.com/v1",
            "model": "MiniMax-Hailuo-H3",
            "api_key_env": "AUTOVID_MINIMAX_API_KEY",
            "timeout_s": 1800,
            "poll_interval_s": 10,
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并：override 覆盖 base，dict 逐层合并，其他类型直接替换。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AutoVidError(f"[config] {path} 不是合法 JSON: {exc}") from exc


@dataclass
class Config:
    raw: dict[str, Any]
    root: Path
    secrets: dict[str, Any]

    # ---------------------------------------------------------------- 加载
    @classmethod
    def load(
        cls,
        root: Path | str = ".",
        config_path: Path | str | None = None,
        secrets_path: Path | str | None = None,
    ) -> "Config":
        root = Path(root).resolve()
        cfg_file = Path(config_path) if config_path else root / CONFIG_PATH
        sec_file = Path(secrets_path) if secrets_path else root / SECRETS_PATH

        raw = _deep_merge(DEFAULTS, _read_json(cfg_file))
        secrets = _read_json(sec_file)
        return cls(raw=raw, root=root, secrets=secrets)

    # ---------------------------------------------------------------- 读取
    def get(self, dotted: str, default: Any = None) -> Any:
        """按 'a.b.c' 取值。"""
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name, {})
        return value if isinstance(value, dict) else {}

    def step_cfg(self, step: str) -> dict[str, Any]:
        cfg = self.get(f"steps.{step}", {})
        return cfg if isinstance(cfg, dict) else {}

    def provider_of(self, step: str) -> str:
        return str(self.step_cfg(step).get("provider", "offline"))

    def provider_cfg(self, provider: str) -> dict[str, Any]:
        cfg = self.get(f"providers.{provider}", {})
        return cfg if isinstance(cfg, dict) else {}

    def step_enabled(self, step: str) -> bool:
        return bool(self.step_cfg(step).get("enabled", True))

    # ---------------------------------------------------------------- 密钥
    def secret(self, key: str, default: str | None = None) -> str | None:
        """先查环境变量，再查 secrets.json。"""
        env_name = key.upper()
        if env_name in os.environ and os.environ[env_name].strip():
            return os.environ[env_name].strip()
        if key in self.secrets and str(self.secrets[key]).strip():
            return str(self.secrets[key]).strip()
        lowered = key.lower()
        if lowered in self.secrets and str(self.secrets[lowered]).strip():
            return str(self.secrets[lowered]).strip()
        return default

    def secret_for(self, provider: str, default_env: str) -> str | None:
        """按 provider 配置里的 *_env 字段找密钥。"""
        env_key = str(self.provider_cfg(provider).get("api_key_env", default_env))
        return self.secret(env_key)

    # ---------------------------------------------------------------- 其他
    @property
    def platform(self) -> dict[str, Any]:
        return self.section("platform")

    def path(self, relative: str | Path) -> Path:
        """把配置里的相对路径解析到项目根目录。"""
        p = Path(relative)
        return p if p.is_absolute() else (self.root / p)

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.raw)

    def with_overrides(self, overrides: dict[str, Any]) -> "Config":
        """返回一份带覆盖项的新配置（Web 端按次调整 provider 用）。

        空值（None / "" / []）会被忽略，这样前端表单里没填的字段不会把默认值抹掉。
        覆盖后的配置会随 manifest 一起落盘，因此也参与 input_hash —— 换了 provider
        会被正确识别为「输入已变」。
        """
        raw = copy.deepcopy(self.raw)
        for dotted, value in (overrides or {}).items():
            if value is None or value == "" or value == []:
                continue
            parts = dotted.split(".")
            node: Any = raw
            for part in parts[:-1]:
                child = node.get(part)
                if not isinstance(child, dict):
                    child = {}
                    node[part] = child
                node = child
            node[parts[-1]] = value
        return Config(raw=raw, root=self.root, secrets=self.secrets)

    def fingerprint(self, dotted_prefix: str) -> str:
        """取某个配置子树的稳定指纹，用于参与步骤的 input_hash。"""
        return json.dumps(self.get(dotted_prefix, {}), sort_keys=True, ensure_ascii=False)
