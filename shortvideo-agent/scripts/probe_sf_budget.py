"""用真实硅基流动接口验证失败预算（不花任何钱）。

这是改造前后的对比实验，靶子就是当初把整条流程拖住的那个故障：

    改造前：同一句连续被拒时，磕 12 次、退避最长 30 秒 —— 实测耗掉两分半钟。
    改造后：连着被拒到上限（6 次）或者累计 60 秒，就判定这家当前不可用，
            立刻抛出去交给回退链，并带上签名和 trace-id。

跑一次真实请求即可。三种结果都有意义：
  - 被拒到预算上限并快速放弃  -> 改造生效（这是当初的场景）
  - 中途成功                 -> 接口恢复了，签名逻辑没有误伤正常请求
  - 命中 fatal               -> Key/额度问题，也应当立刻放弃而不是重试
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autovid import providers as P             # noqa: E402
from autovid.assets import AssetStore          # noqa: E402
from autovid.config import Config              # noqa: E402
from autovid.providers_registry import resolve  # noqa: E402

WORK = ROOT / ".tmp" / "sf_budget"


def main() -> int:
    config = Config.load(root=ROOT)
    entry = resolve(config, "voice:voice-cosyvoice2")
    cfg, key = entry["cfg"], entry["api_key"]
    print(f"provider = {entry['label']}")
    print(f"endpoint = {cfg['url']}")
    print(f"预算     = 连续被拒 {cfg.get('reject_streak_limit')} 次 / "
          f"累计 {cfg.get('reject_budget_s')}s")

    store = AssetStore(config)
    asset = store.get_voice("voice-your-id")
    voice_id = (getattr(asset, "cloud_voice_ids", None) or {}).get("voice:voice-cosyvoice2")
    print(f"音色     = {voice_id}")
    if not voice_id:
        print("资产里没有缓存的云端音色 ID")
        return 2

    # 用条目自己的 cfg（含真实签名与预算），只覆盖 voice_id / 节流
    run_cfg = {**cfg, "voice_id": voice_id, "request_delay_s": 0,
               "audio_encoding": "raw", "audio_path": ""}
    # 少测几句就够：一句成功即说明没误伤，一句被拒到底即说明预算生效
    segments = [{"text": "今天讲一个特别简单的方法", "index": 0},
                {"text": "这个方法我自己用了三年", "index": 1}]

    WORK.mkdir(parents=True, exist_ok=True)
    logs: list[str] = []
    lines: list[str] = []
    started = time.time()
    try:
        result = P._tts_cloud(config, segments, WORK, lines.append,
                              voice_asset=None, cfg=run_cfg, api_key=key,
                              provider_name="voice:voice-cosyvoice2")
        elapsed = time.time() - started
        print(f"\n✓ 成功：{len(result.parts)} 段，耗时 {elapsed:.1f}s")
        for part in result.parts:
            print(f"    {Path(part).name}  {Path(part).stat().st_size / 1024:.0f}KB")
        print("  => 接口当前是好的，签名逻辑没有误伤正常请求。")
    except P.ProviderError as exc:
        elapsed = time.time() - started
        text = str(exc)
        print(f"\n✗ 失败：耗时 {elapsed:.1f}s")
        for line in text.splitlines():
            print(f"    {line}")
        print()
        # 关键对比：改造前这个场景是 12 次尝试、最长退避 30s
        slow = "12" in text
        if "判定这家当前不可用" in text or "连续" in text:
            print(f"  => 预算生效：{elapsed:.0f}s 就放弃并交给回退链"
                  f"（改造前这个场景要磕 12 次、约 150s）。")
        if "ti_" in text:
            print("  => trace-id 已记录，可直接拿去向硅基流动提工单。")
        if not any(k in text for k in ("假装成功", "fatal", "认证", "被拒")):
            print("  => 注意：这次没命中签名，是一个尚未覆盖的失败形态，需要补签名。")
        _ = slow
    finally:
        for line in lines[-40:]:
            print(f"  [log] {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
