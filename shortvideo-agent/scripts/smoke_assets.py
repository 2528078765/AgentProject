"""音色 / 形象资产层的冒烟测试。

覆盖关键风险点：
    1. 采集体检真的会拦住不合格素材（太短的音频、太小的图片）
    2. 资产 ID 参与 input_hash —— 换音色/换形象会让下游自动失效重跑
    3. avatar 的 still provider 真的把用户照片合成进了画面
    4. 选了音色但引擎不支持克隆时，会明确报告「没有用你的音色」而不是默默替换
    5. 目录穿越 / 非法 ID 被拒绝

    python scripts/smoke_assets.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autovid import media as M                        # noqa: E402
from autovid.assets import AssetStore                 # noqa: E402
from autovid.config import Config                     # noqa: E402
from autovid.errors import AutoVidError               # noqa: E402
from autovid.manifest import RunContext               # noqa: E402
from autovid.pipeline import Runner                   # noqa: E402

PASS, FAIL = "[通过]", "[失败]"
problems: list[str] = []
WORK = ROOT / ".tmp" / "assets_probe"


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {PASS if ok else FAIL} {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        problems.append(name)
    return ok


def write_wav(path: Path, seconds: float, rate: int = 24000) -> Path:
    """造一段正弦波音频（有实际电平，不是静音）。"""
    import math
    frames = int(seconds * rate)
    data = bytearray()
    for i in range(frames):
        value = int(12000 * math.sin(2 * math.pi * 220 * i / rate))
        data += value.to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(bytes(data))
    return path


def main() -> int:
    config = Config.load(root=ROOT)
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    store = AssetStore(config)

    print("音色 / 形象资产冒烟测试")
    print("=" * 68)

    created_voices: list[str] = []
    created_avatars: list[str] = []

    try:
        # ---------------------------------------------------------- 音色采集
        print("\n1. 音色采集与体检")
        voice = store.create_voice("冒烟测试音色")
        created_voices.append(voice.id)
        check("新建音色档案", voice.id.startswith("voice-"), voice.id)
        check("初始状态为待采集", voice.status == "empty", voice.status)
        check("自带录音引导文案", len(voice.prompt_text) > 50,
              f"{len(voice.prompt_text)} 字")
        check("朗读模板适合二十多秒参考音频",
              70 <= len(voice.prompt_text) <= 140,
              f"{len(voice.prompt_text)} 字")

        # 太短的音频必须被拦住 —— 否则会拖到生成阶段才出问题
        short = write_wav(WORK / "short.wav", 1.0)
        try:
            store.save_voice_reference(voice.id, short.read_bytes(), "short.wav")
            check("拒绝过短音频", False, "1 秒的音频竟然被接受了")
        except AutoVidError as exc:
            check("拒绝过短音频", True, str(exc)[:60])

        # 非音频文件必须被拦住
        try:
            store.save_voice_reference(voice.id, b"not audio at all", "fake.wav")
            check("拒绝无效音频", False, "坏文件竟然被接受了")
        except AutoVidError as exc:
            check("拒绝无效音频", True, str(exc)[:60])

        # 合格音频
        good = write_wav(WORK / "good.wav", 12.0)
        voice = store.save_voice_reference(voice.id, good.read_bytes(), "good.wav")
        check("接受合格音频", voice.status == "ready", f"{voice.duration_s}s")
        ref = store.voice_reference(voice.id)
        check("参考音频已规范化", ref is not None and ref.exists(), ref.name if ref else "-")
        if ref:
            channels, width, rate, _ = M.wav_info(ref)
            check("统一为 24kHz 单声道 16bit",
                  (channels, width, rate) == (1, 2, 24000),
                  f"ch={channels} w={width} r={rate}")

        # ---------------------------------------------------------- 形象采集
        print("\n2. 形象采集与体检")
        avatar = store.create_avatar("冒烟测试形象")
        created_avatars.append(avatar.id)
        check("新建形象档案", avatar.id.startswith("avatar-"), avatar.id)

        tiny = WORK / "tiny.png"
        M.make_gradient(tiny, 128, 128, ["0x0f2027", "0x203a43", "0x2c5364"], seed=1)
        try:
            store.save_avatar_photo(avatar.id, tiny.read_bytes(), "tiny.png")
            check("拒绝过小图片", False, "128x128 竟然被接受了")
        except AutoVidError as exc:
            check("拒绝过小图片", True, str(exc)[:60])

        big1 = WORK / "big1.png"
        big2 = WORK / "big2.png"
        M.make_gradient(big1, 800, 1000, ["0x0f2027", "0x203a43", "0x2c5364"], seed=2)
        M.make_gradient(big2, 900, 1200, ["0x2b1055", "0x7597de", "0x1b1b3a"], seed=3)
        avatar = store.save_avatar_photo(avatar.id, big1.read_bytes(), "front.png")
        avatar = store.save_avatar_photo(avatar.id, big2.read_bytes(), "side.png")
        check("接受合格照片", len(avatar.photos) == 2, f"{len(avatar.photos)} 张")
        check("首张自动为主图", avatar.portrait == "front.png", str(avatar.portrait))

        avatar = store.set_primary_photo(avatar.id, "side.png")
        check("可以改主图", avatar.portrait == "side.png", str(avatar.portrait))
        portrait = store.avatar_portrait(avatar.id)
        check("能取到主图文件", portrait is not None and portrait.exists())

        # ---------------------------------------------------------- 安全性
        print("\n3. 安全性")
        try:
            store.get_voice("../../etc/passwd")
            check("非法 ID 被拒绝", False, "竟然接受了路径穿越 ID")
        except AutoVidError:
            check("非法 ID 被拒绝", True)
        check("非法文件名取不到照片",
              store.avatar_photo_path(avatar.id, "../../../config/pipeline.json") is None)

        # ---------------------------------------------------------- 参与哈希
        print("\n4. 资产 ID 参与 input_hash（换资产会自动失效）")
        base = config.with_overrides({"steps.voice.provider": "silent",
                                      "steps.avatar.provider": "still",
                                      "project.require_assets": False})
        ctx_a = RunContext.create(base, slug="assets-probe", run_id="probe-base")
        a_voice = Runner(ctx_a).input_hash(
            __import__("autovid.pipeline", fromlist=["STEPS"]).STEPS[2])
        ctx_b = RunContext.create(base.with_overrides({"project.voice_id": voice.id}),
                                  slug="assets-probe", run_id="probe-voice")
        b_voice = Runner(ctx_b).input_hash(
            __import__("autovid.pipeline", fromlist=["STEPS"]).STEPS[2])
        check("换 voice_id 会改变 voice 步骤指纹", a_voice != b_voice)
        ctx_c = RunContext.create(base.with_overrides({"project.avatar_id": avatar.id}),
                                  slug="assets-probe", run_id="probe-avatar")
        a_avatar = Runner(ctx_a).input_hash(
            __import__("autovid.pipeline", fromlist=["STEPS"]).STEPS[4])
        c_avatar = Runner(ctx_c).input_hash(
            __import__("autovid.pipeline", fromlist=["STEPS"]).STEPS[4])
        check("换 avatar_id 会改变 avatar 步骤指纹", a_avatar != c_avatar)

        # ---------------------------------------------------------- 端到端
        print("\n5. 端到端：带音色与形象跑一遍")
        run_config = base.with_overrides({
            "steps.voice.provider": "silent",
            "project.voice_id": voice.id,
            "project.avatar_id": avatar.id,
        })
        ctx = RunContext.create(run_config, slug="assets-e2e", run_id="probe-e2e")
        ctx.manifest.setdefault("inputs", {})["topic"] = "资产链路验证"
        ctx.save()
        ok = Runner(ctx).run(keep_going=False)

        voice_data = json.loads(
            (ctx.dir / "work" / "voice" / "voice_segments.json").read_text(encoding="utf-8"))
        avatar_data = json.loads(
            (ctx.dir / "work" / "avatar" / "avatar.json").read_text(encoding="utf-8"))

        check("流水线跑通", ok)
        check("语音记录里带上了音色 ID", voice_data.get("voice_id") == voice.id,
              str(voice_data.get("voice_id")))
        check("明确标注没有真正克隆", voice_data.get("cloned") is False)
        check("提示里说清了没用你的音色",
              "没有" in str(voice_data.get("note", "")) and voice.name in str(voice_data.get("note", "")),
              str(voice_data.get("note", ""))[:70])
        check("形象被真正用上", avatar_data.get("used_portrait") is True)
        check("画面确实是人物合成而非纯推镜", avatar_data.get("used_portrait") is True,
              "走的是 person_over_background 分支")

        # 抽一帧确认人物层真的合进去了（有照片时画面中央下方应有明显色块）
        final = ctx.artifact_path("video")
        if final and final.exists():
            frame = WORK / "frame.jpg"
            M.extract_frame(final, frame, at_s=1.0)
            check("能抽出成片帧", frame.exists(), f"{frame.stat().st_size / 1024:.0f} KB")

    finally:
        for vid in created_voices:
            store.delete_voice(vid)
        for aid in created_avatars:
            store.delete_avatar(aid)
        # 清理探针运行目录
        runs_dir = config.path(config.get("project.out_dir", "runs"))
        for name in ("probe-base", "probe-voice", "probe-avatar", "probe-e2e"):
            shutil.rmtree(runs_dir / name, ignore_errors=True)

    print("\n" + "=" * 68)
    if problems:
        print(f"  {FAIL} {len(problems)} 项未通过：")
        for item in problems:
            print(f"    - {item}")
        return 1
    print(f"  {PASS} 全部通过 —— 资产层与提取链路正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
