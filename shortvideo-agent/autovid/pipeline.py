"""流水线引擎：步骤注册、依赖解析、缓存跳过、局部重跑。

核心机制：
  * 每个 step 声明 `requires` / `produces`，引擎据此组装 state、计算 input_hash。
  * input_hash = 步骤版本 + 相关配置 + 上游产物哈希 + CLI 输入。
  * 若该 step 上次成功、input_hash 未变、且产物文件都还在 -> 直接跳过（cached）。
  * `--only` / `--from` / `--force` 支持只重跑指定环节。
"""

from __future__ import annotations

import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from . import steps as S
from .errors import AutoVidError
from .manifest import (
    STATUS_CACHED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    RunContext,
    compute_input_hash,
)

StepFn = Callable[[RunContext, dict[str, Any], Callable[[str], None]], None]


@dataclass(frozen=True)
class StepDef:
    name: str
    title: str
    version: str
    requires: tuple[str, ...]
    produces: tuple[str, ...]
    config_keys: tuple[str, ...]
    fn: StepFn


STEPS: list[StepDef] = [
    StepDef("topic", "选题", "1", (), ("topic",), ("steps.topic",), S.step_topic),
    StepDef("script", "口播稿改写", "1", ("topic",), ("script",),
            ("steps.script", "providers.llm"), S.step_script),
    StepDef("voice", "音色克隆 / 语音合成", "2", ("script",), ("voice", "voice_segments"),
            ("steps.voice", "providers.edge_tts", "providers.http_tts", "project"),
            S.step_voice),
    StepDef("visuals", "背景图生成", "1", ("script",), ("visuals",),
            ("steps.visuals", "providers.comfy", "providers.openai_images", "platform"),
            S.step_visuals),
    StepDef("avatar", "数字人驱动", "3", ("script", "voice_segments", "visuals"), ("avatar",),
            ("steps.avatar", "providers.avatar_http", "providers.minimax_h3", "platform",
             "project"),
            S.step_avatar),
    StepDef("subtitles", "字幕生成", "1", ("script", "voice_segments"), ("subtitles",),
            ("steps.subtitles", "platform"), S.step_subtitles),
    StepDef("compose", "合成成片 + 封面", "2", ("script", "avatar", "voice", "subtitles"),
            ("video", "cover", "cover_3x4"),
            ("steps.compose", "steps.subtitles", "platform"), S.step_compose),
    StepDef("metadata", "标题 / 简介 / 标签", "1", ("script",), ("metadata",),
            ("steps.metadata", "providers.llm"), S.step_metadata),
    StepDef("publish", "生成发布包", "2",
            ("video", "cover", "cover_3x4", "metadata"), ("publish_report",),
            ("steps.publish",), S.step_publish),
]

STEP_NAMES = [s.name for s in STEPS]
_BY_NAME = {s.name: s for s in STEPS}


def get_step_def(name: str) -> StepDef:
    if name not in _BY_NAME:
        raise AutoVidError(f"未知步骤 '{name}'。可用：{', '.join(STEP_NAMES)}")
    return _BY_NAME[name]


# --------------------------------------------------------------------------- #
# 日志：同时打到终端和 run 目录下的文件
# --------------------------------------------------------------------------- #
class StepLogger:
    def __init__(self, step: str, log_dir: Path, emitter: Callable[[dict], None] | None = None):
        self.step = step
        self.log_dir = log_dir
        self.emitter = emitter
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / f"{step}.log"
        self._fh = self.path.open("w", encoding="utf-8")
        self.lines: list[str] = []

    def __call__(self, message: str) -> None:
        for line in str(message).splitlines() or [""]:
            text = f"  {line}"
            print(text, flush=True)
            self._fh.write(text + "\n")
            self.lines.append(text)
            if self.emitter is not None:
                self.emitter({"type": "step_log", "step": self.step, "line": line})
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def tail(self, count: int = 6) -> str:
        return "\n".join(self.lines[-count:])


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #
@dataclass
class RunPlan:
    to_run: list[str] = field(default_factory=list)
    to_skip: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)


class Runner:
    def __init__(self, ctx: RunContext, emitter: Callable[[dict[str, Any]], None] | None = None):
        self.ctx = ctx
        self.config = ctx.config
        # emitter 是可选的进度事件出口：CLI 不传（只打印），Web 端传入以推送 SSE。
        self.emitter = emitter
        out_dir = ctx.config.path(ctx.config.get("project.out_dir", "runs"))
        self.log_dir = out_dir / "_logs" / ctx.run_id

    def _emit(self, event: dict[str, Any]) -> None:
        """事件推送绝不能因为订阅方出错而影响流水线本身。"""
        if self.emitter is None:
            return
        try:
            self.emitter(event)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ hash
    def input_hash(self, step: StepDef) -> str:
        fingerprint = "|".join(
            f"{key}={self.config.fingerprint(key)}" for key in step.config_keys
        )
        upstream = self.ctx.upstream_hashes(list(step.requires))
        extra: dict[str, Any] = {}
        inputs = self.ctx.manifest.get("inputs", {})
        if step.name == "topic":
            extra["topic"] = inputs.get("topic", "")
        if step.name == "script":
            extra["script_file"] = inputs.get("script_file", "")
            extra["script_mtime"] = inputs.get("script_hash", "")
            # 人工审核打回重写时 revision 会 +1，靠它让这一步真正重新生成
            extra["revision"] = inputs.get("revision", 0)
        return compute_input_hash(step.version, fingerprint, upstream, extra)

    # ------------------------------------------------------------ 选择
    def _select(
        self,
        only: Sequence[str] | None,
        from_step: str | None,
        to_step: str | None,
    ) -> list[StepDef]:
        selected = list(STEPS)
        if from_step:
            start = STEP_NAMES.index(get_step_def(from_step).name)
            selected = [s for s in selected if STEP_NAMES.index(s.name) >= start]
        if to_step:
            end = STEP_NAMES.index(get_step_def(to_step).name)
            selected = [s for s in selected if STEP_NAMES.index(s.name) <= end]
        if only:
            wanted = {get_step_def(n).name for n in only}
            selected = [s for s in selected if s.name in wanted]
        return [s for s in selected if self.config.step_enabled(s.name)]

    def plan(
        self,
        only: Sequence[str] | None = None,
        force: Sequence[str] = (),
        from_step: str | None = None,
        to_step: str | None = None,
    ) -> RunPlan:
        """算出运行计划。

        关键点：**失效必须沿依赖链向下传播**。

        计划是在任何步骤执行之前一次性算出来的，此时上游产物还是旧的。如果只看
        缓存的 input_hash，就会出现「voice 因为换了 provider 要重跑，但 compose
        看到的是旧 voice.wav 的哈希、于是被判定为可复用」—— 结果成片里还是旧音频。
        所以这里按拓扑序推进：只要某个上游步骤要重跑，它的所有下游也一律重跑。
        """
        force_set = set(force)
        selected = self._select(only, from_step, to_step)
        selected_names = {s.name for s in selected}

        # 产物 key -> 产出它的步骤名（requires 里写的是产物 key，不是步骤名）
        producer: dict[str, str] = {}
        for step_def in STEPS:
            for key in step_def.produces:
                producer.setdefault(key, step_def.name)

        plan = RunPlan()
        dirty: set[str] = set()
        for step in selected:
            forced = "all" in force_set or step.name in force_set
            changed_upstream = list(dict.fromkeys(
                producer[key] for key in step.requires
                if producer.get(key) in dirty and producer.get(key) in selected_names
            ))
            if forced:
                plan.to_run.append(step.name)
                plan.reasons[step.name] = "强制重跑"
            elif changed_upstream:
                plan.to_run.append(step.name)
                plan.reasons[step.name] = f"上游已变更（{'、'.join(changed_upstream)}）"
            elif self.ctx.is_reusable(step.name, self.input_hash(step)):
                plan.to_skip.append(step.name)
                plan.reasons[step.name] = "输入未变，复用产物"
                continue
            else:
                plan.to_run.append(step.name)
                plan.reasons[step.name] = "首次运行或输入已变"
            dirty.add(step.name)
        return plan

    # ------------------------------------------------------------ 前置检查
    def preflight(self) -> list[str]:
        """生成前的硬性检查。

        数字人口播视频的前提就是「要克隆的音色」+「形象参考图」。
        缺了这两样，跑出来的根本不是用户要的东西 —— 所以宁可提前拒绝，
        也不要浪费几分钟算力产出一条没用的视频。
        """
        if not bool(self.config.get("project.require_assets", True)):
            return []
        from .assets import AssetStore

        store = AssetStore(self.config)
        problems: list[str] = []

        voice_id = str(self.config.get("project.voice_id", "") or "").strip()
        if not voice_id:
            problems.append("没有选择音色 —— 数字人口播必须指定要克隆的音色")
        else:
            voice = store.get_voice(voice_id)
            if voice is None:
                problems.append(f"音色 {voice_id} 在资产库里不存在")
            elif not voice.ref_audio:
                problems.append(f"音色「{voice.name}」还没有参考音频，请先录音或上传")

        avatar_id = str(self.config.get("project.avatar_id", "") or "").strip()
        if not avatar_id:
            problems.append("没有选择形象 —— 数字人口播必须指定形象参考图")
        else:
            avatar = store.get_avatar(avatar_id)
            if avatar is None:
                problems.append(f"形象 {avatar_id} 在资产库里不存在")
            elif not avatar.photos:
                problems.append(f"形象「{avatar.name}」还没有上传照片")
        return problems

    # ------------------------------------------------------------ 执行
    def run(
        self,
        only: Sequence[str] | None = None,
        force: Sequence[str] = (),
        from_step: str | None = None,
        to_step: str | None = None,
        dry_run: bool = False,
        keep_going: bool = False,
    ) -> bool:
        plan = self.plan(only, force, from_step, to_step)
        selected = self._select(only, from_step, to_step)
        total = len(selected)
        if total == 0:
            print("没有需要执行的步骤（检查 --only / enabled 配置）")
            return True

        # 前置检查：数字人口播必须有音色 + 形象，缺了就别浪费算力
        problems = self.preflight()
        if problems:
            message = (
                "生成前的检查没通过：\n  - " + "\n  - ".join(problems) +
                "\n\n请在页面的「音色库 / 形象库」里选好它们，"
                "或把 config/pipeline.json 的 project.require_assets 设为 false（不推荐）。"
            )
            if dry_run:
                print(f"\n⚠ {message}")
                self._emit({"type": "preflight", "problems": problems})
            else:
                self._emit({"type": "preflight", "problems": problems})
                raise AutoVidError(message)

        print(f"\n运行计划（{total} 步）")
        self._emit({
            "type": "plan",
            "run_id": self.ctx.run_id,
            "steps": [
                {
                    "name": s.name, "title": s.title, "index": i,
                    "action": "run" if s.name in plan.to_run else "skip",
                    "reason": plan.reasons[s.name],
                }
                for i, s in enumerate(selected, start=1)
            ],
        })
        for index, step in enumerate(selected, start=1):
            action = "执行" if step.name in plan.to_run else "跳过"
            print(f"  {index}. {step.name:<10} {step.title:<18} [{action}] {plan.reasons[step.name]}")
        print()

        if dry_run:
            print("--dry-run：仅显示计划，未执行任何步骤")
            self._emit({"type": "run_done", "ok": True, "dry_run": True,
                        "run_id": self.ctx.run_id})
            return True

        force_set = set(force)
        failures: list[str] = []

        for index, step in enumerate(selected, start=1):
            h = self.input_hash(step)
            if step.name in plan.to_skip and "all" not in force_set:
                rec = self.ctx.ensure_step(step.name, STEP_NAMES.index(step.name), step.version)
                rec.status = STATUS_CACHED
                rec.input_hash = h
                self.ctx.save()
                print(f"[{index}/{total}] {step.name} —— 跳过（复用已有产物）")
                self._emit({"type": "step_done", "step": step.name, "index": index,
                            "total": total, "status": "cached", "duration_s": 0,
                            "artifacts": []})
                continue

            print(f"\n[{index}/{total}] {step.name} —— {step.title}")
            self._emit({"type": "step_start", "step": step.name, "index": index,
                        "total": total, "title": step.title})
            logger = StepLogger(step.name, self.log_dir, self.emitter)
            self.ctx.start_step(step.name, STEP_NAMES.index(step.name), step.version, h)
            try:
                state = self.load_state(step.requires)
                step.fn(self.ctx, state, logger)
                self.ctx.finish_step(step.name, STATUS_OK)
                rec = self.ctx.get_step(step.name)
                dur = rec.duration_s if rec else None
                print(f"  ✓ {step.name} 完成（{dur}s）")
                self._emit({
                    "type": "step_done", "step": step.name, "index": index, "total": total,
                    "status": "ok", "duration_s": dur,
                    "artifacts": [
                        {"key": a.key, "path": a.path, "size": a.size, "kind": a.kind}
                        for a in (rec.artifacts.values() if rec else [])
                    ],
                })
            except SystemExit:
                logger.close()
                self.ctx.finish_step(step.name, STATUS_FAILED, "SystemExit")
                self._emit({"type": "step_done", "step": step.name, "index": index,
                            "total": total, "status": "failed", "error": "SystemExit"})
                raise
            except Exception as exc:  # noqa: BLE001
                detail = f"{type(exc).__name__}: {exc}"
                logger(f"✗ 失败：{detail}")
                logger(traceback.format_exc())
                self.ctx.finish_step(step.name, STATUS_FAILED, detail)
                failures.append(step.name)
                logger.close()
                print(f"  ✗ {step.name} 失败：{detail}")
                print(f"    完整日志：{logger.path}")
                self._emit({"type": "step_done", "step": step.name, "index": index,
                            "total": total, "status": "failed", "error": detail})
                if not keep_going:
                    raise
                continue
            else:
                logger.close()

        if failures:
            print(f"\n完成，但有 {len(failures)} 步失败：{', '.join(failures)}")
            return False
        return True

    # ------------------------------------------------------------ 状态组装
    def load_state(self, keys: Sequence[str]) -> dict[str, Any]:
        """从磁盘产物重建 state —— 这是「局部重跑」能成立的关键。"""
        state: dict[str, Any] = {}
        for key in keys:
            state[key] = self.ctx.load_artifact(key)
        return state


def run_pipeline(
    ctx: RunContext,
    only: Sequence[str] | None = None,
    force: Sequence[str] = (),
    from_step: str | None = None,
    to_step: str | None = None,
    dry_run: bool = False,
    keep_going: bool = False,
    emitter: Callable[[dict[str, Any]], None] | None = None,
) -> bool:
    return Runner(ctx, emitter).run(only, force, from_step, to_step, dry_run, keep_going)
