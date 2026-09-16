"""Artifact 契约与运行清单（Manifest）。

这是整个项目最重要的地基：每一步的产物都以「带 sha256 的 Artifact」形式落盘，
并记录该步骤的 `input_hash`。由此得到两个能力：

  1. **缓存跳过** —— input_hash 没变且产物还在，就跳过该步（不重复烧钱/烧显存）。
  2. **局部重跑** —— 只重跑某几步，下游按需重跑，上游直接复用磁盘产物。

没有这一层，整条流水线每次都要从头跑 40 分钟，项目会退化成玩具。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import ArtifactError, AutoVidError

MANIFEST_NAME = "manifest.json"
ARTIFACT_DIR = "artifacts"
STEPS_DIR = "steps"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_CACHED = "cached"
STATUS_SKIPPED = "skipped"


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def file_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def slugify(text: str, max_len: int = 24, fallback: str = "run") -> str:
    """把中文/英文标题压成安全目录名。中文直接保留（Windows 支持）。"""
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "-", (text or "").strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    if not cleaned:
        return fallback
    return cleaned[:max_len]


def short_hash(value: str, length: int = 8) -> str:
    return (value or "")[:length]


def compute_input_hash(
    version: str,
    config_fingerprint: str,
    upstream: dict[str, str | None],
    extra: dict[str, Any] | None = None,
) -> str:
    """步骤的输入指纹 = 步骤版本 + 相关配置 + 所有上游产物哈希 + 额外参数。"""
    return stable_hash(
        {
            "version": version,
            "config": config_fingerprint,
            "upstream": upstream,
            "extra": extra or {},
        }
    )


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Artifact:
    key: str
    path: str          # 相对 run_dir
    sha256: str
    size: int
    kind: str = "file"  # file | json | image | audio | video | text
    created: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "kind": self.kind,
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Artifact":
        return cls(
            key=data["key"],
            path=data["path"],
            sha256=data.get("sha256", ""),
            size=int(data.get("size", 0)),
            kind=data.get("kind", "file"),
            created=data.get("created", ""),
        )


@dataclass
class StepRecord:
    name: str
    index: int
    version: str = "1"
    status: str = STATUS_PENDING
    input_hash: str = ""
    started: str | None = None
    finished: str | None = None
    duration_s: float | None = None
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "index": self.index,
            "version": self.version,
            "status": self.status,
            "input_hash": self.input_hash,
            "started": self.started,
            "finished": self.finished,
            "duration_s": self.duration_s,
            "artifacts": {k: v.to_dict() for k, v in self.artifacts.items()},
            "error": self.error,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StepRecord":
        return cls(
            name=data["name"],
            index=int(data.get("index", 0)),
            version=data.get("version", "1"),
            status=data.get("status", STATUS_PENDING),
            input_hash=data.get("input_hash", ""),
            started=data.get("started"),
            finished=data.get("finished"),
            duration_s=data.get("duration_s"),
            artifacts={
                k: Artifact.from_dict(v)
                for k, v in (data.get("artifacts") or {}).items()
            },
            error=data.get("error"),
            notes=list(data.get("notes") or []),
        )


# --------------------------------------------------------------------------- #
# 运行上下文
# --------------------------------------------------------------------------- #
class RunContext:
    """一次运行的上下文：目录、清单读写、产物登记。"""

    def __init__(self, run_dir: Path, config: Any, manifest: dict[str, Any]):
        self.dir = Path(run_dir)
        self.config = config
        self.manifest = manifest
        self._steps: dict[str, StepRecord] = {
            rec["name"]: StepRecord.from_dict(rec)
            for rec in manifest.get("steps", [])
        }
        self._order: list[str] = [rec["name"] for rec in manifest.get("steps", [])]

    # ------------------------------------------------------------ 创建/打开
    @classmethod
    def create(
        cls,
        config: Any,
        slug: str = "run",
        run_id: str | None = None,
        persist: bool = True,
    ) -> "RunContext":
        """新建运行上下文。

        persist=False 用于 --dry-run：不落盘、不抢 .latest 指针，
        保证「只看计划」这个动作完全没有副作用。
        """
        out_dir = config.path(config.get("project.out_dir", "runs"))
        run_id = run_id or f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slugify(slug)}"
        run_dir = out_dir / run_id
        manifest = {
            "run_id": run_id,
            "created": now_iso(),
            "updated": now_iso(),
            "project_root": str(config.root),
            "config": config.as_dict(),
            "steps": [],
            "logs": [],
        }
        if not persist:
            return cls(run_dir, config, manifest)

        (run_dir / ARTIFACT_DIR).mkdir(parents=True, exist_ok=True)
        (run_dir / STEPS_DIR).mkdir(parents=True, exist_ok=True)
        ctx = cls(run_dir, config, manifest)
        ctx.save()
        ctx._write_latest_pointer()
        return ctx

    @classmethod
    def open(cls, run_dir: Path | str, config: Any) -> "RunContext":
        run_dir = Path(run_dir)
        manifest_path = run_dir / MANIFEST_NAME
        if not manifest_path.exists():
            raise AutoVidError(f"[manifest] 找不到 {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return cls(run_dir, config, manifest)

    @classmethod
    def open_latest(cls, config: Any) -> "RunContext":
        out_dir = config.path(config.get("project.out_dir", "runs"))
        pointer = out_dir / ".latest"
        if pointer.exists():
            target = out_dir / pointer.read_text(encoding="utf-8").strip()
            if (target / MANIFEST_NAME).exists():
                return cls.open(target, config)
        # 指针缺失、或指向已被删除的运行 -> 回退到最近一次有效运行
        runs = list_runs(config)
        if not runs:
            raise AutoVidError("[manifest] 还没有任何运行记录，先跑一次 `run`")
        return cls.open(runs[0]["dir"], config)

    def _write_latest_pointer(self) -> None:
        out_dir = self.config.path(self.config.get("project.out_dir", "runs"))
        (out_dir / ".latest").write_text(self.dir.name, encoding="utf-8")

    # ------------------------------------------------------------ 属性
    @property
    def run_id(self) -> str:
        return self.manifest.get("run_id", self.dir.name)

    @property
    def artifact_dir(self) -> Path:
        return self.dir / ARTIFACT_DIR

    def path(self, relative: str | Path) -> Path:
        """run 目录内的相对路径 -> 绝对路径。"""
        p = Path(relative)
        return p if p.is_absolute() else (self.dir / p)

    # ------------------------------------------------------------ 清单读写
    def save(self) -> None:
        self.manifest["updated"] = now_iso()
        self.manifest["steps"] = [self._steps[n].to_dict() for n in self._order]
        tmp = self.dir / (MANIFEST_NAME + ".tmp")
        tmp.write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.dir / MANIFEST_NAME)

    def log(self, message: str) -> None:
        entry = {"at": now_iso(), "msg": message}
        self.manifest.setdefault("logs", []).append(entry)
        self.save()

    # ------------------------------------------------------------ 步骤生命周期
    def get_step(self, name: str) -> StepRecord | None:
        return self._steps.get(name)

    def ensure_step(self, name: str, index: int, version: str) -> StepRecord:
        rec = self._steps.get(name)
        if rec is None:
            rec = StepRecord(name=name, index=index, version=version)
            self._steps[name] = rec
            self._order.append(name)
        rec.index = index
        rec.version = version
        return rec

    def is_reusable(self, name: str, input_hash: str) -> bool:
        """该步能否直接复用磁盘上的产物？"""
        rec = self._steps.get(name)
        if rec is None or rec.status not in (STATUS_OK, STATUS_CACHED):
            return False
        if rec.input_hash != input_hash:
            return False
        if not rec.artifacts:
            return False
        return all(self.path(a.path).exists() for a in rec.artifacts.values())

    def start_step(self, name: str, index: int, version: str, input_hash: str) -> StepRecord:
        rec = self.ensure_step(name, index, version)
        rec.status = STATUS_RUNNING
        rec.input_hash = input_hash
        rec.started = now_iso()
        rec.finished = None
        rec.duration_s = None
        rec.error = None
        rec.artifacts = {}
        rec.notes = []
        self.save()
        return rec

    def finish_step(self, name: str, status: str, error: str | None = None) -> StepRecord:
        rec = self._steps[name]
        rec.status = status
        rec.finished = now_iso()
        rec.error = error
        if rec.started:
            try:
                started = datetime.fromisoformat(rec.started)
                rec.duration_s = round((datetime.now().astimezone() - started).total_seconds(), 2)
            except ValueError:
                rec.duration_s = None
        self.save()
        return rec

    def note(self, name: str, message: str) -> None:
        rec = self._steps.get(name)
        if rec is not None:
            rec.notes.append(message)

    # ------------------------------------------------------------ 产物登记
    def add_artifact(self, step_name: str, key: str, path: Path | str, kind: str = "file") -> Artifact:
        """把一个产物登记进清单（自动算 sha256 与大小）。"""
        abs_path = self.path(path)
        if not abs_path.exists():
            raise AutoVidError(f"[manifest] 产物不存在，无法登记: {abs_path}")
        rel = abs_path.relative_to(self.dir).as_posix()
        art = Artifact(
            key=key,
            path=rel,
            sha256=file_sha256(abs_path),
            size=abs_path.stat().st_size,
            kind=kind,
        )
        self._steps[step_name].artifacts[key] = art
        self.save()
        return art

    def all_artifacts(self) -> dict[str, Artifact]:
        """跨步骤收集所有产物；同名 key 以最后一次写入为准。"""
        merged: dict[str, Artifact] = {}
        for name in self._order:
            merged.update(self._steps[name].artifacts)
        return merged

    def artifact(self, key: str) -> Artifact | None:
        return self.all_artifacts().get(key)

    def artifact_path(self, key: str) -> Path | None:
        art = self.artifact(key)
        return self.path(art.path) if art else None

    def load_artifact(self, key: str) -> Any:
        """读取产物：.json -> 解析后的对象；其他 -> Path。"""
        art = self.artifact(key)
        if art is None:
            raise AutoVidError(f"[manifest] 缺少产物 '{key}'，请先跑产生它的那一步")
        abs_path = self.path(art.path)
        if not abs_path.exists():
            raise AutoVidError(f"[manifest] 产物 '{key}' 记录存在但文件已丢失: {abs_path}")
        if abs_path.suffix.lower() == ".json" or art.kind == "json":
            return json.loads(abs_path.read_text(encoding="utf-8"))
        return abs_path

    def upstream_hashes(self, keys: list[str]) -> dict[str, str | None]:
        arts = self.all_artifacts()
        return {k: (arts[k].sha256 if k in arts else None) for k in keys}

    # ------------------------------------------------------------ 展示
    def summary(self) -> str:
        lines = [f"run: {self.run_id}", f"dir: {self.dir}", ""]
        if not self._order:
            lines.append("(还没有任何步骤记录)")
        total = 0.0
        for name in self._order:
            rec = self._steps[name]
            total += rec.duration_s or 0.0
            dur = f"{rec.duration_s:6.1f}s" if rec.duration_s is not None else "     -"
            lines.append(f"  {rec.index:>2}. {name:<12} {rec.status:<8} {dur}")
            if rec.error:
                lines.append(f"      !! {rec.error.splitlines()[0][:160]}")
            for key, art in rec.artifacts.items():
                lines.append(f"      - {key:<18} {art.path}  ({art.size / 1024:.0f} KB)")
        lines.append("")
        lines.append(f"总耗时: {total:.1f}s")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 运行列表
# --------------------------------------------------------------------------- #
def list_runs(config: Any) -> list[dict[str, Any]]:
    out_dir = config.path(config.get("project.out_dir", "runs"))
    if not out_dir.exists():
        return []
    runs: list[dict[str, Any]] = []
    for child in sorted(out_dir.iterdir(), reverse=True):
        manifest_path = child / MANIFEST_NAME
        if not child.is_dir() or not manifest_path.exists():
            continue
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        steps = data.get("steps", [])
        runs.append(
            {
                "run_id": data.get("run_id", child.name),
                "dir": child,
                "created": data.get("created", ""),
                "steps": len(steps),
                "ok": sum(1 for s in steps if s.get("status") in (STATUS_OK, STATUS_CACHED)),
                "failed": sum(1 for s in steps if s.get("status") == STATUS_FAILED),
            }
        )
    return runs


def purge_runs(config: Any, keep: int = 5) -> list[Path]:
    """只保留最近 keep 次运行的中间产物，删除更早的（含大视频文件）。"""
    runs = list_runs(config)
    removed: list[Path] = []
    for item in runs[keep:]:
        shutil.rmtree(item["dir"], ignore_errors=True)
        removed.append(item["dir"])
    return removed


def wait_for(predicate, timeout_s: float, interval_s: float = 1.0, what: str = "条件"):
    """简单轮询等待（给云 API 提交任务型 provider 用）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    raise TimeoutError(f"等待{what}超时（{timeout_s}s）")
