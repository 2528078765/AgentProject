"""诊断：硅基流动 TTS 到底回了什么。

背景：合成接口会「HTTP 200 + 0 字节」间歇性失败。之前只记了一句「空响应」，
把最有价值的信息（状态码、响应头、响应体）全扔了，导致连查三轮都没定论。

这个脚本把每次请求的**全部**证据打出来：
    状态码 / Content-Type / Content-Length / 响应头里所有 x-ratelimit-* /
    实际收到字节数 / 不是音频时把响应体当文本打出来 / 耗时

然后用两段实验区分「随机」和「文本相关」：
    A. 同一句话连打 N 次  -> 纯随机失败率
    B. 真实气口句各打一次 -> 真实失败率，并且看失败跟长度/内容有没有关系
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

from autovid import media as M              # noqa: E402
from autovid.assets import AssetStore        # noqa: E402
from autovid.config import Config            # noqa: E402
from autovid.providers_registry import resolve  # noqa: E402

RUN = ROOT / "runs" / "20260913-115519-langchain"


def request_once(url: str, key: str, text: str, model: str, voice: str) -> dict:
    body = json.dumps({"model": model, "input": text, "voice": voice,
                       "response_format": "wav"}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"}, method="POST")
    started = time.time()
    info: dict = {"chars": len(text), "text": text}
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            blob = resp.read()
            info["status"] = resp.status
            info["ctype"] = resp.headers.get("Content-Type", "")
            info["clen"] = resp.headers.get("Content-Length", "")
            info["ratelimit"] = {k: v for k, v in resp.headers.items()
                                 if "ratelimit" in k.lower() or "retry" in k.lower()}
            info["headers"] = dict(resp.headers)
    except urllib.error.HTTPError as exc:
        info["status"] = exc.code
        info["ctype"] = exc.headers.get("Content-Type", "") if exc.headers else ""
        blob = exc.read()
        info["error"] = True
    except Exception as exc:  # noqa: BLE001
        info["status"] = f"{type(exc).__name__}"
        blob = b""
        info["error"] = True
    info["elapsed"] = round(time.time() - started, 2)
    info["bytes"] = len(blob)
    from autovid import providers as P
    if blob:
        ok, why = P._audio_sane(blob)
    else:
        ok, why = False, "0 字节"
    info["ok"] = ok
    info["why"] = why
    if not ok and blob:
        info["body"] = blob[:400].decode("utf-8", errors="replace")
    return info


def show(tag: str, r: dict) -> None:
    mark = "OK  " if r["ok"] else "FAIL"
    print(f"  [{mark}] {tag}  {r['chars']:>3}字  HTTP {r.get('status')}  "
          f"{r['bytes']:>7} 字节  {r.get('ctype','')[:30]:<30} {r['elapsed']:>6.2f}s  "
          f"{r['why']}")
    if r.get("ratelimit"):
        print(f"         ratelimit: {r['ratelimit']}")
    if r.get("body"):
        print(f"         body: {r['body'][:300]!r}")


def main() -> int:
    config = Config.load(root=ROOT)
    entry = resolve(config, "voice:voice-cosyvoice2")
    if entry is None:
        print("找不到 voice:voice-cosyvoice2")
        return 2
    cfg, key = entry["cfg"], entry["api_key"]
    url = str(cfg.get("url"))
    model = str(cfg.get("model"))
    print(f"endpoint = {url}")
    print(f"model    = {model}")
    print(f"key      = {key[:8]}...{key[-4:]}")

    store = AssetStore(config)
    asset = store.get_voice("voice-your-id")
    voice = (getattr(asset, "cloud_voice_ids", None) or {}).get("voice:voice-cosyvoice2")
    print(f"voice_id = {voice}")
    if not voice:
        print("资产里没有缓存的云端音色 ID，先跑一次「音色克隆」")
        return 2

    texts: list[str] = []
    script = RUN / "script" / "script.json"
    if script.exists():
        data = json.loads(script.read_text(encoding="utf-8"))
        for seg in data.get("segments", []):
            texts.extend(c for c, _ in M.split_into_breaths(seg["text"], 20))
    print(f"气口句 {len(texts)} 条（来自 {script.name}）\n")

    print("A. 同一句话连打 8 次 —— 看纯随机失败率")
    fixed = "今天讲一个特别简单的方法。"
    a = [request_once(url, key, fixed, model, voice) for _ in range(8)]
    for i, r in enumerate(a, 1):
        show(f"#{i}", r)
    ok_a = sum(1 for r in a if r["ok"])
    print(f"  -> 成功 {ok_a}/8")
    durations = [r["elapsed"] for r in a if r["ok"]]
    if durations:
        print(f"  -> 成功请求耗时 {min(durations):.2f}~{max(durations):.2f}s")

    print("\nB. 真实气口句各打一次 —— 看失败跟文本有没有关系")
    sample = texts[:10]
    b = [request_once(url, key, t, model, voice) for t in sample]
    for t, r in zip(sample, b):
        show(t[:14] + ("…" if len(t) > 14 else ""), r)
    ok_b = sum(1 for r in b if r["ok"])
    print(f"  -> 成功 {ok_b}/{len(sample)}")

    print("\n结论线索")
    fails = [r for r in a + b if not r["ok"]]
    if not fails:
        print("  本轮一次都没失败 —— 说明是间歇性的，需要多打几轮才能定位")
    else:
        codes = {str(r.get("status")) for r in fails}
        print(f"  失败时的状态码：{sorted(codes)}")
        print(f"  失败时字节数：{sorted({r['bytes'] for r in fails})}")
        print(f"  失败时 Content-Type：{sorted({r.get('ctype','') for r in fails})}")
        print(f"  失败句长度：{sorted({r['chars'] for r in fails})}")
        print(f"  成功句长度：{sorted({r['chars'] for r in a + b if r['ok']})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
