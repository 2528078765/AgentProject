"""手工 vendor Python 依赖 —— 绕开 pip。

为什么需要这个：目标机器上 pip 写不了临时文件（`Permission denied ... *.whl.metadata`），
但**网络是通的**，而且 wheel 本质就是 zip。所以我们可以自己：

    1. 从 PyPI JSON API 递归解析依赖
    2. 挑选合适的 wheel（纯 Python 的 py3-none-any，或匹配当前解释器的 cp3XX）
    3. 下载并解包到 .pylibs/，运行时加到 sys.path

这样在「装不了包」的机器上也能跑依赖第三方库的代码。

    python scripts/vendor_deps.py langgraph            # 安装
    python scripts/vendor_deps.py --list langgraph     # 只看解析结果，不下载
    python scripts/vendor_deps.py --check              # 检查已装的可否 import
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import sys
import sysconfig
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIBS = ROOT / ".pylibs"
PYPI = "https://pypi.org/pypi/{name}/json"

# 这些是构建/测试期依赖，运行不需要
SKIP_EXTRA_MARKERS = ("extra ==", "extra==")


def interpreter_tags() -> list[str]:
    """当前解释器能接受哪些 wheel 平台标签（从最具体到最宽松）。"""
    version = sys.version_info
    tags = [f"cp{version.major}{version.minor}"]
    # abi3 向下兼容：cp38-abi3 也能装在 cp314 上
    for minor in range(version.minor, 5, -1):
        tags.append(f"cp3{minor}-abi3")
    tags.append("py3")
    return tags


def platform_tags() -> list[str]:
    platext = sysconfig.get_platform().replace("-", "_").replace(".", "_")
    tags = [platext, "any"]
    if "amd64" in platext:
        tags.insert(0, "win_amd64" if sys.platform == "win32" else "manylinux_2_17_x86_64")
    return tags


def pick_wheel(files: list[dict], allow_source: bool = False) -> dict | None:
    """挑一个能装的 wheel。

    三个必须同时校验的点：
      1. **python 标签**：cp314 / py3
      2. **abi 标签**：`cp314t` 是自由线程构建，装在普通 cp314 上会崩
      3. **abi3 向前兼容**：`cp310-abi3` 的 wheel 在 cp314 上也能用！
         漏掉这条会把 onnx / safetensors / tokenizers 误判成「装不了」。
    """
    version = sys.version_info
    cp_tag = f"cp{version.major}{version.minor}"
    plat = platform_tags()

    best: tuple[int, dict] | None = None
    for item in files:
        name = item.get("filename", "")
        if not name.endswith(".whl"):
            continue
        parts = name[:-4].split("-")
        if len(parts) < 5:
            continue
        py_tag, abi_tag, plat_tag = parts[-3], parts[-2], parts[-1]
        abis = set(abi_tag.split("."))
        pys = set(py_tag.split("."))

        py_ok = bool(pys & {cp_tag, "py3"})
        # abi3 wheel 的 python 标签可以是任意更早的 cp3X
        if not py_ok and "abi3" in abis:
            for tag in pys:
                if tag.startswith("cp3") and tag[3:].isdigit() \
                        and int(tag[3:]) <= version.minor:
                    py_ok = True
                    break
        if not py_ok or not (abis & {"none", "abi3", cp_tag}):
            continue
        plat_ok = any(tag in plat_tag.split(".") for tag in plat)
        if not plat_ok:
            continue
        # 同一版本同时提供 any 和当前平台 wheel 时，平台 wheel 往往额外捆绑
        # 必需的 DLL（SoundFile/libsndfile 就是典型）。优先精确平台构建；只有
        # 没有平台构建时才退到纯 Python wheel。
        score = 4 if plat_tag != "any" else 3
        if py_tag.startswith(f"cp{sys.version_info.major}{sys.version_info.minor}"):
            score += 1
        if best is None or score > best[0]:
            best = (score, item)
    return best[1] if best else None


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements(requires_dist: list[str] | None) -> list[tuple[str, str]]:
    """把 requires_dist 解成 [(包名, 版本约束)]，跳过 extra 和不适用的环境标记。"""
    try:
        from packaging.requirements import InvalidRequirement, Requirement
    except ModuleNotFoundError:
        # packaging 本身可能正是待安装依赖，不能要求它预先存在。
        # 官方 Python 自带的 pip 内含同一套解析器，可作为启动阶段回退。
        from pip._vendor.packaging.requirements import (  # type: ignore[import-not-found]
            InvalidRequirement,
            Requirement,
        )

    out: list[tuple[str, str]] = []
    for raw in requires_dist or []:
        try:
            requirement = Requirement(raw)
        except InvalidRequirement:
            continue
        # 不主动安装可选 extra；环境中 extra 为空时，Marker.evaluate() 会把
        # `extra == ...` 判为不适用。它同时正确处理 sys_platform、
        # platform_system、python_version 以及 and/or 组合。
        if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
            continue
        out.append((normalize(requirement.name), str(requirement.specifier)))
    return out


def fetch_json(url: str, timeout: int = 40) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "autovid-vendor"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


_META_CACHE: dict[str, dict] = {}


def fetch_version_meta(name: str, version: str | None = None) -> dict:
    """取包元数据（带缓存）。不传版本就是最新版。"""
    key = f"{name}=={version}" if version else name
    if key in _META_CACHE:
        return _META_CACHE[key]
    url = (f"https://pypi.org/pypi/{name}/{version}/json" if version
           else PYPI.format(name=name))
    request = urllib.request.Request(url, headers={"User-Agent": "autovid-vendor"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))
    _META_CACHE[key] = data
    return data


def resolve(roots: list[str]) -> tuple[dict[str, dict], list[str]]:
    """解析依赖树，**并且遵守版本约束**。

    这一点必须做对：不同包对同一个依赖的约束会互相打架，
    例如 pydantic 2.13.5 要求 `pydantic-core==2.46.5`。
    如果无脑取最新版（2.49.0），运行时会直接 SystemError。
    """
    try:
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
        from packaging.version import InvalidVersion, Version
    except ModuleNotFoundError:
        from pip._vendor.packaging.specifiers import (  # type: ignore[import-not-found]
            InvalidSpecifier,
            SpecifierSet,
        )
        from pip._vendor.packaging.version import (  # type: ignore[import-not-found]
            InvalidVersion,
            Version,
        )

    constraints: dict[str, list[str]] = {}
    chosen: dict[str, dict] = {}
    problems: list[str] = []
    queue: list[str] = []

    def push(name: str) -> None:
        if name not in queue:
            queue.append(name)

    for root in roots:
        # 命令行可以直接写包名，也可以写带版本约束的，如 transformers==4.57.6。
        # 关键：normalize 只能作用在**包名**上，版本约束要拆出来单独存，
        # 否则 "4.57.6" 会被规范化成 "4-57-6" 拿去查 PyPI，直接 404。
        match = re.match(r"^([A-Za-z0-9._-]+)\s*(.*)$", root)
        name = match.group(1) if match else root
        spec = (match.group(2) if match else "").strip()
        normalized = normalize(name)
        bucket = constraints.setdefault(normalized, [])
        if spec and spec not in bucket:
            bucket.append(spec)
        push(normalized)

    rounds = 0
    while queue and rounds < 200:
        rounds += 1
        name = queue.pop(0)
        spec_text = ",".join(constraints.get(name, []))
        try:
            spec = SpecifierSet(spec_text) if spec_text else SpecifierSet()
        except InvalidSpecifier:
            spec = SpecifierSet()

        try:
            meta = fetch_version_meta(name)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{name}: 查询 PyPI 失败 {type(exc).__name__}")
            continue

        releases = meta.get("releases") or {}
        candidates: list[tuple[Version, list[dict]]] = []
        for raw_version, files in releases.items():
            try:
                parsed = Version(raw_version)
            except InvalidVersion:
                continue
            if parsed.is_prerelease or parsed.is_devrelease:
                continue
            if spec and parsed not in spec:
                continue
            candidates.append((parsed, files or []))
        if not candidates:
            problems.append(
                f"{name}：没有任何版本满足约束 '{spec_text or '(无)'}'"
            )
            continue
        candidates.sort(key=lambda item: item[0], reverse=True)

        picked: tuple[Version, dict] | None = None
        for parsed, files in candidates:
            wheel = pick_wheel(files)
            if wheel:
                picked = (parsed, wheel)
                break
        if picked is None:
            problems.append(
                f"{name}：约束 '{spec_text or '(无)'}' 下没有匹配当前解释器"
                f"（cp{sys.version_info.major}{sys.version_info.minor} / "
                f"{sysconfig.get_platform()}）的 wheel —— 需要编译，装不了"
            )
            continue

        parsed, wheel = picked
        version_text = str(parsed)
        if name in chosen and chosen[name]["version"] == version_text:
            continue      # 这个版本已经处理过，依赖也收集过了
        chosen[name] = {"version": version_text, "wheel": wheel}

        # 取「这个版本」的依赖，而不是最新版的
        try:
            version_meta = fetch_version_meta(name, version_text)
        except Exception:  # noqa: BLE001
            version_meta = meta
        for child, child_spec in parse_requirements(
                (version_meta.get("info") or {}).get("requires_dist")):
            bucket = constraints.setdefault(child, [])
            if child_spec and child_spec not in bucket:
                bucket.append(child_spec)
                push(child)          # 约束变了，重新解析它
            elif child not in chosen:
                push(child)
    return chosen, problems


def install(resolved: dict[str, dict]) -> list[str]:
    LIBS.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    for name, data in sorted(resolved.items()):
        wheel = data["wheel"]
        target = LIBS / f"{name}-{data['version']}.installed"
        # 标记文件只能表示上一次解包走到了最后，不能证明包文件现在还在。
        # 机器迁移、杀毒软件清理或一次中断的复制都可能留下 marker，却丢掉
        # 真正的模块。旧逻辑会永久跳过这种“幽灵安装”。利用 wheel 元数据里的
        # top_level.txt / RECORD 做完整性检查；不完整就重新下载覆盖。
        intact = False
        normalized = name.replace("-", "_").lower()
        dist_infos = list(LIBS.glob(f"{normalized}-{data['version']}*.dist-info"))
        for dist_info in dist_infos:
            top_level = dist_info / "top_level.txt"
            candidates: list[str] = []
            if top_level.is_file():
                candidates = [line.strip() for line in top_level.read_text(
                    encoding="utf-8", errors="replace").splitlines() if line.strip()]
            if not candidates:
                record = dist_info / "RECORD"
                if record.is_file():
                    for line in record.read_text(encoding="utf-8", errors="replace").splitlines():
                        path = line.split(",", 1)[0].replace("\\", "/")
                        head = path.split("/", 1)[0]
                        if head and not head.endswith((".dist-info", ".data")):
                            candidates.append(head.removesuffix(".py"))
            intact = bool(candidates) and all(
                (LIBS / module).exists() or (LIBS / f"{module}.py").exists()
                for module in set(candidates)
            )
            if intact:
                break
        if target.exists() and intact:
            installed.append(f"{name} {data['version']}（已验证，跳过）")
            continue
        if target.exists():
            target.unlink()
            installed.append(f"{name} {data['version']}（安装不完整，重新获取）")
        request = urllib.request.Request(wheel["url"], headers={"User-Agent": "autovid-vendor"})
        with urllib.request.urlopen(request, timeout=180) as response:
            blob = response.read()
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            archive.extractall(LIBS)
        target.write_text(wheel["filename"], encoding="utf-8")
        installed.append(f"{name} {data['version']}  ({wheel['filename']})")
    return installed


def check(modules: list[str]) -> None:
    print(f"vendor 目录: {LIBS}\n")
    boot = LIBS.is_dir() and str(LIBS) in sys.path
    print(f"  目录存在: {LIBS.is_dir()}   已在 sys.path: {str(LIBS) in sys.path}")
    for module in modules:
        try:
            mod = __import__(module)
            print(f"  [OK]   {module}  {getattr(mod, '__version__', '')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {module}  -> {type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="手工 vendor Python 依赖（绕开 pip）")
    parser.add_argument("packages", nargs="*", default=[], help="要安装的顶层包")
    parser.add_argument("--list", action="store_true", help="只解析依赖树，不下载")
    parser.add_argument("--check", action="store_true", help="检查已 vendor 的包能否 import")
    parser.add_argument("--clean", action="store_true", help="清空 .pylibs")
    args = parser.parse_args()

    if args.clean:
        shutil.rmtree(LIBS, ignore_errors=True)
        print(f"已清空 {LIBS}")
        return 0

    if args.check:
        check(["langgraph", "langchain_core", "langgraph.checkpoint.memory"])
        return 0

    if not args.packages:
        parser.error("请给出要安装的包名，例如：langgraph")

    print(f"解释器: Python {sys.version_info.major}.{sys.version_info.minor} "
          f"({sysconfig.get_platform()})")
    print(f"目标目录: {LIBS}\n")
    print("正在解析依赖树…")
    resolved, problems = resolve(args.packages)

    print(f"\n解析到 {len(resolved)} 个包：")
    for name, data in sorted(resolved.items()):
        print(f"  {name:<28} {data['version']:<12} {data['wheel']['filename']}")

    if problems:
        print(f"\n⚠ {len(problems)} 项无法满足：")
        for item in problems:
            print(f"  - {item}")

    if args.list:
        return 0 if not problems else 1
    if problems:
        print("\n存在无法满足的依赖，中止安装（半装的状态更难排查）。")
        return 1

    print("\n开始下载并解包…")
    for line in install(resolved):
        print(f"  {line}")
    print(f"\n完成。运行时把 {LIBS} 加到 sys.path 即可（autovid/__init__.py 已自动处理）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
