"""Web + LangGraph 流程的集成测试。

验证页面上的「LangGraph 流程」模式端到端可用：

    1. bootstrap 里带上了流程节点表和闸门表
    2. POST /api/flow/run 能启动，SSE 能收到逐节点进度
    3. 前置条件缺失时，收到 preflight 错误 + run_done(flow_status=failed)
    4. 开了闸门会在 script 处中断，run_done 里带上 gate 与 payload
    5. POST /api/flow/resume 能恢复，且**已完成的节点不会重跑**
    6. 流程跑完后：/api/result 能回看、/media 能取成片
    7. API 异常后可以下载已完成片段，并从 LangGraph 检查点续跑

    python scripts/smoke_flow_web.py
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autovid.config import Config                    # noqa: E402
from autovid import graph as G                       # noqa: E402
from autovid.assets import AssetStore                # noqa: E402
from autovid.providers import ProviderError          # noqa: E402
from autovid.web.server import Handler, Workbench    # noqa: E402

PASS, FAIL = "[通过]", "[失败]"
problems: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {PASS if ok else FAIL} {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        problems.append(name)
    return ok


def get_json(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_json(base: str, path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        base + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def stream_until_done(base: str, run_id: str, timeout_s: int = 420) -> list[dict]:
    """读 SSE，直到收到 run_done。"""
    rid = urllib.parse.quote(run_id)
    events: list[dict] = []
    deadline = time.time() + timeout_s
    with urllib.request.urlopen(base + "/api/stream?run_id=" + rid, timeout=timeout_s) as stream:
        for raw in stream:
            if time.time() > deadline:
                break
            line = raw.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            events.append(event)
            if event.get("type") == "run_done":
                break
    return events


class LocalFixtureVideoFlow(G.VideoFlow):
    """Web 集成测试使用 silent/still 夹具，不放宽真实工作流的严格检查。"""

    failed_avatar_topics: set[str] = set()

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

    def node_tts(self, state: G.FlowState) -> dict:
        if state.get("topic") == "__mock_api_failure__":
            raise ProviderError("语音 API「mock-cloud」失败：厂商返回空音频")
        return super().node_tts(state)

    def node_avatar(self, state: G.FlowState) -> dict:
        if (state.get("topic") == "__mock_avatar_failure__"
                and str(state.get("topic")) not in self.failed_avatar_topics):
            self.failed_avatar_topics.add(str(state.get("topic")))
            raise ProviderError("数字人 API「mock-avatar」失败：临时服务异常")
        return super().node_avatar(state)


def main() -> int:
    config = Config.load(root=ROOT)
    # 用真实资产，但把引擎换成快的
    config = config.with_overrides({
        "steps.voice.provider": "silent",
        "steps.avatar.provider": "still",
        "project.out_dir": ".tmp/flow_web_runs",
        "project.scenes_dir": ".tmp/flow_web_scenes",
    })
    assets = AssetStore(config).summary()
    if assets.get("voices"):
        config.raw["project"]["voice_id"] = assets["voices"][0]["id"]
    if assets.get("avatars"):
        config.raw["project"]["avatar_id"] = assets["avatars"][0]["id"]
    app = Workbench(config, flow_cls=LocalFixtureVideoFlow)
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    print("Web + LangGraph 流程集成测试")
    print("=" * 74)
    print(f"  测试服务: {base}\n")

    created: list[str] = []
    try:
        # ---------------------------------------------------------- bootstrap
        print("1. bootstrap 暴露流程信息")
        boot = get_json(base, "/api/bootstrap")
        flow = boot.get("flow") or {}
        nodes = [n["name"] for n in flow.get("nodes") or []]
        check("带上了流程节点表", len(nodes) == 8, str(nodes))
        check("节点顺序符合设计（没有背景图节点）",
              nodes == ["preflight", "voice_clone", "script", "tts",
                        "avatar", "subtitles", "compose", "metadata"],
              str(nodes))
        check("带上了闸门表",
              [g["name"] for g in flow.get("gates") or []] == ["script", "voice"],
              str(flow.get("gates")))
        check("节点都有中文标题",
              all(n.get("title") for n in flow.get("nodes") or []),
              str([n["title"] for n in (flow.get("nodes") or [])[:4]]))

        # ---------------------------------------------------------- 场景照片
        print("\n1.5 上传本次场景照片（画面与背景的来源）")
        scene_dir = ROOT / ".tmp" / "flow_web_probe"
        scene_dir.mkdir(parents=True, exist_ok=True)
        from autovid import media as M2
        scene_file = scene_dir / "scene.png"
        M2.make_gradient(scene_file, 800, 1000,
                         ["0x1a1a2e", "0x16213e", "0x0f3460"], seed=5)
        request = urllib.request.Request(
            base + "/api/scene?filename=" + urllib.parse.quote("现拍.png"),
            data=scene_file.read_bytes(),
            headers={"Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(request, timeout=60) as resp:
            uploaded = json.loads(resp.read().decode("utf-8"))
        scene_path = uploaded.get("path", "")
        check("POST /api/scene 返回路径与尺寸",
              bool(scene_path) and uploaded.get("width") == 800,
              f"{uploaded.get('name')} {uploaded.get('width')}×{uploaded.get('height')}")
        check("中文文件名也能用", "现拍" in str(uploaded.get("name")),
              str(uploaded.get("name")))
        with urllib.request.urlopen(base + str(uploaded.get("url") or ""), timeout=30) as resp:
            check("能回显照片（页面预览用）",
                  resp.headers.get("Content-Type", "").startswith("image/"),
                  resp.headers.get("Content-Type", ""))
        try:
            urllib.request.urlopen(base + "/api/scene?name=../../config/pipeline.json",
                                   timeout=20)
            check("场景照片读取防目录穿越", False, "竟然读到了 scenes 之外的文件")
        except urllib.error.HTTPError as exc:
            check("场景照片读取防目录穿越", exc.code == 404, f"HTTP {exc.code}")

        # ---------------------------------------------------------- 前置拦截
        print("\n2. 前置条件缺失 -> 图自己中止并说明原因")
        strict = Config(raw=config.as_dict(), root=config.root, secrets=config.secrets)
        strict.raw["project"]["voice_id"] = ""
        strict.raw["project"]["avatar_id"] = ""
        strict.raw["project"]["require_assets"] = True
        saved = app.config
        app.config = strict
        started = post_json(base, "/api/flow/run",
                            {"topic": "", "script_text": "", "gates": []})
        run_id = started["run_id"]
        created.append(run_id)
        events = stream_until_done(base, run_id)
        kinds = [e["type"] for e in events]
        check("收到了 preflight 事件", "preflight" in kinds, str(sorted(set(kinds))))
        pf = next((e for e in events if e["type"] == "preflight"), {})
        check("preflight 报告了缺什么（含场景照片）",
              len(pf.get("errors") or []) >= 4
              and any("场景照片" in str(x) for x in pf.get("errors") or []),
              str(pf.get("errors"))[:130])
        done = next((e for e in events if e["type"] == "run_done"), {})
        check("flow_status = failed", done.get("flow_status") == "failed",
              str(done.get("flow_status")))
        check("带回了失败原因文本",
              "文案没准备好" in str(done.get("failure")), str(done.get("failure"))[:60])
        check("没有产出成片", not (Path(app.run_dir(run_id)) / "compose" / "final.mp4").exists())
        app.config = saved

        # ---------------------------------------------------------- 正常流程
        print("\n3. 启动流程 -> 逐节点进度 -> 跑完")
        started = post_json(base, "/api/flow/run",
                            {"topic": "Web流程集成验证", "script_text": "",
                             "gates": [], "scene_photo": scene_path})
        run_id = started["run_id"]
        created.append(run_id)
        check("返回了 run_id 与 thread_id",
              bool(run_id) and bool(started.get("thread_id")), started.get("thread_id", ""))
        events = stream_until_done(base, run_id)
        started_nodes = [e["step"] for e in events if e["type"] == "step_start"]
        check("SSE 收到全部 8 个节点", len(started_nodes) == 8, str(len(started_nodes)))
        check("节点顺序正确",
              started_nodes == ["preflight", "voice_clone", "script", "tts",
                                "avatar", "subtitles", "compose", "metadata"],
              str(started_nodes))
        done = next((e for e in events if e["type"] == "run_done"), {})
        check("flow_status = finished", done.get("flow_status") == "finished",
              str(done.get("flow_status")))
        result = done.get("result") or {}
        check("结果里有成片（相对路径，供 /media 用）",
              bool(result.get("video")) and not str(result["video"]).startswith("D:"),
              str(result.get("video")))
        check("结果里如实标了没克隆",
              (result.get("audio") or {}).get("cloned") is False,
              str((result.get("audio") or {}).get("note"))[:60])

        # ---------------------------------------------------------- 回看与下载
        print("\n4. 流程模式的回看 / 取片（无 manifest 兜底）")
        viewed = get_json(base, f"/api/result?run_id={urllib.parse.quote(run_id)}")
        check("/api/result 能回看流程运行", viewed.get("mode") == "flow",
              str(viewed.get("mode")))
        check("回看结果含成片", bool((viewed.get("result") or {}).get("video")))

        media_url = (f"{base}/media?run_id={urllib.parse.quote(run_id)}"
                     f"&p={urllib.parse.quote(result['video'])}")
        with urllib.request.urlopen(media_url, timeout=60) as resp:
            head = resp.headers
            chunk = resp.read(1024)
        check("/media 能取到成片", head.get("Content-Type", "").startswith("video/"),
              head.get("Content-Type", ""))
        check("支持 Range（可拖进度条）", head.get("Accept-Ranges") == "bytes")
        request = urllib.request.Request(media_url, headers={"Range": "bytes=0-99"})
        with urllib.request.urlopen(request, timeout=60) as resp:
            check("Range 返回 206", resp.status == 206, str(resp.status))

        # ---------------------------------------------------------- 闸门
        print("\n5. 闸门中断 -> 页面拿到审批所需信息 -> 恢复")
        started = post_json(base, "/api/flow/run",
                            {"topic": "闸门Web验证", "gates": ["script", "voice"],
                             "scene_photo": scene_path})
        run_id = started["run_id"]
        created.append(run_id)
        events = stream_until_done(base, run_id)
        gate_events = [e for e in events if e["type"] == "gate"]
        check("收到 gate 事件（页面据此弹审批框）", len(gate_events) == 1,
              str([e.get("gate") for e in gate_events]))
        check("闸门是 script", gate_events and gate_events[0].get("gate") == "script",
              str(gate_events[0].get("gate")) if gate_events else "-")
        check("闸门载荷里有可审阅的内容",
              bool((gate_events[0].get("payload") or {}).get("segments")) if gate_events else False,
              str(len((gate_events[0].get("payload") or {}).get("segments") or [])) + " 段"
              if gate_events else "-")
        done = next((e for e in events if e["type"] == "run_done"), {})
        check("run_done 带 flow_status=interrupted",
              done.get("flow_status") == "interrupted", str(done.get("flow_status")))
        check("run_done 里也带了 gate 信息（页面好恢复按钮）",
              bool(done.get("gate")) and bool(done.get("payload")))

        ran = [e["step"] for e in events if e["type"] == "step_start"]
        check("中断前只跑了 3 个节点", ran == ["preflight", "voice_clone", "script"],
              str(ran))

        # 页面点「通过」
        post_json(base, "/api/flow/resume",
                  {"run_id": run_id, "decision": {"action": "approve"},
                   "gates": ["script", "voice"]})
        events2 = stream_until_done(base, run_id)
        ran2 = [e["step"] for e in events2 if e["type"] == "step_start"]
        check("恢复后没有重跑已完成的前两个节点",
              "preflight" not in ran2 and "voice_clone" not in ran2, str(ran2))
        check("恢复后跑的是 tts 起", ran2 and ran2[0] == "tts", str(ran2[:3]))
        done2 = next((e for e in events2 if e["type"] == "run_done"), {})
        check("第二次停在 voice 闸门",
              done2.get("flow_status") == "interrupted" and done2.get("gate") == "voice",
              f"{done2.get('flow_status')}/{done2.get('gate')}")

        # 页面点「通过」直到跑完
        for _ in range(4):
            if done2.get("flow_status") != "interrupted":
                break
            post_json(base, "/api/flow/resume",
                      {"run_id": run_id, "decision": {"action": "approve"},
                       "gates": ["script", "voice"]})
            events2 = stream_until_done(base, run_id)
            done2 = next((e for e in events2 if e["type"] == "run_done"), {})
        check("连续通过后再无闸门，最终 finished",
              done2.get("flow_status") == "finished", str(done2.get("flow_status")))

        # ---------------------------------------------------------- 打回
        print("\n6. 打回 -> 条件边回到上一步 -> revision 递增")
        started = post_json(base, "/api/flow/run",
                            {"topic": "打回验证", "gates": ["script"],
                             "scene_photo": scene_path})
        run_id = started["run_id"]
        created.append(run_id)
        stream_until_done(base, run_id)
        before = app.flow_state(run_id).get("revision", 0)
        post_json(base, "/api/flow/resume",
                  {"run_id": run_id, "decision": {"action": "reject"}, "gates": ["script"]})
        events3 = stream_until_done(base, run_id)
        after = app.flow_state(run_id).get("revision", 0)
        done3 = next((e for e in events3 if e["type"] == "run_done"), {})
        check("打回后 revision 递增", int(after) > int(before), f"{before} -> {after}")
        check("打回后重新停在同一个闸门",
              done3.get("flow_status") == "interrupted" and done3.get("gate") == "script",
              f"{done3.get('flow_status')}/{done3.get('gate')}")

        # ---------------------------------------------------------- 运行时 API 失败
        print("\n7. API 失败 -> 不回退，页面只显示明确错误")
        started = post_json(base, "/api/flow/run",
                            {"topic": "__mock_api_failure__", "gates": [],
                             "scene_photo": scene_path})
        run_id = started["run_id"]
        created.append(run_id)
        failed_events = stream_until_done(base, run_id)
        failed_done = next((e for e in failed_events if e["type"] == "run_done"), {})
        failed_nodes = [e["step"] for e in failed_events if e["type"] == "step_start"]
        check("失败停在语音步骤，没有继续生成数字人",
              failed_done.get("failed_step") == "tts" and "avatar" not in failed_nodes,
              f"{failed_done.get('failed_step')} / {failed_nodes}")
        check("页面错误点明是哪一个 API 失败",
              "语音 API「mock-cloud」失败" in str(failed_done.get("failure")),
              str(failed_done.get("failure")))
        check("SSE 不再发送整段 traceback",
              "Traceback" not in json.dumps(failed_events, ensure_ascii=False))
        error_log = Path(app.run_dir(run_id)) / "_logs" / "flow-error.log"
        check("完整堆栈只保存在运行目录", error_log.exists())

        # ---------------------------------------------------------- 异常续跑
        print("\n8. 异常后下载片段 -> 从失败节点继续")
        started = post_json(base, "/api/flow/run",
                            {"topic": "__mock_avatar_failure__", "gates": [],
                             "scene_photo": scene_path})
        run_id = started["run_id"]
        created.append(run_id)
        interrupted = stream_until_done(base, run_id)
        interrupted_done = next(
            (e for e in interrupted if e["type"] == "run_done"), {})
        partial_result = interrupted_done.get("result") or {}
        check("数字人异常前的语音片段被保留",
              interrupted_done.get("failed_step") == "avatar"
              and partial_result.get("has_partial") is True,
              str(partial_result.get("partial_count")))
        check("失败结果标记为可以继续", partial_result.get("can_resume") is True)

        with urllib.request.urlopen(
                f"{base}/fragments?run_id={urllib.parse.quote(run_id)}", timeout=120) as resp:
            blob = resp.read()
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            fragment_names = archive.namelist()
        check("片段下载包里有已完成的音频",
              any(name.endswith(".wav") for name in fragment_names),
              ", ".join(fragment_names[:5]))

        post_json(base, "/api/flow/continue", {"run_id": run_id})
        resumed = stream_until_done(base, run_id)
        resumed_nodes = [e["step"] for e in resumed if e["type"] == "step_start"]
        resumed_done = next((e for e in resumed if e["type"] == "run_done"), {})
        check("续跑从失败的数字人节点开始",
              resumed_nodes and resumed_nodes[0] == "avatar"
              and "tts" not in resumed_nodes and "script" not in resumed_nodes,
              str(resumed_nodes))
        check("续跑后最终完成", resumed_done.get("flow_status") == "finished",
              str(resumed_done.get("flow_status")))

    finally:
        httpd.shutdown()
        httpd.server_close()
        runs_dir = config.path(config.get("project.out_dir", "runs"))
        for rid in created:
            shutil.rmtree(runs_dir / rid, ignore_errors=True)
        shutil.rmtree(runs_dir / "_graph", ignore_errors=True)
        shutil.rmtree(runs_dir, ignore_errors=True)
        shutil.rmtree(config.path(config.get("project.scenes_dir")), ignore_errors=True)

    print("\n" + "=" * 74)
    if problems:
        print(f"  {FAIL} {len(problems)} 项未通过：")
        for item in problems:
            print(f"    - {item}")
        return 1
    print(f"  {PASS} 全部通过 —— 页面上的 LangGraph 流程可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
