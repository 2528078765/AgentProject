"""LangGraph 编排（自包含实现）。

设计原则
--------
这是一个**独立、完整的 LangGraph 应用**：每个节点里放的是真实业务逻辑，
不经过 `pipeline.py` 的 Runner、不经过 `steps.py`、也不依赖 manifest 那套
Artifact 缓存。

复用的是**能力层**（抽象得比较干净，没必要重写）：
  * `providers`  —— TTS / 出图 / 数字人 / LLM 改写（可插拔）
  * `media`      —— ffmpeg 封装、ASS 字幕、断句与气口、媒体体检
  * `assets`     —— 音色与形象资产库
  * `comfy`      —— ComfyUI 客户端

图的形状
--------
    START
      │
      ▼
    preflight ──(缺东西)──> fail ──> END
      │
      └──(齐全)──> voice_clone ──> script ──> tts ──> visuals ──> avatar
                                                                   │
                       END <── metadata <── compose <── subtitles

闸门（可选，默认关闭，`--gates` 打开）：
    gate_script / gate_voice
打回会沿条件边回到上一步形成循环，`revision` 递增让内容真正重新生成。

为什么用 checkpointer
--------------------
`SqliteSaver` 让「跑到一半中断 → 关掉进程 → 明天继续」成为可能。
这是这套编排相对自研引擎的核心增量价值。
"""

from __future__ import annotations

import json
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypedDict

from . import media as M
from . import provider_caps as CAP
from . import providers as P
from .assets import AssetStore
from .config import Config
from .errors import AutoVidError

DEFAULT_GATES: tuple[str, ...] = ()
GATE_TITLES = {"script": "文案确认", "voice": "试听音色"}

# 图里会被执行到的业务节点（供 Web 端画进度条用），顺序即执行顺序
#
# 注意：**没有「背景图」节点**。背景来自本次拍摄的场景照片 ——
# 形象库存一次身份，每次生成时现拍一张带人物的照片，照片里的环境就是背景，
# 所以背景天然每次都不同，不需要再生成。
FLOW_NODES: list[str] = ["preflight", "voice_clone", "script", "tts",
                         "avatar", "subtitles", "compose", "metadata"]

NODE_TITLES: dict[str, str] = {
    "preflight": "前置判断",
    "voice_clone": "音色克隆",
    "script": "口播稿",
    "tts": "语音合成",
    "avatar": "数字人",
    "subtitles": "字幕",
    "compose": "合成成片",
    "metadata": "标题标签",
    "fail": "终止（说明原因）",
    "gate_script": "闸门·文案确认",
    "gate_voice": "闸门·试听音色",
}


# --------------------------------------------------------------------------- #
# 依赖
# --------------------------------------------------------------------------- #
def require_langgraph():
    try:
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import Command, interrupt
    except ImportError as exc:  # pragma: no cover
        raise AutoVidError(
            "没有安装 langgraph。这台机器 pip 装不了包，用项目自带的 vendor 脚本：\n"
            "    python scripts/vendor_deps.py langgraph\n"
            "    python scripts/vendor_deps.py langgraph-checkpoint-sqlite\n"
            f"  （原始错误：{exc}）"
        ) from exc
    return StateGraph, START, END, InMemorySaver, Command, interrupt


# --------------------------------------------------------------------------- #
# 状态
#
# 全部字段都必须是可序列化的（checkpointer 要把它写进 SQLite），
# 所以路径统一用字符串，不放 Path 对象。
# --------------------------------------------------------------------------- #
class FlowState(TypedDict, total=False):
    # ---- 输入 ----
    run_id: str
    run_dir: str
    topic: str
    script_text: str
    script_file: str
    voice_id: str
    avatar_id: str
    # 本次拍摄的场景照片（带人物）—— 它提供画面与背景
    scene_photo: str

    # ---- 前置检查 ----
    errors: list[str]
    warnings: list[str]
    preflight_info: dict[str, Any]
    failure: str

    # ---- 节点产物 ----
    voice_profile: dict[str, Any]   # 音色克隆结果
    script: dict[str, Any]          # 口播稿
    voice: dict[str, Any]           # 语音 + 时间轴 + 气口
    visuals: dict[str, Any]         # 背景图
    avatar: dict[str, Any]          # 人物片段
    subtitles: dict[str, Any]       # 字幕文件
    video: dict[str, Any]           # 成片与封面
    metadata: dict[str, Any]        # 标题 / 标签

    # ---- 编排 ----
    trace: list[dict[str, Any]]
    approvals: dict[str, Any]
    revision: int


# --------------------------------------------------------------------------- #
# 主体
# --------------------------------------------------------------------------- #
class VideoFlow:
    def __init__(
        self,
        config: Config,
        log: Callable[[str], None] = print,
        gates: tuple[str, ...] | list[str] = DEFAULT_GATES,
        emitter: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.config = config
        self.log = log
        self.gates = tuple(gates or ())
        self.emitter = emitter
        self._compiled = None
        self._checkpointer = None
        self.checkpointer_kind = "unknown"

    # ================================================================ 基础设施
    def _emit(self, event: dict[str, Any]) -> None:
        if self.emitter is None:
            return
        try:
            self.emitter(event)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _dir(state: FlowState, name: str) -> Path:
        target = Path(str(state["run_dir"])) / name
        target.mkdir(parents=True, exist_ok=True)
        return target

    @staticmethod
    def _grow(state: FlowState, name: str, status: str, **extra: Any) -> list[dict[str, Any]]:
        entry = {"node": name, "status": status, **extra}
        return list(state.get("trace") or []) + [entry]

    def _write(self, state: FlowState, name: str, filename: str,
               payload: dict[str, Any]) -> str:
        path = self._dir(state, name) / filename
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)

    def checkpointer(self):
        """优先 SQLite：进程退出后还能接着跑。"""
        if self._checkpointer is not None:
            return self._checkpointer
        _, _, _, InMemorySaver, _, _ = require_langgraph()
        try:
            import sqlite3

            from langgraph.checkpoint.sqlite import SqliteSaver

            root = self.config.path(self.config.get("project.out_dir", "runs")) / "_graph"
            root.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(root / "flow.sqlite"), check_same_thread=False)
            self._checkpointer = SqliteSaver(connection)
            self.checkpointer_kind = "sqlite"
        except Exception as exc:  # noqa: BLE001
            self.log(f"  SQLite 检查点不可用（{type(exc).__name__}），退回内存检查点")
            self._checkpointer = InMemorySaver()
            self.checkpointer_kind = "memory"
        return self._checkpointer

    # ================================================================ 1. 前置判断
    def node_preflight(self, state: FlowState) -> dict[str, Any]:
        """判断必备条件是否都齐了。缺任何一项都不往下走。"""
        errors: list[str] = []
        warnings: list[str] = []
        info: dict[str, Any] = {}
        store = AssetStore(self.config)

        # ---- 文案 ----
        topic = str(state.get("topic") or "").strip()
        script_text = str(state.get("script_text") or "").strip()
        script_file = str(state.get("script_file") or "").strip()
        if script_file and not Path(script_file).exists():
            errors.append(f"文案文件不存在：{script_file}")
        elif not (topic or script_text or script_file):
            errors.append("文案没准备好：既没有给选题，也没有给文案内容或文案文件")
        else:
            info["文案来源"] = ("文案文件" if script_file
                               else "自有文案" if script_text else f"选题「{topic}」")

        # ---- 音色 ----
        voice_id = str(state.get("voice_id") or "").strip()
        if not voice_id:
            errors.append("音色没准备好：没有选择要克隆的音色")
        else:
            voice_asset = store.get_voice(voice_id)
            if voice_asset is None:
                errors.append(f"音色 {voice_id} 在资产库里不存在")
            elif not voice_asset.ref_audio:
                errors.append(f"音色「{voice_asset.name}」还没有参考音频，请先录音或上传")
            elif store.voice_reference(voice_id) is None:
                errors.append(f"音色「{voice_asset.name}」的参考音频文件已丢失")
            else:
                info["音色"] = f"{voice_asset.name}（{voice_asset.duration_s}s 参考音频）"

        # ---- 形象 ----
        avatar_id = str(state.get("avatar_id") or "").strip()
        if not avatar_id:
            errors.append("形象没准备好：没有选择形象参考图")
        else:
            avatar_asset = store.get_avatar(avatar_id)
            if avatar_asset is None:
                errors.append(f"形象 {avatar_id} 在资产库里不存在")
            elif not avatar_asset.photos:
                errors.append(f"形象「{avatar_asset.name}」还没有上传照片")
            elif store.avatar_portrait(avatar_id) is None:
                errors.append(f"形象「{avatar_asset.name}」的主图文件已丢失")
            else:
                info["形象"] = f"{avatar_asset.name}（{len(avatar_asset.photos)} 张照片，身份参考）"

        # ---- 本次场景照片 ----
        # 这张照片提供画面和背景：形象库存的是「身份」，这里存的是「今天在哪拍」。
        scene = str(state.get("scene_photo") or "").strip()
        if not scene:
            errors.append(
                "场景照片没准备好：需要一张本次拍摄的、带人物的照片"
                "（它会成为视频的画面与背景）")
        elif not Path(scene).exists():
            errors.append(f"场景照片文件不存在：{scene}")
        else:
            probe = M.probe_media(scene)
            if not probe.get("has_video"):
                errors.append(
                    f"场景照片不是有效图片：{probe.get('error') or '无法解码'}")
            elif min(int(probe.get("width") or 0), int(probe.get("height") or 0)) < 256:
                errors.append(
                    f"场景照片分辨率太低（{probe.get('width')}×{probe.get('height')}），"
                    "最短边至少 256px")
            else:
                info["场景照片"] = (f"{Path(scene).name}"
                                    f"（{probe.get('width')}×{probe.get('height')}）")

        # ---- 引擎可用性：严格模式下不能“先跑再偷偷换一家” ----
        status_by_name = {item.name: item for item in P.provider_statuses(self.config)}
        tts = str(self.config.provider_of("voice"))
        tts_status = status_by_name.get(tts)
        if tts_status is None:
            errors.append(f"语音接口「{tts}」不存在或已停用")
        elif not tts_status.available:
            errors.append(f"语音接口「{tts}」不可用：{tts_status.detail}")
        # 注意：判断「会不会克隆」必须走 providers.can_clone()，不能直接查
        # _CLONE_CAPABLE —— 那个集合只有内置引擎，用户在设置里配的
        # voice:xxx 会被误判成「不支持克隆」，报出与实际相反的警告
        # （实测踩过：硅基流动明明克隆成功，前置判断却说不会是你的音色）。
        if voice_id and not P.can_clone(tts, self.config):
            errors.append(f"语音接口「{tts}」不支持音色克隆，已停止生成")
        if tts == "http_json" and not self.config.provider_cfg("http_tts").get("url"):
            errors.append("语音接口 http_json 没配 providers.http_tts.url")
        avatar_engine = str(self.config.provider_of("avatar"))
        avatar_status = status_by_name.get(avatar_engine)
        if avatar_status is None:
            errors.append(f"数字人接口「{avatar_engine}」不存在或已停用")
        elif not avatar_status.available:
            errors.append(f"数字人接口「{avatar_engine}」不可用：{avatar_status.detail}")
        if avatar_engine == "comfy" and not self.config.provider_cfg("comfy").get(
                "avatar_workflow"):
            errors.append("数字人接口 comfy 没配 providers.comfy.avatar_workflow")
        if avatar_engine == "still":
            errors.append("数字人接口 still 不会生成人物动作，已停止生成")

        # ---- 分辨率与能力协商（**在花钱之前**说清楚）----
        # 这一步的价值在于「先知道再付钱」：D-ID 的照片数字人只能出 512×512，
        # 拉到 1080×1920 是放大约 7.9 倍。等跑完再发现画面糊，钱和时间都已经花了。
        platform = self.config.platform
        want_w = int(platform.get("width", 1080))
        want_h = int(platform.get("height", 1920))
        want_fps = int(platform.get("fps", 30))
        spec = P.avatar_output_spec(self.config, avatar_engine, want_w, want_h, want_fps)
        info["数字人输出规格"] = spec.describe()
        _, _, notes = CAP.negotiate_canvas(want_w, want_h, spec)
        warnings.extend(notes)
        if spec.declared and not spec.verified:
            warnings.append(
                f"「{avatar_engine}」的输出规格来自文档而非实测"
                f"（{spec.source or '来源未知'}）—— 首次使用请先单跑数字人这一步验证")
        if not spec.declared and spec.note:
            warnings.append(
                f"「{avatar_engine}」没有声明输出规格：成片分辨率无法在开跑前保证，"
                f"要等它返回才知道。{spec.note}")

        img_limits = P.avatar_image_limits(self.config, avatar_engine)
        if img_limits.min_side or img_limits.max_pixels or img_limits.formats:
            info["图片来源要求"] = img_limits.describe()

        aud_limits = P.avatar_audio_limits(self.config, avatar_engine)
        if aud_limits.max_s or aud_limits.max_mb:
            info["音频限制"] = aud_limits.describe()

        # ---- 输出目录可写 ----
        try:
            probe = self._dir(state, "preflight") / ".probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            errors.append(f"输出目录不可写：{exc}")

        self.log("【前置判断】" + ("全部通过" if not errors else f"发现 {len(errors)} 个问题"))
        for key, value in info.items():
            self.log(f"    ✓ {key}：{value}")
        for item in warnings:
            self.log(f"    ⚠ {item}")
        for item in errors:
            self.log(f"    ✗ {item}")
        self._emit({"type": "preflight", "errors": errors, "warnings": warnings, "info": info})
        return {"errors": errors, "warnings": warnings, "preflight_info": info,
                "trace": self._grow(state, "preflight",
                                    "ok" if not errors else "failed")}

    @staticmethod
    def route_preflight(state: FlowState) -> str:
        return "fail" if state.get("errors") else "ok"

    def node_fail(self, state: FlowState) -> dict[str, Any]:
        """说明错误原因，然后走向 END。"""
        errors = list(state.get("errors") or [])
        lines = ["生成前的检查没通过，已中止："]
        lines += [f"  ✗ {item}" for item in errors]
        warnings = list(state.get("warnings") or [])
        if warnings:
            lines.append("（另有提醒）")
            lines += [f"  ⚠ {item}" for item in warnings]
        lines.append("")
        lines.append("怎么修：打开页面 http://127.0.0.1:8899/ ，在「音色库 / 形象库」里补上缺的东西并选中；")
        lines.append("        文案来源在「创作」页填选题，或粘贴自有文案。")
        message = "\n".join(lines)
        self.log("\n" + message)
        self._emit({"type": "flow_failed", "errors": errors, "warnings": warnings})
        return {"failure": message, "trace": self._grow(state, "fail", "failed")}

    # ================================================================ 2. 音色克隆
    def node_voice_clone(self, state: FlowState) -> dict[str, Any]:
        """把参考音频变成可用的音色（云端则注册克隆）。"""
        store = AssetStore(self.config)
        asset = store.get_voice(str(state["voice_id"]))
        if asset is None:
            raise AutoVidError(f"音色 {state.get('voice_id')} 不存在")
        profile = P.register_voice(self.config, asset, self.log)
        path = self._write(state, "voice", "voice_profile.json", profile)
        return {"voice_profile": {**profile, "profile_file": path},
                "trace": self._grow(state, "voice_clone",
                                    "cloned" if profile["cloned"] else "ready")}

    # ================================================================ 3. 文案
    def node_script(self, state: FlowState) -> dict[str, Any]:
        """产出结构化口播稿：有自有文案就用它，否则让 LLM/模板生成。"""
        topic = str(state.get("topic") or "").strip()
        source = str(state.get("script_text") or "")
        script_file = str(state.get("script_file") or "").strip()
        if script_file:
            source = Path(script_file).read_text(encoding="utf-8")
        revision = int(state.get("revision", 0) or 0)
        if revision:
            self.log(f"  第 {revision} 次修订：重新生成口播稿")

        cfg = self.config.provider_cfg("llm")
        temperature = float(cfg.get("temperature", 0.8))
        if revision:
            # 让修订真的产出不同内容，而不是原样再来一遍
            self.config = self.config.with_overrides(
                {"providers.llm.temperature": min(1.5, temperature + 0.1 * revision)})

        script = P.llm_rewrite(self.config, topic or "未命名选题", source, self.log)
        script["revision"] = revision
        path = self._write(state, "script", "script.json", script)
        self.log(f"  口播稿：{len(script['segments'])} 段 / "
                 f"{script['word_count']} 字 / 预估 {script['estimated_duration_s']}s")
        return {"script": {**script, "script_file": path},
                "trace": self._grow(state, "script", "ok",
                                    word_count=script["word_count"])}

    # ================================================================ 4. 语音合成
    @staticmethod
    def _merge_short_breaths(groups: list[tuple[str, str]],
                             min_chars: int = 8) -> list[tuple[str, str]]:
        """把太短的气口句并入下一句。

        为什么必须合并：实测硅基流动 CosyVoice2 对**很短**的句子返回
        HTTP 200 + 0 字节（'大家好，'、'今天用三分钟，' 全空；17 字正常）。
        一段视频几十个气口句里短句很多，第一个短句就会让整条链路回退。
        长度按「去掉标点后的字数」算；合并让字幕也更少碎片。
        """

        def weight(text: str) -> int:
            return len(re.sub(r"[\s，。！？；：、,.!?;:'\"“”‘’（）()]", "",
                              text or ""))

        merged: list[tuple[str, str]] = []
        carry = ""
        for text, ending in groups:
            combined = carry + text
            if weight(combined) < min_chars:
                carry = combined
                continue
            merged.append((combined, ending))
            carry = ""
        if carry:
            if merged:
                prev_text, prev_end = merged[-1]
                merged[-1] = (prev_text + carry, prev_end)
            else:
                merged.append((carry, groups[-1][1] if groups else ""))
        return merged

    @staticmethod
    def _plan_utterances(segments: list[dict], cfg: dict) -> list[dict[str, Any]]:
        """把每段口播切成「一口气说得完」的气口句，并给每句配停顿。

        停顿按标点分级（逗号短、句号长、段落最长），带固定种子的抖动，
        避免均匀停顿的机械感，同时保证时间轴可复现。
        太短的气口句会先合并（见 _merge_short_breaths）。
        """
        max_chars = int(cfg.get("breath_max_chars", 20))
        min_chars = max(6, int(cfg.get("breath_min_chars", 8)))
        jitter = max(0, int(cfg.get("pause_jitter_ms", 40)))
        rng = random.Random(str(cfg.get("pause_seed", "autovid")))
        segment_pause = int(cfg.get("pause_segment_ms", M.DEFAULT_PAUSES["segment"]))

        items: list[dict[str, Any]] = []
        for segment in segments:
            groups = VideoFlow._merge_short_breaths(
                M.split_into_breaths(str(segment["text"]), max_chars), min_chars)
            for index, (text, ending) in enumerate(groups):
                last = index == len(groups) - 1
                if last:
                    pause = segment_pause + (rng.randint(-jitter, jitter) if jitter else 0)
                else:
                    pause = M.pause_for_punctuation(ending, cfg, rng)
                items.append({
                    "segment_id": segment["id"],
                    "segment_index": segment["index"],
                    "text": text,
                    "ending": ending,
                    "pause_ms": max(0, pause),
                    "is_segment_end": last,
                })
        return items

    def node_tts(self, state: FlowState) -> dict[str, Any]:
        """逐句合成 -> 按标点停顿拼接 -> 得到精确时间轴。"""
        script = state["script"]
        segments = script["segments"]
        cfg = self.config.step_cfg("voice")
        out = self._dir(state, "voice")

        items = self._plan_utterances(segments, cfg)
        if not items:
            raise AutoVidError("断句后没有任何可合成的句子")
        log = self.log
        log(f"  断句：{len(segments)} 段 -> {len(items)} 个气口句"
            f"（每句最多 {cfg.get('breath_max_chars', 20)} 字）")

        asset = AssetStore(self.config).get_voice(str(state["voice_id"]))
        # 气口句放 utt/ 子目录：所有 provider 都按 seg_NN.wav 命名，
        # 和下面「每段一个文件」会同名互覆盖
        result = P.tts_synthesize(self.config, items, out / "utt", log, voice_asset=asset)

        by_segment: dict[str, list[int]] = {}
        for index, item in enumerate(items):
            by_segment.setdefault(item["segment_id"], []).append(index)

        utterances: list[dict[str, Any]] = []
        segment_wavs: list[Path] = []
        metas: list[dict[str, Any]] = []
        for segment in segments:
            indexes = by_segment.get(segment["id"], [])
            if not indexes:
                continue
            wav = out / f"seg_{segment['index']:02d}.wav"
            relative = M.concat_wav_pcm(
                [result.parts[i] for i in indexes], wav,
                gaps_ms=[items[i]["pause_ms"] for i in indexes], trailing_gap=True)
            segment_wavs.append(wav)
            metas.append({"segment": segment, "indexes": indexes, "relative": relative})

        full = out / "voice.wav"
        whole = M.concat_wav_pcm(segment_wavs, full, gap_ms=0, trailing_gap=False)

        merged: list[dict[str, Any]] = []
        for meta_index, (meta, span) in enumerate(zip(metas, whole)):
            offset = span["start"]
            for i, timing in zip(meta["indexes"], meta["relative"]):
                item = items[i]
                utterances.append({
                    "segment_id": item["segment_id"],
                    "text": item["text"],
                    "ending": item["ending"],
                    "pause_ms": item["pause_ms"],
                    "start": round(offset + timing["start"], 4),
                    "end": round(offset + timing["end"], 4),
                    "duration": round(timing["duration"], 4),
                })
            segment = meta["segment"]
            merged.append({
                "id": segment["id"], "index": segment["index"],
                "headline": segment["headline"], "text": segment["text"],
                "start": round(span["start"], 4), "end": round(span["end"], 4),
                "duration": round(span["duration"], 4),
                "clip_duration": round(span["duration"], 4),
                "audio_file": str(segment_wavs[meta_index]),
            })

        payload = {
            "provider": result.provider,
            "note": result.note,
            "cloned": bool(result.cloned),
            "voice_id": result.voice_id,
            "voice_name": result.voice_name,
            "total_duration_s": round(M.wav_duration(full), 3),
            "wav": str(full),
            "utterances": utterances,
            "segments": merged,
        }
        path = self._write(state, "voice", "voice.json", payload)
        log(f"  语音完成：provider={result.provider}，"
            f"{payload['total_duration_s']}s，{len(merged)} 段 / {len(utterances)} 个气口句")
        if not result.cloned and asset is not None:
            log(f"  ⚠ 本次没有用上你的音色「{asset.name}」")
        return {"voice": {**payload, "voice_file": path},
                "trace": self._grow(state, "tts", "ok",
                                    provider=result.provider, cloned=bool(result.cloned))}

    # ================================================================ 5. 数字人
    def node_avatar(self, state: FlowState) -> dict[str, Any]:
        """把「本次场景照片」交给数字人引擎，配上语音生成口播片段。

        画面与背景来自场景照片本身（拍照时的真实环境），
        形象资产提供身份参考（ComfyUI 工作流可以用它做身份一致）。
        """
        platform = self.config.platform
        width = int(platform.get("width", 1080))
        height = int(platform.get("height", 1920))
        fps = int(platform.get("fps", 30))
        out = self._dir(state, "avatar")

        scene = Path(str(state["scene_photo"]))
        if not scene.exists():
            raise AutoVidError(f"场景照片丢失：{scene}")

        store = AssetStore(self.config)
        avatar_id = str(state.get("avatar_id") or "")
        identity = store.avatar_portrait(avatar_id) if avatar_id else None

        segments = [
            {**segment, "audio_path": segment["audio_file"]}
            for segment in state["voice"]["segments"]
        ]
        # visuals 参数就是「画面底图」：只有场景照片这一张（每个片段都用它）
        clips, provider = P.avatar_render(
            self.config, segments, [scene], out, width, height, fps, self.log,
            portrait=identity)

        # 分辨率协商：这家实际能出什么规格，和我们要的 1080×1920 差多少。
        # 差就说清楚，让下游（和用户）知道成片是放大来的，别以为是原生高清。
        spec = P.avatar_output_spec(self.config, provider, width, height, fps)
        canvas_w, canvas_h, output_notes = CAP.negotiate_canvas(width, height, spec)
        self.log(f"  该引擎输出规格：{spec.describe()}")
        for note in output_notes:
            self.log(f"  ⚠ {note}")

        payload = {
            "provider": provider,
            "scene_photo": str(scene),
            "avatar_id": avatar_id or None,
            "identity_photo": str(identity) if identity else None,
            "used_portrait": identity is not None,
            "clips": [str(c) for c in clips],
            "output_spec": {"width": spec.width, "height": spec.height,
                            "fps": spec.fps, "aspect": spec.aspect,
                            "note": spec.note},
            "canvas": {"width": canvas_w, "height": canvas_h},
            "output_notes": output_notes,
        }
        path = self._write(state, "avatar", "avatar.json", payload)
        self.log(f"  画面来自场景照片：{scene.name}")
        return {"avatar": {**payload, "avatar_file": path},
                "trace": self._grow(state, "avatar", "ok", provider=provider)}

    # ================================================================ 7. 字幕
    def node_subtitles(self, state: FlowState) -> dict[str, Any]:
        cfg = self.config.step_cfg("subtitles")
        platform = self.config.platform
        width = int(platform.get("width", 1080))
        height = int(platform.get("height", 1920))
        out = self._dir(state, "subtitles")
        script = state["script"]
        voice = state["voice"]

        font = str(cfg.get("font", "Microsoft YaHei"))
        main_style = M.make_style(
            "Main", font, int(cfg.get("font_size", 76)),
            primary=str(cfg.get("primary", "&H00FFFFFF")),
            outline_colour=str(cfg.get("outline_colour", "&H00202020")),
            back_colour=str(cfg.get("back_colour", "&H80000000")),
            bold=bool(cfg.get("bold", True)),
            outline=int(cfg.get("outline", 5)),
            shadow=int(cfg.get("shadow", 2)),
            alignment=2, margin_v=int(cfg.get("margin_v", 360)))
        keyword_style = M.make_style(
            "Keyword", font, int(cfg.get("keyword_size", 88)),
            primary=str(cfg.get("keyword_colour", "&H0000E5FF")),
            outline_colour=str(cfg.get("outline_colour", "&H00202020")),
            back_colour="&H80000000",
            bold=True, outline=int(cfg.get("outline", 5)) + 1, shadow=2,
            alignment=8, margin_v=int(cfg.get("keyword_margin_v", 210)))

        max_chars = int(cfg.get("max_chars_per_line", 13))
        events: list[dict[str, Any]] = []
        segments = voice["segments"]

        hook = str(script.get("hook") or "").strip()
        if bool(cfg.get("show_hook", True)) and hook:
            head = hook.split("，")[0].split("。")[0][:9]
            events.append({"style": "Keyword", "start": 0.0,
                           "end": min(2.4, segments[0]["end"] if segments else 2.4),
                           "text": head, "max_chars": 10})

        for index, segment in enumerate(segments):
            headline = str(segment["headline"]).split("，")[0][:9]
            start, end = float(segment["start"]), float(segment["end"])
            if index == 0 and events:
                if headline[:6] == str(events[0]["text"])[:6]:
                    continue                       # 和开场标题重复，不重复上屏
                start = max(start, float(events[0]["end"]))
            if headline and start < end:
                events.append({"style": "Keyword", "start": round(start, 3),
                               "end": round(end, 3), "text": headline, "max_chars": 10})

        for item in voice["utterances"]:
            events.append({"style": "Main", "start": round(float(item["start"]), 3),
                           "end": round(float(item["end"]), 3),
                           "text": str(item["text"]), "max_chars": max_chars})

        ass = out / "subtitles.ass"
        M.write_ass(ass, M.build_ass([main_style, keyword_style], events, width, height,
                                     title=str(script.get("topic", "AutoVid"))))
        payload = {"file": str(ass), "events": len(events),
                   "utterances": len(voice["utterances"])}
        self.log(f"  字幕完成：{len(events)} 条事件（一条正文 = 一口气）")
        return {"subtitles": payload,
                "trace": self._grow(state, "subtitles", "ok", events=len(events))}

    # ================================================================ 8. 合成
    def node_compose(self, state: FlowState) -> dict[str, Any]:
        platform = self.config.platform
        width = int(platform.get("width", 1080))
        height = int(platform.get("height", 1920))
        out = self._dir(state, "compose")
        cfg = self.config.step_cfg("subtitles")

        clips = [Path(c) for c in state["avatar"]["clips"]]
        voice_wav = Path(state["voice"]["wav"])
        ass = Path(state["subtitles"]["file"])

        # 尺寸/帧率对齐：云端数字人（实测 D-ID）回的是 512×512 @25fps，
        # 而正片是 1080×1920 @30fps。流拷贝拼接要求每段参数完全一致，
        # 否则拼出来是花屏或时长错乱。先探测，不一致才逐个适配画布
        # （已经一致的不重编码）。
        fps = int(platform.get("fps", 30))
        specs = {M.probe_video_spec(c) for c in clips}
        if specs - {(width, height, float(fps))}:
            self.log(f"  片段规格不一致 {sorted(specs)}，先适配到 {width}×{height}@{fps}fps…")
            # 厂商能力声明：做不到就如实说，不假装成片是原生高清
            for note in state["avatar"].get("output_notes") or []:
                self.log(f"  ⚠ {note}")
            fixed: list[Path] = []
            for index, clip in enumerate(clips):
                # 注意用返回值：已经完全一致的片段会原样返回，不会生成新文件
                fixed.append(M.fit_to_canvas(clip, out / f"canvas_{index:02d}.mp4",
                                             width, height, fps, cwd=out))
            clips = fixed

        self.log("  拼接视频轨…")
        track = M.concat_clips(clips, out / "vtrack.mp4", cwd=out)
        self.log("  合并音频轨…")
        merged = M.mux_audio(track, voice_wav, out / "av.mp4", cwd=out,
                             audio_bitrate=str(platform.get("audio_bitrate", "192k")))
        self.log("  烧录字幕…")
        final = M.burn_subtitles(merged, ass, out / "final.mp4", cwd=out,
                                 crf=int(platform.get("crf", 20)))
        self.log("  渲染封面…")
        title = (state["script"].get("title_options") or [state["script"].get("topic", "")])[0]
        font = str(cfg.get("font", "Microsoft YaHei"))

        def cover(height_px: int, name: str) -> Path:
            style = M.make_style("Cover", font, int(cfg.get("cover_size", 110)),
                                 primary="&H00FFFFFF", outline_colour="&H00202020",
                                 bold=True, outline=7, shadow=3, alignment=5,
                                 margin_l=90, margin_r=90, margin_v=0)
            style_file = M.write_ass(
                out / f"{name}.ass",
                M.build_ass([style], [{"style": "Cover", "start": 0.0, "end": 3.0,
                                       "text": str(title), "max_chars": 11}],
                            width, height_px, title=name))
            return M.render_card(clips[0], style_file, out / name, width, height_px, cwd=out)

        cover_main = cover(height, "cover.jpg")
        cover_34 = cover(int(width * 4 / 3), "cover_3x4.jpg")
        duration = M.probe_duration(final)
        payload = {
            "video": str(final), "cover": str(cover_main), "cover_3x4": str(cover_34),
            "duration_s": round(duration, 3),
            "size_mb": round(final.stat().st_size / 1024 / 1024, 2),
        }
        self.log(f"  成片完成：{payload['duration_s']}s，{payload['size_mb']} MB")
        return {"video": payload,
                "trace": self._grow(state, "compose", "ok",
                                    duration_s=payload["duration_s"])}

    # ================================================================ 9. 元数据
    def node_metadata(self, state: FlowState) -> dict[str, Any]:
        script = state["script"]
        cfg = self.config.step_cfg("metadata")
        titles = list(script.get("title_options") or [script.get("topic", "")])
        tags = list(script.get("tags") or [])
        description = str(script.get("description", ""))
        provider = str(cfg.get("provider", "offline"))

        if provider == "llm":
            try:
                refined = self._refine_metadata(script, int(cfg.get("title_count", 3)),
                                                int(cfg.get("tag_count", 6)))
                titles = refined.get("titles") or titles
                tags = refined.get("tags") or tags
                description = refined.get("description") or description
                self.log("  标题/标签由 LLM 生成")
            except Exception as exc:  # noqa: BLE001
                self.log(f"  ⚠ LLM 生成元数据失败，用脚本自带：{str(exc).splitlines()[0][:80]}")

        payload = {
            "titles": [t for t in titles if t][:int(cfg.get("title_count", 3))],
            "tags": [str(t).lstrip("#") for t in tags if t][:int(cfg.get("tag_count", 6))],
            "description": description,
            "cta": script.get("cta", ""),
            "topic": script.get("topic", ""),
        }
        path = self._write(state, "metadata", "metadata.json", payload)
        return {"metadata": {**payload, "metadata_file": path},
                "trace": self._grow(state, "metadata", "ok")}

    def _refine_metadata(self, script: dict, title_count: int, tag_count: int) -> dict:
        cfg = self.config.provider_cfg("llm")
        base = str(cfg.get("base_url", "")).rstrip("/")
        key = self.config.secret_for("llm", "AUTOVID_LLM_API_KEY")
        payload = {
            "model": cfg.get("model", "deepseek-chat"),
            "temperature": 0.9,
            "messages": [
                {"role": "system", "content": (
                    "你是抖音标题优化师。标题≤30个汉字、有钩子、不夸大不承诺收益。"
                    "只输出 JSON：{\"titles\":[...],\"tags\":[...],\"description\":\"...\"}，"
                    "tags 不带 # 号，4-8 个。")},
                {"role": "user", "content": json.dumps(
                    {"topic": script.get("topic"), "hook": script.get("hook"),
                     "segments": [s["text"] for s in script.get("segments", [])]},
                    ensure_ascii=False)},
            ],
        }
        data = P._http(f"{base}/chat/completions", payload,  # noqa: SLF001
                       headers=P._auth_header(key), timeout_s=int(cfg.get("timeout_s", 120)))
        parsed = P._extract_json(data["choices"][0]["message"]["content"])  # noqa: SLF001
        return {
            "titles": [str(t)[:30] for t in parsed.get("titles", [])][:title_count],
            "tags": [str(t).lstrip("#") for t in parsed.get("tags", [])][:tag_count],
            "description": str(parsed.get("description", "")),
        }

    # ================================================================ 闸门
    def _make_gate(self, name: str):
        title = GATE_TITLES[name]

        def node(state: FlowState) -> dict[str, Any]:
            approvals = dict(state.get("approvals") or {})
            if name not in self.gates:
                approvals[name] = {"action": "approve", "auto": True}
                return {"approvals": approvals,
                        "trace": self._grow(state, f"gate_{name}", "auto")}
            _, _, _, _, _, interrupt = require_langgraph()
            payload: dict[str, Any] = {"gate": name, "title": title}
            if name == "script":
                payload["hook"] = state["script"].get("hook")
                payload["segments"] = [s["text"] for s in state["script"]["segments"]]
            elif name == "voice":
                payload["provider"] = state["voice"].get("provider")
                payload["cloned"] = state["voice"].get("cloned")
                payload["duration_s"] = state["voice"].get("total_duration_s")
                payload["wav"] = state["voice"].get("wav")
            self._emit({"type": "gate", "gate": name, "title": title, "payload": payload})
            decision = interrupt(payload)
            if isinstance(decision, str):
                decision = {"action": decision}
            action = str((decision or {}).get("action", "approve")).lower()
            approvals[name] = {"action": action, "auto": False}
            revision = int(state.get("revision", 0) or 0)
            if action == "reject":
                revision += 1
                self.log(f"【{title}】打回重做（第 {revision} 次修订）")
            else:
                self.log(f"【{title}】通过")
            return {"approvals": approvals, "revision": revision,
                    "trace": self._grow(state, f"gate_{name}", action)}
        node.__name__ = f"gate_{name}"
        return node

    @staticmethod
    def _gate_route(name: str):
        def route(state: FlowState) -> str:
            decision = (state.get("approvals") or {}).get(name) or {}
            return "reject" if str(decision.get("action", "")).lower() == "reject" else "ok"
        return route

    # ================================================================ 组装
    def _wrapped(self, name: str):
        """给节点套一层进度事件，Web 端就能实时亮灯。

        包裹而不是在节点内部到处 emit，是为了让业务逻辑保持干净 ——
        节点只管干活，进度是编排层的事。
        """
        title = NODE_TITLES.get(name, name)
        index = FLOW_NODES.index(name) + 1 if name in FLOW_NODES else 0
        total = len(FLOW_NODES)
        inner = getattr(self, f"node_{name}")

        def run(state: FlowState) -> dict[str, Any]:
            import time as _time
            self._emit({"type": "step_start", "step": name, "title": title,
                        "index": index, "total": total})
            started = _time.perf_counter()
            try:
                out = inner(state)
            except Exception as exc:  # noqa: BLE001
                self._emit({"type": "step_done", "step": name, "status": "failed",
                            "error": f"{type(exc).__name__}: {exc}",
                            "duration_s": round(_time.perf_counter() - started, 2)})
                raise
            status = "ok"
            if isinstance(out, dict) and out.get("trace"):
                raw = str(out["trace"][-1].get("status") or "ok")
                status = "failed" if raw == "failed" else "ok"
            self._emit({"type": "step_done", "step": name, "status": status,
                        "duration_s": round(_time.perf_counter() - started, 2)})
            return out

        run.__name__ = f"wrapped_{name}"
        return run

    def build(self):
        if self._compiled is not None:
            return self._compiled
        StateGraph, START, END, _mem, _cmd, _intr = require_langgraph()
        builder = StateGraph(FlowState)

        for name in FLOW_NODES:
            builder.add_node(name, self._wrapped(name))
        builder.add_node("fail", self._wrapped("fail"))
        for gate in GATE_TITLES:
            builder.add_node(f"gate_{gate}", self._make_gate(gate))

        builder.add_edge(START, "preflight")
        builder.add_conditional_edges("preflight", self.route_preflight,
                                      {"ok": "voice_clone", "fail": "fail"})
        builder.add_edge("fail", END)

        builder.add_edge("voice_clone", "script")
        builder.add_edge("script", "gate_script")
        builder.add_conditional_edges("gate_script", self._gate_route("script"),
                                      {"ok": "tts", "reject": "script"})
        builder.add_edge("tts", "gate_voice")
        builder.add_conditional_edges("gate_voice", self._gate_route("voice"),
                                      {"ok": "avatar", "reject": "tts"})
        builder.add_edge("avatar", "subtitles")
        builder.add_edge("subtitles", "compose")
        builder.add_edge("compose", "metadata")
        builder.add_edge("metadata", END)

        self._compiled = builder.compile(checkpointer=self.checkpointer())
        return self._compiled

    # ================================================================ 执行
    @staticmethod
    def new_run_dir(config: Config, slug: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = "".join(ch for ch in slug if ch not in '\\/:*?"<>|').strip()[:24] or "flow"
        target = config.path(config.get("project.out_dir", "runs")) / f"{stamp}-{safe}"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def run(self, inputs: dict[str, Any], thread_id: str) -> dict[str, Any]:
        graph = self.build()
        config = {"configurable": {"thread_id": thread_id}}
        result = graph.invoke(inputs, config)
        return {"thread_id": thread_id, "checkpointer": self.checkpointer_kind,
                "result": result, **summarize(result)}

    def resume(self, decision: Any, thread_id: str) -> dict[str, Any]:
        """用 `Command(resume=...)` 恢复 —— 直接传 dict 会被当成新输入，闸门会原地打转。"""
        graph = self.build()
        _, _, _, _, Command, _ = require_langgraph()
        config = {"configurable": {"thread_id": thread_id}}
        result = graph.invoke(Command(resume=decision), config)
        return {"thread_id": thread_id, "checkpointer": self.checkpointer_kind,
                "result": result, **summarize(result)}

    def continue_failed(self, thread_id: str) -> dict[str, Any]:
        """从异常节点继续。

        LangGraph 会在节点开始前保存检查点。节点抛异常时用同一 thread_id 并传入
        ``None``，只会重新执行失败节点及其后续节点，不会重跑已经完成的步骤。
        闸门恢复仍走 :meth:`resume`，两类恢复不能混用。
        """
        graph = self.build()
        config = {"configurable": {"thread_id": thread_id}}
        snapshot = graph.get_state(config)
        if not snapshot.values:
            raise AutoVidError("没有找到可恢复的流程检查点")
        if not snapshot.next:
            raise AutoVidError("这次运行没有可继续执行的失败节点")
        result = graph.invoke(None, config)
        return {"thread_id": thread_id, "checkpointer": self.checkpointer_kind,
                "result": result, **summarize(result)}

    def state_of(self, thread_id: str) -> dict[str, Any]:
        graph = self.build()
        snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
        return {"values": snapshot.values, "next": list(snapshot.next or [])}


# --------------------------------------------------------------------------- #
# 结果解读
# --------------------------------------------------------------------------- #
def summarize(result: dict[str, Any]) -> dict[str, Any]:
    interrupts = result.get("__interrupt__") or []
    if interrupts:
        value = getattr(interrupts[0], "value", None)
        return {"status": "interrupted",
                "gate": (value or {}).get("gate") if isinstance(value, dict) else None,
                "payload": value, "trace": result.get("trace") or []}
    if result.get("failure"):
        return {"status": "failed", "failure": result["failure"],
                "errors": result.get("errors") or [], "trace": result.get("trace") or []}
    return {"status": "finished", "trace": result.get("trace") or [],
            "video": (result.get("video") or {}).get("video"),
            "duration_s": (result.get("video") or {}).get("duration_s")}


def parse_decision(text: str) -> Any:
    """'approve' / 'reject' 直接返回；'script=reject,voice=approve' 返回字典。"""
    text = (text or "approve").strip()
    if "=" not in text:
        return {"action": text or "approve"}
    return {k.strip(): v.strip() or "approve"
            for k, _, v in (piece.partition("=") for piece in text.split(","))
            if k.strip()}
