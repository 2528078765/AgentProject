"""LangGraph 编排（自包含版）的冒烟测试。

产品形状（用户明确要求）：

    start -> 判断节点（缺条件就 -> fail -> end，并说明原因）
          -> 音色克隆 -> ... -> end

    没有「背景图」节点：形象库存一次身份，每次生成现拍一张带人物的场景照片，
    照片里的环境就是画面背景，所以背景天然每次不同。

所以这个测试既要验证功能，也要验证「自包含」和「没有背景图」这两件事：

    1. 图结构 = preflight + fail + 7 个业务节点（**不含 visuals / publish**）+ 2 个可选闸门
    2. 缺前置条件（含缺场景照片）时走进 fail 节点并逐条说明
    3. 条件齐备时按 voice_clone -> script -> tts -> avatar -> ... -> metadata 跑完
    4. graph.py 不 import pipeline / steps / manifest（自包含的硬证据）
    5. 画面确实来自场景照片，且默认不会出现「两个人」
    6. 跨进程恢复 + 打回循环

    python scripts/smoke_flow.py
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autovid import graph as G                      # noqa: E402
from autovid import media as M                      # noqa: E402
from autovid.config import Config                   # noqa: E402

PASS, FAIL = "[通过]", "[失败]"
problems: list[str] = []
EXPECTED_ORDER = ["preflight", "voice_clone", "script", "tts",
                  "avatar", "subtitles", "compose", "metadata"]
WORK = ROOT / ".tmp" / "flow_probe"


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {PASS if ok else FAIL} {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        problems.append(name)
    return ok


def make_scene(name: str = "scene.png", size: tuple[int, int] = (800, 1000)) -> Path:
    """造一张「本次场景照片」当测试输入。

    真实场景下这是你现拍的带人物的照片 —— 它决定画面的样子。
    """
    WORK.mkdir(parents=True, exist_ok=True)
    scene = WORK / name
    M.make_gradient(scene, size[0], size[1],
                    ["0x1a1a2e", "0x16213e", "0x0f3460"], seed=11)
    return scene


def flow_config(clear_assets: bool = False) -> Config:
    config = Config.load(root=ROOT)
    if clear_assets:
        config.raw.setdefault("project", {})["voice_id"] = ""
        config.raw["project"]["avatar_id"] = ""
    return config.with_overrides({
        "steps.voice.provider": "silent",
        "steps.avatar.provider": "still",
        "project.require_assets": True,
    })


def base_inputs(config: Config, run_dir: Path, scene: Path | None, topic: str) -> dict:
    return {
        "run_id": run_dir.name, "run_dir": str(run_dir),
        "topic": topic, "script_text": "", "script_file": "",
        "voice_id": str(config.get("project.voice_id") or ""),
        "avatar_id": str(config.get("project.avatar_id") or ""),
        "scene_photo": str(scene) if scene else "",
        "revision": 0, "approvals": {}, "trace": [],
    }


class LocalFixtureVideoFlow(G.VideoFlow):
    """让图编排测试使用 silent/still 夹具，但不放宽产品的严格前置检查。"""

    def node_preflight(self, state: G.FlowState) -> dict:
        result = super().node_preflight(state)
        fixture_errors = {
            "语音接口「silent」不支持音色克隆，已停止生成",
            "数字人接口 still 不会生成人物动作，已停止生成",
        }
        result["errors"] = [
            item for item in (result.get("errors") or []) if item not in fixture_errors
        ]
        if not result["errors"] and result.get("trace"):
            result["trace"][-1]["status"] = "ok"
        return result


def main() -> int:
    print("LangGraph 编排（自包含版）冒烟测试")
    print("=" * 74)

    # ---------------------------------------------------------- 依赖
    print("\n1. 依赖与图结构")
    try:
        import langgraph  # noqa: F401
        check("langgraph 可用（手工 vendor）", True)
    except ImportError as exc:
        check("langgraph 可用", False, str(exc))
        return 1

    config = flow_config()
    # 固定 thread_id 的冒烟测试必须从空检查点开始；否则上次中断的状态会污染本次结果。
    shutil.rmtree(config.path(config.get("project.out_dir", "runs")) / "_graph",
                  ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)
    scene = make_scene()
    flow = G.VideoFlow(config, log=lambda _m: None)
    nodes = set(getattr(flow.build(), "nodes", {}) or {})
    check("有 preflight 判断节点", "preflight" in nodes)
    check("有 fail 节点（缺条件时说明原因）", "fail" in nodes)
    check("**没有** 背景图节点（画面来自场景照片）", "visuals" not in nodes)
    check("业务节点齐全", all(n in nodes for n in EXPECTED_ORDER),
          "、".join(EXPECTED_ORDER))
    check("闸门节点存在（默认不启用）",
          all(f"gate_{g}" in nodes for g in G.GATE_TITLES))
    check("默认不启用任何闸门", G.DEFAULT_GATES == (), str(G.DEFAULT_GATES))
    check("检查点是 sqlite", flow.checkpointer_kind == "sqlite", flow.checkpointer_kind)

    # ---------------------------------------------------------- 自包含
    print("\n2. 自包含：不调用之前的编排代码（用户的核心要求）")
    source = Path(G.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    bad_imports = sorted(m for m in imported
                         if m.rsplit(".", 1)[-1] in {"pipeline", "steps", "manifest",
                                                     "cli", "web"})
    check("没有 import pipeline / steps / manifest / cli / web",
          not bad_imports, f"发现 {bad_imports}")
    identifiers = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    identifiers |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    bad_names = sorted({"Runner", "RunContext", "StepDef"} & identifiers)
    check("代码里没有出现 Runner / RunContext / StepDef", not bad_names,
          f"发现 {bad_names}")
    pulled = sorted(m for m in ("autovid.pipeline", "autovid.steps", "autovid.manifest")
                    if m in sys.modules)
    check("导入 graph.py 不会连带加载 pipeline / steps / manifest", not pulled,
          f"被牵连 {pulled}")
    used = [m for m in ("providers", "media", "assets", "comfy")
            if re.search(rf"from \.{m} import|import {m}", source)]
    check("复用的是能力层（providers/media/assets）",
          set(used) >= {"providers", "media", "assets"}, f"{used}")

    # ---------------------------------------------------------- 前置判断拦截
    print("\n3. 缺前置条件 -> fail 节点 -> END，并逐条说明")
    strict = flow_config(clear_assets=True)
    strict_flow = G.VideoFlow(strict, log=lambda _m: None)
    run_dir = G.VideoFlow.new_run_dir(strict, "flow-blocked")
    blocked_thread = f"{run_dir.name}-blocked"
    outcome = strict_flow.run(base_inputs(strict, run_dir, None, ""),
                              thread_id=blocked_thread)
    check("状态是 failed", outcome["status"] == "failed", outcome["status"])
    failure = str(outcome.get("failure") or "")
    for token in ("文案没准备好", "音色没准备好", "形象没准备好", "场景照片没准备好"):
        check(f"说明了「{token}」", token in failure)
    check("给了修复指引", "音色库" in failure and "形象库" in failure)
    check("走到的是 fail 节点", outcome["trace"][-1]["node"] == "fail",
          str(outcome["trace"][-1]))
    check("没有执行任何生成节点",
          not any(e["node"] in EXPECTED_ORDER[1:] for e in outcome["trace"]))
    check("没有产出成片", not (run_dir / "compose" / "final.mp4").exists())
    shutil.rmtree(run_dir, ignore_errors=True)

    tiny = make_scene("tiny.png", (120, 120))
    run_dir = G.VideoFlow.new_run_dir(strict, "flow-badscene")
    badscene_thread = f"{run_dir.name}-badscene"
    outcome = strict_flow.run(base_inputs(strict, run_dir, tiny, "选题"),
                              thread_id=badscene_thread)
    check("场景照片分辨率太低会被拦住",
          "分辨率太低" in str(outcome.get("failure") or ""),
          str(outcome.get("failure") or "")[:70])
    shutil.rmtree(run_dir, ignore_errors=True)

    # ---------------------------------------------------------- 正常链路
    print("\n4. 条件齐备 -> 按顺序跑完整条图")
    normal_flow = LocalFixtureVideoFlow(config, log=lambda _m: None)
    run_dir = G.VideoFlow.new_run_dir(config, "flow-ok")
    normal_thread = f"{run_dir.name}-ok"
    outcome = normal_flow.run(base_inputs(config, run_dir, scene, "场景照片驱动验证"),
                              thread_id=normal_thread)
    check("状态是 finished", outcome["status"] == "finished", outcome["status"])
    nodes_run = [e["node"] for e in outcome["trace"] if not e["node"].startswith("gate_")]
    check("节点执行顺序符合预期", nodes_run == EXPECTED_ORDER, str(nodes_run))
    check("音色克隆在文案之前（按用户指定的顺序）",
          nodes_run.index("voice_clone") < nodes_run.index("script"))

    result = outcome["result"]
    profile = result.get("voice_profile") or {}
    check("音色克隆节点报告了是否克隆",
          "cloned" in profile and "can_clone" in profile,
          f"provider={profile.get('provider')} cloned={profile.get('cloned')}")
    check("如实说明没克隆（silent 不出声也不克隆）",
          profile.get("cloned") is False and "silent" in str(profile.get("note")),
          str(profile.get("note"))[:56])

    script = result.get("script") or {}
    check("产出了口播稿", len(script.get("segments") or []) > 0,
          f"{len(script.get('segments') or [])} 段 / {script.get('word_count')} 字")
    voice = result.get("voice") or {}
    check("产出了语音与气口时间轴",
          bool(voice.get("wav")) and len(voice.get("utterances") or []) > 0,
          f"{voice.get('total_duration_s')}s / {len(voice.get('utterances') or [])} 个气口句")
    check("气口句比段落更细",
          bool(voice.get("segments"))
          and len(voice["utterances"]) >= len(voice["segments"]),
          f"{len(voice.get('segments') or [])} 段 -> {len(voice.get('utterances') or [])} 句")

    avatar = result.get("avatar") or {}
    check("画面来自场景照片",
          Path(str(avatar.get("scene_photo") or "")).name == scene.name,
          Path(str(avatar.get("scene_photo") or "")).name)
    check("形象作为身份参考传下去了", bool(avatar.get("identity_photo")),
          Path(str(avatar.get("identity_photo") or "")).name)
    check("默认不把身份照叠到画面上（避免出现两个人）",
          not bool((config.step_cfg("avatar") or {}).get("use_identity_overlay")))

    video = result.get("video") or {}
    final = Path(str(video.get("video") or ""))
    check("产出了成片", final.exists() and final.stat().st_size > 0,
          f"{video.get('duration_s')}s / {video.get('size_mb')} MB")
    check("产出了两种封面",
          Path(str(video.get("cover"))).exists()
          and Path(str(video.get("cover_3x4"))).exists())
    check("状态可 JSON 序列化（checkpointer 的前提）",
          isinstance(json.loads(json.dumps(result, ensure_ascii=False, default=str)), dict))
    shutil.rmtree(run_dir, ignore_errors=True)

    # ---------------------------------------------------------- 闸门
    print("\n5. 人工闸门：中断 + 跨进程恢复 + 打回循环")
    gated = LocalFixtureVideoFlow(config, gates=("script", "voice"), log=lambda _m: None)
    run_dir = G.VideoFlow.new_run_dir(config, "flow-gate")
    gate_thread = f"{run_dir.name}-gate"
    first = gated.run(base_inputs(config, run_dir, scene, "闸门验证"),
                      thread_id=gate_thread)
    check("在 script 闸门被中断",
          first["status"] == "interrupted" and first["gate"] == "script",
          f"{first['status']}/{first.get('gate')}")
    check("中断前只跑了 3 个节点",
          [e["node"] for e in first["trace"]] == ["preflight", "voice_clone", "script"],
          str([e["node"] for e in first["trace"]]))

    fresh = LocalFixtureVideoFlow(config, gates=("script", "voice"), log=lambda _m: None)
    snapshot = fresh.state_of(gate_thread)
    check("新实例能读到旧线程状态",
          (snapshot.get("values") or {}).get("run_dir") == str(run_dir))

    approved = fresh.resume({"action": "approve"}, thread_id=gate_thread)
    check("通过后前进到 voice 闸门",
          approved["status"] == "interrupted" and approved["gate"] == "voice",
          f"{approved['status']}/{approved.get('gate')}")
    ran = [e["node"] for e in approved["trace"]]
    check("恢复后没有重跑已完成的前两个节点",
          ran.count("voice_clone") == 1 and ran.count("script") == 1, str(ran))

    before = int((fresh.state_of(gate_thread).get("values") or {}).get("revision", 0))
    rejected = fresh.resume({"action": "reject"}, thread_id=gate_thread)
    after = int((fresh.state_of(gate_thread).get("values") or {}).get("revision", 0))
    check("打回后 revision 递增", after == before + 1, f"{before} -> {after}")
    check("打回后被再次中断", rejected["status"] == "interrupted", rejected["status"])
    shutil.rmtree(run_dir, ignore_errors=True)
    shutil.rmtree(config.path(config.get("project.out_dir", "runs")) / "_graph",
                  ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)

    print("\n" + "=" * 74)
    if problems:
        print(f"  {FAIL} {len(problems)} 项未通过：")
        for item in problems:
            print(f"    - {item}")
        return 1
    print(f"  {PASS} 全部通过 —— LangGraph 自包含编排可用")
    print("\n  图的形状：")
    print("    START -> preflight ──(缺条件)──> fail -> END")
    print("                       └──(齐备)──> voice_clone -> script -> tts")
    print("                            -> avatar -> subtitles -> compose")
    print("                            -> metadata -> END")
    print("    （画面来自每次上传的场景照片，没有背景图节点）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
