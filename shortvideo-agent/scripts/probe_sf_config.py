"""只读体检：到底是不是配置/模型信息不对。

不做任何修改，只发 GET + 少量 POST，把官方文档里能对照的点逐条验掉：
  1. GET /v1/audio/voice/list   -> 我们那个克隆音色在不在账号里
  2. GET /v1/models             -> 这个 key 有没有 CosyVoice2-0.5B 的权限
  3. 空格实验：同一句话「带空格 / 不带空格」交替打（文档明确写「输入内容不要加空格」）
  4. 抓 x-siliconcloud-trace-id -> 失败请求到底有没有被平台受理
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autovid.assets import AssetStore          # noqa: E402
from autovid.config import Config              # noqa: E402
from autovid.providers_registry import resolve  # noqa: E402

API = "https://api.siliconflow.cn/v1"
MODEL = "FunAudioLLM/CosyVoice2-0.5B"


def get(path: str, key: str, timeout: int = 60):
    req = urllib.request.Request(f"{API}{path}",
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:300]
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__, str(exc)[:200]


def speak(key: str, voice: str, text: str) -> dict:
    body = json.dumps({"model": MODEL, "input": text, "voice": voice,
                       "response_format": "wav"}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{API}/audio/speech", data=body,
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"},
                                 method="POST")
    t0 = time.time()
    out = {"text": text, "chars": len(text), "spaces": text.count(" ")}
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            blob = resp.read()
            out["status"] = resp.status
            out["ctype"] = resp.headers.get("Content-Type", "")
            out["trace"] = resp.headers.get("x-siliconcloud-trace-id", "")
            out["request_id"] = resp.headers.get("x-request-id", "")
    except urllib.error.HTTPError as exc:
        out["status"] = exc.code
        out["ctype"] = exc.headers.get("Content-Type", "") if exc.headers else ""
        out["trace"] = exc.headers.get("x-siliconcloud-trace-id", "") if exc.headers else ""
        out["body"] = exc.read().decode("utf-8", "replace")[:200]
        blob = b""
    except Exception as exc:  # noqa: BLE001
        out["status"] = type(exc).__name__
        out["ctype"] = ""
        out["trace"] = ""
        blob = b""
    out["elapsed"] = round(time.time() - t0, 2)
    out["bytes"] = len(blob)
    out["ok"] = blob[:4] == b"RIFF"
    return out


def main() -> int:
    config = Config.load(root=ROOT)
    entry = resolve(config, "voice:voice-cosyvoice2")
    cfg, key = entry["cfg"], entry["api_key"]
    print(f"endpoint = {cfg['url']}")
    print(f"model    = {cfg['model']}")
    print(f"key      = {key[:10]}...{key[-4:]}  (长度 {len(key)})")

    print("\n[1] GET /audio/voice/list —— 克隆音色在不在账号里")
    code, data = get("/audio/voice/list", key)
    print(f"  HTTP {code}")
    if isinstance(data, dict):
        items = data.get("data") or data.get("results") or data
        if isinstance(items, list):
            for it in items:
                print(f"    uri={it.get('uri')}  name={it.get('customName') or it.get('name')}")
        else:
            print(f"    {json.dumps(data, ensure_ascii=False)[:400]}")
    else:
        print(f"    {data}")

    store = AssetStore(config)
    voice = ((getattr(store.get_voice("voice-your-id"), "cloud_voice_ids", None) or {})
             .get("voice:voice-cosyvoice2"))
    print(f"  我们用的 voice_id = {voice}")
    listed = json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data
    print(f"  => 在列表里？{'是' if voice and voice in listed else '否 / 无法判断'}")

    print("\n[2] GET /models —— 这个 key 有没有该模型的权限")
    code, data = get("/models", key)
    print(f"  HTTP {code}")
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        ids = [m.get("id") for m in data["data"]]
        tts = [i for i in ids if i and ("CosyVoice" in i or "TTSD" in i or "fish" in i)]
        print(f"  账号可见模型 {len(ids)} 个；其中语音类：{tts or '（无）'}")
        print(f"  => CosyVoice2-0.5B 有权限？{'是' if MODEL in ids else '不在列表里'}")
    else:
        print(f"    {str(data)[:300]}")

    print("\n[3] 空格实验：文档写「输入内容不要加空格」，交替对比")
    plain = "今天讲一个特别简单的方法"
    spaced = "今天讲一个特别简单的 方法 试试"
    print(f"  A = {plain!r}（0 空格）")
    print(f"  B = {spaced!r}（{spaced.count(' ')} 空格）")
    a_ok = b_ok = 0
    for i in range(8):
        ra = speak(key, voice, plain)
        rb = speak(key, voice, spaced)
        a_ok += ra["ok"]
        b_ok += rb["ok"]
        print(f"  轮{i+1}  A {'OK ' if ra['ok'] else 'FAIL'} {ra['bytes']:>7}B "
              f"{ra['elapsed']:>5.2f}s trace={str(ra['trace'])[:20]:<20} | "
              f"B {'OK ' if rb['ok'] else 'FAIL'} {rb['bytes']:>7}B "
              f"{rb['elapsed']:>5.2f}s trace={str(rb['trace'])[:20]}")
        time.sleep(0.5)
    print(f"  => 无空格 {a_ok}/8    有空格 {b_ok}/8")

    print("\n[4] 失败请求有没有 trace-id（有=被平台受理过，无=网关层面就丢了）")
    fails, oks = [], []
    for t in ["大家好，", "快速入门。", "工具、", "而是一个开发框架。"] * 3:
        r = speak(key, voice, t)
        (oks if r["ok"] else fails).append(r)
        time.sleep(0.4)
    for label, group in (("成功", oks), ("失败", fails)):
        traces = [g.get("trace") for g in group]
        print(f"  {label} {len(group)} 次：带 trace-id 的 {sum(1 for t in traces if t)} 次"
              f"  样例={next((t for t in traces if t), '（无）')}")
        ctypes = sorted({g.get("ctype", "") for g in group})
        print(f"       Content-Type={ctypes}  字节数={sorted({g['bytes'] for g in group})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
