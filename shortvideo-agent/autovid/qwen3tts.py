"""本地 Qwen3-TTS 引擎封装 —— 让视频里的声音真的是你自己的。

组件分工：
    Talker / Predictor  → llama.cpp GGUF（Vulkan，AMD 卡可用，RX 6750 GRE 没问题）
    Decoder / CodecEncoder / SpeakerEncoder → ONNX Runtime DirectML

设计：
    * **克隆一次，反复用**：从参考音频提取「说话人嵌入 + 音频码」存成无损锚点
      （anchor.json），之后每次合成直接载入锚点，不再重算。
    * **一个引擎跑完所有句子**：引擎是懒加载单例（按 model_dir 区分），
      一条视频的所有口播句共用一个流，省掉反复加载 GGUF 的时间。
    * **GPU 失败自动降级**：Vulkan 起不来就退回 CPU（慢，但能出结果）。

没部署 / 没选这个 provider 时不会 import 引擎相关代码，所以不影响其它功能。
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent


def _provider_error(message: str) -> Exception:
    """构造 ProviderError。

    它定义在 providers.py 里，而 providers 又会调用本模块 ——
    顶层互相 import 会成环，所以在这里局部导入。
    """
    from .providers import ProviderError  # noqa: PLC0415
    return ProviderError(message)


REPO_DIR = ROOT / "vendor" / "qwen3-tts-gguf"
STUBS_DIR = ROOT / "vendor" / "stubs"

MODEL_FILES = ("qwen3_tts_talker.q5_k.gguf", "qwen3_tts_predictor.q8_0.gguf",
               "qwen3_tts_decoder.fp16.onnx", "tokenizer.json")


def model_dir_of(config: Any) -> Path:
    cfg = config.provider_cfg("local_qwen_tts") or {}
    return config.path(str(cfg.get("model_dir")
                           or "vendor/qwen3-tts-export/model-base-small"))


def model_ready(config: Any) -> bool:
    """模型是否已部署（部署脚本跑完且产物齐全）。"""
    model_dir = model_dir_of(config)
    return bool(model_dir) and all((model_dir / name).exists() for name in MODEL_FILES)


def _ensure_repo_on_path() -> None:
    """把引擎仓库和兼容 stub 挂到 sys.path，并让引擎优先用 pip 装的那套依赖。

    两个必须处理的路径问题：

    1. **pip 装的依赖要排在 .pylibs 前面**：.pylibs 是沙箱里「pip 被禁」时
       手工 vendor 的退路，里面可能残留同名半成品包。它会盖住 pip 装好的
       完整版本 —— 实测 soundfile 就因此出现「Python 模块与 libsndfile DLL
       版本错配」，报 `no function ... named 'sf_wchar_open'`。

    2. 引擎仓库和 stub 在 vendor/ 下，不在任何 site-packages 里。
    """
    import site  # noqa: PLC0415

    preferred: list[str] = []
    try:
        user_site = site.getusersitepackages()
        if user_site:
            preferred.append(user_site)
    except Exception:  # noqa: BLE001
        pass
    for name in ("getsitepackages",):
        try:
            preferred.extend(site.__dict__[name]() or [])
        except Exception:  # noqa: BLE001
            pass
    for path in reversed([p for p in preferred if p and Path(p).is_dir()]):
        if path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)

    for candidate in (STUBS_DIR, REPO_DIR):
        text = str(candidate)
        if text not in sys.path:
            sys.path.insert(0, text)

    # 已经被 .pylibs 那份导入过的模块，光改 sys.path 没用 —— 它已在 sys.modules
    # 里缓存。把它们踢掉，重新从 pip 版导入（langgraph 之类只存在于 .pylibs
    # 的包不受影响：重新导入时仍会从 .pylibs 找到）。
    stale = [name for name, module in list(sys.modules.items())
             if module is not None
             and ".pylibs" in (getattr(module, "__file__", "") or "")]
    for name in stale:
        sys.modules.pop(name, None)


class Qwen3TTSBackend:
    """懒加载单例：一次 run 里所有句子共用一个引擎（省下反复加载 GGUF）。"""

    _instance: "Qwen3TTSBackend | None" = None
    # 必须是**可重入**锁：get() 持锁期间会调 shutdown()，而 shutdown() 也要拿
    # 这把锁。普通 Lock 会自己等自己 —— 表现为流程无声卡死（实测踩过：
    # 只要 anchor.json 已存在，第二次 get() 就死锁，也就是第二次生成必挂）。
    _lock = threading.RLock()

    def __init__(self, config: Any) -> None:
        self.config = config
        self._engine: Any = None
        self._engine_model_dir: str = ""
        self._engine_onnx = ""
        self._engine_gpu = True
        # 「想要」的模型目录在构造时就记下，不能等引擎加载完才记 ——
        # 否则引擎没加载过时 get() 会误判成「换了模型」。
        self._requested_model_dir = str(model_dir_of(config))
        # 已「准备好」的音色锚点缓存：key 是 anchor.json 路径，(mtime, TTSResult)
        self._voice_cache: dict[str, tuple[float, Any]] = {}

    # ------------------------------------------------------------------ 引擎
    @classmethod
    def get(cls, config: Any) -> "Qwen3TTSBackend":
        """拿单例；配置里换了 model_dir 才重建。"""
        wanted = str(model_dir_of(config))
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(config)
            elif cls._instance._requested_model_dir != wanted:
                cls._instance.shutdown()
                cls._instance = cls(config)
            return cls._instance

    def shutdown(self) -> None:
        with self._lock:
            if self._engine is not None:
                try:
                    self._engine.shutdown()
                except Exception:  # noqa: BLE001
                    pass
                self._engine = None

    def ensure_engine(self, log: Callable[[str], None]) -> Any:
        with self._lock:
            if self._engine is not None and getattr(self._engine, "ready", False):
                return self._engine

            _ensure_repo_on_path()
            cfg = self.config.provider_cfg("local_qwen_tts") or {}
            model_dir = model_dir_of(self.config)
            onnx_provider = str(cfg.get("onnx_provider") or "DML")
            llm_use_gpu = bool(cfg.get("llm_use_gpu", True))

            missing = [name for name in MODEL_FILES if not (model_dir / name).exists()]
            if missing:
                raise _provider_error(
                    "本地 Qwen3-TTS 模型还没部署好，缺：\n  - " + "\n  - ".join(missing)
                    + "\n先跑：python scripts/deploy_qwen3tts.py")

            from qwen3_tts_gguf.inference import TTSEngine  # noqa: PLC0415

            def build(use_gpu: bool) -> Any:
                return TTSEngine(
                    model_dir=str(model_dir), onnx_provider=onnx_provider,
                    llm_use_gpu=use_gpu, verbose=False, subprocess_decoder=False,
                )

            log(f"加载本地 Qwen3-TTS 引擎（{model_dir.name}，ONNX={onnx_provider}，"
                f"LLM {'Vulkan' if llm_use_gpu else 'CPU'}）…")
            engine = build(llm_use_gpu)
            if not getattr(engine, "ready", False) and llm_use_gpu:
                log("⚠ Vulkan 初始化失败，退回 CPU 跑 LLM（更慢但能出结果）")
                engine = build(False)
            if not getattr(engine, "ready", False):
                raise _provider_error(
                    "本地 Qwen3-TTS 引擎初始化失败。可能原因：\n"
                    "  - 显卡驱动 / Vulkan 运行时问题（看上面日志）\n"
                    "  - 显存不足（Talker 301MB + Predictor 144MB，10GB 卡足够）")
            self._engine = engine
            self._engine_model_dir = str(model_dir)
            self._engine_onnx = onnx_provider
            self._engine_gpu = llm_use_gpu
            return engine

    # ------------------------------------------------------------- 音色锚点
    def prepare_anchor(self, ref_wav: Path, anchor_json: Path,
                       log: Callable[[str], None]) -> Path:
        """从参考音频提取音色锚点（无损：codes + spk_emb），存成 anchor.json。

        幂等：锚点已存在且对应同一个参考音频就直接复用 —— 这就是「克隆一次」。
        参考音频签名（大小 + 修改时间）记在锚点 info 字段里做比对。
        """
        anchor_json = Path(anchor_json)
        anchor_json.parent.mkdir(parents=True, exist_ok=True)
        ref_sig = f"{ref_wav.stat().st_size}:{int(ref_wav.stat().st_mtime)}"

        if anchor_json.exists():
            try:
                saved = json.loads(anchor_json.read_text(encoding="utf-8"))
                if saved.get("info") == ref_sig and saved.get("codes"):
                    log("  复用已提取的音色锚点（anchor.json）")
                    return anchor_json
            except Exception:  # noqa: BLE001
                pass

        log(f"  从参考音频提取音色锚点：{ref_wav.name}…")
        engine = self.ensure_engine(log)
        stream = engine.create_stream()
        try:
            res = stream.set_voice(str(ref_wav))  # 克隆只用嵌入，text 可省
            if not res or not getattr(res, "is_valid_anchor", False):
                raise _provider_error(
                    "音色锚点提取失败：参考音频无法编码或太短。"
                    "请在「音色库」重新录一段（建议 10 秒以上）。")
            res.save_json(str(anchor_json), info=ref_sig)
            log(f"  音色锚点就绪：{anchor_json.name}"
                f"（{len(res.codes)} 帧码 / {len(res.spk_emb)} 维嵌入）")
        finally:
            stream.shutdown()
        return anchor_json

    # ----------------------------------------------------------------- 合成
    def prepared_voice(self, anchor_json: Path, log: Callable[[str], None]) -> Any:
        """载入锚点并「准备好」（含解码器记忆对齐），结果缓存。

        为什么值得缓存：把锚点交给引擎时，引擎会**解码整段参考音频**来对齐
        流式解码器的记忆（67 秒参考 ≈ 35 秒 CPU）。这一趟每轮都付就很亏，
        而 prepared 对象带 final_state，缓存住就能跳过后续的重解码。
        锚点文件变了（mtime 变）自动失效。
        """
        key = str(anchor_json)
        stamp = anchor_json.stat().st_mtime
        cached = self._voice_cache.get(key)
        if cached is not None and cached[0] == stamp:
            log("  音色锚点已在内存中准备好（跳过参考音频重解码）")
            return cached[1]

        engine = self.ensure_engine(log)
        stream = engine.create_stream()
        try:
            log("  准备音色锚点（首次需解码参考音频以对齐解码器记忆，约 30 秒）…")
            prepared = stream.set_voice(str(anchor_json))
        finally:
            stream.shutdown()
        if not prepared:
            raise _provider_error("无法载入音色锚点（anchor.json 损坏？请重录音色）")
        self._voice_cache[key] = (stamp, prepared)
        return prepared

    def synthesize(self, anchor_json: Path, items: list[dict[str, Any]],
                   out_dir: Path, log: Callable[[str], None]) -> list[Path]:
        """逐句克隆合成。一条视频共用一个流，每句单独出 wav（字幕好对齐）。"""
        out_dir.mkdir(parents=True, exist_ok=True)
        engine = self.ensure_engine(log)
        prepared = self.prepared_voice(anchor_json, log)
        cfg = self.config.provider_cfg("local_qwen_tts") or {}
        language = str(cfg.get("language") or "chinese")
        zero_shot = bool(cfg.get("zero_shot", True))

        from qwen3_tts_gguf.inference import TTSConfig  # noqa: PLC0415
        tts_cfg = TTSConfig(
            temperature=float(cfg.get("temperature", 0.8)),
            sub_temperature=float(cfg.get("sub_temperature", 0.8)),
            max_steps=int(cfg.get("max_steps", 400)),
            streaming=True,
        )

        stream = engine.create_stream()
        try:
            # 传入已准备好的对象（带 final_state），避免再解一遍参考音频
            if not stream.set_voice(prepared):
                raise _provider_error("无法载入音色锚点（anchor.json 损坏？请重录音色）")
            parts: list[Path] = []
            for index, item in enumerate(items):
                text = str(item.get("text") or "").strip()
                if not text:
                    parts.append(_silent_wav(out_dir, index))
                    continue
                log(f"  [{index + 1}/{len(items)}] 合成：{text[:26]}…")
                result = stream.clone(text=text, language=language,
                                      zero_shot=zero_shot, config=tts_cfg)
                if result is None:
                    raise _provider_error(f"第 {index + 1} 句合成失败：{text[:40]}")
                wav = out_dir / f"utt_{index:02d}.wav"
                result.save(str(wav))
                if not wav.exists() or wav.stat().st_size == 0:
                    raise _provider_error(f"第 {index + 1} 句没有产出音频：{text[:40]}")
                parts.append(wav)
            stream.join()
            return parts
        finally:
            stream.shutdown()


def _silent_wav(out_dir: Path, index: int) -> Path:
    """空文本的占位 wav（保持字幕时间轴，不因为一句话为空就崩）。"""
    import numpy as np
    import soundfile as sf

    wav = out_dir / f"utt_{index:02d}.wav"
    sf.write(wav, np.zeros(2400, dtype=np.float32), 24000)
    return wav
