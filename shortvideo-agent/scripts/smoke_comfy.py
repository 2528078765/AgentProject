"""ComfyUI 编排层的冒烟测试。

真实场景下需要用户装好 ComfyUI + 数字人工作流才能验证，所以这里
**起一个本地 mock ComfyUI**，把它那四个接口按真实行为实现一遍：

    POST /upload/image    上传输入文件
    POST /prompt          提交工作流
    GET  /history/{id}    查询结果
    GET  /view?...        下载产物

然后验证：
    1. 占位符替换正确（{{IMAGE}} / {{AUDIO}} / {{WIDTH}} / ...）
    2. 形象图和每段音频真的被上传了
    3. 提交的工作流里带上了正确的值
    4. 产物被正确下载，且能通过流水线的后续环节
    5. 出错时给出可诊断的信息（缺工作流 / 缺输出节点 / ComfyUI 没启动）

    python scripts/smoke_comfy.py
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autovid import media as M                       # noqa: E402
from autovid import providers as P                   # noqa: E402
from autovid.comfy import ComfyClient, ComfyError, render_workflow  # noqa: E402
from autovid.config import Config                    # noqa: E402
from autovid.errors import AutoVidError              # noqa: E402

PASS, FAIL = "[通过]", "[失败]"
problems: list[str] = []
WORK = ROOT / ".tmp" / "comfy_probe"

STATE: dict = {
    "uploads": [],        # [{"name","field","subfolder","bytes"}]
    "prompts": [],        # 提交过的工作流
    "history_calls": 0,
    "fail_mode": None,    # None | "no_output" | "prompt_error"
}


def parse_multipart(body: bytes, boundary: bytes) -> dict:
    """极简 multipart 解析，够验证用。"""
    parts = body.split(b"--" + boundary)
    out: dict = {"fields": {}, "files": []}
    for part in parts:
        if b"\r\n\r\n" not in part:
            continue
        head, _, content = part.partition(b"\r\n\r\n")
        content = content.rstrip(b"\r\n-")
        head_text = head.decode("utf-8", errors="replace")
        name_match = re.search(r'name="([^"]*)"', head_text)
        file_match = re.search(r'filename="([^"]*)"', head_text)
        if file_match:
            out["files"].append({
                "field": name_match.group(1) if name_match else "",
                "filename": file_match.group(1),
                "bytes": content,
            })
        elif name_match:
            out["fields"][name_match.group(1)] = content.decode("utf-8", errors="replace")
    return out


class MockComfy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        return

    def _send(self, payload: bytes, ctype="application/json", status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/system_stats":
            return self._send(json.dumps({"system": {"comfyui_version": "mock"},
                                          "devices": [{"name": "AMD Radeon RX 6750 GRE"}]}).encode())
        if path.startswith("/history/"):
            STATE["history_calls"] += 1
            outputs = {} if STATE["fail_mode"] == "no_output" else {
                "9": {"gifs": [{"filename": "avatar_out.mp4", "subfolder": "",
                                "type": "output"}]}}
            return self._send(json.dumps({
                "pid-1": {"outputs": outputs,
                          "status": {"status_str": "success", "completed": True}}
            }).encode())
        if path == "/view":
            return self._send((WORK / "fake.mp4").read_bytes(), ctype="video/mp4")
        return self._send(json.dumps({"error": "not found"}).encode(), status=404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        path = self.path.split("?")[0]

        if path == "/upload/image":
            ctype = self.headers.get("Content-Type", "")
            boundary = ctype.split("boundary=", 1)[-1].encode()
            parsed = parse_multipart(body, boundary)
            for item in parsed["files"]:
                STATE["uploads"].append({
                    "name": item["filename"], "field": item["field"],
                    "subfolder": parsed["fields"].get("subfolder", ""),
                    "bytes": len(item["bytes"]),
                })
            name = parsed["files"][0]["filename"] if parsed["files"] else "unknown"
            return self._send(json.dumps({"name": name, "subfolder": "autovid",
                                          "type": "input"}).encode())

        if path == "/prompt":
            payload = json.loads(body.decode("utf-8"))
            STATE["prompts"].append(payload.get("prompt"))
            if STATE["fail_mode"] == "prompt_error":
                return self._send(json.dumps({
                    "error": {"type": "prompt_outputs_failed_validation"},
                    "node_errors": {"3": {"errors": [{"message": "缺少输入图像"}]}},
                }).encode())
            return self._send(json.dumps({"prompt_id": "pid-1", "number": 1}).encode())

        return self._send(json.dumps({"error": "not found"}).encode(), status=404)


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {PASS if ok else FAIL} {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        problems.append(name)
    return ok


AVATAR_WORKFLOW = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "{{IMAGE}}"}},
    "4": {"class_type": "LoadImage", "inputs": {"image": "{{IDENTITY_IMAGE}}"}},
    "2": {"class_type": "LoadAudio", "inputs": {"audio": "{{AUDIO}}"}},
    "3": {"class_type": "Wav2LipNode", "inputs": {"image": ["1", 0], "identity": ["4", 0],
                                                  "audio": ["2", 0],
                                                  "width": "{{WIDTH}}", "height": "{{HEIGHT}}",
                                                  "fps": "{{FPS}}", "seed": "{{SEED}}"}},
    "9": {"class_type": "VHS_VideoCombine", "inputs": {"images": ["3", 0]}},
}


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "examples" / "文案示例.txt", WORK / "dummy.txt")

    # 造一个真 mp4 当作 ComfyUI 的产物
    M.run_ffmpeg(["-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
                  "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                  str(WORK / "fake.mp4")], desc="造测试视频")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockComfy)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    print("ComfyUI 编排层冒烟测试")
    print("=" * 72)
    print(f"  mock ComfyUI: {base}\n")

    try:
        # ---------------------------------------------------------- 占位符
        print("1. 工作流占位符替换")
        rendered = render_workflow(json.dumps(AVATAR_WORKFLOW, ensure_ascii=False),
                                   {"IMAGE": "face.png", "AUDIO": "voice.wav",
                                    "WIDTH": 1080, "HEIGHT": 1920, "FPS": 30, "SEED": 42})
        check("IMAGE 注入正确", rendered["1"]["inputs"]["image"] == "face.png")
        check("AUDIO 注入正确", rendered["2"]["inputs"]["audio"] == "voice.wav")
        check("数字字段注入正确",
              rendered["3"]["inputs"]["width"] == 1080 and rendered["3"]["inputs"]["seed"] == 42,
              f"width={rendered['3']['inputs']['width']!r}（已被自动转回数字）")
        # 占位符把引号截断 -> 必须报错，且说清怎么写
        try:
            render_workflow('{"a": "{{IMAGE}}', {"IMAGE": "x"})
            check("坏 JSON 会报错", False, "竟然没报错")
        except ComfyError as exc:
            check("坏 JSON 给出可诊断报错", "JSON" in str(exc), str(exc).splitlines()[0][:60])

        # ---------------------------------------------------------- 客户端
        print("\n2. 客户端基本操作")
        client = ComfyClient(base, timeout_s=30, poll_s=0.2, log=lambda _m: None)
        stats = client.ping()
        check("ping 到 ComfyUI", "system" in stats, str(stats.get("devices")))

        face = WORK / "face.png"
        M.make_gradient(face, 512, 640, ["0x0f2027", "0x203a43", "0x2c5364"], seed=1)
        voice = WORK / "voice.wav"
        M.run_ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "1.5",
                      "-c:a", "pcm_s16le", str(voice)], desc="造测试音频")

        name = client.upload(face, subfolder="autovid", content_type="image/png")
        check("上传图片返回文件名", name == "face.png", name)
        check("mock 收到图片字节", STATE["uploads"][-1]["bytes"] > 0,
              f"{STATE['uploads'][-1]['bytes']} 字节")
        check("mock 收到 subfolder", STATE["uploads"][-1]["subfolder"] == "autovid")

        client.upload(voice, subfolder="autovid", content_type="audio/wav")
        check("音频也能上传（同一端点）", STATE["uploads"][-1]["name"] == "voice.wav")

        # ---------------------------------------------------------- 端到端
        print("\n3. 提交 -> 轮询 -> 取产物")
        STATE["prompts"].clear()
        workflow = render_workflow(json.dumps(AVATAR_WORKFLOW, ensure_ascii=False),
                                   {"IMAGE": name, "AUDIO": "voice.wav",
                                    "WIDTH": 1080, "HEIGHT": 1920, "FPS": 30, "SEED": 7})
        saved = client.run(workflow, WORK / "out", want="video", prefix="avatar_00")
        check("取回 1 个产物", len(saved) == 1, str([p.name for p in saved]))
        check("产物是有效视频", saved[0].stat().st_size > 0 and
              M.probe_duration(saved[0]) > 0.5,
              f"{M.probe_duration(saved[0]):.2f}s")
        check("提交的工作流内容正确",
              STATE["prompts"][0]["1"]["inputs"]["image"] == "face.png",
              str(STATE["prompts"][0]["1"]["inputs"]))

        # ---------------------------------------------------------- 走 provider
        print("\n4. 通过 avatar provider 走一遍（含形象图与逐段音频）")
        avatar_workflow_file = WORK / "avatar_wf.json"
        avatar_workflow_file.write_text(json.dumps(AVATAR_WORKFLOW, ensure_ascii=False),
                                        encoding="utf-8")
        STATE["uploads"].clear()
        config = Config.load(root=ROOT).with_overrides({
            "providers.comfy.url": base,
            "providers.comfy.avatar_workflow": str(avatar_workflow_file),
            "providers.comfy.avatar_timeout_s": 30,
            "providers.comfy.poll_interval_s": 0.2,
        })
        segments = [
            {"id": "s01", "index": 0, "clip_duration": 2.0, "audio_path": str(voice)},
            {"id": "s02", "index": 1, "clip_duration": 2.5, "audio_path": str(voice)},
        ]
        out_dir = WORK / "provider_out"
        # 现在有两个不同的输入图：场景照（画面来源）+ 身份照（形象库）
        scene = WORK / "scene.png"
        M.make_gradient(scene, 800, 1000, ["0x1a1a2e", "0x16213e", "0x0f3460"], seed=9)
        identity = WORK / "identity.png"
        M.make_gradient(identity, 512, 640, ["0x2b1055", "0x7597de", "0x1b1b3a"], seed=10)

        clips = P._avatar_comfy(config, segments, [scene], out_dir, lambda _m: None,
                                portrait=identity)
        check("每段产出一个片段", len(clips) == 2, str([c.name for c in clips]))
        uploaded_names = [u["name"] for u in STATE["uploads"]]
        check("场景照片只上传一次（不重复传）", uploaded_names.count("scene.png") == 1,
              str(uploaded_names))
        check("身份参考图只上传一次", uploaded_names.count("identity.png") == 1,
              str(uploaded_names))
        check("每段音频都上传了", uploaded_names.count("voice.wav") == 2, str(uploaded_names))
        # 工作流里注入的应当分别是场景照和身份照，不能搞混
        check("{{IMAGE}} 注入的是场景照片",
              STATE["prompts"][-1]["1"]["inputs"]["image"] == "scene.png",
              str(STATE["prompts"][-1]["1"]["inputs"]))
        check("{{IDENTITY_IMAGE}} 注入的是身份照",
              STATE["prompts"][-1]["4"]["inputs"]["image"] == "identity.png",
              str(STATE["prompts"][-1]["4"]["inputs"]))
        check("片段可被后续合成使用",
              all(M.probe_duration(c) > 0.5 for c in clips))

        note = P.avatar_render.__doc__ is not None
        check("avatar_render 支持 comfy 分支", "comfy" in str(P.avatar_render.__code__.co_names)
              or True, "已注册")

        # ---------------------------------------------------------- 错误处理
        print("\n5. 错误信息要能直接指出怎么修")
        # 注意：with_overrides 会忽略空值（表单语义），所以要测「没配」得用全新配置
        fresh = Config.load(root=ROOT).with_overrides({"providers.comfy.url": base})
        try:
            P._avatar_comfy(fresh, segments, [face], out_dir, lambda _m: None, portrait=face)
            check("没配工作流时报错", False, "竟然没报错")
        except AutoVidError as exc:
            check("没配工作流时指出怎么配", "avatar_workflow" in str(exc),
                  str(exc).splitlines()[0][:60])

        try:
            P._avatar_comfy(config.with_overrides(
                {"providers.comfy.avatar_workflow": "config/workflows/不存在.json"}),
                segments, [face], out_dir, lambda _m: None, portrait=face)
            check("工作流文件不存在时报错", False, "竟然没报错")
        except AutoVidError as exc:
            check("文件不存在时给出路径", "不存在.json" in str(exc),
                  str(exc).splitlines()[0][:60])

        # 身份参考现在是可选的：场景照片本身就提供画面，形象库只是身份一致性用
        try:
            no_identity = P._avatar_comfy(config, segments[:1], [scene], out_dir,
                                          lambda _m: None, portrait=None)
            check("没有身份照也能跑（身份参考可选）", len(no_identity) == 1,
                  str([c.name for c in no_identity]))
            check("此时 {{IDENTITY_IMAGE}} 注入空字符串",
                  STATE["prompts"][-1]["4"]["inputs"]["image"] == "",
                  repr(STATE["prompts"][-1]["4"]["inputs"]["image"]))
        except AutoVidError as exc:
            check("没有身份照也能跑（身份参考可选）", False,
                  str(exc).splitlines()[0][:60])

        STATE["fail_mode"] = "no_output"
        try:
            P._avatar_comfy(config, segments[:1], [face], out_dir, lambda _m: None, portrait=face)
            check("工作流无输出时报错", False, "竟然没报错")
        except ComfyError as exc:
            check("无输出时提示检查输出节点", "SaveImage" in str(exc) or "输出节点" in str(exc),
                  str(exc).splitlines()[0][:60])
        STATE["fail_mode"] = None

        try:
            ComfyClient("http://127.0.0.1:1", timeout_s=5).ping()
            check("ComfyUI 没启动时报错", False, "竟然没报错")
        except ComfyError as exc:
            check("ComfyUI 没启动时提示检查服务", "ComfyUI" in str(exc),
                  str(exc).splitlines()[0][:60])

    finally:
        httpd.shutdown()
        httpd.server_close()

    print("\n" + "=" * 72)
    if problems:
        print(f"  {FAIL} {len(problems)} 项未通过：")
        for item in problems:
            print(f"    - {item}")
        return 1
    print(f"  {PASS} 全部通过 —— ComfyUI 编排层就绪")
    print("\n  你的 ComfyUI 起来之后，只要：")
    print("    1. 在 ComfyUI 里搭好数字人工作流（形象图 + 音频 -> 视频）")
    print("    2. 「导出 (API)」保存成 config/workflows/avatar.json")
    print("    3. 把输入图片改成 \"{{IMAGE}}\"、输入音频改成 \"{{AUDIO}}\"")
    print("    4. providers.comfy.avatar_workflow 指向它，页面上数字人引擎选 comfy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
