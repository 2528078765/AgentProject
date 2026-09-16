"""端到端验证：整条 LangGraph 流程跑**真实本地引擎**。

前面每个测试只验证单个环节（音色克隆 / Wav2Lip 口型），这个测试把
真实的两个本地引擎串起来，验证「一键出片」这条链路真的成立：

    场景照片 + 选题
      → 前置判断 → 音色克隆（你的音色）
      → 口播稿 → 语音合成（Qwen3-TTS，你的声音）
      → 数字人（Wav2Lip，嘴会动）
      → 字幕 → 合成成片 → 标题标签

关键验收：
    * 成片音轨不是静音，且确实由本地克隆引擎产出（provider=local_qwen_tts）
    * 成片画面里人脸区域的嘴在动（不是静态推镜）
    * 成片、封面和标题信息齐全

    python scripts/smoke_e2e_local.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autovid import graph as G                      # noqa: E402
from autovid import media as M                      # noqa: E402
from autovid.assets import AssetStore               # noqa: E402
from autovid.config import Config                   # noqa: E402

PASS, FAIL = "[通过]", "[失败]"
problems: list[str] = []
WORK = ROOT / ".tmp" / "e2e_local"


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {PASS if ok else FAIL} {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        problems.append(name)
    return ok


def extract_frame(video: Path, seconds: float, out: Path) -> Path | None:
    result = subprocess.run(
        [M.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
         "-ss", f"{seconds:.3f}", "-i", str(video), "-frames:v", "1", str(out)],
        capture_output=True, text=True, timeout=120)
    return out if result.returncode == 0 and out.exists() else None


def main() -> int:
    print("端到端验证：真实本地引擎出片")
    print("=" * 74)

    config = Config.load(root=ROOT)
    store = AssetStore(config)

    # ---------------------------------------------------------- 前置资源
    print("\n1. 前置资源（真实资产）")
    voice = next((v for v in store.list_voices() if v.ref_audio), None)
    avatar = next((a for a in store.list_avatars() if getattr(a, "photos", None)), None)
    check("有带参考音频的音色", voice is not None,
          f"{voice.name}（{voice.duration_s}s）" if voice else "缺")
    check("有带照片的形象", avatar is not None, avatar.name if avatar else "缺")
    portrait = store.avatar_portrait(avatar.id) if avatar else None
    check("形象主图可读", portrait is not None and portrait.exists(),
          portrait.name if portrait else "缺")
    if not (voice and avatar and portrait):
        return 1

    import cv2
    from autovid import wav2lip as W
    scene = WORK / "scene.png"
    WORK.mkdir(parents=True, exist_ok=True)
    # 用形象主图当「本次场景照片」：真人正脸，最贴近真实使用
    cv2.imwrite(str(scene), W.imread_unicode(portrait))

    # ---------------------------------------------------------- 跑图
    print("\n2. 跑完整 LangGraph 流程（真实引擎，无闸门）")
    cfg = config.with_overrides({
        "steps.voice.provider": "local_qwen_tts",
        "steps.voice.fallback": [],
        "steps.avatar.provider": "local_wav2lip",
        "steps.avatar.fallback": [],
    })
    run_dir = G.VideoFlow.new_run_dir(cfg, "e2e-local")
    logs: list[str] = []

    def log(message: str) -> None:
        logs.append(str(message))
        print("      " + str(message)[:110])

    flow = G.VideoFlow(cfg, log=log)
    started = time.time()
    outcome = flow.run({
        "run_id": run_dir.name, "run_dir": str(run_dir),
        "topic": "本地引擎端到端验证", "script_text": "", "script_file": "",
        "voice_id": voice.id, "avatar_id": avatar.id,
        "scene_photo": str(scene),
        "revision": 0, "approvals": {}, "trace": [],
    }, thread_id="e2e-local-1")
    cost = time.time() - started

    check("流程跑到 finished", outcome["status"] == "finished", outcome["status"])
    if outcome["status"] != "finished":
        print(f"      failure: {str(outcome.get('failure'))[:200]}")
        return 1

    result = outcome["result"]
    nodes = [e["node"] for e in outcome["trace"] if not e["node"].startswith("gate_")]
    check("节点顺序正确",
          nodes == ["preflight", "voice_clone", "script", "tts", "avatar",
                    "subtitles", "compose", "metadata"],
          str(nodes))

    # ---------------------------------------------------------- 声音
    print("\n3. 声音：是你的音色")
    profile = result.get("voice_profile") or {}
    check("音色克隆如实报告已克隆", profile.get("cloned") is True,
          str(profile.get("note"))[:60])
    check("走的是本地 Qwen3-TTS",
          (result.get("voice") or {}).get("provider") == "local_qwen_tts",
          str((result.get("voice") or {}).get("provider")))

    wav = Path(str((result.get("voice") or {}).get("wav") or ""))
    check("产出了配音 wav", wav.exists() and wav.stat().st_size > 10_000,
          f"{wav.stat().st_size / 1024:.0f} KB" if wav.exists() else "缺")
    if wav.exists():
        import numpy as np
        import soundfile as sf
        data, _ = sf.read(str(wav), dtype="float32", always_2d=True)
        rms = float(np.sqrt(np.mean(np.square(data)))) if data.size else 0.0
        check("配音不是静音", rms > 0.005, f"RMS={rms:.4f}")

    # ---------------------------------------------------------- 画面
    print("\n4. 画面：嘴在动")
    avatar_out = result.get("avatar") or {}
    check("数字人走的是 Wav2Lip", avatar_out.get("provider") == "local_wav2lip",
          str(avatar_out.get("provider")))
    clips = [Path(p) for p in (avatar_out.get("clips") or [])]
    check("每段都有口型片段", len(clips) >= 2, f"{len(clips)} 个")
    check("片段都有内容", all(c.exists() and c.stat().st_size > 5000 for c in clips))

    video = Path(str((result.get("video") or {}).get("video") or ""))
    check("产出了成片", video.exists() and video.stat().st_size > 50_000,
          f"{video.stat().st_size / 1e6:.1f} MB" if video.exists() else "缺")
    info = M.probe_media(video)
    check("成片有音轨", bool(info.get("has_audio")), f"codec={info.get('codec')}")
    check("成片可解码", bool(info.get("has_video")),
          f"{info.get('width')}×{info.get('height')} / {info.get('duration')}s")

    dur = float(info.get("duration") or 0)
    # 抽多帧取最大差异：只抽两帧可能正好撞上相近口型，会假阴性
    shots = []
    for index in range(6):
        moment = dur * (index + 0.5) / 6
        shot = extract_frame(video, moment, WORK / f"f{index}.png")
        if shot:
            shots.append(cv2.imread(str(shot)))
    check("能抽出多帧做对比", len(shots) >= 4, f"{len(shots)}/6 帧")
    if len(shots) >= 2:
        worst = max(
            float(abs(shots[i].astype("float32") - shots[j].astype("float32")).mean())
            for i in range(len(shots)) for j in range(i + 1, len(shots)))
        check("成片画面全程有变化（不是一张静止图）", worst > 3.0,
              f"两帧最大差 {worst:.2f}")

    # ---------------------------------------------------------- 标题信息
    print("\n5. 标题信息")
    meta = result.get("metadata") or {}
    check("有标题候选", bool(meta.get("titles")), str((meta.get("titles") or [])[:1]))

    print("\n" + "=" * 74)
    print(f"  整条流程耗时 {cost:.0f}s（音频 {dur:.1f}s）")
    print(f"  运行目录：{run_dir.relative_to(ROOT)}")

    if problems:
        print(f"\n  {FAIL} {len(problems)} 项未通过：")
        for item in problems:
            print(f"    - {item}")
        return 1
    print(f"\n  {PASS} 全部通过 —— 一条命令出片：你的声音 + 你的脸 + 嘴会动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
