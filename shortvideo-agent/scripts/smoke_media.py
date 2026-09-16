"""媒体层冒烟测试：不经过流水线，单独验证 FFmpeg / SAPI / libass / zoompan 是否可用。

用法：
    python scripts/smoke_media.py

产出在 .tmp/smoke/ 下。最后会打印一帧截图路径，可以直接打开看中文字幕有没有正常渲染。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autovid import media as M  # noqa: E402

WORK = ROOT / ".tmp" / "smoke"
TEXTS = [
    "大家好，这是一条完全自动生成的口播视频。",
    "第二句用来测试中文字幕换行、描边和字体是否正常。",
]
W, H, FPS = 1080, 1920, 30


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    print(f"工作目录: {WORK}\n")

    # 1) 渐变背景（纯 ffmpeg，无模型）
    bg = M.make_gradient(WORK / "bg.png", W, H, ["0x0f2027", "0x203a43", "0x2c5364"], seed=7)
    print(f"[1/6] 背景图  OK  {bg.stat().st_size / 1024:.0f} KB")

    # 2) 中文语音合成（Windows SAPI，离线）
    voices = M.list_sapi_voices()
    zh = [v for v in voices if v["culture"].lower().startswith("zh")]
    print(f"[2/6] SAPI 语音: {[v['name'] for v in voices]}")
    voice = zh[0]["name"] if zh else (voices[0]["name"] if voices else "")
    # 文件名必须和降级实现（_tts_silent 用 f"seg_{i:02d}.wav"）保持一致，
    # 否则降级后 parts 仍指向 SAPI 留下的 0 字节空文件。
    parts = [WORK / f"seg_{i:02d}.wav" for i in range(len(TEXTS))]
    try:
        M.sapi_synthesize(list(zip(parts, TEXTS)), voice=voice, sample_rate=24000, log=print)
    except Exception as exc:  # noqa: BLE001
        # SAPI 在受限/非交互会话下会被系统拒绝。这里降级成静音，
        # 好让后面的拼接/字幕/合成链路仍然能被验证。
        print(f"      SAPI 不可用（{str(exc).splitlines()[0][:80]}），降级为静音轨道")
        from autovid.config import Config
        from autovid.providers import _tts_silent
        _tts_silent(Config.load(root=ROOT), [{"text": t} for t in TEXTS], WORK, print)

    # 3) 拼 PCM 并得到精确时间轴
    timeline = M.concat_wav_pcm(parts, WORK / "voice.wav", gap_ms=220)
    for seg, text in zip(timeline, TEXTS):
        print(f"        {seg['start']:6.2f} -> {seg['end']:6.2f}s  {text[:16]}...")
    print(f"[3/6] 音频拼接 OK  总时长 {M.wav_duration(WORK / 'voice.wav'):.2f}s")

    # 4) ASS 字幕
    styles = [
        M.make_style("Main", "Microsoft YaHei", 76, margin_v=360),
        M.make_style("Title", "Microsoft YaHei", 96, primary="&H0000E5FF", alignment=8, margin_v=220),
    ]
    events = [
        {"style": "Title", "start": 0.0, "end": timeline[-1]["end"], "text": "测试标题", "max_chars": 10}
    ]
    events += [
        {"style": "Main", "start": s["start"], "end": s["end"], "text": t, "max_chars": 13}
        for s, t in zip(timeline, TEXTS)
    ]
    M.write_ass(WORK / "sub.ass", M.build_ass(styles, events, W, H))
    print("[4/6] ASS 字幕 OK")

    # 5) 静图 -> 推镜片段 -> 拼接 -> 配音
    clips = []
    for i, seg in enumerate(timeline):
        clip = WORK / f"clip_{i}.mp4"
        M.kenburns_clip(bg, clip, seg["clip_duration"], W, H, fps=FPS, zoom=0.12, cwd=WORK)
        clips.append(clip)
    print(f"[5/6] 推镜片段 OK  {len(clips)} 个")

    M.concat_clips(clips, WORK / "vtrack.mp4", cwd=WORK)
    M.mux_audio(WORK / "vtrack.mp4", WORK / "voice.wav", WORK / "av.mp4", cwd=WORK)

    # 6) 烧字幕
    M.burn_subtitles(WORK / "av.mp4", WORK / "sub.ass", WORK / "final.mp4", cwd=WORK)
    frame = M.extract_frame(WORK / "final.mp4", WORK / "frame.png", at_s=1.0)
    print(f"[6/6] 成片 OK  时长 {M.probe_duration(WORK / 'final.mp4'):.2f}s  "
          f"大小 {(WORK / 'final.mp4').stat().st_size / 1024:.0f} KB")

    print(f"\n请打开这张截图确认中文字幕渲染正常:\n  {frame}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
