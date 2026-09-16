"""集成测试：模板化云端数字人（不花一分钱、不碰真实厂商）。

用一个本地假厂商把 `_avatar_template` 整条路径跑通，专门验四件容易出事的：

  1. **轮询中途断连要能自愈** —— 模拟第 2 次查状态时 TCP 被掐断
     （真实事故就是这里 SSL 断流，把整条工作流带走了）；
  2. **多句合并成一个任务** —— 5 句只该提交 1 次；
  3. **同一个形象只上传一次** —— 不能每段重传（图有几百 KB）；
  4. **断点续跑** —— 第二次跑要复用已下好的片段，一次都不再请求。

假厂商回的是 **512×512** 方片，正好覆盖「云端尺寸和画布不一致」这个坑。
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autovid import media as M            # noqa: E402
from autovid import providers as P        # noqa: E402

PORT = 0
BASE = ""
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


class FakeDID:
    """记录每一次请求，并刻意在第 2 次轮询时掐断连接。"""

    def __init__(self) -> None:
        self.uploads: list[str] = []          # "image" / "audio"
        self.submits = 0
        self.polls = 0
        self.downloads = 0
        self.result_fetches = 0
        self.oss_uploads = 0
        self.result = ROOT / ".tmp" / "smoke_avatar" / "cloud.mp4"
        # 三个开关，用来复刻真机上踩过的坑
        self.no_faces = False                 # 上传响应里报「没检测到人脸」
        self.reject_size = False              # 提交时回 D-ID 的 InvalidFileSizeError
        self.trace = "ti_fake_trace_0001"     # 用于验证 trace-id 真的被记进日志

    def handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):     # 别刷屏
                pass

            def _json(self, payload: dict, code: int = 200, trace: bool = False) -> None:
                blob = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(blob)))
                if trace:
                    self.send_header("x-siliconcloud-trace-id", fake.trace)
                self.end_headers()
                self.wfile.write(blob)

            def _read(self) -> bytes:
                size = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(size) if size else b""

            def do_POST(self) -> None:
                self._read()
                if self.path == "/oss":
                    fake.oss_uploads += 1
                    self.send_response(200)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif self.path == "/images":
                    fake.uploads.append("image")
                    payload: dict = {"url": f"{BASE}/img/a.jpg"}
                    # 实测：D-ID 的 /images 响应里本来就带 faces[]
                    payload["faces"] = [] if fake.no_faces else [
                        {"size": 943, "top_left": [80, -12], "overlap": "NO",
                         "detect_confidence": 100, "sharpness": 100,
                         "face_occluded": False}]
                    self._json(payload)
                elif self.path == "/audios":
                    fake.uploads.append("audio")
                    self._json({"url": f"{BASE}/aud/a.wav"})
                elif self.path == "/fake-model/talks":
                    fake.submits += 1
                    if fake.reject_size:
                        # 复刻实测：上传 201 通过，提交才报，且信息误导
                        self._json({"kind": "InvalidFileSizeError",
                                    "description": "file size exceeded 10 MB - "
                                                   "the maximum size permitted"},
                                   400, trace=True)
                        return
                    self._json({"id": f"job-{fake.submits}"})
                else:
                    self._json({"error": "not found"}, 404)

            def do_GET(self) -> None:
                if self.path.startswith("/policy"):
                    self._json({"data": {
                        "policy": "fake-policy", "signature": "fake-signature",
                        "upload_dir": "tmp/fake", "upload_host": f"{BASE}/oss",
                        "oss_access_key_id": "fake-access-id",
                        "x_oss_object_acl": "private",
                        "x_oss_forbid_overwrite": "true",
                    }})
                elif self.path.startswith("/responses/"):
                    fake.result_fetches += 1
                    self._json({"video": {"url": f"{BASE}/cloud.mp4"}})
                elif self.path.startswith("/talks/"):
                    fake.polls += 1
                    if fake.polls == 2:
                        # 就这里：不发响应直接把连接掐了
                        self.close_connection = True
                        try:
                            self.connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        return
                    if fake.polls < 4:
                        self._json({"status": "started"})
                    else:
                        self._json({"status": "done",
                                    "result_url": f"{BASE}/cloud.mp4"})
                elif self.path == "/cloud.mp4":
                    fake.downloads += 1
                    blob = fake.result.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Length", str(len(blob)))
                    self.end_headers()
                    self.wfile.write(blob)
                else:
                    self._json({"error": "not found"}, 404)

        return H


def build_inputs(work: Path) -> tuple[list[dict], Path]:
    """5 段各 4s 的 wav + 一张形象照 + 一个 512×512 的「成片」。"""
    work.mkdir(parents=True, exist_ok=True)
    segs = []
    for i in range(5):
        path = work / f"seg_{i:02d}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x02" * 16000 * 4)
        segs.append({"audio_path": str(path), "index": i, "duration": 4.0})

    portrait = work / "portrait.jpg"
    subprocess.run([M.ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi",
                    "-i", "testsrc=size=640x800:rate=1", "-frames:v", "1", str(portrait)],
                   check=True, capture_output=True)

    cloud = work / "cloud.mp4"
    subprocess.run([M.ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi",
                    "-i", "testsrc2=size=512x512:rate=30:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cloud)],
                   check=True, capture_output=True)
    return segs, portrait


def make_cfg(cloud: Path) -> dict:
    return {
        "model": "fake-model",
        "merge_max_s": 100,                 # 5 句 20s -> 1 个任务
        "timeout_s": 120,
        "poll_interval_s": 0.1,
        # 能力声明 + 失败签名：和 D-ID 预设同构，用来验证框架真的会读它们
        "capabilities": {
            "image": {"min_side": 160, "max_pixels": 10 * 1024 * 1024,
                      "formats": ["jpeg", "png"], "reports_faces": True},
            "output": {"width": 512, "height": 512, "fps": 25},
        },
        "failure_signatures": [
            {"name": "did_image_too_large",
             "when": {"status_in": [400],
                      "body_key": {"key": "kind", "equals": "InvalidFileSizeError"}},
             "kind": "rejected",
             "label": "D-ID 拒收：InvalidFileSizeError",
             "hint": "这条几乎总是图片像素超限，不是文件体积超限。"},
        ],
        "upload": {
            "image": {"url": f"{BASE}/images", "field": "image",
                      "filename": "a.jpg", "content_type": "image/jpeg",
                      "url_path": "url"},
            "audio": {"url": f"{BASE}/audios", "field": "audio",
                      "filename": "a.wav", "content_type": "audio/wav",
                      "url_path": "url"},
        },
        "submit": {
            # 故意把模型放在 URL 模板里，防止只渲染 body/header、漏掉 URL。
            "url": f"{BASE}/{{{{model}}}}/talks",
            "headers": {"Authorization": "Basic {{api_key}}"},
            "body": {"source_url": "{{image_url}}",
                     "script": {"type": "audio", "audio_url": "{{audio_url}}"}},
            "job_id_path": "id",
        },
        "query": {
            "url": f"{BASE}/talks/{{job_id}}",
            "headers": {"Authorization": "Basic {{api_key}}"},
            "status_path": "status",
            "done_values": ["done"],
            "failed_values": ["error"],
            "result_path": "result_url",
        },
    }


def main() -> int:
    global BASE
    work = ROOT / ".tmp" / "smoke_avatar"
    shutil.rmtree(work, ignore_errors=True)

    fake = FakeDID()
    segs, portrait = build_inputs(work)
    fake.result = work / "cloud.mp4"

    server = ThreadingHTTPServer(("127.0.0.1", PORT), fake.handler())
    BASE = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        print("[0] 百炼临时 OSS 上传协议")
        oss_url = P._dashscope_oss_upload(
            {"policy_url": f"{BASE}/policy?model={{{{model}}}}"},
            b"fake-image-bytes", "autovid.jpg", "image/jpeg",
            {"api_key": "fake", "model": "wan2.2-s2v"},
        )
        check("先取临时凭证再上传文件", fake.oss_uploads == 1,
              f"OSS 上传 {fake.oss_uploads} 次")
        check("返回百炼可识别的 oss 地址",
              oss_url.startswith("oss://tmp/fake/autovid-") and oss_url.endswith(".jpg"),
              oss_url)

        cfg = make_cfg(fake.result)
        out = work / "avatar"
        logs: list[str] = []

        print("[1] 首次运行：断连自愈 + 合并 + 只传一次图")
        clips = P._avatar_template(None, segs, [portrait], out, logs.append,
                                   cfg=cfg, api_key="fake", provider_name="avatar:fake",
                                   portrait=portrait)
        check("产出 1 个片段", len(clips) == 1, str([c.name for c in clips]))
        check("只提交 1 次任务（5 句已合并）", fake.submits == 1, f"{fake.submits} 次")
        check("形象图只上传 1 次", fake.uploads.count("image") == 1,
              str(fake.uploads))
        check("音频上传 1 次", fake.uploads.count("audio") == 1, str(fake.uploads))
        check("断连那一次确实被补了回来", fake.polls == 4, f"轮询 {fake.polls} 次")
        check("成片下载 1 次", fake.downloads == 1, f"{fake.downloads} 次")
        check("片段文件存在且不是空的",
              clips[0].exists() and clips[0].stat().st_size > 10_000,
              f"{clips[0].stat().st_size} 字节" if clips[0].exists() else "缺失")
        check("有重试日志（说明真的自愈了）",
              any("重试" in line for line in logs),
              next((l for l in logs if "重试" in l), "无"))

        print("\n[2] 第二次运行：断点续跑，一次请求都不发")
        before = (fake.submits, fake.polls, len(fake.uploads), fake.downloads)
        logs2: list[str] = []
        clips2 = P._avatar_template(None, segs, [portrait], out, logs2.append,
                                    cfg=cfg, api_key="fake", provider_name="avatar:fake",
                                    portrait=portrait)
        after = (fake.submits, fake.polls, len(fake.uploads), fake.downloads)
        check("请求数完全没变", before == after, f"{before} -> {after}")
        check("复用了同一个片段", len(clips2) == 1 and clips2[0] == clips[0])
        check("日志说明了是复用",
              any("复用" in line for line in logs2),
              next((l for l in logs2 if "复用" in l), "无"))

        print("\n[3] 任务划分改变：旧片段必须作废，不能拿来冒充")
        # 把上限改成装不下整段，划分变成多个任务 -> 旧的单个片段身份不明
        (out / "avatar_00.mp4").write_bytes(b"x" * 20_000)
        cfg2 = {**cfg, "merge_max_s": 9}      # 4s/段 -> 每 2 段一个任务 = 3 个任务
        fake.uploads.clear()
        logs3: list[str] = []
        clips3 = P._avatar_template(None, segs, [portrait], out, logs3.append,
                                    cfg=cfg2, api_key="fake", provider_name="avatar:fake",
                                    portrait=portrait)
        check("划分变了会重新提交", fake.submits > after[0], f"{fake.submits} 次")
        check("产出 3 个片段", len(clips3) == 3, str([c.name for c in clips3]))
        check("日志里说了作废旧片段",
              any("作废" in line for line in logs3),
              next((l for l in logs3 if "作废" in l), "无"))
        plan = json.loads((out / ".avatar_plan.json").read_text(encoding="utf-8"))
        check("划分记录显示 3 个任务", plan.get("jobs") == 3, str(plan))
        # 9s 上限 / 每段 4s：前 4 段两两合并（8s + 8s），第 5 段自己一个任务。
        # 所以落盘的 talk_*.wav 是 16s，剩下 4s 还是原来的 seg_04.wav 直接送出去。
        merged_s = sum(M.wav_duration(p) for p in out.glob("talk_*.wav"))
        check("合并音频 16s + 独走一段 4s = 20s",
              abs(merged_s - 16.0) < 0.05, f"talk_*.wav 合计 {merged_s:.2f}s")
        print("\n[4] 画布不一致（云端 512×512 vs 正片 1080×1920）")
        fitted = M.fit_to_canvas(clips3[0], work / "fit.mp4", 1080, 1920, 30, cwd=work)
        check("适配后是 1080×1920", M.probe_video_size(fitted) == (1080, 1920),
              str(M.probe_video_size(fitted)))
        check("适配后帧率也被拉到 30（云端是 25）",
              abs(M.probe_video_spec(fitted)[2] - 30.0) < 0.5,
              str(M.probe_video_spec(fitted)))

        print("\n[5] 图太小：上传前就拦下，一次请求都不发")
        # 复刻实测 D-ID：128×128 -> 400 InvalidImageResolutionError。
        # 框架应该在本地就判定出来，而不是把图传上去等它骂。
        tiny = work / "tiny.jpg"
        subprocess.run([M.ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi",
                        "-i", "testsrc=size=100x100:rate=1", "-frames:v", "1", str(tiny)],
                       check=True, capture_output=True)
        before5 = (fake.submits, len(fake.uploads))
        logs5: list[str] = []
        try:
            P._avatar_template(None, segs, [portrait], work / "t5", logs5.append,
                               cfg=cfg, api_key="fake", provider_name="avatar:fake",
                               portrait=tiny)
            check("太小应当直接失败", False, "竟然没报错")
        except P.ProviderError as exc:
            text = str(exc)
            check("太小被拦下", "不接受这张图" in text, text.splitlines()[0][:70])
            check("报错里给了该厂商的要求", "160" in text,
                  next((l for l in text.splitlines() if "160" in l), "无"))
            check("报错里给了人话建议", "换一张" in text or "清晰" in text)
        check("没有发出任何网络请求", (fake.submits, len(fake.uploads)) == before5,
              f"{before5} -> {(fake.submits, len(fake.uploads))}")

        print("\n[6] 免费的人脸闸门：图里没人脸，当场拦下（不花额度）")
        fake.no_faces = True
        before6 = fake.submits
        logs6: list[str] = []
        try:
            P._avatar_template(None, segs, [portrait], work / "t6", logs6.append,
                               cfg=cfg, api_key="fake", provider_name="avatar:fake",
                               portrait=portrait)
            check("没脸应当直接失败", False, "竟然没报错")
        except P.ProviderError as exc:
            check("没脸被拦下", "没检测到人脸" in str(exc),
                  str(exc).splitlines()[0][:70])
            check("说明了这是免费检查", "没有产生费用" in str(exc))
        check("拦在提交之前（没花额度）", fake.submits == before6,
              f"{before6} -> {fake.submits}")
        fake.no_faces = False

        print("\n[7] 提交被拒：认得出签名，且只提交一次（不硬磕）")
        fake.reject_size = True
        before7 = fake.submits
        logs7: list[str] = []
        try:
            P._avatar_template(None, segs, [portrait], work / "t7", logs7.append,
                               cfg=cfg, api_key="fake", provider_name="avatar:fake",
                               portrait=portrait)
            check("被拒应当失败", False, "竟然成功了")
        except P.ProviderError as exc:
            text = str(exc)
            check("报错点出了厂商签名", "InvalidFileSizeError" in text,
                  text.splitlines()[0][:70])
            check("报错给了「怎么办」", "怎么办" in text,
                  next((l for l in text.splitlines() if "怎么办" in l), "无")[:70])
            check("报错带上了 trace-id（提工单用）", "ti_fake_trace_0001" in text,
                  next((l for l in text.splitlines() if "证据" in l), "无")[:90])
        check("只提交了 1 次，没有硬磕",
              fake.submits - before7 == 1, f"提交了 {fake.submits - before7} 次")
        fake.reject_size = False

        print("\n[8] 状态与结果分离的队列 API（fal.ai 这类协议）")
        cfg8 = json.loads(json.dumps(cfg))
        cfg8["query"].pop("result_path", None)
        cfg8["result"] = {
            "url": f"{BASE}/responses/{{{{job_id}}}}",
            "method": "GET",
            "result_path": "video.url",
        }
        before8 = fake.result_fetches
        clips8 = P._avatar_template(
            None, segs, [portrait], work / "t8", lambda _m: None,
            cfg=cfg8, api_key="fake", provider_name="avatar:fake",
            portrait=portrait)
        check("完成后单独获取结果", fake.result_fetches == before8 + 1,
              f"获取 {fake.result_fetches - before8} 次")
        check("分离式结果能下载成片",
              len(clips8) == 1 and clips8[0].stat().st_size > 10_000)

        print("\n[9] 短动作片不能循环冒充长口播")
        cfg9 = json.loads(json.dumps(cfg))
        cfg9["merge_max_s"] = 4
        cfg9["postprocess"] = "local_wav2lip"
        cfg9["require_motion_duration_match"] = True
        try:
            P._avatar_template(
                None, segs[:1], [portrait], work / "t9", lambda _m: None,
                cfg=cfg9, api_key="fake", provider_name="avatar:fake",
                portrait=portrait)
            check("2s 动作不能覆盖 4s 配音", False, "竟然继续做了口型")
        except P.ProviderError as exc:
            check("2s 动作不能覆盖 4s 配音", "循环动作" in str(exc),
                  str(exc).splitlines()[0])
    finally:
        server.shutdown()
        server.server_close()

    print()
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败：{FAILS}")
        return 1
    print("✓ 全部通过 —— 云端数字人路径（含抖动自愈/合并/续跑）正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
