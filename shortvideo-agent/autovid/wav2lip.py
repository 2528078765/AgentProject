"""本地 Wav2Lip 数字人 —— 让照片里的嘴真的动起来。

输入：一张带人物的照片 + 一段音频
输出：嘴型跟着音频动的视频

为什么直连 Wav2Lip 而不走 ComfyUI：
    ComfyUI 在 AMD RDNA2（gfx1031）上要走非官方 ROCm 构建，动辄几个 GB，
    而且 PyTorch 的 Python 3.14 支持还没落地。Wav2Lip 模型本身很小
    （生成器约 35M 参数），CPU 上跑一条 60 秒视频只要一两分钟，
    比装一整套 ROCm 环境稳得多。

复用 vendor/comfyui-wav2lip 里现成的：
    * Wav2Lip/models/wav2lip.py    —— 模型定义
    * Wav2Lip/face_detection/      —— S3FD 人脸检测（自带）

**音频处理是自实现的**：原版用 librosa，而 librosa 依赖 numba，
cp314 上装不了。这里按 Wav2Lip 的 hparams 精确复刻了
Slaney mel 刻度 + preemphasis + STFT + 对称归一化，
少一个环节口型就会错位。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import numpy as np

from . import media as M

ROOT = Path(__file__).resolve().parent.parent
REPO_DIR = ROOT / "vendor" / "comfyui-wav2lip"
W2L_DIR = REPO_DIR / "Wav2Lip"

CKPT_PATH = W2L_DIR / "checkpoints" / "wav2lip_gan.pth"
# 注意文件名必须是 s3fd.pth —— sfd_detector.py 默认按这个名字找，
# 找不到它会自己去 adrianbulat.com 下载（在受限网络里会卡住）。
S3FD_PATH = W2L_DIR / "face_detection" / "detection" / "sfd" / "s3fd.pth"

# Wav2Lip 的音频超参（原版 hparams.py，不能改：改了 mel 就对不上训练分布）
NUM_MELS = 80
N_FFT = 800
HOP_SIZE = 200
WIN_SIZE = 800
SAMPLE_RATE = 16000
PREEMPHASIS = 0.97
REF_LEVEL_DB = 20
MIN_LEVEL_DB = -100
FMIN = 55
FMAX = 7600
MAX_ABS_VALUE = 4.0
MEL_STEP_SIZE = 16

IMG_SIZE = 96


def _provider_error(message: str) -> Exception:
    from .providers import ProviderError  # noqa: PLC0415
    return ProviderError(message)


def missing_files() -> list[str]:
    return [p.name for p in (CKPT_PATH, S3FD_PATH) if not p.exists()]


def models_ready() -> bool:
    return not missing_files()


def imread_unicode(path: Path) -> "np.ndarray | None":
    """读图，支持中文路径。

    OpenCV 的 imread 在 Windows 上走 ANSI 路径，遇到中文文件名直接返回 None
    （用户的照片恰好都叫「形象1.jpg」这类）。改成先读字节再用 imdecode。
    """
    import cv2  # noqa: PLC0415

    try:
        buffer = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if buffer.size == 0:
        return None
    return cv2.imdecode(buffer, cv2.IMREAD_COLOR)


# --------------------------------------------------------------------------- #
# 音频：自实现 mel 频谱（精确对齐 librosa 的 Slaney 刻度）
# --------------------------------------------------------------------------- #
def _hz_to_mel(freq: Any) -> Any:
    """Slaney 刻度（librosa 默认，htk=False）。

    低频线性、1kHz 以上对数 —— 和 HTK 那个 2595*log10(1+f/700) 不是一回事，
    用错刻度 mel 滤波器组就偏了。
    """
    freq = np.asanyarray(freq, dtype=np.float64)
    f_min, f_sp = 0.0, 200.0 / 3.0
    mels = (freq - f_min) / f_sp
    min_log_hz = 1000.0
    min_log_mel = (min_log_hz - f_min) / f_sp
    logstep = np.log(6.4) / 27.0
    if freq.ndim:
        log_t = freq >= min_log_hz
        mels[log_t] = min_log_mel + np.log(freq[log_t] / min_log_hz) / logstep
    elif freq >= min_log_hz:
        mels = min_log_mel + np.log(freq / min_log_hz) / logstep
    return mels


def _mel_to_hz(mels: Any) -> Any:
    mels = np.asanyarray(mels, dtype=np.float64)
    f_min, f_sp = 0.0, 200.0 / 3.0
    freqs = f_min + f_sp * mels
    min_log_hz = 1000.0
    min_log_mel = (min_log_hz - f_min) / f_sp
    logstep = np.log(6.4) / 27.0
    if mels.ndim:
        log_t = mels >= min_log_mel
        freqs[log_t] = min_log_hz * np.exp(logstep * (mels[log_t] - min_log_mel))
    elif mels >= min_log_mel:
        freqs = min_log_hz * np.exp(logstep * (mels - min_log_mel))
    return freqs


_MEL_BASIS: np.ndarray | None = None


def mel_filterbank() -> np.ndarray:
    """librosa.filters.mel(sr=16000, n_fft=800, n_mels=80, fmin=55, fmax=7600)。

    等价于 norm='slaney'（三角形滤波器的面积归一化）。
    """
    global _MEL_BASIS
    if _MEL_BASIS is not None:
        return _MEL_BASIS

    n_freqs = 1 + N_FFT // 2
    fftfreqs = np.fft.rfftfreq(N_FFT, 1.0 / SAMPLE_RATE)
    assert len(fftfreqs) == n_freqs

    mel_f = _mel_to_hz(np.linspace(_hz_to_mel(FMIN), _hz_to_mel(FMAX), NUM_MELS + 2))
    fdiff = np.diff(mel_f)
    ramps = np.subtract.outer(mel_f, fftfreqs)

    weights = np.zeros((NUM_MELS, n_freqs), dtype=np.float32)
    for index in range(NUM_MELS):
        lower = -ramps[index] / fdiff[index]
        upper = ramps[index + 2] / fdiff[index + 1]
        weights[index] = np.maximum(0.0, np.minimum(lower, upper))

    enorm = 2.0 / (mel_f[2:NUM_MELS + 2] - mel_f[:NUM_MELS])
    weights *= enorm[:, np.newaxis]
    _MEL_BASIS = weights
    return weights


def load_wav_16k(path: Path) -> np.ndarray:
    """读成 16kHz 单声道 float32（替代 librosa.load(path, sr=16000)）。"""
    import scipy.signal
    import soundfile as sf

    data, native = sf.read(str(path), dtype="float32", always_2d=True)
    if data.size == 0:
        raise _provider_error(f"音频是空的：{path}")
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    if native != SAMPLE_RATE:
        gcd = int(np.gcd(int(native), SAMPLE_RATE))
        mono = scipy.signal.resample_poly(mono, SAMPLE_RATE // gcd, int(native) // gcd)
    return mono.astype(np.float32)


def _preemphasis(wav: np.ndarray, k: float = PREEMPHASIS) -> np.ndarray:
    import scipy.signal

    return scipy.signal.lfilter([1.0, -k], [1.0], wav)


def _stft(y: np.ndarray) -> np.ndarray:
    """复刻 librosa.stft(y, n_fft=800, hop_length=200, win_length=800)。

    librosa 0.8（Wav2Lip 锁的版本）默认 center=True、pad_mode='reflect'：
    先左右各反射填充 n_fft//2，再按 hop 切窗、加 Hann 窗、做 rFFT。
    """
    pad = N_FFT // 2
    if len(y) <= pad:
        y = np.pad(y, (0, pad - len(y) + 1), mode="reflect")
    y = np.pad(y, (pad, pad), mode="reflect")

    window = np.hanning(WIN_SIZE + 1)[:WIN_SIZE]  # hann(sym=False)
    n_frames = 1 + (len(y) - N_FFT) // HOP_SIZE
    if n_frames <= 0:
        return np.zeros((1 + N_FFT // 2, 0), dtype=np.complex64)

    strides = (y.strides[0] * HOP_SIZE, y.strides[0])
    frames = np.lib.stride_tricks.as_strided(
        y, shape=(n_frames, N_FFT), strides=strides).copy()
    return np.fft.rfft(frames * window, n=N_FFT, axis=1).T


def _amp_to_db(x: np.ndarray) -> np.ndarray:
    min_level = np.exp(MIN_LEVEL_DB / 20 * np.log(10))
    return 20 * np.log10(np.maximum(min_level, x))


def _normalize(S: np.ndarray) -> np.ndarray:
    return np.clip(
        (2 * MAX_ABS_VALUE) * ((S - MIN_LEVEL_DB) / (-MIN_LEVEL_DB)) - MAX_ABS_VALUE,
        -MAX_ABS_VALUE, MAX_ABS_VALUE)


def melspectrogram(wav: np.ndarray) -> np.ndarray:
    """Wav2Lip 的 mel 频谱：preemphasis → STFT → mel → dB → 对称归一化。"""
    D = _stft(_preemphasis(wav))
    S = _amp_to_db(np.dot(mel_filterbank(), np.abs(D))) - REF_LEVEL_DB
    return _normalize(S)


def mel_chunks(mel: np.ndarray, fps: int) -> list[np.ndarray]:
    """按时长切 mel 片段（每片 MEL_STEP_SIZE 帧），对应视频的每一帧。"""
    chunks: list[np.ndarray] = []
    multiplier = 80.0 / fps
    index = 0
    while True:
        start = int(index * multiplier)
        if start + MEL_STEP_SIZE > mel.shape[1]:
            chunks.append(mel[:, mel.shape[1] - MEL_STEP_SIZE:])
            break
        chunks.append(mel[:, start:start + MEL_STEP_SIZE])
        index += 1
    return chunks


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def _for_detection(img: np.ndarray, long_side: int = 640) -> tuple[np.ndarray, float]:
    """人脸检测前先缩图。

    S3FD 是全程卷积、**内部不缩放**：直接把 4096×3072 的原图喂进去，
    stride-4 那层的特征图就有 1024×768，内存和时间都会爆。
    缩到 640 长边既快又准（S3FD 本就是在 640×480 上训的）。
    返回 (缩放后图, 把检测框换算回原图坐标的倍数)。
    """
    import cv2  # noqa: PLC0415

    height, width = img.shape[:2]
    scale = long_side / max(height, width)
    if scale >= 1.0:
        return img, 1.0
    resized = cv2.resize(img, (max(1, int(width * scale)), max(1, int(height * scale))),
                         interpolation=cv2.INTER_AREA)
    return resized, 1.0 / scale


def _fit_canvas(img: np.ndarray, box: list[int],
                target_w: int, target_h: int) -> tuple[np.ndarray, list[int]]:
    """以人脸为中心裁一块目标比例的画布，并缩放到目标尺寸。

    场景照片可能是 3:4 甚至横构图，而成片是 9:16。直接拉伸会把人拉变形，
    所以取「尽可能大、比例正确、以人脸为中心」的窗口再缩放。
    返回 (画布, 换算到画布坐标的人脸框)。
    """
    import cv2  # noqa: PLC0415

    height, width = img.shape[:2]
    ratio = target_w / target_h
    if width / height > ratio:
        crop_h = height
        crop_w = int(round(height * ratio))
    else:
        crop_w = width
        crop_h = int(round(width / ratio))
    crop_w = max(16, min(crop_w, width))
    crop_h = max(16, min(crop_h, height))

    center_x = (box[0] + box[2]) / 2.0
    center_y = (box[1] + box[3]) / 2.0
    x1 = int(round(center_x - crop_w / 2.0))
    y1 = int(round(center_y - crop_h / 2.0))
    x1 = max(0, min(x1, width - crop_w))
    y1 = max(0, min(y1, height - crop_h))

    crop = img[y1:y1 + crop_h, x1:x1 + crop_w]
    canvas = cv2.resize(crop, (target_w, target_h), interpolation=cv2.INTER_AREA)

    sx, sy = target_w / crop_w, target_h / crop_h
    new_box = [
        max(0, int(round((box[0] - x1) * sx))),
        max(0, int(round((box[1] - y1) * sy))),
        min(target_w, int(round((box[2] - x1) * sx))),
        min(target_h, int(round((box[3] - y1) * sy))),
    ]
    return canvas, new_box


class Wav2LipRunner:
    """懒加载单例：模型只加载一次，多条视频复用。"""

    _instance: "Wav2LipRunner | None" = None
    _lock = threading.Lock()

    def __init__(self, config: Any) -> None:
        self.config = config
        self._model: Any = None
        self._detector: Any = None
        # 最近一次渲染用的人脸框与画布尺寸（测试/调试用，避免重复算一遍换算）
        self.last_box: list[int] | None = None
        self.last_size: tuple[int, int] | None = None

    @classmethod
    def get(cls, config: Any) -> "Wav2LipRunner":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(config)
            return cls._instance

    # ------------------------------------------------------------ 加载模型
    def _import_vendor(self) -> tuple[Any, Any]:
        """导入 vendor 里的模型与人脸检测器。

        临时把 Wav2Lip 目录插到 sys.path 最前面（`models` / `face_detection`
        这两个名字太常见，不能常驻以免盖住别的包），导完就撤。
        """
        text = str(W2L_DIR)
        inserted = text not in sys.path
        if inserted:
            sys.path.insert(0, text)
        try:
            from face_detection import FaceAlignment, LandmarksType  # noqa: PLC0415
            from models.wav2lip import Wav2Lip  # noqa: PLC0415
        finally:
            if inserted and text in sys.path:
                sys.path.remove(text)
        return Wav2Lip, (FaceAlignment, LandmarksType)

    def ensure_loaded(self, log: Callable[[str], None]) -> None:
        with self._lock:
            if self._model is not None and self._detector is not None:
                return
            missing = missing_files()
            if missing:
                raise _provider_error(
                    "Wav2Lip 模型还没下载：" + "、".join(missing) + "\n"
                    "先跑：python scripts/deploy_wav2lip.py")

            import torch  # noqa: PLC0415

            Wav2Lip, (FaceAlignment, LandmarksType) = self._import_vendor()

            log("  加载 Wav2Lip 模型与 S3FD 人脸检测器…")
            model = Wav2Lip()
            checkpoint = torch.load(str(CKPT_PATH), map_location="cpu",
                                    weights_only=False)
            state = checkpoint.get("state_dict", checkpoint)
            model.load_state_dict({k.replace("module.", ""): v
                                   for k, v in state.items()})
            model = model.eval()
            with torch.no_grad():
                model(torch.zeros(1, 1, NUM_MELS, MEL_STEP_SIZE),
                      torch.zeros(1, 6, IMG_SIZE, IMG_SIZE))
            self._model = model
            self._detector = FaceAlignment(LandmarksType._2D, device="cpu",
                                           flip_input=False)
            log(f"  Wav2Lip 就绪（{sum(p.numel() for p in model.parameters()) / 1e6:.0f}M 参数，CPU）")

    def shutdown(self) -> None:
        with self._lock:
            self._model = None
            self._detector = None

    # ---------------------------------------------------------------- 人脸
    def _face_box(self, frame: np.ndarray, log: Callable[[str], None]) -> list[int]:
        """检测人脸框（静态图只检一次），带 Wav2Lip 原版的 padding。"""
        detections = self._detector.get_detections_for_batch(np.array([frame]))
        rect = detections[0] if detections else None
        if rect is None:
            raise _provider_error(
                "照片里没检测到人脸，Wav2Lip 无法驱动口型。\n"
                "  换一张人脸清晰、占画面比例大一些的正脸照（最短边 ≥256px）。")
        pad_y1, pad_y2, pad_x1, pad_x2 = 0, 10, 0, 0
        y1 = max(0, int(rect[1]) - pad_y1)
        y2 = min(frame.shape[0], int(rect[3]) + pad_y2)
        x1 = max(0, int(rect[0]) - pad_x1)
        x2 = min(frame.shape[1], int(rect[2]) + pad_x2)
        if x2 - x1 < 16 or y2 - y1 < 16:
            raise _provider_error("检测到的人脸区域太小，换一张人脸更大的照片。")
        log(f"  人脸框 {x1},{y1} - {x2},{y2}（{x2 - x1}×{y2 - y1}）")
        return [x1, y1, x2, y2]

    # ---------------------------------------------------------------- 渲染
    def _render_inprocess(self, jobs: list[dict[str, Any]],
                          log: Callable[[str], None], fps: int,
                          width: int, height: int) -> None:
        """在**当前进程**里逐个渲染。只有 worker 子进程会直接调它。

        jobs: [{"photo": str, "audio": str, "out": str}, ...]
        """
        import cv2  # noqa: PLC0415
        import torch  # noqa: PLC0415

        cfg = self.config.provider_cfg("local_wav2lip") or {}
        batch_size = int(cfg.get("batch_size", 32))

        for job_index, job in enumerate(jobs, start=1):
            photo = Path(job["photo"])
            audio = Path(job["audio"])
            out_path = Path(job["out"])

            source_video = photo.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".avi"}
            capture = cv2.VideoCapture(str(photo)) if source_video else None
            if capture is not None:
                ok, photo_img = capture.read()
                if not ok:
                    photo_img = None
            else:
                photo_img = imread_unicode(photo)
            if photo_img is None:
                raise _provider_error(f"读不了画面源：{photo}")

            self.ensure_loaded(log)
            wav = load_wav_16k(audio)
            mel = melspectrogram(wav)
            if np.isnan(mel.reshape(-1)).any():
                raise _provider_error("mel 频谱出现 NaN —— 音频可能异常")
            chunks = mel_chunks(mel, fps)

            det_img, upscale = _for_detection(photo_img)
            det_box = self._face_box(det_img, log)
            raw_box = [int(round(value * upscale)) for value in det_box]
            frame, box = _fit_canvas(photo_img, raw_box, width, height)
            x1, y1, x2, y2 = box
            if x2 - x1 < 16 or y2 - y1 < 16:
                raise _provider_error("换算后的人脸区域太小，换一张人脸更大的照片。")
            self.last_box = list(box)
            self.last_size = (width, height)
            # 回写元数据：渲染在子进程里做，父进程（和测试）需要知道实际用了
            # 哪个脸框，否则没法验证「改的确实是脸、框外没动」。
            Path(str(out_path) + ".meta.json").write_text(json.dumps({
                "box": list(box), "width": width, "height": height,
                "chunks": len(chunks), "photo": str(photo), "audio": str(audio),
            }, ensure_ascii=False), encoding="utf-8")

            audio_s = len(wav) / SAMPLE_RATE
            log(f"  口型同步 {job_index}/{len(jobs)}：{len(chunks)} 帧 @ {fps}fps"
                f"（音频 {audio_s:.1f}s）")

            if capture is not None:
                # 检测首帧后从头播放；音频比动作片长时循环动作片。
                capture.set(cv2.CAP_PROP_POS_FRAMES, 0)

            def next_frame() -> tuple[np.ndarray, list[int]]:
                if capture is None:
                    return frame.copy(), list(box)
                ok, source = capture.read()
                if not ok:
                    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, source = capture.read()
                if not ok or source is None:
                    raise _provider_error(f"动作视频无法继续解码：{photo}")
                return _fit_canvas(source, raw_box, width, height)

            # 嘴部增强参数。为什么不用 CodeFormer 那类人脸修复：
            # 实测它会**把长相改掉**（眉毛变粗直、眼型改变、皮肤塑料感，
            # 等于换了个人）。数字人最重要的就是「还是本人」，所以走保身份的做法：
            #   1) 96×96 的嘴部补丁用 LANCZOS4 放大（默认双线性更糊）
            #   2) 对下半张脸做非锐化掩模，把边缘提回来
            # 代价每帧几毫秒，且不改变五官。
            sharpen = float(cfg.get("sharpen", 0.6) or 0.0)
            blur_sigma = float(cfg.get("sharpen_sigma", 3.0) or 3.0)

            out_path.parent.mkdir(parents=True, exist_ok=True)
            command = [
                M.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
                "-i", str(audio),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-shortest", str(out_path),
            ]
            process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                       stdout=subprocess.DEVNULL,
                                       stderr=subprocess.PIPE)
            written = 0
            try:
                assert process.stdin is not None
                for start in range(0, len(chunks), batch_size):
                    group = chunks[start:start + batch_size]
                    mels = np.stack([
                        np.reshape(chunk, [chunk.shape[0], chunk.shape[1], 1])
                        for chunk in group])
                    batch_frames: list[np.ndarray] = []
                    batch_boxes: list[list[int]] = []
                    batch_faces: list[np.ndarray] = []
                    for _chunk in group:
                        current, current_box = next_frame()
                        cx1, cy1, cx2, cy2 = current_box
                        face_src = cv2.resize(
                            current[cy1:cy2, cx1:cx2], (IMG_SIZE, IMG_SIZE))
                        masked = face_src.copy()
                        masked[IMG_SIZE // 2:] = 0
                        batch_faces.append(np.concatenate((masked, face_src), axis=2))
                        batch_frames.append(current)
                        batch_boxes.append(current_box)
                    imgs = np.stack(batch_faces).astype(np.float32) / 255.0
                    with torch.no_grad():
                        pred = self._model(
                            torch.FloatTensor(np.transpose(mels, (0, 3, 1, 2))),
                            torch.FloatTensor(np.transpose(imgs, (0, 3, 1, 2))))
                    pred = pred.numpy().transpose(0, 2, 3, 1) * 255.0
                    for patch, full, current_box in zip(
                            pred, batch_frames, batch_boxes, strict=True):
                        x1, y1, x2, y2 = current_box
                        patch_img = cv2.resize(
                            patch.astype(np.uint8), (x2 - x1, y2 - y1),
                            interpolation=cv2.INTER_LANCZOS4)
                        full[y1:y2, x1:x2] = patch_img
                        if sharpen > 0:
                            # 只锐化下半张脸（嘴的位置），上半部分保持原样
                            mouth_top = y1 + (y2 - y1) // 2
                            region = full[mouth_top:y2, x1:x2]
                            blurred = cv2.GaussianBlur(region, (0, 0), blur_sigma)
                            full[mouth_top:y2, x1:x2] = cv2.addWeighted(
                                region, 1.0 + sharpen, blurred, -sharpen, 0)
                        process.stdin.write(full.tobytes())
                        written += 1
                process.stdin.close()
                stderr = (process.stderr.read().decode("utf-8", "replace")
                          if process.stderr else "")
                code = process.wait(timeout=900)
                if code != 0:
                    tail = "\n".join(stderr.strip().splitlines()[-10:])
                    raise _provider_error(f"ffmpeg 合成失败 (exit {code})\n{tail}")
            except Exception:
                process.kill()
                raise
            finally:
                if capture is not None:
                    capture.release()
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                if process.stderr and not process.stderr.closed:
                    process.stderr.close()

            if not out_path.exists() or out_path.stat().st_size == 0:
                raise _provider_error("口型同步没有产出视频")

    def render_batch(self, jobs: list[dict[str, Any]],
                     log: Callable[[str], None], fps: int | None = None,
                     width: int | None = None, height: int | None = None) -> None:
        """在**独立子进程**里渲染所有片段。

        为什么必须隔离进程：llama.cpp（Qwen3-TTS 用）带 libomp.dll，
        torch（Wav2Lip 用）带 libiomp5md.dll —— 两个 OpenMP 运行时在同一个
        进程里会直接 abort（OMP: Error #15）。官方给的 KMP_DUPLICATE_LIB_OK
        只是「继续跑」的开关，明确说了可能算错，不能靠它。
        放子进程各用各的运行时，顺带在渲染结束后把内存全部还给系统。
        """
        if not jobs:
            return
        cfg = self.config.provider_cfg("local_wav2lip") or {}
        fps = int(fps or cfg.get("fps") or self.config.platform.get("fps", 25))
        width = int(width or cfg.get("width") or self.config.platform.get("width", 1080))
        height = int(height or cfg.get("height") or self.config.platform.get("height", 1920))

        payload = ROOT / ".tmp" / "wav2lip_jobs.json"
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_text(json.dumps({
            "jobs": jobs, "fps": fps, "width": width, "height": height,
            # 必须把本进程实际的引擎配置传过去：子进程是自己 Config.load 的，
            # 父进程的 with_overrides（比如临时改锐化强度）传不进去，
            # 会出现「改了配置但渲染行为没变」的假象（实测踩过）。
            "wav2lip_cfg": dict(self.config.provider_cfg("local_wav2lip") or {}),
        }, ensure_ascii=False), encoding="utf-8")

        command = [sys.executable, "-m", "autovid.wav2lip", "--jobs", str(payload)]
        log(f"  启动口型同步子进程（{len(jobs)} 个片段，{width}×{height} @{fps}fps）…")
        process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True,
                                   encoding="utf-8", errors="replace", cwd=str(ROOT))
        assert process.stdout is not None
        for line in process.stdout:
            text = line.rstrip()
            if text:
                log("    " + text)
        code = process.wait(timeout=3600)
        if code != 0:
            raise _provider_error(
                f"口型同步子进程失败（exit {code}）。\n"
                "  常见原因：模型没下齐（scripts/deploy_wav2lip.py）、"
                "照片里检不到人脸、显存/内存不足。")

        # 把子进程算出来的人脸框读回来（验证与调试要用）
        meta_path = Path(str(jobs[-1]["out"]) + ".meta.json")
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                self.last_box = list(meta["box"])
                self.last_size = (int(meta["width"]), int(meta["height"]))
            except Exception:  # noqa: BLE001
                pass

    def render(self, photo: Path, audio: Path, out_path: Path,
               log: Callable[[str], None], fps: int | None = None,
               width: int | None = None, height: int | None = None) -> Path:
        """单个片段（测试与简单场景用）。"""
        self.render_batch(
            [{"photo": str(photo), "audio": str(audio), "out": str(out_path)}],
            log, fps=fps, width=width, height=height)
        return out_path


# --------------------------------------------------------------------------- #
# 子进程入口
#
# Wav2Lip 必须单独一个进程跑：llama.cpp（Qwen3-TTS）与 torch 各自带一个
# OpenMP 运行时（libomp.dll / libiomp5md.dll），同进程会 OMP Error #15 直接
# abort。用 `python -m autovid.wav2lip --jobs <json>` 拉起。
# --------------------------------------------------------------------------- #
def _worker_main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Wav2Lip 渲染子进程")
    parser.add_argument("--jobs", required=True, help="任务 JSON 路径")
    args = parser.parse_args()

    payload = json.loads(Path(args.jobs).read_text(encoding="utf-8"))
    from .config import Config  # noqa: PLC0415

    config = Config.load(root=ROOT)
    passed = payload.get("wav2lip_cfg")
    if isinstance(passed, dict) and passed:
        config = config.with_overrides({"providers.local_wav2lip": passed})
    runner = Wav2LipRunner(config)

    def log(message: str) -> None:
        print(message, flush=True)

    runner._render_inprocess(
        payload["jobs"], log,
        int(payload["fps"]), int(payload["width"]), int(payload["height"]))
    print(f"__DONE__ 渲染完成 {len(payload['jobs'])} 个片段", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker_main())
