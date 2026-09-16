"""本地面部修复（CodeFormer ONNX）—— 解决 Wav2Lip 嘴部糊的问题。

问题根源：Wav2Lip 只在 **96×96** 上生成嘴部，再放大贴回 1080×1920 的画面。
比如人脸框在成片里占 700×1000 像素，96×96 放大 7~10 倍，细节全没了。

CodeFormer 是专门做「人脸修复」的模型：把贴回来的脸再修一遍，
纹理和边缘能回来相当一部分。这是社区里跑 Wav2Lip 的标准后处理。

为什么用 ONNX 而不是官方的 gfpgan / basicsr：
    basicsr 依赖旧版 torch + 需要编译的扩展，Python 3.14 上基本装不了
    （本项目已经在 librosa/numba、sox 上踩过同样的坑）。
    而 onnxruntime 项目里已经有了（Qwen3-TTS 在用），拿 ONNX 模型直接推理最稳。

模型：https://huggingface.co/yuvraj108c/facerestore-onnx （codeformer.onnx，337MB）
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = ROOT / "vendor" / "facerestore" / "codeformer.onnx"

# CodeFormer 的固定输入边长
INPUT_SIZE = 512


def model_ready() -> bool:
    return MODEL_PATH.exists() and MODEL_PATH.stat().st_size > 100e6


def missing_files() -> list[str]:
    return [] if model_ready() else [MODEL_PATH.name]


class FaceRestorer:
    """懒加载单例：模型只读一次，一条视频的所有帧复用同一个会话。"""

    _instance: "FaceRestorer | None" = None
    _lock = threading.Lock()

    def __init__(self, config: Any) -> None:
        self.config = config
        self._session: Any = None
        self._input_names: list[str] = []
        self._weight_name: str | None = None

    @classmethod
    def get(cls, config: Any) -> "FaceRestorer":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(config)
            return cls._instance

    # ------------------------------------------------------------ 加载模型
    def ensure_loaded(self, log: Callable[[str], None]) -> None:
        with self._lock:
            if self._session is not None:
                return
            if not model_ready():
                raise RuntimeError(
                    "CodeFormer 模型还没下载。跑：python scripts/deploy_facerestore.py")
            import onnxruntime as ort  # noqa: PLC0415

            cfg = self.config.provider_cfg("face_restore") or {}
            provider = str(cfg.get("onnx_provider") or "CPU").upper()
            available = ort.get_available_providers()
            providers = ["CPUExecutionProvider"]
            if provider == "DML" and "DmlExecutionProvider" in available:
                providers.insert(0, "DmlExecutionProvider")

            options = ort.SessionOptions()
            options.log_severity_level = 3
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            log(f"  加载 CodeFormer 人脸修复模型（{providers[0]}）…")
            self._session = ort.InferenceSession(
                str(MODEL_PATH), sess_options=options, providers=providers)
            self._input_names = [i.name for i in self._session.get_inputs()]
            # CodeFormer 有个可选的「保真度权重」输入（名子各版本不一）
            self._weight_name = next(
                (n for n in self._input_names if n.lower() in ("weight", "w")), None)
            log(f"  修复模型就绪（输入 {self._input_names}）")

    # ------------------------------------------------------------ 单帧修复
    def enhance(self, frame: np.ndarray, box: list[int],
                weight: float = 0.7) -> np.ndarray:
        """对 frame（BGR）里 box 指定的脸做修复，返回新的整帧。

        box = [x1, y1, x2, y2]（画布坐标）。会向外扩一点边距，
        让 512 的输入框住整个头部，修复效果更稳。
        """
        import cv2  # noqa: PLC0415

        height, width = frame.shape[:2]
        x1, y1, x2, y2 = box
        # 外扩 25%：CodeFormer 在 512 输入里希望看到完整头部
        margin_x = int((x2 - x1) * 0.25)
        margin_y = int((y2 - y1) * 0.25)
        cx1 = max(0, x1 - margin_x)
        cy1 = max(0, y1 - margin_y)
        cx2 = min(width, x2 + margin_x)
        cy2 = min(height, y2 + margin_y)
        if cx2 - cx1 < 32 or cy2 - cy1 < 32:
            return frame

        crop = frame[cy1:cy2, cx1:cx2]
        resized = cv2.resize(crop, (INPUT_SIZE, INPUT_SIZE),
                             interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32)
        tensor = (rgb / 255.0 - 0.5) / 0.5                 # → [-1, 1]
        tensor = np.transpose(tensor, (2, 0, 1))[np.newaxis, ...]

        feeds: dict[str, Any] = {}
        for name in self._input_names:
            if name == self._weight_name:
                feeds[name] = np.array([weight], dtype=np.float32)
            else:
                feeds[name] = tensor
        outputs = self._session.run(None, feeds)
        out = outputs[0][0]
        out = np.transpose(out, (1, 2, 0))                 # → HWC
        out = np.clip((out + 1.0) / 2.0, 0.0, 1.0) * 255.0
        restored = cv2.cvtColor(out.astype(np.uint8), cv2.COLOR_RGB2BGR)
        restored = cv2.resize(restored, (cx2 - cx1, cy2 - cy1),
                              interpolation=cv2.INTER_LANCZOS4)

        result = frame.copy()
        result[cy1:cy2, cx1:cx2] = restored
        return result
