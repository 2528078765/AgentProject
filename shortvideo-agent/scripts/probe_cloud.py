"""云端 TTS / 音色克隆的「字段探测」工具。

拿到 API Key 之后，厂商文档里的字段名十有八九和实际响应有出入。
这个脚本的作用是：**打一次真实请求，把原始响应原样打出来**，
你照着填 config/pipeline.json 里的模板即可，不需要改任何代码。

    # 看看有哪些厂商预设
    python scripts/probe_cloud.py --list

    # 用你的音色资产测一遍「克隆 + 合成」全流程
    python scripts/probe_cloud.py --preset minimax --key sk-xxx --asset voice-your-id

    # 只测合成（用厂商自带音色）
    python scripts/probe_cloud.py --preset openai --key sk-xxx --voice alloy

    # 探测成功后，把模板写进配置
    python scripts/probe_cloud.py --preset minimax --key sk-xxx --asset voice-xxx --write-config
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autovid import providers as P          # noqa: E402
from autovid.assets import AssetStore       # noqa: E402
from autovid.config import Config           # noqa: E402

WORK = ROOT / ".tmp" / "cloud_probe_out"

# --------------------------------------------------------------------------- #
# 厂商预设
#
# ⚠️ 这些是**起点模板**，字段名按各家公开文档整理，但我无法在本地验证。
#    所以这个脚本存在的意义就是：跑一次，看真实响应，然后照着改。
# --------------------------------------------------------------------------- #
PRESETS: dict[str, dict] = {
    "minimax": {
        "label": "MiniMax 海螺语音（支持音色克隆，两步：上传文件 -> 建音色）",
        "hint": "部分账号需要在 URL 上加 ?GroupId=xxx；语音模型建议 speech-02-hd",
        "config": {
            "url": "https://api.minimaxi.com/v1/t2a_v2",
            "model": "speech-02-hd",
            "audio_path": "data.audio",
            "audio_encoding": "hex",
            "body": {
                "model": "{{model}}",
                "text": "{{text}}",
                "voice_setting": {"voice_id": "{{voice_id}}", "speed": 1.0,
                                  "vol": 1.0, "pitch": 0},
                "audio_setting": {"sample_rate": 24000, "bitrate": 128000,
                                  "format": "wav"},
            },
            "clone": {
                "enabled": True,
                "upload": {
                    "url": "https://api.minimaxi.com/v1/files/upload",
                    "body": {"purpose": "voice_clone", "file": "{{audio_base64}}"},
                    "id_path": "file.file_id",
                },
                "url": "https://api.minimaxi.com/v1/voice_clone",
                "body": {"file_id": "{{upload_id}}", "voice_id": "{{new_voice_id}}"},
                "voice_id_path": "voice_id",
            },
        },
    },
    "siliconflow": {
        "label": "硅基流动 SiliconFlow（CosyVoice2，OpenAI 兼容）",
        "hint": "音色克隆要先用表单上传音频换 voice URI，见 upload 模板",
        "config": {
            "url": "https://api.siliconflow.cn/v1/audio/speech",
            "model": "FunAudioLLM/CosyVoice2-0.5B",
            "audio_path": "",
            "audio_encoding": "raw",
            "body": {"model": "{{model}}", "input": "{{text}}",
                     "voice": "{{voice_id}}", "response_format": "wav"},
            "clone": {
                "enabled": True,
                "upload": {
                    "url": "https://api.siliconflow.cn/v1/uploads/audio/voice",
                    "mode": "multipart",
                    "file_field": "file",
                    "filename": "ref.wav",
                    "fields": {"model": "{{model}}",
                               "customName": "{{voice_name}}"},
                    "id_path": "uri",
                },
                "url": "https://api.siliconflow.cn/v1/uploads/audio/voice",
                "body": {},
                "voice_id_path": "uri",
            },
        },
    },
    "openai": {
        "label": "OpenAI 兼容 /v1/audio/speech（不支持自定义克隆，只能选内置音色）",
        "hint": "把 url 换成任何 OpenAI 兼容端点即可（Azure、通义、本地 vLLM 等）",
        "config": {
            "url": "https://api.openai.com/v1/audio/speech",
            "model": "gpt-4o-mini-tts",
            "audio_path": "",
            "audio_encoding": "raw",
            "body": {"model": "{{model}}", "input": "{{text}}",
                     "voice": "{{voice_id}}", "response_format": "wav"},
            "clone": {"enabled": False},
        },
    },
    "fish": {
        "label": "Fish Audio（支持零样本克隆）",
        "hint": "克隆走 /model 创建，字段以官方文档为准",
        "config": {
            "url": "https://api.fish.audio/v1/tts",
            "model": "s1",
            "audio_path": "",
            "audio_encoding": "raw",
            "body": {"text": "{{text}}", "reference_id": "{{voice_id}}",
                     "format": "wav"},
            "clone": {"enabled": False},
        },
    },
}

MAX_STR = 96


def shrink(value, depth: int = 0):
    """把响应里的音频大字符串截断，否则一屏全是 hex。"""
    if isinstance(value, dict):
        return {k: shrink(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [shrink(v, depth + 1) for v in value[:4]]
    if isinstance(value, str) and len(value) > MAX_STR:
        head = value[:MAX_STR]
        looks_audio = bool(re.fullmatch(r"[0-9a-fA-F]+", head)) or "base64" in head
        kind = "疑似音频(hex)" if looks_audio else "长字符串"
        return f"<{kind} 共 {len(value)} 字符> {head[:48]}…"
    return value


def build_config(args) -> Config:
    preset = PRESETS.get(args.preset)
    config = Config.load(root=ROOT)
    overrides: dict = {
        "steps.voice.provider": "cloud_tts",
        "steps.voice.fallback": [],
        "steps.voice.strict": True,
        "providers.cloud_tts.api_key_env": "AUTOVID_PROBE_KEY",
        "providers.cloud_tts.model": args.model or "",
    }
    if preset:
        template = json.loads(json.dumps(preset["config"]))  # 深拷贝
        if args.model:
            template["model"] = args.model
        # 用户显式传的 url 优先
        if args.url:
            template["url"] = args.url
        for key, value in template.items():
            overrides[f"providers.cloud_tts.{key}"] = value
    else:
        overrides["providers.cloud_tts.url"] = args.url or ""
        overrides["providers.cloud_tts.audio_path"] = args.audio_path or ""
        overrides["providers.cloud_tts.audio_encoding"] = args.encoding or "raw"
    if args.voice:
        overrides["providers.cloud_tts.voice_id"] = args.voice
    return config.with_overrides(overrides)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="探测云端 TTS / 音色克隆接口，打印原始响应以便对齐字段")
    parser.add_argument("--list", action="store_true", help="列出厂商预设")
    parser.add_argument("--preset", default="minimax", choices=sorted(PRESETS) + ["custom"])
    parser.add_argument("--key", help="API Key（会写进环境变量，不落盘）")
    parser.add_argument("--asset", help="用哪个音色资产做克隆测试（assets/voices/<id>）")
    parser.add_argument("--voice", help="不使用克隆时，厂商自带的音色名")
    parser.add_argument("--text", default="这是一段云端语音合成的探测文本，用来确认接口能不能正常出声。")
    parser.add_argument("--url", help="自定义合成接口地址（preset=custom 时用）")
    parser.add_argument("--audio-path", help="响应里音频字段的路径（custom）")
    parser.add_argument("--encoding", choices=["hex", "base64", "url", "raw"], help="音频编码（custom）")
    parser.add_argument("--model", help="覆盖模型名")
    parser.add_argument("--write-config", action="store_true",
                        help="探测成功后把模板写进 config/pipeline.json")
    args = parser.parse_args()

    if args.list:
        print("可用的厂商预设：\n")
        for name, preset in PRESETS.items():
            print(f"  {name:<12} {preset['label']}")
            print(f"  {'':<12} 提示：{preset['hint']}\n")
        print("  custom       自己填 --url / --audio-path / --encoding\n")
        print("用法示例：")
        print("  python scripts\\probe_cloud.py --preset minimax --key sk-xxx --asset voice-your-id")
        return 0

    if not args.key:
        print("[失败] 必须提供 --key（或先看 --list 挑一家厂商）", file=sys.stderr)
        return 2
    import os
    os.environ["AUTOVID_PROBE_KEY"] = args.key

    config = build_config(args)
    cfg = config.provider_cfg("cloud_tts")
    preset = PRESETS.get(args.preset)

    print("云端接口探测")
    print("=" * 70)
    print(f"  厂商预设 : {args.preset}{'  ' + preset['label'] if preset else ''}")
    print(f"  合成地址 : {cfg.get('url') or '(未配置)'}")
    print(f"  模型     : {cfg.get('model') or '(未指定)'}")
    print(f"  音频位置 : audio_path={cfg.get('audio_path')!r} encoding={cfg.get('audio_encoding')}")
    print(f"  克隆     : {'开启' if (cfg.get('clone') or {}).get('enabled') else '关闭'}")
    if preset:
        print(f"  ⚠ 提示   : {preset['hint']}")
    print()

    voice_asset = None
    store = AssetStore(config)
    if args.asset:
        voice_asset = store.get_voice(args.asset)
        if voice_asset is None:
            print(f"[失败] 找不到音色资产 {args.asset}", file=sys.stderr)
            return 2
        print(f"  音色资产 : {voice_asset.name}（{voice_asset.duration_s}s）")
        cache = (voice_asset.cloud_voice_ids or {}).get("cloud_tts")
        print(f"  已缓存ID : {cache or '(无，本次会走克隆注册)'}\n")
    elif not args.voice:
        print("[失败] 要么给 --asset 测克隆，要么给 --voice 用厂商自带音色", file=sys.stderr)
        return 2

    segments = [{"id": "s01", "index": 0, "headline": "探测", "text": args.text}]
    WORK.mkdir(parents=True, exist_ok=True)
    try:
        result = P.tts_synthesize(config, segments, WORK, log=print,
                                  voice_asset=voice_asset)
    except Exception as exc:  # noqa: BLE001
        print(f"\n{'=' * 70}\n[失败] {exc}\n")
        print("怎么排查：")
        print("  1. 上面的报错里已经带了服务端原始响应，先看它的字段名")
        print("  2. 按实际字段改 --audio-path / --encoding，或直接改 config/pipeline.json")
        print("  3. 权限类错误（401/403）先确认 Key 和账号额度")
        return 1

    from autovid import media as M
    wav = result.parts[0]
    duration = M.wav_duration(wav)
    print(f"\n{'=' * 70}")
    print(f"  [通过] 合成成功")
    print(f"    provider  : {result.provider}")
    print(f"    音频文件  : {wav}")
    print(f"    时长      : {duration:.2f}s")
    print(f"    是否克隆  : {'是 —— 用的是你的音色' if result.cloned else '否 —— 用的是厂商音色'}")
    print(f"    说明      : {result.note}")

    # 实测电平：静音轨也算「合成成功」，必须量一下
    import re as _re
    import subprocess
    probe = subprocess.run(
        [M.ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(wav),
         "-af", "volumedetect", "-f", "null", "NUL"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    match = _re.search(r"max_volume:\s*(-?[\d.]+) dB", probe.stderr or "")
    if match:
        peak = float(match.group(1))
        ok = peak > -80
        print(f"    峰值电平  : {peak:.1f} dB  {'（有声音）' if ok else '（⚠ 几乎是静音！）'}")

    if args.write_config and preset:
        target = ROOT / "config" / "pipeline.json"
        data = json.loads(target.read_text(encoding="utf-8"))
        data.setdefault("providers", {})["cloud_tts"] = {
            "url": cfg.get("url", ""),
            "model": cfg.get("model", ""),
            "audio_path": cfg.get("audio_path", ""),
            "audio_encoding": cfg.get("audio_encoding", "base64"),
            "api_key_env": "AUTOVID_CLOUD_TTS_KEY",
            "timeout_s": cfg.get("timeout_s", 300),
            "headers": {"Authorization": "Bearer {{api_key}}",
                        "Content-Type": "application/json"},
            "body": cfg.get("body", {}),
            "voice_id": cfg.get("voice_id", ""),
            "clone": cfg.get("clone", {"enabled": False}),
        }
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  已把模板写入 {target}")
        print("  接着在 config/secrets.json 里放 Key：")
        print('      { "AUTOVID_CLOUD_TTS_KEY": "你的Key" }')
        print("  然后在页面上把「语音引擎」切到 cloud_tts 即可。")
    else:
        print("\n  确认无误后加 --write-config 把模板写进配置。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
