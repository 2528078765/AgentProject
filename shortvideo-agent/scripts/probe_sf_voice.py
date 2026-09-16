"""分清楚：失败是「认这个克隆音色」还是「所有请求都这样」。

只问一个问题：内置音色和克隆音色的失败率有没有差别？
有差别 -> 克隆音色那条路的问题；没差别 -> 服务端请求级的随机丢包。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autovid.assets import AssetStore          # noqa: E402
from autovid.config import Config              # noqa: E402
from autovid.providers_registry import resolve  # noqa: E402

TEXT = "今天讲一个特别简单的方法。"


def call(url: str, key: str, model: str, voice: str, text: str) -> tuple[bool, int, float, str]:
    body = json.dumps({"model": model, "input": text, "voice": voice,
                       "response_format": "wav"}, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"},
                                 method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            blob = resp.read()
            ctype = resp.headers.get("Content-Type", "")
    except Exception as exc:  # noqa: BLE001
        return False, 0, time.time() - t0, type(exc).__name__
    ok = len(blob) > 512 and blob[:4] == b"RIFF"
    return ok, len(blob), time.time() - t0, ctype


def main() -> int:
    config = Config.load(root=ROOT)
    entry = resolve(config, "voice:voice-cosyvoice2")
    cfg, key = entry["cfg"], entry["api_key"]
    url, model = str(cfg["url"]), str(cfg["model"])
    store = AssetStore(config)
    cloned = ((getattr(store.get_voice("voice-your-id"), "cloud_voice_ids", None) or {})
              .get("voice:voice-cosyvoice2"))

    voices = [("内置 alex", "alex"), ("内置 bella", "bella"), ("克隆 csy", cloned)]
    rounds = 10

    print(f"{'音色':<12} {'成功':<6} {'失败':<6} 失败耗时         成功耗时")
    print("-" * 72)
    summary = {}
    for label, voice in voices:
        if not voice:
            print(f"{label:<12} 跳过（没有缓存的克隆音色）")
            continue
        ok_n = 0
        fail_times: list[float] = []
        ok_times: list[float] = []
        for _ in range(rounds):
            ok, nbytes, dt, ctype = call(url, key, model, voice, TEXT)
            if ok:
                ok_n += 1
                ok_times.append(dt)
            else:
                fail_times.append(dt)
            time.sleep(0.4)
        summary[label] = (ok_n, fail_times, ok_times)
        ft = f"{min(fail_times):.2f}~{max(fail_times):.2f}s" if fail_times else "-"
        ot = f"{min(ok_times):.2f}~{max(ok_times):.2f}s" if ok_times else "-"
        print(f"{label:<12} {ok_n}/{rounds:<4} {rounds - ok_n}/{rounds:<4} {ft:<16} {ot}")

    print()
    cloned_ok = summary.get("克隆 csy", (None,))[0]
    builtin_ok = [v[0] for k, v in summary.items() if k.startswith("内置")]
    if cloned_ok is not None and builtin_ok:
        avg_builtin = sum(builtin_ok) / len(builtin_ok)
        if abs(cloned_ok - avg_builtin) >= 3:
            print("=> 克隆音色和内置音色表现明显不同，问题出在克隆音色上")
        else:
            print("=> 克隆音色和内置音色表现接近，问题是**请求级随机**的，与音色无关")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
