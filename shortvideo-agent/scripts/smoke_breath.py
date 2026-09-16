"""断句与气口的专项测试。

「一口气说不完 40 个字」，所以配音前必须先把文本切成气口单位，再按标点分级给停顿。
这个脚本验证三件事：

    1. 切分不丢字、不超长、结束标点级别判断正确
    2. 停顿分级合理（句末 > 半句 > 逗号），且带抖动但不失控
    3. 接进流水线后时间轴自洽：气口句首尾相接、段与段不重叠、总时长 == 视频轨长度

    python scripts/smoke_breath.py
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autovid import media as M              # noqa: E402
from autovid.config import Config           # noqa: E402
from autovid.manifest import RunContext     # noqa: E402
from autovid.pipeline import Runner         # noqa: E402

PASS, FAIL = "[通过]", "[失败]"
problems: list[str] = []
WORK = ROOT / ".tmp" / "breath_probe"


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {PASS if ok else FAIL} {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        problems.append(name)
    return ok


def no_ws(text: str) -> str:
    return re.sub(r"\s+", "", text)


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)

    print("断句与气口测试")
    print("=" * 70)

    # ---------------------------------------------------------------- 切分
    print("\n1. 切分正确性")
    samples = [
        "先说结论：大多数人第一步就做错了。",
        "第一，别急着上手；第二，把过程记下来，这样下一次才有据可依。",
        "他说：“这个方向不对。”然后就走了。",
        "这是一段完全没有标点的超长句子用来验证硬切逻辑是否会把文字丢掉并且每段都不超过限制长度",
        "短句。",
        "",
    ]
    max_chars = 20
    for text in samples:
        chunks = M.split_into_breaths(text, max_chars)
        joined = "".join(c for c, _ in chunks)
        if not text.strip():
            check(f"空文本返回空 -> {chunks!r}", chunks == [])
            continue
        longest = max(len(c) for c, _ in chunks)
        check(
            f"不丢字（{len(text)} 字 -> {len(chunks)} 句）",
            no_ws(joined) == no_ws(text),
            f"最长 {longest} 字",
        )
        check(f"不超长（<= {max_chars}）", longest <= max_chars, f"最长 {longest}")

    chunks = M.split_into_breaths("先做这个，再做那个。最后收尾。", 20)
    endings = [e for _, e in chunks]
    check("逗号判为 comma", "comma" in endings, str(endings))
    check("句号判为 sentence", "sentence" in endings, str(endings))

    quoted = M.split_into_breaths("他说：“这个方向不对。”然后走了。", 20)
    check("能跳过右引号判断结束标点",
          any(e == "sentence" for _, e in quoted), str([e for _, e in quoted]))

    # ---------------------------------------------------------------- 停顿
    print("\n2. 停顿分级")
    comma = M.pause_for_punctuation("comma", {})
    clause = M.pause_for_punctuation("clause", {})
    sentence = M.pause_for_punctuation("sentence", {})
    check("句末 > 半句 > 逗号", sentence > clause > comma,
          f"{comma} / {clause} / {sentence} ms")

    import random
    rng = random.Random("t")
    values = [M.pause_for_punctuation("sentence", {"pause_jitter_ms": 40}, rng) for _ in range(40)]
    check("抖动生效但幅度可控",
          len(set(values)) > 1 and all(390 <= v <= 470 for v in values),
          f"范围 {min(values)}~{max(values)} ms")

    check("固定种子可复现",
          [M.pause_for_punctuation("sentence", {"pause_jitter_ms": 40}, random.Random("x"))
           for _ in range(3)] ==
          [M.pause_for_punctuation("sentence", {"pause_jitter_ms": 40}, random.Random("x"))
           for _ in range(3)])

    # ---------------------------------------------------------------- 拼接
    print("\n3. 逐句停顿真的落进音频里")
    import wave, math, io

    def tone(path: Path, seconds: float, freq: float) -> Path:
        rate = 24000
        frames = int(seconds * rate)
        with wave.open(str(path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(rate)
            writer.writeframes(b"".join(
                int(8000 * math.sin(2 * math.pi * freq * i / rate)).to_bytes(2, "little", signed=True)
                for i in range(frames)))
        return path

    parts = [tone(WORK / f"p{i}.wav", 1.0, 300 + i * 50) for i in range(3)]
    gaps = [100, 500, 900]
    out = WORK / "joined.wav"
    timeline = M.concat_wav_pcm(parts, out, gaps_ms=gaps, trailing_gap=True)
    total = M.wav_duration(out)
    expected = 3 * 1.0 + sum(gaps) / 1000.0
    check("总时长 = 各段时长 + 各自停顿", abs(total - expected) < 0.01,
          f"{total:.3f}s vs 期望 {expected:.3f}s")
    check("每段记录了各自的停顿",
          [t["pause_ms"] for t in timeline] == gaps, str([t["pause_ms"] for t in timeline]))
    check("时间轴首尾相接",
          all(abs(timeline[i]["end"] + gaps[i] / 1000 - timeline[i + 1]["start"]) < 0.01
              for i in range(len(timeline) - 1)))

    # ---------------------------------------------------------------- 端到端
    print("\n4. 接进流水线后时间轴自洽")
    config = Config.load(root=ROOT).with_overrides({
        "steps.script.provider": "offline",
        "steps.voice.provider": "silent",
        "steps.avatar.provider": "still",
        "project.require_assets": False,
        "steps.voice.breath_max_chars": 20,
    })
    ctx = RunContext.create(config, slug="breath", run_id="probe-breath")
    ctx.manifest.setdefault("inputs", {})["topic"] = "断句气口验证"
    ctx.save()

    runner = Runner(ctx)
    ok = runner.run(only=["topic", "script", "voice"])
    check("流水线跑通", ok)

    data = json.loads(
        (ctx.dir / "work" / "voice" / "voice_segments.json").read_text(encoding="utf-8"))
    utterances = data.get("utterances") or []
    segments = data.get("segments") or []
    total_duration = data["total_duration_s"]

    check("产出了气口句时间轴", len(utterances) > 0, f"{len(utterances)} 句")
    check("气口句比段落更细", len(utterances) >= len(segments),
          f"{len(segments)} 段 -> {len(utterances)} 句")
    check("每个气口句都不超长",
          all(len(u["text"]) <= 20 for u in utterances),
          f"最长 {max(len(u['text']) for u in utterances)} 字")

    # 抖动让停顿有变化，但必须落在合理区间
    pauses = [u["pause_ms"] for u in utterances]
    check("停顿有变化（不是均匀的机械感）", len(set(pauses)) > 1,
          f"{min(pauses)}~{max(pauses)} ms")

    # 时间轴连续性
    ordered = sorted(utterances, key=lambda u: u["start"])
    check("气口句按时间有序且不重叠",
          all(ordered[i]["end"] <= ordered[i + 1]["start"] + 0.001
              for i in range(len(ordered) - 1)))
    # 末尾那句之后还有「段落停顿」，所以末尾 speech 结束时间会比总长少一个尾停顿
    tail_gap = (total_duration - ordered[-1]["end"]) * 1000
    check("音频总长 = 末尾语音结束 + 尾停顿",
          abs(tail_gap - ordered[-1]["pause_ms"]) < 50,
          f"尾停顿 {tail_gap:.0f}ms vs 记录 {ordered[-1]['pause_ms']}ms")

    seg_span = sum(s["clip_duration"] for s in segments)
    check("段视频时长之和 == 音频总长（音视频对齐）",
          abs(seg_span - total_duration) < 0.35,
          f"{seg_span:.2f}s vs {total_duration:.2f}s")

    # 每段都要有整段音频文件（云端数字人接口要送整段，不能只送第一口气）
    missing = [s["id"] for s in segments if not (ctx.dir / s["audio_file"]).exists()]
    check("每段都有整段音频文件（供数字人接口使用）", not missing, str(missing))

    # 字幕应该直接吃气口句
    runner.run(only=["subtitles"])
    ass = (ctx.dir / "work" / "subtitles" / "subtitles.ass").read_text(encoding="utf-8")
    dialogue = [line for line in ass.splitlines() if line.startswith("Dialogue")]
    main_lines = [line for line in dialogue if ",Main," in line]
    check("字幕条数 = 气口句数", len(main_lines) == len(utterances),
          f"{len(main_lines)} 条 vs {len(utterances)} 句")

    shutil.rmtree(ctx.dir, ignore_errors=True)

    print("\n" + "=" * 70)
    if problems:
        print(f"  {FAIL} {len(problems)} 项未通过：")
        for item in problems:
            print(f"    - {item}")
        return 1
    print(f"  {PASS} 全部通过 —— 断句与气口正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
