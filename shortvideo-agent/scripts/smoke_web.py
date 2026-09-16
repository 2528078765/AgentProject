"""Web 工作台冒烟测试。

在进程内起一个真实的 HTTP 服务（随机空闲端口），然后完整走一遍前端会走的路径：
    GET /               页面能返回
    GET /api/bootstrap  能力探测
    POST /api/plan      只出计划、不落盘
    POST /api/run       真跑一条
    GET /api/stream     SSE 事件流（含 run_done）
    GET /api/result     结果负载
    GET /media          Range 请求（视频能拖动进度条的前提）
    GET /package        发布包 zip

    python scripts/smoke_web.py
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autovid.config import Config          # noqa: E402
from autovid.web.server import Handler, Workbench  # noqa: E402

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
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    config = Config.load(root=ROOT)
    app = Workbench(config)
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)   # 端口 0 = 让系统给个空闲端口
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    print("Web 工作台冒烟测试")
    print("=" * 64)
    print(f"  测试服务地址: {base}\n")

    try:
        # ---------------------------------------------------------- 页面
        print("1. 静态页面与能力探测")
        with urllib.request.urlopen(base + "/", timeout=30) as resp:
            html = resp.read().decode("utf-8")
            ctype = resp.headers.get("Content-Type", "")
        check("GET / 返回 HTML", "AutoVid" in html and "<!DOCTYPE html>" in html,
              f"{len(html)} 字节")
        check("Content-Type 带 charset", "charset=utf-8" in ctype, ctype)
        check("设置页保留推荐与 Key 申请入口",
              html.count("推荐 / 申请 Key") >= 2 and "去官网申请 Key" in html)
        check("音色库和形象库列表都有直接删除入口",
              "data-delete-voice" in html and "data-delete-avatar" in html)

        settings = get_json(base, "/api/settings/recommend")
        video_catalog = (settings.get("catalog") or {}).get("video") or []
        first_video = video_catalog[0] if video_catalog else {}
        check("数字人推荐首项是国内低成本自然讲解方案",
              first_video.get("preset") == "dashscope_s2v",
              str(first_video.get("name") or "无"))
        check("不再推荐已明确排除的 HeyGen / D-ID",
              not ({"HeyGen", "D-ID"} & {str(x.get("name")) for x in video_catalog}))
        check("推荐项提供直接申请 Key 的地址",
              str(first_video.get("key_url") or "").startswith("https://"),
              str(first_video.get("key_url") or "无"))

        providers = settings.get("providers") or []
        if providers:
            fake_result = {"ok": True, "level": "success", "message": "连接成功，Key 有效",
                           "status_code": 200, "latency_ms": 18}
            with patch("autovid.providers_registry.test_connection", return_value=fake_result):
                tested = post_json(base, "/api/settings/providers/test", {"id": providers[0]["id"]})
            check("提供商测试连通入口可用且不提交生成任务",
                  tested.get("result") == fake_result)
        else:
            check("提供商测试连通入口可用且不提交生成任务", False, "没有可测试的提供商")

        boot = get_json(base, "/api/bootstrap")
        check("GET /api/bootstrap", len(boot.get("steps", [])) == 9,
              f"{len(boot.get('steps', []))} 个步骤 / {len(boot.get('providers', []))} 个 provider")
        check("provider 选项齐备", all(k in boot.get("options", {}) for k in
                                      ("script", "voice", "visuals", "avatar", "metadata")))

        # 下拉框必须自己说清楚「哪个现在能用」，不能让用户瞎试
        info = boot.get("option_info") or {}
        check("每个环节都带选项可用状态",
              all(k in info for k in ("script", "voice", "visuals", "avatar")),
              str(sorted(info.keys())))
        check("可用状态都含 available + detail",
              bool(info) and all("available" in v and "detail" in v
                                 for step in info.values() for v in step.values()))
        rec = boot.get("recommended") or {}
        check("给出了「一键推荐」配置", len(rec) >= 4, f"{len(rec)} 项")
        bad_rec = [f"{k}={v}" for k, v in rec.items()
                   if k != "steps.metadata.provider"
                   and not (info.get(k.split(".")[1], {}).get(v) or {}).get("available", False)]
        check("推荐的都是当前可用的选项", not bad_rec, str(bad_rec))
        check("选项说明是准确的（不是笼统套话）",
              "静音占位" in str((info.get("voice", {}).get("silent") or {}).get("detail"))
              and "工作流" in str((info.get("avatar", {}).get("comfy") or {}).get("detail")),
              str((info.get("avatar", {}).get("comfy") or {}).get("detail")))

        # ---------------------------------------------------------- 计划
        print("\n2. 只看计划（不应产生任何副作用）")
        # 用 total 而不是列表长度：列表默认只回最近 30 条，
        # 跑过 30 次之后列表长度就恒等于 30，「有没有增加」会永远看不出来。
        runs_before = int(get_json(base, "/api/runs").get("total", 0))
        plan = post_json(base, "/api/plan", {"topic": "web 冒烟测试选题", "overrides": {}})
        runs_after = int(get_json(base, "/api/runs").get("total", 0))
        check("POST /api/plan 返回 9 步", len(plan.get("steps", [])) == 9)
        check("plan 不落盘", runs_before == runs_after,
              f"运行数 {runs_before} -> {runs_after}")

        # ---------------------------------------------------------- 真跑
        print("\n3. 真实运行 + SSE 进度流")
        started = post_json(base, "/api/run", {
            "topic": "web 冒烟测试选题",
            "overrides": {
                "steps.script.provider": "offline",
                "steps.voice.provider": "silent",
                "steps.visuals.provider": "ffmpeg_gradient",
                "steps.avatar.provider": "still",
                # 冒烟测试不准备资产，显式关掉「必须有音色+形象」的闸门
                "project.require_assets": False,
            },
        })
        run_id = started.get("run_id", "")
        check("POST /api/run 返回 run_id", bool(run_id), run_id)
        # run_id 里含中文选题（目录名友好），所有 URL 都必须做百分号编码 ——
        # 前端用 encodeURIComponent，这里用 urllib.parse.quote。
        rid = urllib.parse.quote(run_id)

        kinds: list[str] = []
        done: dict = {}
        deadline = time.time() + 420
        with urllib.request.urlopen(base + "/api/stream?run_id=" + rid, timeout=420) as stream:
            for raw in stream:
                if time.time() > deadline:
                    break
                line = raw.decode("utf-8").strip()
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                kinds.append(event.get("type", ""))
                if event.get("type") == "run_done":
                    done = event
                    break
        check("SSE 收到 plan 事件", "plan" in kinds)
        check("SSE 收到全部 9 个 step_start", kinds.count("step_start") == 9,
              f"实际 {kinds.count('step_start')} 个")
        check("SSE 收到 run_done", bool(done))
        check("运行成功", bool(done.get("ok")), str(done.get("error") or ""))

        # ---------------------------------------------------------- 结果
        print("\n4. 结果与文件服务")
        result = get_json(base, "/api/result?run_id=" + rid).get("result", {})
        check("结果含成片", bool(result.get("video")), str(result.get("video")))
        check("结果含标题候选", len(result.get("titles", [])) > 0,
              f"{len(result.get('titles', []))} 个")
        check("结果含话题标签", len(result.get("tags", [])) > 0)
        check("时长可读", isinstance(result.get("duration_s"), (int, float)),
              f"{result.get('duration_s')}s")

        video_url = f"{base}/media?run_id={rid}&p={urllib.parse.quote(result['video'])}"
        # 无 Range：应返回 200 且带 Accept-Ranges
        with urllib.request.urlopen(video_url, timeout=60) as resp:
            head = resp.headers
            check("GET /media 支持 Accept-Ranges", head.get("Accept-Ranges") == "bytes")
            check("GET /media 返回视频", head.get("Content-Type", "").startswith("video/"),
                  head.get("Content-Type", ""))

        # 带 Range：应返回 206 + Content-Range（浏览器拖动进度条依赖这个）
        req = urllib.request.Request(video_url, headers={"Range": "bytes=100-599"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read()
            check("Range 请求返回 206", resp.status == 206, str(resp.status))
            check("Content-Range 正确", resp.headers.get("Content-Range", "").startswith("bytes 100-599/"),
                  resp.headers.get("Content-Range", ""))
            check("Range 返回 500 字节", len(body) == 500, f"{len(body)} 字节")

        # 目录穿越防护
        try:
            urllib.request.urlopen(f"{base}/media?run_id={rid}&p=../../autovid/cli.py", timeout=30)
            check("目录穿越被拦截", False, "竟然能读到 run 目录外的文件")
        except urllib.error.HTTPError as exc:
            check("目录穿越被拦截", exc.code == 404, f"HTTP {exc.code}")

        # ---------------------------------------------------------- 发布包
        print("\n5. 发布包下载")
        with urllib.request.urlopen(f"{base}/package?run_id={rid}", timeout=120) as resp:
            blob = resp.read()
            disposition = resp.headers.get("Content-Disposition", "")
        check("GET /package 返回 zip", blob[:2] == b"PK", f"{len(blob) / 1024:.0f} KB")
        check("带下载文件名", "attachment" in disposition, disposition)
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            names = archive.namelist()
        check("zip 内含成片", any(n.endswith("video.mp4") for n in names), ", ".join(names[:6]))
        check("zip 内含文案与清单",
              any("发布文案" in n for n in names) and any("发布清单" in n for n in names))

        # ---------------------------------------------------------- 历史
        print("\n6. 历史运行")
        runs = get_json(base, "/api/runs?limit=500").get("runs", [])
        runs_total = int(get_json(base, "/api/runs").get("total", 0))
        check("历史列表含本次运行", any(r["run_id"] == run_id for r in runs))
        check("runs 数量增加", runs_total > runs_before, f"{runs_before} -> {runs_total}")

    finally:
        httpd.shutdown()
        httpd.server_close()

    print("\n" + "=" * 64)
    if problems:
        print(f"  {FAIL} {len(problems)} 项未通过：")
        for item in problems:
            print(f"    - {item}")
        return 1
    print(f"  {PASS} 全部通过 —— Web 工作台功能正常")
    print(f"\n  启动方式：python -m autovid web --open")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
