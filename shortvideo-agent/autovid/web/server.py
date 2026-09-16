"""零依赖本地 Web 工作台。

只用标准库 http.server —— 因为目标机器上 pip 装不了东西也连不上外网。
功能：
  * 表单发起生成（选题 / 自有文案 / 按次切换 provider）
  * SSE 实时推送 8 个节点的进度与日志
  * 结果预览：视频（支持 Range，可拖动）、封面、标题候选、话题标签
  * 异常后下载已完成片段，并从 LangGraph 检查点继续
  * 历史运行回看
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import queue
import re
import threading
import traceback
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from .. import __version__
from .. import media as M
from ..assets import AssetStore
from ..config import Config
from ..errors import AutoVidError
from ..graph import FLOW_NODES, GATE_TITLES, NODE_TITLES, VideoFlow
from ..manifest import RunContext, list_runs
from ..pipeline import STEP_NAMES, STEPS, Runner
from .. import providers as P
from ..providers import provider_statuses

STATIC_DIR = Path(__file__).resolve().parent / "static"

# 失败时允许用户带走的阶段性产物。严格使用扩展名白名单，避免把配置、密钥、
# 检查点数据库或排错日志打进下载包。
PARTIAL_EXTENSIONS = {".mp4", ".mov", ".wav", ".mp3", ".m4a", ".srt", ".ass"}


def partial_artifacts(base: Path) -> list[Path]:
    """返回一次运行中已经生成、可直接使用的媒体片段。"""
    found: list[Path] = []
    for folder in ("voice", "avatar", "subtitles", "compose"):
        root = base / folder
        if not root.is_dir():
            continue
        found.extend(
            item for item in root.rglob("*")
            if item.is_file() and item.suffix.lower() in PARTIAL_EXTENSIONS
        )
    return sorted(set(found))


# --------------------------------------------------------------------------- #
# 一次运行的会话：事件缓冲 + 多订阅者
# --------------------------------------------------------------------------- #
@dataclass
class RunSession:
    run_id: str
    ctx: RunContext | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: list[queue.Queue] = field(default_factory=list)
    finished: bool = False
    ok: bool = False
    error: str | None = None
    # LangGraph 流程专有
    mode: str = "pipeline"          # pipeline | flow
    run_dir: str = ""
    flow_thread: str | None = None
    flow_status: str = ""           # finished | interrupted | failed
    gate: dict[str, Any] | None = None
    current_step: str = ""

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def emit(self, event: dict[str, Any]) -> None:
        with self._lock:
            self.events.append(event)
            targets = list(self.subscribers)
        for target in targets:
            target.put(event)

    def subscribe(self) -> queue.Queue:
        """新订阅者先收到历史事件，避免中途打开页面丢失进度。"""
        box: queue.Queue = queue.Queue()
        with self._lock:
            replay = list(self.events)
            self.subscribers.append(box)
        for event in replay:
            box.put(event)
        return box

    def unsubscribe(self, box: queue.Queue) -> None:
        with self._lock:
            if box in self.subscribers:
                self.subscribers.remove(box)


# --------------------------------------------------------------------------- #
# 应用状态
# --------------------------------------------------------------------------- #
def option_names(config: Config) -> dict[str, list[str]]:
    """下拉框的候选项 —— **直接由 provider_statuses 推导**。

    以前这里是硬编码的一份列表，和 provider_statuses / PREFERRED 各维护一套，
    结果加了 local_qwen_tts / local_wav2lip 之后漏改，用户在界面上**根本选不到
    本地引擎**（顶部状态标签显示可用，下拉框里却没有）。现在单一数据源。
    """
    by_kind: dict[str, list[str]] = {}
    for status in provider_statuses(config):
        bucket = by_kind.setdefault(status.kind, [])
        if status.name not in bucket:
            bucket.append(status.name)

    names: dict[str, list[str]] = {}
    for step, kind in OPTION_KIND.items():
        available = by_kind.get(kind, [])
        preferred = PREFERRED.get(step) or []
        names[step] = ([n for n in preferred if n in available]
                       + [n for n in available if n not in preferred])
    # metadata 用的是 offline / llm 这套名字，和 script 不是同一套
    names["metadata"] = ["offline", "llm"]
    return names


class Workbench:
    def __init__(self, config: Config, flow_cls: type[VideoFlow] = VideoFlow):
        self.config = config
        self.flow_cls = flow_cls
        self.sessions: dict[str, RunSession] = {}
        self._lock = threading.Lock()
        self._active_run: str | None = None

    # ------------------------------------------------------------ 运行管理
    @property
    def active_run(self) -> str | None:
        with self._lock:
            return self._active_run

    def _out_dir(self) -> Path:
        return self.config.path(self.config.get("project.out_dir", "runs"))

    def open_ctx(self, run_id: str) -> RunContext | None:
        if run_id in self.sessions:
            return self.sessions[run_id].ctx
        root = self._out_dir().resolve()
        target = (root / run_id).resolve()
        if target.parent != root:
            return None
        if (target / "manifest.json").exists():
            try:
                return RunContext.open(target, self.config)
            except Exception:  # noqa: BLE001
                return None
        return None

    def run_dir(self, run_id: str) -> Path | None:
        """取一次运行的目录。

        LangGraph 流程不写 manifest.json，所以不能只靠 open_ctx ——
        这里兜底直接看目录是否存在。
        """
        ctx = self.open_ctx(run_id)
        if ctx is not None:
            return ctx.dir
        root = self._out_dir().resolve()
        target = (root / run_id).resolve()
        if target.parent != root:
            return None
        return target if target.is_dir() else None

    def flow_state(self, run_id: str) -> dict[str, Any]:
        """读流程写下的状态快照（用于回看历史运行）。"""
        base = self.run_dir(run_id)
        if base is None:
            return {}
        snapshot = base / "flow_state.json"
        if snapshot.exists():
            try:
                return json.loads(snapshot.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}

        # 兼容升级前异常退出的任务：旧版本只写了 flow-error.log，没有写历史快照，
        # 但 LangGraph 的 SQLite 检查点仍在。把它恢复成新格式后，旧任务也能续跑。
        if not (base / "_logs" / "flow-error.log").exists():
            return {}
        thread_id = f"flow-{run_id}"
        try:
            flow = self.flow_cls(self.config)
            checkpoint = flow.state_of(thread_id)
            values = dict(checkpoint.get("values") or {})
            if not values:
                return {}
            values.update({
                "thread_id": thread_id,
                "status": "failed",
                "next": list(checkpoint.get("next") or []),
                "config": self.config.as_dict(),
                "gates": [],
            })
            self._write_flow_state(base, values)
            return values
        except Exception:  # noqa: BLE001 - 历史恢复失败不应拖垮整个首页
            return {}

    @staticmethod
    def _write_flow_state(base: Path, snapshot: dict[str, Any]) -> None:
        if not base.is_dir():
            return
        (base / "flow_state.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    # ------------------------------------------------------------ 流程模式
    def start_flow(
        self,
        topic: str,
        script_text: str,
        overrides: dict[str, Any],
        gates: list[str],
        scene_photo: str = "",
    ) -> RunSession:
        """启动一次 LangGraph 流程（前置判断在图里做，这里不做拦截）。"""
        with self._lock:
            if self._active_run is not None:
                raise RuntimeError(f"已有任务在运行（{self._active_run}），请等它结束。")
            cfg = self.config.with_overrides(overrides)
            slug = topic or "flow"
            run_dir = self.flow_cls.new_run_dir(cfg, slug)
            run_id = run_dir.name
            self._active_run = run_id

        script_file = ""
        text = script_text.strip()
        if text:
            path = run_dir / "input_script.txt"
            path.write_text(text, encoding="utf-8")
            script_file = str(path)

        inputs: dict[str, Any] = {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "topic": topic.strip(),
            "script_text": text,
            "script_file": script_file,
            "voice_id": str(cfg.get("project.voice_id", "") or ""),
            "avatar_id": str(cfg.get("project.avatar_id", "") or ""),
            # 本次场景照片：提供画面与背景，每次换一张背景就不同
            "scene_photo": str(scene_photo or ""),
            "revision": 0,
            "approvals": {},
            "trace": [],
        }
        session = RunSession(run_id=run_id, mode="flow",
                             run_dir=str(run_dir), flow_thread=f"flow-{run_id}")
        with self._lock:
            self.sessions[run_id] = session
        # 一创建任务就写历史索引。即使进程在第一个节点前被关闭，历史记录里也不会消失。
        self._write_flow_state(run_dir, {
            **inputs,
            "thread_id": session.flow_thread,
            "status": "running",
            "next": ["preflight"],
            "gates": list(gates or []),
            "config": cfg.as_dict(),
        })
        threading.Thread(target=self._flow_worker,
                         args=(session, cfg, gates, inputs, None, False),
                         name=f"autovid-flow-{run_id}", daemon=True).start()
        return session

    def resume_flow(self, run_id: str, decision: Any, gates: list[str]) -> RunSession:
        """从闸门处恢复。整张图有 checkpointer，已完成的节点不会重跑。"""
        session = self.sessions.get(run_id)
        if session is None:
            with self._lock:
                if self._active_run is not None:
                    raise RuntimeError(f"已有任务在运行（{self._active_run}）。")
            state = self.flow_state(run_id)
            if not state:
                raise AutoVidError(f"找不到运行 {run_id} 的流程状态")
            session = RunSession(run_id=run_id, mode="flow",
                                 run_dir=str(self.run_dir(run_id) or ""),
                                 flow_thread=state.get("thread_id") or f"flow-{run_id}")
            with self._lock:
                self.sessions[run_id] = session
            with self._lock:
                self._active_run = run_id
        else:
            with self._lock:
                if self._active_run is not None and self._active_run != run_id:
                    raise RuntimeError(f"已有任务在运行（{self._active_run}）。")
                self._active_run = run_id
            session.finished = False
            session.flow_status = ""

        # 关键：清掉上一段的事件缓冲。
        # 否则页面点「通过」后重新订阅 SSE 时，会先收到**旧的 run_done**，
        # 前端立刻以为流程又结束了 —— 闸门按钮失效、进度也乱掉。
        session.events.clear()
        session.gate = None

        snapshot = self.flow_state(run_id)
        stored = snapshot.get("config")
        cfg = (Config(raw=stored, root=self.config.root, secrets=self.config.secrets)
               if isinstance(stored, dict) else self.config)
        threading.Thread(target=self._flow_worker,
                         args=(session, cfg, gates, None, decision, False),
                         name=f"autovid-flow-{run_id}", daemon=True).start()
        return session

    def continue_failed_flow(self, run_id: str) -> RunSession:
        """从历史记录中的异常节点继续，不重复已经成功的步骤。"""
        with self._lock:
            if self._active_run is not None:
                raise RuntimeError(f"已有任务在运行（{self._active_run}）。")

        snapshot = self.flow_state(run_id)
        if not snapshot:
            raise AutoVidError(f"找不到运行 {run_id} 的流程检查点")
        if str(snapshot.get("status") or "") != "failed":
            raise AutoVidError("只有异常中断的任务可以从失败处继续")
        if not (snapshot.get("next") or []):
            raise AutoVidError("这次失败没有可继续的节点，请修正配置后重新生成")

        base = self.run_dir(run_id)
        if base is None:
            raise AutoVidError(f"找不到运行目录：{run_id}")
        stored = snapshot.get("config")
        cfg = (Config(raw=stored, root=self.config.root, secrets=self.config.secrets)
               if isinstance(stored, dict) else self.config)
        gates = [g for g in (snapshot.get("gates") or []) if g in GATE_TITLES]
        session = self.sessions.get(run_id) or RunSession(
            run_id=run_id, mode="flow", run_dir=str(base),
            flow_thread=str(snapshot.get("thread_id") or f"flow-{run_id}"),
        )
        session.events.clear()
        session.finished = False
        session.ok = False
        session.error = None
        session.flow_status = ""
        session.gate = None
        session.current_step = ""
        with self._lock:
            self.sessions[run_id] = session
            self._active_run = run_id
        threading.Thread(
            target=self._flow_worker,
            args=(session, cfg, gates, None, None, True),
            name=f"autovid-flow-continue-{run_id}", daemon=True,
        ).start()
        return session

    def _flow_worker(self, session: RunSession, cfg: Config, gates: list[str],
                     inputs: dict[str, Any] | None, decision: Any,
                     retry_failed: bool = False) -> None:
        flow: VideoFlow | None = None
        try:
            def emit(event: dict[str, Any]) -> None:
                if event.get("type") == "step_start":
                    session.current_step = str(event.get("step") or "")
                session.emit(event)

            flow = self.flow_cls(cfg, gates=tuple(gates or ()), emitter=emit,
                                 log=lambda line: session.emit(
                                     {"type": "step_log", "step": "-", "line": line}))
            thread_id = session.flow_thread or f"flow-{session.run_id}"
            if retry_failed:
                session.emit({"type": "flow_continue", "run_id": session.run_id})
                outcome = flow.continue_failed(thread_id=thread_id)
            elif decision is None:
                session.emit({"type": "run_start", "run_id": session.run_id,
                              "dir": session.run_dir, "mode": "flow"})
                outcome = flow.run(inputs or {}, thread_id=thread_id)
            else:
                session.emit({"type": "flow_resume", "run_id": session.run_id,
                              "decision": decision})
                outcome = flow.resume(decision, thread_id=thread_id)

            session.flow_thread = outcome["thread_id"]
            session.flow_status = outcome["status"]
            session.gate = ({"gate": outcome.get("gate"), "payload": outcome.get("payload")}
                            if outcome["status"] == "interrupted" else None)
            session.ok = outcome["status"] == "finished"

            # 落一份状态快照，历史运行才回看得见
            snapshot = dict(outcome.get("result") or {})
            snapshot["thread_id"] = outcome["thread_id"]
            snapshot["status"] = outcome["status"]
            try:
                checkpoint = flow.state_of(outcome["thread_id"])
                snapshot["next"] = list(checkpoint.get("next") or [])
            except Exception:  # noqa: BLE001
                snapshot["next"] = []
            snapshot["gates"] = list(gates or [])
            # 存完整配置：恢复时要用「同一次运行的配置」，否则引擎/资产可能对不上
            snapshot["config"] = cfg.as_dict()
            base = Path(session.run_dir or ".")
            self._write_flow_state(base, snapshot)

            session.emit({
                "type": "run_done",
                "ok": session.ok,
                "run_id": session.run_id,
                "flow_status": outcome["status"],
                "gate": outcome.get("gate"),
                "payload": outcome.get("payload"),
                "failure": outcome.get("failure"),
                "errors": outcome.get("errors") or [],
                "result": build_flow_result(session.run_id, session.run_dir,
                                            outcome.get("result") or {},
                                            outcome["status"], outcome["thread_id"]),
            })
        except BaseException as exc:  # noqa: BLE001
            raw = str(exc).strip()
            summary = next((line.strip() for line in raw.splitlines() if line.strip()),
                           type(exc).__name__)
            title = NODE_TITLES.get(session.current_step, session.current_step or "生成")
            detail = f"{title}失败：{summary}"
            session.error = detail
            session.flow_status = "failed"
            # 完整 traceback 留在运行目录供排错，页面只显示一句可读错误。
            # 之前把整段堆栈推到实时日志，真正的厂商错误反而被淹没。
            base = Path(session.run_dir or ".")
            try:
                logs = base / "_logs"
                logs.mkdir(parents=True, exist_ok=True)
                (logs / "flow-error.log").write_text(
                    traceback.format_exc(), encoding="utf-8")
            except OSError:
                pass
            failed_state: dict[str, Any] = {}
            if flow is not None:
                try:
                    checkpoint = flow.state_of(session.flow_thread or f"flow-{session.run_id}")
                    failed_state = dict(checkpoint.get("values") or {})
                    failed_state["next"] = list(checkpoint.get("next") or [])
                except Exception:  # noqa: BLE001
                    failed_state = {}
            failed_state.update({
                "thread_id": session.flow_thread or f"flow-{session.run_id}",
                "status": "failed",
                "failed_step": session.current_step,
                "error": detail,
                "failure": detail,
                "gates": list(gates or []),
                "config": cfg.as_dict(),
            })
            self._write_flow_state(base, failed_state)
            session.emit({"type": "step_log", "step": session.current_step,
                          "line": detail})
            session.emit({"type": "run_done", "ok": False, "run_id": session.run_id,
                          "flow_status": "failed", "error": detail,
                          "failure": detail, "errors": [detail],
                          "failed_step": session.current_step,
                          "result": build_flow_result(
                              session.run_id, session.run_dir, failed_state, "failed",
                              str(failed_state.get("thread_id") or ""))})
        finally:
            session.finished = True
            session.gate = session.gate if session.flow_status == "interrupted" else None
            with self._lock:
                self._active_run = None

    def start_run(
        self,
        topic: str,
        script_text: str,
        overrides: dict[str, Any],
        force: bool = False,
        only: list[str] | None = None,
    ) -> RunSession:
        with self._lock:
            if self._active_run is not None:
                raise RuntimeError(f"已有任务在运行（{self._active_run}），请等它结束。")
            cfg = self.config.with_overrides(overrides)
            # 前置检查：没选音色/形象就直接拒绝，别让它跑一半才失败
            problems = Runner(
                RunContext.create(cfg, slug="preflight", persist=False)).preflight()
            if problems:
                raise AutoVidError(
                    "生成前的检查没通过：\n· " + "\n· ".join(problems) +
                    "\n\n请到「音色库 / 形象库」里准备并选好它们。"
                )
            slug = topic or "web"
            ctx = RunContext.create(cfg, slug=slug)
            self._active_run = ctx.run_id

        inputs = ctx.manifest.setdefault("inputs", {})
        if topic.strip():
            inputs["topic"] = topic.strip()
        text = script_text.strip()
        if text:
            # 复用 CLI 的 --script-file 通道：写进 run 目录，交给同一段代码处理
            script_path = ctx.dir / "input_script.txt"
            script_path.write_text(text, encoding="utf-8")
            inputs["script_file"] = str(script_path)
            inputs["script_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        ctx.save()

        session = RunSession(run_id=ctx.run_id, ctx=ctx)
        with self._lock:
            self.sessions[ctx.run_id] = session

        thread = threading.Thread(
            target=self._worker, args=(session, force, only),
            name=f"autovid-{ctx.run_id}", daemon=True,
        )
        thread.start()
        return session

    def _worker(self, session: RunSession, force: bool, only: list[str] | None) -> None:
        ctx = session.ctx
        try:
            session.emit({"type": "run_start", "run_id": ctx.run_id, "dir": str(ctx.dir)})
            runner = Runner(ctx, session.emit)
            ok = runner.run(only=only, force=["all"] if force else [])
            session.ok = ok
            session.emit({
                "type": "run_done", "ok": ok, "run_id": ctx.run_id,
                "result": build_result(ctx),
                "error": None if ok else "部分步骤失败，详见日志",
            })
        except BaseException as exc:  # noqa: BLE001
            # 这里刻意捕获 BaseException：后台线程一旦漏掉任何异常就会静默死亡，
            # 前端只会看到 SSE 连接断开、永远等不到 run_done。
            detail = f"{type(exc).__name__}: {exc}"
            session.error = detail
            session.emit({"type": "step_log", "step": "-", "line": traceback.format_exc()})
            session.emit({"type": "run_done", "ok": False, "run_id": ctx.run_id,
                          "error": detail, "result": build_result(ctx)})
        finally:
            session.finished = True
            with self._lock:
                self._active_run = None

    # ------------------------------------------------------------ bootstrap
    def custom_providers(self) -> list[dict[str, Any]]:
        """用户自己配的付费 API 提供商（设置面板管理）。"""
        from ..providers_registry import availability, load_entries  # noqa: PLC0415
        try:
            entries = load_entries(self.config)
            for entry in entries:
                ready, detail = availability(self.config, entry)
                entry["available"] = ready and entry.get("enabled") is not False
                entry["availability_detail"] = (detail if entry.get("enabled") is not False
                                                else "已停用")
            return entries
        except Exception:  # noqa: BLE001
            return []

    def custom_provider_labels(self) -> dict[str, str]:
        """{voice:<id>: 显示名} —— 前端下拉框靠它区分「本地」和「你配的付费 API」。"""
        from ..providers_registry import labels  # noqa: PLC0415
        try:
            return labels(self.config)
        except Exception:  # noqa: BLE001
            return {}

    def bootstrap(self) -> dict[str, Any]:
        current = {s.name: self.config.provider_of(s.name) for s in STEPS}
        return {
            "version": __version__,
            "steps": [{"name": s.name, "title": s.title, "index": i}
                      for i, s in enumerate(STEPS, start=1)],
            "current": current,
            # 前端要靠这个把「你的音色 / 你的形象」默认选中，
            # 否则每次进页面都得手动重选一遍（而且容易忘了选形象）。
            "project": {
                "voice_id": str(self.config.get("project.voice_id", "") or ""),
                "avatar_id": str(self.config.get("project.avatar_id", "") or ""),
                "require_assets": bool(self.config.get("project.require_assets", True)),
            },
            "options": option_names(self.config),
            # 设置里配的付费 API 的显示名，让下拉框能区分「本地」和「你配的」
            "provider_labels": self.custom_provider_labels(),
            "providers": [
                {"kind": s.kind, "name": s.name, "available": s.available, "detail": s.detail}
                for s in provider_statuses(self.config)
            ],
            "platform": self.config.platform,
            "assets": AssetStore(self.config).summary(),
            "preflight": Runner(
                RunContext.create(self.config, slug="preflight", persist=False)).preflight(),
            # 每个 provider 现在能不能用 —— 前端据此在下拉框里标注
            "option_info": option_matrix(self.config),
            "recommended": recommended_overrides(self.config),
            # LangGraph 流程模式：节点表给前端画进度条用
            "flow": {
                "nodes": [{"name": n, "title": NODE_TITLES.get(n, n), "index": i}
                          for i, n in enumerate(FLOW_NODES, start=1)],
                "gates": [{"name": g, "title": t} for g, t in GATE_TITLES.items()],
            },
            "runs": self.runs(),
            "active_run": self.active_run,
        }

    def runs(self, limit: int = 30) -> list[dict[str, Any]]:
        items = list_runs(self.config)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            ctx = self.open_ctx(item["run_id"])
            result = build_result(ctx) if ctx else {}
            seen.add(item["run_id"])
            out.append({
                "run_id": item["run_id"],
                "created": item["created"],
                "steps": item["steps"], "ok": item["ok"], "failed": item["failed"],
                "topic": (ctx.manifest.get("inputs", {}) or {}).get("topic", "") if ctx else "",
                "video": result.get("video"),
                "cover": result.get("cover"),
                "duration_s": result.get("duration_s"),
            })

        # LangGraph 运行不写 manifest.json，必须把 flow_state.json 单独并进历史列表。
        root = self._out_dir()
        if root.is_dir():
            candidates = [p for p in root.iterdir()
                          if p.is_dir() and not p.name.startswith(("_", "."))]
            for base in candidates:
                if base.name in seen:
                    continue
                if not ((base / "flow_state.json").exists()
                        or (base / "_logs" / "flow-error.log").exists()):
                    continue
                state = self.flow_state(base.name)
                if not state:
                    continue
                result = build_flow_result(
                    base.name, base, state, str(state.get("status") or ""),
                    str(state.get("thread_id") or ""),
                )
                status = str(state.get("status") or "")
                fragments = partial_artifacts(base)
                created = datetime.fromtimestamp(base.stat().st_mtime).isoformat(timespec="seconds")
                out.append({
                    "run_id": base.name,
                    "created": created,
                    "steps": result.get("steps") or [],
                    "ok": status == "finished",
                    "failed": status == "failed",
                    "status": status,
                    "topic": state.get("topic") or result.get("topic") or "",
                    "video": result.get("video"),
                    "cover": result.get("cover"),
                    "duration_s": result.get("duration_s"),
                    "has_partial": bool(fragments),
                    "partial_count": len(fragments),
                    "can_resume": status == "failed" and bool(state.get("next")),
                    "failed_step": state.get("failed_step") or "",
                    "error": state.get("error") or state.get("failure") or "",
                })

        out.sort(key=lambda row: str(row.get("created") or row.get("run_id") or ""),
                 reverse=True)
        return out[:limit]


# --------------------------------------------------------------------------- #
# 选项可用性
# --------------------------------------------------------------------------- #
# 每个环节对应 provider_statuses 里的哪一类
OPTION_KIND: dict[str, str] = {
    "script": "llm",
    "voice": "tts",
    "visuals": "image",
    "avatar": "avatar",
}

# 每个环节的偏好顺序：越靠前越优先（前提是「现在就能用」）
PREFERRED: dict[str, list[str]] = {
    "script": ["openai_compat", "offline"],
    "voice": ["local_qwen_tts", "cloud_tts", "edge_native", "sapi",
              "edge", "http_json", "silent"],
    "visuals": ["comfy", "openai_images", "ffmpeg_gradient"],
    "avatar": ["local_wav2lip", "comfy", "http_job", "minimax_h3", "still"],
}


def option_matrix(config: Config) -> dict[str, dict[str, dict[str, Any]]]:
    """每个环节、每个 provider 的可用状态。

    前端据此在下拉框里标出「✓ 现在就能用」还是「⚠ 还缺东西」，
    省得用户在一堆看不懂的名字里瞎试。

    数字人环节额外带上**这家能出多大画面**：D-ID 的照片数字人只有 512×512，
    拉到 1080×1920 是放大约 7.9 倍；HeyGen 能原生出 9:16 1080×1920。
    同样是「数字人」，选错一家的代价是画面糊，而不是报个错 ——
    所以这件事必须在**选的时候**就能看见，不能等跑完才发现。
    """
    from .. import provider_caps as CAP  # noqa: PLC0415

    by_kind: dict[str, dict[str, dict[str, Any]]] = {}
    for status in provider_statuses(config):
        by_kind.setdefault(status.kind, {})[status.name] = {
            "available": status.available,
            "detail": status.detail,
        }
    out = {step: by_kind.get(kind, {}) for step, kind in OPTION_KIND.items()}

    platform = config.platform
    want_w = int(platform.get("width", 1080))
    want_h = int(platform.get("height", 1920))
    want_fps = int(platform.get("fps", 30))
    for name, entry in (out.get("avatar") or {}).items():
        try:
            spec = P.avatar_output_spec(config, name, want_w, want_h, want_fps)
            _, _, notes = CAP.negotiate_canvas(want_w, want_h, spec)
        except Exception:  # noqa: BLE001 - 能力查询不该拖垮整个 bootstrap
            continue
        entry["output"] = {
            "width": spec.width, "height": spec.height, "fps": spec.fps,
            "text": spec.describe(),
            "verified": spec.verified, "declared": spec.declared,
        }
        entry["output_notes"] = notes
    return out


def recommended_overrides(config: Config) -> dict[str, str]:
    """按当前环境算出一套「现在就能跑」的配置。"""
    matrix = option_matrix(config)

    def pick(step: str) -> str:
        candidates = PREFERRED.get(step) or []
        for name in candidates:
            info = (matrix.get(step) or {}).get(name)
            if info and info.get("available"):
                return name
        return candidates[-1] if candidates else ""

    llm_ready = bool(config.secret_for("llm", "AUTOVID_LLM_API_KEY"))
    return {
        "steps.script.provider": pick("script"),
        "steps.voice.provider": pick("voice"),
        "steps.visuals.provider": pick("visuals"),
        "steps.avatar.provider": pick("avatar"),
        # metadata 的 provider 是 offline / llm，和 script 不是一套名字
        "steps.metadata.provider": "llm" if llm_ready else "offline",
    }


# --------------------------------------------------------------------------- #
# 结果组装
# --------------------------------------------------------------------------- #
def build_result(ctx: RunContext) -> dict[str, Any]:
    if ctx is None:
        return {}

    def rel(key: str) -> str | None:
        path = ctx.artifact_path(key)
        if path is None or not path.exists():
            return None
        try:
            return path.relative_to(ctx.dir).as_posix()
        except ValueError:
            return None

    meta: dict[str, Any] = {}
    try:
        meta = ctx.load_artifact("metadata") or {}
    except Exception:  # noqa: BLE001
        pass

    duration = None
    video_path = ctx.artifact_path("video")
    if video_path and video_path.exists():
        try:
            duration = round(M.probe_duration(video_path), 2)
        except Exception:  # noqa: BLE001
            duration = None

    # 语音信息：静音成片必须让用户在页面上就能看出来，别让他下载完才发现没声音
    audio_provider = None
    audio_note = ""
    audio_cloned = False
    audio_voice_name = ""
    try:
        voice_data = ctx.load_artifact("voice_segments") or {}
        audio_provider = voice_data.get("provider")
        audio_note = str(voice_data.get("note", ""))
        audio_cloned = bool(voice_data.get("cloned"))
        audio_voice_name = str(voice_data.get("voice_name") or "")
    except Exception:  # noqa: BLE001
        pass
    audio = {
        "provider": audio_provider,
        "real": audio_provider not in (None, "silent"),
        "cloned": audio_cloned,
        "voice_name": audio_voice_name,
        "note": audio_note,
    }

    avatar_used_portrait = False
    avatar_id = None
    try:
        avatar_data = ctx.load_artifact("avatar") or {}
        avatar_used_portrait = bool(avatar_data.get("used_portrait"))
        avatar_id = avatar_data.get("avatar_id")
    except Exception:  # noqa: BLE001
        pass

    publish_dir = ctx.dir / "publish"
    return {
        "run_id": ctx.run_id,
        "video": rel("video"),
        "cover": rel("cover"),
        "cover_3x4": rel("cover_3x4"),
        "titles": list(meta.get("titles") or meta.get("title_options") or []),
        "tags": list(meta.get("tags") or []),
        "description": str(meta.get("description", "")),
        "topic": str(meta.get("topic", "")),
        "duration_s": duration,
        "audio": audio,
        "avatar": {"id": avatar_id, "used_portrait": avatar_used_portrait},
        "has_package": publish_dir.exists(),
        "steps": [
            {"name": name, "status": ctx.get_step(name).status if ctx.get_step(name) else "pending",
             "duration_s": ctx.get_step(name).duration_s if ctx.get_step(name) else None}
            for name in STEP_NAMES
        ],
    }


def build_flow_result(run_id: str, run_dir: str | Path, state: dict[str, Any],
                      status: str = "", thread_id: str = "") -> dict[str, Any]:
    """把 LangGraph 流程的最终状态整理成前端能直接渲染的结果。

    流程不走 manifest，所以这里从 state 里取产物路径，并换算成相对运行目录的路径
    （`/media` 接口是按相对路径取文件的）。
    """
    base = Path(str(run_dir)) if run_dir else Path(".")

    def rel(value: Any) -> str | None:
        if not value:
            return None
        try:
            return Path(str(value)).relative_to(base).as_posix()
        except ValueError:
            return None

    video = state.get("video") or {}
    voice = state.get("voice") or {}
    script = state.get("script") or {}
    profile = state.get("voice_profile") or {}
    avatar = state.get("avatar") or {}
    metadata = state.get("metadata") or {}
    provider = voice.get("provider") or profile.get("provider")
    fragments = partial_artifacts(base) if base.is_dir() else []

    steps = []
    for entry in state.get("trace") or []:
        raw = str(entry.get("status") or "ok")
        steps.append({
            "name": entry.get("node"),
            "status": "failed" if raw == "failed" else "ok",
            "duration_s": entry.get("duration_s"),
        })

    return {
        "run_id": run_id,
        "mode": "flow",
        "video": rel(video.get("video")),
        "cover": rel(video.get("cover")),
        "cover_3x4": rel(video.get("cover_3x4")),
        "titles": metadata.get("titles") or script.get("title_options") or [],
        "tags": metadata.get("tags") or script.get("tags") or [],
        "description": metadata.get("description") or script.get("description") or "",
        "topic": metadata.get("topic") or script.get("topic") or "",
        "duration_s": video.get("duration_s"),
        "audio": {
            "provider": provider,
            "real": bool(provider) and provider != "silent",
            "cloned": bool(voice.get("cloned") or profile.get("cloned")),
            "voice_name": voice.get("voice_name") or profile.get("name") or "",
            "note": voice.get("note") or profile.get("note") or "",
        },
        "avatar": {"id": avatar.get("avatar_id"),
                   "used_portrait": bool(avatar.get("used_portrait"))},
        "has_package": False,
        "has_partial": bool(fragments),
        "partial_count": len(fragments),
        "can_resume": status == "failed" and bool(state.get("next")),
        "failed_step": state.get("failed_step") or "",
        "error": state.get("error") or state.get("failure") or "",
        "steps": steps,
        "flow_status": status,
        "thread_id": thread_id,
    }


# --------------------------------------------------------------------------- #
# HTTP 处理
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"AutoVid/{__version__}"
    app: Workbench  # 由 serve() 注入

    # -------------------------------------------------------- 基础工具
    def log_message(self, fmt: str, *args: Any) -> None:  # 静音访问日志
        return

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"error": message}, status)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _read_body(self) -> bytes:
        """读原始请求体。上传音频/照片走原始字节，避免 multipart 解析。"""
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _query(self) -> dict[str, str]:
        parsed = urlparse(self.path)
        return {k: v[0] for k, v in parse_qs(parsed.query).items()}

    # -------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._serve_static("index.html")
            if path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])
            if path == "/api/bootstrap":
                return self._json(self.app.bootstrap())
            if path == "/api/settings/recommend":
                from ..providers_registry import preset_options  # noqa: PLC0415
                from ..recommend import catalog  # noqa: PLC0415
                return self._json({"catalog": catalog(),
                                   "presets": preset_options(),
                                   "providers": self.app.custom_providers()})
            if path == "/api/runs":
                # 支持 ?limit=：默认只回最近 30 条（页面够用），
                # 但调用方（比如测试）需要看全量时必须能拿到，否则
                # 「运行数有没有增加」这种判断会被截断悄悄骗过去。
                raw = self._query().get("limit", "")
                try:
                    limit = max(1, min(500, int(raw))) if raw else 30
                except ValueError:
                    limit = 30
                runs = self.app.runs(limit)
                return self._json({"runs": runs, "total": len(self.app.runs(500))})
            if path == "/api/result":
                return self._result()
            if path == "/api/stream":
                return self._stream()
            if path == "/api/logs":
                return self._logs()
            if path == "/api/assets":
                return self._json(AssetStore(self.app.config).summary())
            if path == "/api/assets/voice/audio":
                return self._asset_file("voice_audio")
            if path == "/api/assets/avatar/photo":
                return self._asset_file("avatar_photo")
            if path == "/api/scene":
                return self._scene_file()
            if path == "/api/voice/preview":
                return self._voice_preview_file()
            if path == "/media":
                return self._media()
            if path == "/fragments":
                return self._fragments()
            if path == "/package":
                return self._package()
            return self._error("未知路径", 404)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            try:
                self._error(f"{type(exc).__name__}: {exc}", 500)
            except Exception:  # noqa: BLE001
                pass

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/plan":
                return self._plan()
            if path == "/api/run":
                return self._start()
            if path == "/api/flow/run":
                return self._start_flow()
            if path == "/api/flow/resume":
                return self._resume_flow()
            if path == "/api/flow/continue":
                return self._continue_flow()
            if path == "/api/scene":
                return self._upload_scene()
            if path == "/api/voice/preview":
                return self._voice_preview()
            if path == "/api/settings/providers":
                return self._save_provider()
            if path == "/api/settings/providers/test":
                return self._test_provider()
            if path == "/api/reveal":
                return self._reveal()
            if path == "/api/assets/voice":
                return self._create_voice()
            if path == "/api/assets/voice/audio":
                return self._upload_voice_audio()
            if path == "/api/assets/avatar":
                return self._create_avatar()
            if path == "/api/assets/avatar/photo":
                return self._upload_avatar_photo()
            if path == "/api/assets/avatar/primary":
                return self._set_primary_photo()
            return self._error("未知路径", 404)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            try:
                self._error(f"{type(exc).__name__}: {exc}", 500)
            except Exception:  # noqa: BLE001
                pass

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        query = self._query()
        store = AssetStore(self.app.config)
        try:
            if path == "/api/assets/voice":
                store.delete_voice(query.get("voice_id", ""))
                return self._json({"ok": True, "summary": store.summary()})
            if path == "/api/settings/providers":
                return self._delete_provider()
            if path == "/api/assets/avatar":
                store.delete_avatar(query.get("avatar_id", ""))
                return self._json({"ok": True, "summary": store.summary()})
            if path == "/api/assets/avatar/photo":
                asset = store.delete_avatar_photo(
                    query.get("avatar_id", ""), query.get("filename", ""))
                return self._json({"avatar": asset.to_dict(), "summary": store.summary()})
            return self._error("未知路径", 404)
        except AutoVidError as exc:
            return self._error(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            try:
                self._error(f"{type(exc).__name__}: {exc}", 500)
            except Exception:  # noqa: BLE001
                pass

    # -------------------------------------------------------- 静态文件
    def _serve_static(self, name: str) -> None:
        target = (STATIC_DIR / name).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            return self._error("资源不存在", 404)
        body = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -------------------------------------------------------- API
    def _plan(self) -> None:
        """只出计划，不执行、不落盘。"""
        payload = self._read_json()
        cfg = self.app.config.with_overrides(payload.get("overrides") or {})
        probe = RunContext.create(cfg, slug="plan", persist=False)
        inputs = probe.manifest.setdefault("inputs", {})
        if (payload.get("topic") or "").strip():
            inputs["topic"] = payload["topic"].strip()
        if (payload.get("script_text") or "").strip():
            inputs["script_file"] = "<web 粘贴的文案>"
            inputs["script_hash"] = hashlib.sha256(
                payload["script_text"].encode("utf-8")).hexdigest()
        runner = Runner(probe)
        plan = runner.plan()
        return self._json({
            "problems": runner.preflight(),
            "steps": [
                {"name": s.name, "title": s.title, "index": i,
                 "action": "run" if s.name in plan.to_run else "skip",
                 "reason": plan.reasons[s.name]}
                for i, s in enumerate(STEPS, start=1)
            ]
        })

    def _start_flow(self) -> None:
        """启动 LangGraph 流程。前置判断在图里做，所以这里不拦截 —— 缺什么由 fail 节点说明。"""
        payload = self._read_json()
        gates = [g for g in (payload.get("gates") or []) if g in GATE_TITLES]
        try:
            session = self.app.start_flow(
                topic=str(payload.get("topic") or ""),
                script_text=str(payload.get("script_text") or ""),
                overrides=payload.get("overrides") or {},
                gates=gates,
                scene_photo=str(payload.get("scene_photo") or ""),
            )
        except RuntimeError as exc:
            return self._error(str(exc), 409)
        return self._json({"run_id": session.run_id, "mode": "flow",
                           "thread_id": session.flow_thread, "gates": gates})

    def _resume_flow(self) -> None:
        """从闸门处恢复。整张图有 checkpointer，已完成的节点不会重跑。"""
        payload = self._read_json()
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            return self._error("缺少 run_id", 400)
        decision = payload.get("decision") or {"action": "approve"}
        if isinstance(decision, str):
            decision = {"action": decision}
        gates = [g for g in (payload.get("gates") or []) if g in GATE_TITLES]
        try:
            session = self.app.resume_flow(run_id, decision, gates)
        except AutoVidError as exc:
            return self._error(str(exc), 400)
        except RuntimeError as exc:
            return self._error(str(exc), 409)
        return self._json({"run_id": session.run_id, "mode": "flow",
                           "thread_id": session.flow_thread})

    def _continue_flow(self) -> None:
        """从异常节点继续，已完成节点由 LangGraph 检查点直接复用。"""
        payload = self._read_json()
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            return self._error("缺少 run_id", 400)
        try:
            session = self.app.continue_failed_flow(run_id)
        except AutoVidError as exc:
            return self._error(str(exc), 400)
        except RuntimeError as exc:
            return self._error(str(exc), 409)
        return self._json({"run_id": session.run_id, "mode": "flow",
                           "thread_id": session.flow_thread})

    def _start(self) -> None:
        payload = self._read_json()
        try:
            session = self.app.start_run(
                topic=str(payload.get("topic") or ""),
                script_text=str(payload.get("script_text") or ""),
                overrides=payload.get("overrides") or {},
                force=bool(payload.get("force")),
                only=payload.get("only") or None,
            )
        except AutoVidError as exc:
            # 前置检查没过：明确告诉前端缺什么
            return self._error(str(exc), 400)
        except RuntimeError as exc:
            return self._error(str(exc), 409)
        return self._json({"run_id": session.run_id})

    def _result(self) -> None:
        run_id = self._query().get("run_id", "")
        ctx = self.app.open_ctx(run_id)
        session = self.app.sessions.get(run_id)
        if ctx is not None:
            return self._json({
                "result": build_result(ctx),
                "running": bool(session and not session.finished),
                "error": session.error if session else None,
                "mode": "pipeline",
            })
        # 流程模式没有 manifest，从状态快照还原
        state = self.app.flow_state(run_id)
        if state:
            return self._json({
                "result": build_flow_result(run_id, self.app.run_dir(run_id) or "",
                                            state, str(state.get("status") or ""),
                                            str(state.get("thread_id") or "")),
                "running": bool(session and not session.finished),
                "error": (session.error if session else None)
                         or state.get("error") or state.get("failure"),
                "mode": "flow",
                "gate": session.gate if session else None,
                "payload": (session.gate or {}).get("payload") if session else None,
            })
        return self._error(f"找不到运行 {run_id}", 404)

    def _logs(self) -> None:
        query = self._query()
        run_id, step = query.get("run_id", ""), query.get("step", "")
        ctx = self.app.open_ctx(run_id)
        if ctx is None:
            return self._error("找不到运行", 404)
        out_dir = ctx.config.path(ctx.config.get("project.out_dir", "runs"))
        log_path = out_dir / "_logs" / run_id / f"{step}.log"
        if not log_path.is_file():
            return self._json({"step": step, "log": ""})
        return self._json({"step": step, "log": log_path.read_text(encoding="utf-8")})

    # -------------------------------------------------------- SSE
    def _stream(self) -> None:
        run_id = self._query().get("run_id", "")
        session = self.app.sessions.get(run_id)
        if session is None:
            return self._error(f"找不到运行会话 {run_id}", 404)

        box = session.subscribe()
        # 事件流没有 Content-Length，HTTP/1.1 下客户端只能靠连接关闭判断结束，
        # 所以这里必须显式要求关闭连接。
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                try:
                    event = box.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")   # 防代理断连
                    self.wfile.flush()
                    continue
                chunk = f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
                if event.get("type") == "run_done":
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            session.unsubscribe(box)

    # -------------------------------------------------------- 场景照片
    def _scenes_dir(self) -> Path:
        target = self.app.config.path(
            self.app.config.get("project.scenes_dir", "scenes"))
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _upload_scene(self) -> None:
        """上传「本次场景照片」（带人物）。

        这张照片决定画面的样子 —— 每次换一张，视频背景就不同。
        形象库存的是身份，这里存的是「今天在哪拍」。
        """
        query = self._query()
        data = self._read_body()
        if not data:
            return self._error("没有收到图片数据", 400)

        raw_name = Path(unquote(query.get("filename") or "scene.jpg")).name
        suffix = Path(raw_name).suffix.lower() or ".jpg"
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
            return self._error(f"不支持的图片格式：{suffix}", 400)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self._scenes_dir() / f"{stamp}-{raw_name[:40]}"
        target.write_bytes(data)

        info = M.probe_media(target)
        if not info.get("has_video"):
            target.unlink(missing_ok=True)
            return self._error(f"不是有效图片：{info.get('error') or '无法解码'}", 400)
        width, height = int(info.get("width") or 0), int(info.get("height") or 0)
        if min(width, height) < 256:
            target.unlink(missing_ok=True)
            return self._error(
                f"照片分辨率太低（{width}×{height}），最短边至少 256px", 400)

        return self._json({
            "name": target.name,
            "path": str(target),
            "width": width, "height": height,
            "url": f"/api/scene?name={quote(target.name)}",
        })

    def _scene_file(self) -> None:
        """回显场景照片（页面预览用）。只允许读 scenes 目录里的文件。"""
        name = Path(unquote(self._query().get("name", ""))).name
        if not name:
            return self._error("缺少 name", 400)
        target = (self._scenes_dir() / name).resolve()
        if not str(target).startswith(str(self._scenes_dir().resolve())) or not target.is_file():
            return self._error("文件不存在", 404)
        self._send_file(target)

    # -------------------------------------------------------- 文件服务
    def _resolve_media(self) -> tuple[Path, Path] | None:
        query = self._query()
        base = self.app.run_dir(query.get("run_id", ""))
        if base is None:
            return None
        relative = unquote(query.get("p", ""))
        if not relative:
            return None
        target = (base / relative).resolve()
        # 目录穿越防护：必须落在该 run 目录内
        if not str(target).startswith(str(base.resolve())):
            return None
        if not target.is_file():
            return None
        return base, target

    def _media(self) -> None:
        found = self._resolve_media()
        if found is None:
            return self._error("文件不存在或越权访问", 404)
        _, target = found
        self._send_file(target)

    def _send_file(self, target: Path, download_name: str | None = None) -> None:
        size = target.stat().st_size
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        status = 200

        # 视频拖动进度条依赖 Range，必须支持
        range_header = self.headers.get("Range") or ""
        if range_header.startswith("bytes="):
            spec = range_header[6:].split(",")[0]
            first, _, last = spec.partition("-")
            try:
                if first:
                    start = int(first)
                if last:
                    end = int(last)
            except ValueError:
                start, end = 0, size - 1
            start = max(0, min(start, max(0, size - 1)))
            end = max(start, min(end, size - 1))
            status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download_name:
            # HTTP 头只能放 latin-1，中文文件名必须走 RFC 5987 的 filename* 形式；
            # 同时给一个纯 ASCII 的 filename 兼容老客户端。
            ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", download_name) or "download"
            encoded = quote(download_name, safe="")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded}',
            )
        self.end_headers()

        with open(target, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _package(self) -> None:
        query = self._query()
        run_id = query.get("run_id", "")
        base = self.app.run_dir(run_id)
        if base is None:
            return self._error("找不到运行", 404)
        publish_dir = base / "publish"
        if not publish_dir.is_dir():
            return self._error("该运行还没有发布包", 404)

        zip_path = base / "publish_package.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in sorted(publish_dir.rglob("*")):
                if item.is_file():
                    archive.write(item, item.relative_to(publish_dir).as_posix())
        # 文件名保持 ASCII，避免 Content-Disposition 的编码兼容问题
        self._send_file(zip_path, download_name=f"autovid-publish-{run_id}.zip")

    def _fragments(self) -> None:
        """下载异常前已生成的语音、人物、字幕和合成片段。"""
        run_id = self._query().get("run_id", "")
        base = self.app.run_dir(run_id)
        if base is None:
            return self._error("找不到运行", 404)
        files = partial_artifacts(base)
        if not files:
            return self._error("这次运行还没有可下载的片段", 404)
        zip_path = base / "completed_fragments.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in files:
                archive.write(item, item.relative_to(base).as_posix())
        self._send_file(zip_path, download_name=f"autovid-fragments-{run_id}.zip")

    def _reveal(self) -> None:
        """在资源管理器里打开该运行的目录（仅本地使用）。"""
        payload = self._read_json()
        base = self.app.run_dir(str(payload.get("run_id") or ""))
        if base is None:
            return self._error("找不到运行", 404)
        if not hasattr(os, "startfile"):
            return self._error("当前系统不支持打开文件夹", 400)
        os.startfile(str(base))  # noqa: S606 - 本地工具，路径来自自己的 runs 目录
        return self._json({"ok": True, "dir": str(base)})

    # -------------------------------------------------------- 资产：音色
    def _create_voice(self) -> None:
        payload = self._read_json()
        name = str(payload.get("name") or "").strip()
        if not name:
            return self._error("请先给这个音色起个名字", 400)
        store = AssetStore(self.app.config)
        asset = store.create_voice(name, language=str(payload.get("language") or "zh-CN"))
        return self._json({"voice": asset.to_dict(), "summary": store.summary()})

    def _upload_voice_audio(self) -> None:
        query = self._query()
        data = self._read_body()
        if not data:
            return self._error("没有收到音频数据", 400)
        store = AssetStore(self.app.config)
        try:
            asset = store.save_voice_reference(
                query.get("voice_id", ""), data,
                query.get("filename") or "recording.wav",
                ref_text=query.get("ref_text", ""),
            )
        except AutoVidError as exc:
            return self._error(str(exc), 400)
        return self._json({"voice": asset.to_dict(), "summary": store.summary()})

    # -------------------------------------------------------- 资产：形象
    def _create_avatar(self) -> None:
        payload = self._read_json()
        name = str(payload.get("name") or "").strip()
        if not name:
            return self._error("请先给这个形象起个名字", 400)
        store = AssetStore(self.app.config)
        asset = store.create_avatar(name)
        return self._json({"avatar": asset.to_dict(), "summary": store.summary()})

    def _upload_avatar_photo(self) -> None:
        query = self._query()
        data = self._read_body()
        if not data:
            return self._error("没有收到图片数据", 400)
        store = AssetStore(self.app.config)
        try:
            asset = store.save_avatar_photo(
                query.get("avatar_id", ""), data,
                query.get("filename") or "photo.jpg",
                make_primary=query.get("primary") in ("1", "true", "yes"),
            )
        except AutoVidError as exc:
            return self._error(str(exc), 400)
        return self._json({"avatar": asset.to_dict(), "summary": store.summary()})

    def _set_primary_photo(self) -> None:
        payload = self._read_json()
        store = AssetStore(self.app.config)
        try:
            asset = store.set_primary_photo(
                str(payload.get("avatar_id") or ""), str(payload.get("filename") or ""))
        except AutoVidError as exc:
            return self._error(str(exc), 400)
        return self._json({"avatar": asset.to_dict(), "summary": store.summary()})

    # -------------------------------------------------------- 资产：文件读取
    def _asset_file(self, kind: str) -> None:
        """把资产文件回给前端（试听参考音频 / 显示照片缩略图）。"""
        query = self._query()
        store = AssetStore(self.app.config)
        try:
            if kind == "voice_audio":
                path = store.voice_reference(query.get("voice_id", ""))
            else:
                path = store.avatar_photo_path(
                    query.get("avatar_id", ""), query.get("filename", ""))
        except AutoVidError as exc:
            return self._error(str(exc), 400)
        if path is None or not path.exists():
            return self._error("文件不存在", 404)
        self._send_file(path)

    # ------------------------------------------------------------ 设置
    def _save_provider(self) -> None:
        """新增/更新一个付费 API 提供商条目。"""
        from ..providers_registry import upsert_entry  # noqa: PLC0415
        payload = self._read_json()
        try:
            entry = upsert_entry(self.app.config, payload)
        except ValueError as exc:
            return self._error(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            return self._error(f"保存失败：{type(exc).__name__}: {exc}", 500)
        return self._json({"provider": entry})

    def _delete_provider(self) -> None:
        from ..providers_registry import delete_entry  # noqa: PLC0415
        entry_id = self._query().get("id", "")
        if not entry_id:
            return self._error("缺少 id", 400)
        ok = delete_entry(self.app.config, entry_id)
        if not ok:
            return self._error("找不到这个提供商", 404)
        return self._json({"ok": True})

    def _test_provider(self) -> None:
        """只读测试网络和鉴权，不触发任何生成任务。"""
        from ..providers_registry import test_connection  # noqa: PLC0415
        entry_id = str(self._read_json().get("id") or "").strip()
        if not entry_id:
            return self._error("缺少提供商 id", 400)
        try:
            result = test_connection(self.app.config, entry_id)
        except ValueError as exc:
            return self._error(str(exc), 404)
        except Exception as exc:  # noqa: BLE001
            return self._error(f"测试失败：{type(exc).__name__}: {exc}", 500)
        return self._json({"result": result})

    # ------------------------------------------------------------ 试音
    def _preview_dir(self) -> Path:
        target = self.app.config.path("runs") / "_preview"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _voice_preview(self) -> None:
        """用**当前选中的引擎**合成一句话，让用户立刻听有没有声音、是不是自己的音色。

        为什么单做这个入口：以前只能跑完整条视频才知道声音对不对，
        用户面对一堆引擎根本不知道选哪个。这里合成一句短的，
        并且**如实回报**是否用了克隆音色、音频是否真的有声音（RMS）。
        """
        payload = self._read_json()
        provider = str(payload.get("provider") or "").strip()
        voice_id = str(payload.get("voice_id") or "").strip()
        text = str(payload.get("text") or "").strip() or "你好，这是用你的声音合成的试听片段。"

        if not provider:
            return self._error("没有指定语音引擎", 400)
        if provider not in P.known_tts_providers(self.app.config):
            known = "、".join(sorted(P.known_tts_providers(self.app.config)))
            return self._error(f"未知语音引擎：{provider}\n当前可用：{known}", 400)

        store = AssetStore(self.app.config)
        asset = store.get_voice(voice_id) if voice_id else None
        if asset is None:
            return self._error("没有选音色。先在「音色库」录一段，再回来试音。", 400)

        cfg = self.app.config.with_overrides({
            "steps.voice.provider": provider,
            "steps.voice.strict": True,     # 试音就是要看这个引擎行不行，别偷偷回退
            "steps.voice.fallback": [],
        })
        out_dir = self._preview_dir() / datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir.mkdir(parents=True, exist_ok=True)

        lines: list[str] = []
        try:
            result = P.tts_synthesize(
                cfg, [{"id": "preview", "index": 0, "text": text}],
                out_dir, lines.append, voice_asset=asset)
        except Exception as exc:  # noqa: BLE001
            return self._json({
                "ok": False,
                "provider": provider,
                "error": f"{type(exc).__name__}: {exc}",
                "log": lines[-20:],
            })

        wav = Path(result.parts[0]) if result.parts else None
        if wav is None or not wav.exists():
            return self._json({"ok": False, "provider": provider,
                               "error": "引擎没有产出音频文件", "log": lines[-20:]})

        # 量一下音量：0 就是静音，用户听到「没声音」时至少能有个客观依据
        rms, duration = 0.0, 0.0
        try:
            import numpy as np
            import soundfile as sf
            data, rate = sf.read(str(wav), dtype="float32", always_2d=True)
            if data.size:
                rms = float(np.sqrt(np.mean(np.square(data))))
                duration = len(data) / float(rate)
        except Exception:  # noqa: BLE001
            pass

        return self._json({
            "ok": True,
            "provider": result.provider,
            "cloned": bool(result.cloned),
            "voice_name": result.voice_name or asset.name,
            "note": result.note,
            "rms": round(rms, 5),
            "duration_s": round(duration, 2),
            "audible": rms > 0.002,
            "url": f"/api/voice/preview?name={quote(wav.name)}&dir={quote(out_dir.name)}",
            "log": lines[-12:],
        })

    def _voice_preview_file(self) -> None:
        query = self._query()
        name = Path(unquote(query.get("name", ""))).name
        folder = Path(unquote(query.get("dir", ""))).name
        if not name or not folder:
            return self._error("缺少参数", 400)
        base = self._preview_dir()
        target = (base / folder / name).resolve()
        if not str(target).startswith(str(base.resolve())) or not target.is_file():
            return self._error("文件不存在", 404)
        self._send_file(target)


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #
def _bind_httpd(host: str, port: int, handler: type[BaseHTTPRequestHandler]
                ) -> tuple[ThreadingHTTPServer, bool]:
    """绑定工作台端口；被占用或被 Windows 保留时自动换可用端口。"""
    try:
        return ThreadingHTTPServer((host, port), handler), False
    except OSError as exc:
        # Windows：10013=系统保留/无权绑定，10048=已被占用。
        # POSIX：13=EACCES，98=EADDRINUSE。其他错误不能掩盖。
        codes = {getattr(exc, "errno", None), getattr(exc, "winerror", None)}
        if port == 0 or not ({13, 98, 10013, 10048} & codes):
            raise
        return ThreadingHTTPServer((host, 0), handler), True


def serve(config: Config, host: str = "127.0.0.1", port: int = 8899,
          open_browser: bool = False) -> None:
    app = Workbench(config)
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd, changed_port = _bind_httpd(host, port, handler)
    httpd.daemon_threads = True

    actual_port = int(httpd.server_address[1])
    url = f"http://{host}:{actual_port}/"
    print("=" * 62)
    print("  AutoVid 工作台已启动")
    if changed_port:
        print(f"  端口 {port} 被占用或被系统保留，已自动改用 {actual_port}")
    print(f"  打开这个地址: {url}")
    print(f"  运行目录    : {config.path(config.get('project.out_dir', 'runs'))}")
    print("  按 Ctrl+C 停止")
    print("=" * 62)

    if open_browser:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止...")
    finally:
        httpd.shutdown()
        httpd.server_close()
