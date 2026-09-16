"""本地 Qwen3-TTS 音色克隆的冒烟测试。

这是「声音终于是你自己的」这条链路的端到端验证：

    1. 部署检测：模型产物齐全 -> provider 标为可用
    2. 音色克隆：从真实参考音频提取锚点（codes + spk_emb）-> anchor.json
    3. 克隆幂等：重复调用不重算（这就是「克隆一次，反复用」）
    4. 真正合成：出 wav，而且**不是静音**（算 RMS 验证）
    5. 引擎复用：第二次合成不再重新加载 GGUF

    python scripts/smoke_qwen3tts.py
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autovid import providers as P                    # noqa: E402
from autovid.assets import AssetStore                 # noqa: E402
from autovid.config import Config                     # noqa: E402
from autovid.qwen3tts import Qwen3TTSBackend, model_ready  # noqa: E402

PASS, FAIL = "[通过]", "[失败]"
problems: list[str] = []
WORK = ROOT / ".tmp" / "qwen3tts_probe"


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {PASS if ok else FAIL} {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        problems.append(name)
    return ok


def rms_of(wav: Path) -> float:
    """算音频 RMS，用来判断是真出声还是静音。"""
    import numpy as np
    import soundfile as sf

    data, _ = sf.read(str(wav), dtype="float32", always_2d=True)
    if data.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(data))))


def main() -> int:
    print("本地 Qwen3-TTS 音色克隆冒烟测试")
    print("=" * 74)

    config = Config.load(root=ROOT).with_overrides({
        "steps.voice.provider": "local_qwen_tts",
        "steps.voice.strict": True,      # 失败就报错，别静默回退到别的引擎
        "steps.voice.fallback": [],
    })
    logs: list[str] = []

    def log(message: str) -> None:
        logs.append(str(message))
        print("      " + str(message))

    # ---------------------------------------------------------- 1. 部署检测
    print("\n1. 部署检测")
    check("模型产物齐全（Vulkan GGUF + ONNX）", model_ready(config))
    statuses = {s.name: s for s in P.provider_statuses(config)}
    check("provider 列表里有 local_qwen_tts", "local_qwen_tts" in statuses)
    check("已自动标为可用", statuses.get("local_qwen_tts") and
          statuses["local_qwen_tts"].available,
          statuses.get("local_qwen_tts").detail if statuses.get("local_qwen_tts") else "-")
    check("算作「支持克隆」的 provider",
          "local_qwen_tts" in P._CLONE_CAPABLE)
    check("已注册进 TTS provider 表", "local_qwen_tts" in P._TTS_PROVIDERS)

    # ---------------------------------------------------------- 2. 找音色资产
    print("\n2. 音色资产")
    store = AssetStore(config)
    asset = next((v for v in store.list_voices() if v.ref_audio), None)
    if asset is None:
        check("资产库里有带参考音频的音色", False, "请先在「音色库」录一段")
        return 1
    ref = store.voice_reference(asset.id)
    check("找到带参考音频的音色", ref is not None,
          f"{asset.name}（{asset.id}），{asset.duration_s}s")
    anchor = store.voice_dir(asset.id) / "anchor.json"

    # ---------------------------------------------------------- 3. 音色克隆
    print("\n3. 音色克隆（提取锚点）")
    if anchor.exists():
        anchor.unlink()
    started = time.time()
    profile = P.register_voice(config, asset, log)
    first_cost = time.time() - started
    check("克隆成功", profile.get("cloned") is True, profile.get("note", ""))
    check("报告为支持克隆", profile.get("can_clone") is True)
    check("产出 anchor.json（无损锚点）", anchor.exists(),
          f"{anchor.stat().st_size / 1e6:.2f} MB")
    check("第一次克隆耗时已记录", first_cost > 0, f"{first_cost:.1f}s")

    import json
    saved = json.loads(anchor.read_text(encoding="utf-8"))
    check("锚点含音频码（codes）", bool(saved.get("codes")),
          f"{len(saved.get('codes') or [])} 帧")
    check("锚点含说话人嵌入（spk_emb）", bool(saved.get("spk_emb")),
          "Base64 fp32")
    import base64
    import numpy as np
    emb = np.frombuffer(base64.b64decode(saved["spk_emb"]), dtype=np.float32)
    check("嵌入维度正确（0.6B 应为 1024）", emb.shape[0] == 1024, f"{emb.shape[0]} 维")
    check("嵌入不是全零（真的提到了音色特征）",
          float(np.abs(emb).mean()) > 1e-6, f"均值 {float(np.abs(emb).mean()):.4f}")

    # ---------------------------------------------------------- 4. 幂等
    print("\n4. 克隆幂等（不重复算）")
    stamp = anchor.stat().st_mtime
    started = time.time()
    P.register_voice(config, asset, log)
    second_cost = time.time() - started
    check("第二次没有重算（文件未变）", anchor.stat().st_mtime == stamp)
    check("第二次明显更快", second_cost < max(1.0, first_cost / 2),
          f"{second_cost:.2f}s vs 第一次 {first_cost:.1f}s")
    check("日志说明是复用", any("复用" in m for m in logs))

    # ---------------------------------------------------------- 5. 真正合成
    print("\n5. 克隆合成（用你的音色说话）")
    WORK.mkdir(parents=True, exist_ok=True)
    segments = [
        {"id": "s01", "index": 0, "text": "这是我自己的声音，不是别人的。"},
        {"id": "s02", "index": 1, "text": "背景每次都不一样，因为照片是现拍的。"},
        {"id": "s03", "index": 2, "text": "形象只要采集一次，之后每次换一张照片就行。"},
    ]
    started = time.time()
    result = P.tts_synthesize(config, segments, WORK, log, voice_asset=asset)
    first_wall = time.time() - started
    check("provider 是本地引擎", result.provider == "local_qwen_tts", result.provider)
    check("如实报告用到了你的音色", result.cloned is True, result.note)
    check("每句产出一个 wav", len(result.parts) == 3,
          str([p.name for p in result.parts]))

    import soundfile as sf
    durations = []
    for part in result.parts:
        check(f"{part.name} 存在且非空", part.exists() and part.stat().st_size > 1024,
              f"{part.stat().st_size / 1024:.0f} KB" if part.exists() else "缺失")
        info = sf.info(str(part))
        durations.append(info.duration)
        level = rms_of(part)
        check(f"{part.name} 真的出声了（不是静音）", level > 0.005,
              f"RMS={level:.4f}, {info.duration:.2f}s @ {info.samplerate}Hz")
    total_audio = sum(durations)
    check("总音频时长合理", total_audio > 3, f"{total_audio:.2f}s")

    # ---------------------------------------------------------- 6. 稳态速度
    print("\n6. 稳态速度（第二次不重解码参考音频）")
    started = time.time()
    warm = P.tts_synthesize(config, segments, WORK / "warm", log, voice_asset=asset)
    warm_wall = time.time() - started
    check("第二次合成可用", len(warm.parts) == 3)
    steady_rtf = warm_wall / total_audio if total_audio else 99
    check("稳态 RTF < 1.5（比实时快）", steady_rtf < 1.5,
          f"音频 {total_audio:.2f}s / 耗时 {warm_wall:.1f}s = RTF {steady_rtf:.2f}")
    check("第二次显著快于第一次（省掉参考音频重解码）",
          warm_wall < first_wall,
          f"首次 {first_wall:.1f}s（含准备） vs 稳态 {warm_wall:.1f}s")
    check("日志说明用了内存缓存", any("跳过参考音频重解码" in m for m in logs))

    # ------------------------------------------------- 7. 单例死锁回归守卫
    # 曾经的真事故：get() 持锁期间调 shutdown()，而 shutdown() 也要同一把锁。
    # 普通 Lock 会自己等自己 -> 流程无声卡死。触发条件是 anchor.json 已存在
    # （引擎没加载过）时的第二次 get()，也就是**第二次生成必挂**。
    # 所以这里用一个独立线程 + 超时来守：卡住就判定失败，而不是把测试挂死。
    print("\n7. 单例不会死锁（回归守卫）")
    import threading

    outcome: dict[str, Any] = {}

    def probe() -> None:
        try:
            first = Qwen3TTSBackend.get(config)
            second = Qwen3TTSBackend.get(config)
            outcome["same"] = first is second
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=probe, daemon=True)
    worker.start()
    worker.join(timeout=10)
    check("连续两次 get() 不卡死（锁可重入）", not worker.is_alive(),
          "10 秒内没返回 = 死锁" if worker.is_alive() else "及时返回")
    if not worker.is_alive():
        check("拿到的仍是同一个实例", outcome.get("same") is True,
              str(outcome.get("error") or ""))

    Qwen3TTSBackend.get(config).shutdown()
    shutil.rmtree(WORK, ignore_errors=True)

    print("\n" + "=" * 74)
    if problems:
        print(f"  {FAIL} {len(problems)} 项未通过：")
        for item in problems:
            print(f"    - {item}")
        return 1
    print(f"  {PASS} 全部通过 —— 视频里的声音现在是你自己的了")
    print(f"\n  首次克隆 {first_cost:.1f}s | 首次合成 {first_wall:.1f}s | 稳态 RTF {steady_rtf:.2f}")
    print(f"  锚点：{anchor.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
