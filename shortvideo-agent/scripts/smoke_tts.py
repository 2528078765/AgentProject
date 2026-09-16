"""语音合成体检：逐个 provider 试一遍，并**实测音量**。

    python scripts/smoke_tts.py

光看"合成成功"没有意义 —— 静音轨也能算成功。这个脚本会对每个 provider 实际
合成一句话，然后用 ffmpeg 的 volumedetect 量出电平，告诉你到底是"有声音"
还是"安静的假成功"。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autovid import media as M              # noqa: E402
from autovid import providers as P          # noqa: E402
from autovid.config import Config           # noqa: E402

TEST_TEXT = "大家好，这是一段语音合成测试，用来确认到底有没有声音。"
WORK = ROOT / ".tmp" / "tts_probe"

SILENT_DB = -80.0     # 低于这个电平基本等同于静音


def measure(path: Path) -> tuple[float | None, float | None, float | None]:
    """返回 (mean_volume_dB, max_volume_dB, duration_s)。"""
    proc = subprocess.run(
        [M.ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(path),
         "-af", "volumedetect", "-f", "null", "NUL"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    text = (proc.stderr or "")

    def _grab(key: str) -> float | None:
        match = re.search(rf"{key}:\s*(-?[\d.]+) dB", text)
        return float(match.group(1)) if match else None

    duration = None
    try:
        duration = round(M.probe_duration(path), 2)
    except Exception:  # noqa: BLE001
        pass
    return _grab("mean_volume"), _grab("max_volume"), duration


def probe(config: Config, provider: str) -> dict:
    """用真实代码路径跑单个 provider（关掉回退链，避免被 silent 兜住）。"""
    probe_config = config.with_overrides({
        "steps.voice.provider": provider,
        "steps.voice.fallback": [],
        "steps.voice.strict": True,
    })
    out_dir = WORK / provider
    out_dir.mkdir(parents=True, exist_ok=True)
    segments = [{"id": "s01", "index": 0, "headline": "测试", "text": TEST_TEXT}]
    try:
        result = P.tts_synthesize(probe_config, segments, out_dir, log=lambda _m: None)
    except Exception as exc:  # noqa: BLE001
        return {"provider": provider, "ok": False, "error": str(exc).splitlines()[0][:120]}

    part = result.parts[0]
    wav = out_dir / "probe.wav"
    try:
        if part.suffix.lower() != ".wav":
            M.run_ffmpeg(["-i", str(part), "-ac", "1", "-ar", "24000",
                          "-c:a", "pcm_s16le", str(wav)], desc="转 wav")
        else:
            wav = part
        mean_db, max_db, duration = measure(wav)
    except Exception as exc:  # noqa: BLE001
        return {"provider": provider, "ok": False, "error": f"音量检测失败: {exc}"}

    has_sound = max_db is not None and max_db > SILENT_DB
    return {
        "provider": result.provider, "ok": True, "note": result.note,
        "bytes": part.stat().st_size, "duration": duration,
        "mean_db": mean_db, "max_db": max_db, "has_sound": has_sound,
        "file": str(part),
    }


def main() -> int:
    config = Config.load(root=ROOT)
    WORK.mkdir(parents=True, exist_ok=True)
    print("语音合成体检")
    print("=" * 72)
    print(f"  测试文本: {TEST_TEXT}\n")

    rows = []
    for provider in ("edge_native", "sapi", "edge", "http_json", "silent"):
        print(f"  → 测试 {provider} ...", flush=True)
        rows.append(probe(config, provider))

    print(f"\n{'provider':<14}{'结果':<8}{'时长':>8}{'峰值dB':>10}{'有声音':>8}  说明")
    print("-" * 72)
    real_audio: list[str] = []
    for row in rows:
        if not row["ok"]:
            print(f"{row['provider']:<14}{'失败':<8}{'-':>8}{'-':>10}{'否':>8}  {row['error']}")
            continue
        mark = "是" if row["has_sound"] else "否"
        if row["has_sound"]:
            real_audio.append(row["provider"])
        duration = f"{row['duration']}s" if row["duration"] else "-"
        max_db = f"{row['max_db']:.1f}" if row["max_db"] is not None else "-"
        print(f"{row['provider']:<14}{'成功':<8}{duration:>8}{max_db:>10}{mark:>8}  {row['note'][:40]}")

    print("\n" + "=" * 72)
    if real_audio:
        print(f"  [通过] 这些 provider 能产出真实声音：{', '.join(real_audio)}")
        print(f"\n  建议把 config/pipeline.json 里的 steps.voice.provider 设为 "
              f"\"{real_audio[0]}\"，")
        print('  并把 fallback 设为 ["' + '", "'.join(real_audio[1:] + ['silent']) + '"]')
        print(f"\n  试听文件：{WORK}")
        return 0
    print("  [失败] 没有任何 provider 能产出真实声音 —— 成片会是静音的。")
    print("    修复方向：")
    print("      1. 确认能连外网（edge_native 需要访问 speech.platform.bing.com）")
    print("      2. 或接入自建 GPT-SoVITS / CosyVoice 服务并配置 providers.http_tts.url")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
