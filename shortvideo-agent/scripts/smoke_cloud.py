"""云端 TTS / 音色克隆适配器的冒烟测试。

问题在于：真实的云厂商接口需要 Key，我无法在这里验证。
所以做法是**起一个本地 mock 服务，模仿四种常见的音频返回形式**，
把适配器的模板渲染、字段提取、音频解码、克隆缓存全部走一遍。

这样等用户拿到真 Key 时，只剩下「字段名对不对」这一件事需要核对
（用 scripts/probe_cloud.py 打一次真实请求就能看出来），
而不是连代码都没验证过。

    python scripts/smoke_cloud.py
"""

from __future__ import annotations

import base64
import json
import math
import os
import shutil
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autovid import media as M                     # noqa: E402
from autovid import providers as P                 # noqa: E402
from autovid.assets import AssetStore              # noqa: E402
from autovid.config import Config                  # noqa: E402

PASS, FAIL = "[通过]", "[失败]"
problems: list[str] = []
WORK = ROOT / ".tmp" / "cloud_probe"
ENV_KEY = "AUTOVID_SMOKE_CLOUD_KEY"

# mock 服务的共享状态
STATE = {"clone_calls": 0, "tts_calls": 0, "mode": "base64", "audio": b""}


def make_wav(seconds: float = 2.0, freq: float = 330.0, rate: int = 24000) -> bytes:
    import io
    buffer = io.BytesIO()
    frames = int(seconds * rate)
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"".join(
            int(9000 * math.sin(2 * math.pi * freq * i / rate)).to_bytes(2, "little", signed=True)
            for i in range(frames)
        ))
    return buffer.getvalue()


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        return

    def _send(self, payload: bytes, ctype: str = "application/json", status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        path = self.path.split("?")[0]

        if path == "/clone":
            STATE["clone_calls"] += 1
            data = json.loads(body.decode("utf-8"))
            # 校验注册请求里确实带了参考音频
            if not data.get("audio"):
                return self._send(json.dumps({"error": "no audio"}).encode(), status=400)
            return self._send(json.dumps({
                "voice_id": "cloud-voice-abc123",
                "data": {"voice_id": "cloud-voice-abc123"},
            }).encode())

        if path == "/tts":
            STATE["tts_calls"] += 1
            data = json.loads(body.decode("utf-8"))
            text = str(data.get("text") or "")
            if not text:
                return self._send(json.dumps({"error": "no text"}).encode(), status=400)
            # 每段音频时长跟文本长度挂钩，方便验证时间轴
            audio = make_wav(seconds=max(1.0, len(text) / 20.0))
            mode = STATE["mode"]
            if mode == "hex":
                return self._send(json.dumps({"data": {"audio": audio.hex()}}).encode())
            if mode == "base64":
                return self._send(json.dumps({"data": {"audio": base64.b64encode(audio).decode()}}).encode())
            if mode == "url":
                port = self.server.server_address[1]
                return self._send(json.dumps({
                    "data": {"audio_url": f"http://127.0.0.1:{port}/audio.wav"}}).encode())
            if mode == "raw":
                return self._send(audio, ctype="audio/wav")
        return self._send(json.dumps({"error": "not found"}).encode(), status=404)

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] == "/audio.wav":
            return self._send(STATE["audio"], ctype="audio/wav")
        return self._send(json.dumps({"error": "not found"}).encode(), status=404)


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {PASS if ok else FAIL} {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        problems.append(name)
    return ok


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    STATE["audio"] = make_wav(2.0)
    os.environ[ENV_KEY] = "mock-api-key"

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    print("云端 TTS / 音色克隆适配器冒烟测试")
    print("=" * 70)
    print(f"  mock 服务: {base}\n")

    store = AssetStore(Config.load(root=ROOT))
    voice = store.create_voice("云端克隆测试")
    try:
        ref = WORK / "ref.wav"
        ref.write_bytes(make_wav(12.0))
        store.save_voice_reference(voice.id, ref.read_bytes(), "ref.wav")
        voice = store.get_voice(voice.id)

        segments = [
            {"id": "s01", "index": 0, "headline": "测试", "text": "这是云端音色克隆的第一段测试文字。"},
            {"id": "s02", "index": 1, "headline": "测试", "text": "第二段用来验证多段合成与时间轴。"},
        ]

        print("1. 四种音频返回形式都要能解码")
        for mode, encoding, audio_path in (
            ("hex", "hex", "data.audio"),
            ("base64", "base64", "data.audio"),
            ("url", "url", "data.audio_url"),
            ("raw", "raw", ""),
        ):
            STATE["mode"] = mode
            config = Config.load(root=ROOT).with_overrides({
                "steps.voice.provider": "cloud_tts",
                "steps.voice.fallback": [],
                "steps.voice.strict": True,
                "providers.cloud_tts.url": f"{base}/tts",
                "providers.cloud_tts.model": "mock-tts",
                "providers.cloud_tts.audio_path": audio_path,
                "providers.cloud_tts.audio_encoding": encoding,
                "providers.cloud_tts.api_key_env": ENV_KEY,
                "providers.cloud_tts.voice_id": "fixed-voice-001",
                "providers.cloud_tts.body": {
                    "model": "{{model}}", "text": "{{text}}", "voice": "{{voice_id}}",
                },
            })
            out = WORK / f"mode_{mode}"
            shutil.rmtree(out, ignore_errors=True)
            out.mkdir(parents=True, exist_ok=True)
            try:
                result = P.tts_synthesize(config, segments, out, log=lambda _m: None)
                total = sum(M.wav_duration(p) for p in result.parts)
                check(f"{mode:<7} 解码成功", len(result.parts) == 2 and total > 1.5,
                      f"{len(result.parts)} 段 / {total:.2f}s")
            except Exception as exc:  # noqa: BLE001
                check(f"{mode:<7} 解码成功", False, str(exc).splitlines()[0][:90])

        print("\n2. 克隆注册：走真实流程 + 只注册一次")
        STATE["mode"] = "base64"
        STATE["clone_calls"] = 0
        config = Config.load(root=ROOT).with_overrides({
            "steps.voice.provider": "cloud_tts",
            "steps.voice.fallback": [],
            "steps.voice.strict": True,
            "providers.cloud_tts.url": f"{base}/tts",
            "providers.cloud_tts.model": "mock-tts",
            "providers.cloud_tts.audio_path": "data.audio",
            "providers.cloud_tts.audio_encoding": "base64",
            "providers.cloud_tts.api_key_env": ENV_KEY,
            "providers.cloud_tts.body": {
                "model": "{{model}}", "text": "{{text}}", "voice": "{{voice_id}}",
            },
            "providers.cloud_tts.clone": {
                "enabled": True,
                "url": f"{base}/clone",
                "headers": {"Authorization": "Bearer {{api_key}}",
                            "Content-Type": "application/json"},
                "body": {"audio": "{{audio_base64}}", "format": "{{audio_format}}",
                         "name": "{{voice_name}}"},
                "voice_id_path": "data.voice_id",
            },
        })

        out = WORK / "clone_run"
        out.mkdir(parents=True, exist_ok=True)
        result = P.tts_synthesize(config, segments, out, log=lambda _m: None,
                                  voice_asset=voice)
        check("首次调用会注册音色", STATE["clone_calls"] == 1, f"clone 被调用 {STATE['clone_calls']} 次")
        check("结果标记为已克隆", result.cloned is True)
        check("返回的记录带音色信息", result.voice_id == voice.id, str(result.voice_id))
        check("云端音色 ID 来自嵌套路径 data.voice_id", "cloud-voice-abc123" in result.note,
              result.note[:60])

        refreshed = store.get_voice(voice.id)
        check("云端音色 ID 已缓存进资产",
              (refreshed.cloud_voice_ids or {}).get("cloud_tts") == "cloud-voice-abc123",
              str(refreshed.cloud_voice_ids))

        # 第二次运行必须复用缓存，不能重复克隆（既慢又费钱）
        result2 = P.tts_synthesize(config, segments, out, log=lambda _m: None,
                                   voice_asset=refreshed)
        check("第二次不重复注册", STATE["clone_calls"] == 1,
              f"clone 累计 {STATE['clone_calls']} 次")
        check("第二次仍然标记为已克隆", result2.cloned is True)

        print("\n3. 错误处理要给出可诊断的信息")
        # 字段路径配错时，报错里必须带上原始响应，否则没法排查
        bad = config.with_overrides({
            "providers.cloud_tts.audio_path": "data.wrong_field",
            "providers.cloud_tts.clone.enabled": False,
            "providers.cloud_tts.voice_id": "fixed-voice-001",
        })
        try:
            P.tts_synthesize(bad, segments[:1], out, log=lambda _m: None)
            check("字段路径错误会报错", False, "竟然没报错")
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            check("字段路径错误会报错", "wrong_field" in message)
            check("报错里带上了原始响应", "data" in message and "{" in message,
                  message.splitlines()[0][:60])

        # 缺 Key 时应该明确告诉用户往哪填
        os.environ.pop(ENV_KEY, None)
        try:
            P.tts_synthesize(config, segments[:1], out, log=lambda _m: None)
            check("缺 Key 会报错", False, "竟然没报错")
        except Exception as exc:  # noqa: BLE001
            check("缺 Key 会报错，并说明往哪填", "secrets.json" in str(exc),
                  str(exc).splitlines()[0][:60])
        os.environ[ENV_KEY] = "mock-api-key"

    finally:
        httpd.shutdown()
        httpd.server_close()
        store.delete_voice(voice.id)

    print("\n" + "=" * 70)
    if problems:
        print(f"  {FAIL} {len(problems)} 项未通过：")
        for item in problems:
            print(f"    - {item}")
        return 1
    print(f"  {PASS} 全部通过 —— 适配器逻辑正确")
    print("\n  剩下唯一的不确定项是「真实厂商的字段名」。拿到 Key 后跑：")
    print("      python scripts\\probe_cloud.py --preset minimax --key 你的Key")
    print("  它会打印原始响应，照着填 config/pipeline.json 即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
