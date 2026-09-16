"""Wav2Lip 本地数字人的冒烟测试。

验证「照片 + 音频 → 嘴真的在动」这条链路：

    1. 模型文件就位（wav2lip_gan.pth + s3fd.pth）
    2. 自实现的音频 mel 与 Wav2Lip 训练分布对得上（Slaney 刻度、形状、取值范围）
    3. 真人照片能检出人脸
    4. 端到端渲染出视频，且**嘴部区域逐帧在变**（不是一张静止图）
    5. 音频轨在、时长对得上；人脸框以外的像素没被动过

    python scripts/smoke_wav2lip.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autovid import media as M                      # noqa: E402
from autovid import providers as P                  # noqa: E402
from autovid import wav2lip as W                    # noqa: E402
from autovid.assets import AssetStore               # noqa: E402
from autovid.config import Config                   # noqa: E402

PASS, FAIL = "[通过]", "[失败]"
problems: list[str] = []
WORK = ROOT / ".tmp" / "wav2lip_probe"


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
    print("Wav2Lip 本地数字人冒烟测试")
    print("=" * 74)

    config = Config.load(root=ROOT)
    logs: list[str] = []

    def log(message: str) -> None:
        logs.append(str(message))
        print("      " + str(message))

    # ---------------------------------------------------------- 1. 模型
    print("\n1. 模型文件")
    missing = W.missing_files()
    check("Wav2Lip 模型齐全", not missing, f"缺 {missing}" if missing else "wav2lip_gan + s3fd")
    if missing:
        print("\n  先跑：python scripts/deploy_wav2lip.py")
        return 1
    check("生成器大小合理（>400MB）", W.CKPT_PATH.stat().st_size > 400e6,
          f"{W.CKPT_PATH.stat().st_size / 1e6:.0f} MB")
    check("人脸检测器大小合理（>80MB）", W.S3FD_PATH.stat().st_size > 80e6,
          f"{W.S3FD_PATH.stat().st_size / 1e6:.0f} MB")

    # ---------------------------------------------------------- 2. 音频管线
    print("\n2. 音频管线（自实现，绕开装不了的 librosa）")
    # Slaney 刻度的定义点：1000Hz 正好等于 15 mel
    check("mel 刻度是 Slaney（1000Hz = 15 mel）",
          abs(float(W._hz_to_mel(1000.0)) - 15.0) < 1e-9,
          f"{float(W._hz_to_mel(1000.0)):.6f}")
    check("mel 刻度低频线性（200Hz = 3 mel）",
          abs(float(W._hz_to_mel(200.0)) - 3.0) < 1e-9)
    check("mel 往返一致", abs(float(W._mel_to_hz(W._hz_to_mel(440.0))) - 440.0) < 1e-6)

    basis = W.mel_filterbank()
    check("mel 滤波器组形状 = (80, 401)",
          basis.shape == (80, 1 + W.N_FFT // 2), str(basis.shape))
    check("滤波器组非负", float(basis.min()) >= 0.0)

    # 用一段合成语音测试 mel
    import numpy as np
    import soundfile as sf
    WORK.mkdir(parents=True, exist_ok=True)
    tone = WORK / "tone.wav"
    t = np.linspace(0, 2.0, int(W.SAMPLE_RATE * 2.0), endpoint=False)
    tone_data = (0.3 * np.sin(2 * np.pi * 220 * t)
                 * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t))).astype(np.float32)
    sf.write(tone, tone_data, W.SAMPLE_RATE)

    wav = W.load_wav_16k(tone)
    check("读到 16kHz 单声道", wav.ndim == 1 and abs(len(wav) / W.SAMPLE_RATE - 2.0) < 0.05,
          f"{len(wav)} 样本 / {len(wav) / W.SAMPLE_RATE:.2f}s")
    mel = W.melspectrogram(wav)
    check("mel 形状 = (80, T)", mel.shape[0] == 80 and mel.shape[1] > 10, str(mel.shape))
    check("mel 无 NaN", not np.isnan(mel).any())
    check("mel 落在 [-4, 4]（对称归一化）",
          float(mel.min()) >= -4.001 and float(mel.max()) <= 4.001,
          f"[{mel.min():.2f}, {mel.max():.2f}]")
    check("mel 不是常数（有内容）", float(mel.std()) > 0.1, f"std={mel.std():.3f}")
    chunks = W.mel_chunks(mel, fps=25)
    check("mel 片段形状 = (80, 16)",
          len(chunks) > 0 and chunks[0].shape == (80, W.MEL_STEP_SIZE),
          f"{len(chunks)} 片")

    # ---------------------------------------------------------- 3. 人脸检测
    print("\n3. 人脸检测（用形象库里的真实照片）")
    store = AssetStore(config)
    avatar = next((a for a in store.list_avatars()
                   if getattr(a, "photos", None)), None)
    if avatar is None:
        check("资产库里有带照片的形象", False, "请先在「形象库」传照片")
        return 1
    portrait = store.avatar_portrait(avatar.id)
    check("拿到形象主图", portrait is not None and portrait.exists(),
          f"{avatar.name}（{portrait.name if portrait else '-'}）")

    runner = W.Wav2LipRunner.get(config)
    runner.ensure_loaded(log)
    check("模型加载完成", runner._model is not None and runner._detector is not None,
          f"{sum(p.numel() for p in runner._model.parameters()) / 1e6:.0f}M 参数")
    import cv2
    frame = W.imread_unicode(portrait)
    check("照片能读入", frame is not None,
          f"{frame.shape[1]}×{frame.shape[0]}" if frame is not None else "读不了")
    # 大图检测前会先缩小，这里走同一套换算，保证和渲染时一致
    det_img, upscale = W._for_detection(frame)
    det_box = runner._face_box(det_img, log)
    raw_box = [int(round(value * upscale)) for value in det_box]
    check("检测到人脸框（已换算回原图）",
          len(raw_box) == 4 and raw_box[2] > raw_box[0] and raw_box[3] > raw_box[1],
          f"{raw_box[2] - raw_box[0]}×{raw_box[3] - raw_box[1]} @ ({raw_box[0]},{raw_box[1]})")

    # ---------------------------------------------------------- 4. 端到端
    print("\n4. 端到端渲染（照片 + 真实语音 → 口型同步视频）")
    speech = WORK / "speech.wav"
    made = False
    try:
        voice_asset = next((v for v in store.list_voices() if v.ref_audio), None)
        if voice_asset is not None:
            cfg = config.with_overrides({
                "steps.voice.provider": "local_qwen_tts",
                "steps.voice.fallback": [],
            })
            result = P.tts_synthesize(
                cfg, [{"id": "s1", "text": "大家好，这是我的数字人形象。"}],
                WORK / "tts", log, voice_asset=voice_asset)
            shutil.copy2(result.parts[0], speech)
            made = True
            check("用你的音色生成了测试语音", True, f"{speech.stat().st_size / 1024:.0f} KB")
    except Exception as exc:  # noqa: BLE001
        log(f"本地音色合成不可用（{type(exc).__name__}），退回 edge_native")

    if not made:
        try:
            cfg = config.with_overrides({
                "steps.voice.provider": "edge_native",
                "steps.voice.fallback": [],
            })
            result = P.tts_synthesize(
                cfg, [{"id": "s1", "text": "大家好，这是我的数字人形象。"}],
                WORK / "tts2", log)
            shutil.copy2(result.parts[0], speech)
            made = True
            check("生成了测试语音", True, f"{speech.stat().st_size / 1024:.0f} KB")
        except Exception as exc:  # noqa: BLE001
            check("能生成测试语音", False, f"{type(exc).__name__}: {str(exc)[:70]}")

    if not made:
        return 1

    video = WORK / "lipsync.mp4"
    started = time.time()
    runner.render(portrait, speech, video, log, fps=25)
    cost = time.time() - started
    check("产出了口型同步视频", video.exists() and video.stat().st_size > 10_000,
          f"{video.stat().st_size / 1e6:.1f} MB / {cost:.1f}s")

    info = M.probe_media(video)
    check("视频可解码", bool(info.get("has_video")), str(info.get("error") or ""))
    check("含有音频轨", bool(info.get("has_audio")), f"codec={info.get('codec')}")
    dur = float(info.get("duration") or 0)
    check("时长与音频接近（3.4s 左右）", 1.0 < dur < 8.0, f"{dur:.2f}s")
    check("分辨率是平台尺寸",
          (info.get("width"), info.get("height")) == (1080, 1920),
          f"{info.get('width')}×{info.get('height')}")

    # ---------------------------------------------------------- 5. 嘴真的在动
    print("\n5. 关键验证：嘴真的在动（不是静止图）")
    # 只抽两帧可能正好撞上相近的口型（比如都是闭嘴），判定会假阴性。
    # 抽多帧取「任意两帧之间的最大差异」，只要全程嘴动过就一定能测出来。
    frames = []
    sample_count = 6
    for index in range(sample_count):
        moment = dur * (index + 0.5) / sample_count
        shot = extract_frame(video, moment, WORK / f"s{index}.png")
        if shot:
            frames.append(cv2.imread(str(shot)))
    check("能抽出多帧做对比", len(frames) >= 4, f"{len(frames)}/{sample_count} 帧")
    if len(frames) < 4:
        return 1

    img_a = frames[0]
    canvas_w, canvas_h = runner.last_size or (img_a.shape[1], img_a.shape[0])
    check("输出尺寸符合平台设定",
          (img_a.shape[1], img_a.shape[0]) == (canvas_w, canvas_h),
          f"{img_a.shape[1]}×{img_a.shape[0]}")

    box = runner.last_box or raw_box
    x1, y1, x2, y2 = box

    def mouth_of(image):
        face = image[y1:y2, x1:x2].astype(np.float32)
        return face[face.shape[0] // 2:, :]

    mouths = [mouth_of(f) for f in frames]
    pair_diffs = [float(np.abs(mouths[i] - mouths[j]).mean())
                  for i in range(len(mouths)) for j in range(i + 1, len(mouths))]
    worst = max(pair_diffs)
    check("嘴部区域全程有变化（最大两帧差）", worst > 3.0,
          f"最大差 {worst:.2f}，最小 {min(pair_diffs):.2f}，共 {len(pair_diffs)} 对")

    # 原照片按同一套裁切缩放到画布，作为「没做口型同步」的对照
    orig_canvas, orig_box = W._fit_canvas(frame, raw_box, canvas_w, canvas_h)
    ox1, oy1, ox2, oy2 = orig_box
    orig_face = orig_canvas[oy1:oy2, ox1:ox2].astype(np.float32)
    orig_mouth = orig_face[orig_face.shape[0] // 2:, :]
    to_orig = [float(np.abs(orig_mouth - m).mean()) for m in mouths]
    check("嘴部相对原照片被改写过", max(to_orig) > 3.0,
          f"与原图最大差 {max(to_orig):.2f}")

    outside = np.ones(img_a.shape[:2], dtype=bool)
    outside[y1:y2, x1:x2] = False
    outside_diff = max(
        float(np.abs(frames[i][outside].astype(np.float32)
                     - frames[j][outside].astype(np.float32)).mean())
        for i in range(len(frames)) for j in range(i + 1, len(frames)))
    check("人脸框外几乎不动（只改脸）", outside_diff < 1.0,
          f"框外最大差 {outside_diff:.3f}")

    W.Wav2LipRunner.get(config).shutdown()
    shutil.rmtree(WORK, ignore_errors=True)

    print("\n" + "=" * 74)
    if problems:
        print(f"  {FAIL} {len(problems)} 项未通过：")
        for item in problems:
            print(f"    - {item}")
        return 1
    print(f"  {PASS} 全部通过 —— 照片里的嘴会跟着声音动了")
    print(f"\n  渲染 {dur:.1f}s 视频耗时 {cost:.1f}s"
          f"（RTF {cost / max(0.1, dur):.2f}，CPU）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
