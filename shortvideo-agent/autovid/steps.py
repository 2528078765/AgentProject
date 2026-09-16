"""各环节的具体实现。每个 step 的职责：读上游产物 -> 干活 -> 登记自己的产物。"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Callable

from . import media as M
from . import providers as P
from .assets import AssetStore
from .errors import AutoVidError
from .manifest import RunContext

Logger = Callable[[str], None]

# 内置选题池：离线兜底用。接真实热榜请换成 DailyHotApi 之类的数据源。
TOPIC_POOL = [
    "为什么你越努力越焦虑",
    "普通人存下第一个十万的真实路径",
    "副业做不起来，往往卡在这一步",
    "信息过载时代怎么筛选真正有用的东西",
    "把一件小事做到极致是什么体验",
    "如何用最小成本验证一个想法",
    "为什么计划总是执行不下去",
    "真正拉开差距的是复盘能力",
    "别再用忙碌感动自己",
    "学会给自己的生活做减法",
]


def work_dir(ctx: RunContext, name: str) -> Path:
    path = ctx.dir / "work" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------- #
# 1. 选题
# --------------------------------------------------------------------------- #
def step_topic(ctx: RunContext, state: dict, log: Logger) -> None:
    provided = str(ctx.manifest.get("inputs", {}).get("topic", "")).strip()
    if provided:
        topic, source = provided, "cli"
        log(f"使用指定选题：{topic}")
    else:
        rng = random.Random(ctx.run_id)
        topic, source = rng.choice(TOPIC_POOL), "pool"
        log(f"从内置选题池抽取：{topic}")
    out = work_dir(ctx, "topic") / "topic.json"
    out.write_text(
        json.dumps({"topic": topic, "source": source}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    ctx.add_artifact("topic", "topic", out, "json")


# --------------------------------------------------------------------------- #
# 2. 口播稿（改写）
# --------------------------------------------------------------------------- #
def step_script(ctx: RunContext, state: dict, log: Logger) -> None:
    topic_data = state["topic"]
    source_text = ""
    script_file = str(ctx.manifest.get("inputs", {}).get("script_file", "")).strip()
    if script_file:
        path = Path(script_file)
        if not path.exists():
            raise AutoVidError(f"[script] 找不到文案文件：{path}")
        source_text = path.read_text(encoding="utf-8")
        log(f"读入原始素材：{path.name}（{len(source_text)} 字）")

    script = P.llm_rewrite(ctx.config, topic_data["topic"], source_text, log)
    out = work_dir(ctx, "script") / "script.json"
    out.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.add_artifact("script", "script", out, "json")


# --------------------------------------------------------------------------- #
# 3. 音色克隆 / 语音合成
# --------------------------------------------------------------------------- #
def _plan_utterances(segments: list[dict], cfg: dict, log: Logger) -> list[dict[str, Any]]:
    """把一个 segment 拆成若干「气口句」，并给每句配上停顿长度。

    这是「优化断句和气口」的核心：
      * 一口气说不完 40 个字，所以按标点 + 最大字数切成 12~20 字的小句；
      * 停顿按标点分级（逗号短、句号长、段落最长），而不是所有地方都停一样久；
      * 用固定种子做抖动，避免均匀停顿的机械感，同时保证时间轴可复现。
    """
    max_chars = int(cfg.get("breath_max_chars", 20))
    jitter = max(0, int(cfg.get("pause_jitter_ms", 40)))
    rng = random.Random(str(cfg.get("pause_seed", "autovid")))
    segment_pause = int(cfg.get("pause_segment_ms", M.DEFAULT_PAUSES["segment"]))

    items: list[dict[str, Any]] = []
    for segment in segments:
        groups = M.split_into_breaths(str(segment["text"]), max_chars)
        if not groups:
            continue
        for index, (text, ending) in enumerate(groups):
            is_last = index == len(groups) - 1
            if is_last:
                # 段落的最后一句：用更长的停顿，模拟换段落时的换气
                pause = segment_pause + (rng.randint(-jitter, jitter) if jitter else 0)
            else:
                pause = M.pause_for_punctuation(ending, cfg, rng)
            items.append({
                "segment_id": segment["id"],
                "segment_index": segment["index"],
                "text": text,
                "ending": ending,
                "pause_ms": max(0, pause),
                "is_segment_end": is_last,
            })
    return items


def step_voice(ctx: RunContext, state: dict, log: Logger) -> None:
    script = state["script"]
    segments = script["segments"]
    cfg = ctx.config.step_cfg("voice")
    out = work_dir(ctx, "voice")

    voice_asset = _load_voice_asset(ctx, log)

    # 1) 断句：段 -> 气口句（带分级停顿）
    items = _plan_utterances(segments, cfg, log)
    if not items:
        raise AutoVidError("断句后没有任何可合成的句子，请检查口播稿")
    max_chars = int(cfg.get("breath_max_chars", 20))
    avg_pause = sum(i["pause_ms"] for i in items) / len(items)
    log(f"断句：{len(segments)} 段 -> {len(items)} 个气口句"
        f"（每句最多 {max_chars} 字，平均停顿 {avg_pause:.0f}ms）")

    # 2) 逐句合成。
    #    注意输出到 utt/ 子目录：所有 TTS provider 都按 seg_NN.wav 命名，
    #    而下面「每段一个文件」也叫 seg_NN.wav —— 同目录会互相覆盖，读出空文件。
    result = P.tts_synthesize(ctx.config, items, out / "utt", log, voice_asset=voice_asset)

    # 3) 先按段拼（保留段内气口），再拼成全篇。
    #    这样做的好处：段落之间的停顿天然包含在段尾，全篇拼接不需要再插空隙，
    #    而且「每段一个音频文件」的契约保留下来（云端数字人接口需要整段音频）。
    by_segment: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        by_segment.setdefault(item["segment_id"], []).append(index)

    utterances: list[dict[str, Any]] = []
    segment_wavs: list[Path] = []
    segment_meta: list[dict[str, Any]] = []

    for segment in segments:
        indexes = by_segment.get(segment["id"], [])
        if not indexes:
            continue
        segment_wav = out / f"seg_{segment['index']:02d}.wav"
        relative = M.concat_wav_pcm(
            [result.parts[i] for i in indexes], segment_wav,
            gaps_ms=[items[i]["pause_ms"] for i in indexes], trailing_gap=True,
        )
        segment_wavs.append(segment_wav)
        segment_meta.append({"segment": segment, "indexes": indexes, "relative": relative})

    full = out / "voice.wav"
    global_timeline = M.concat_wav_pcm(segment_wavs, full, gap_ms=0, trailing_gap=False)

    # 4) 把段内相对时间加上段起点，得到全篇绝对时间轴
    merged: list[dict[str, Any]] = []
    for meta_index, (meta, whole) in enumerate(zip(segment_meta, global_timeline)):
        segment = meta["segment"]
        offset = whole["start"]
        for i, timing in zip(meta["indexes"], meta["relative"]):
            item = items[i]
            utterances.append({
                "segment_id": item["segment_id"],
                "segment_index": item["segment_index"],
                "text": item["text"],
                "ending": item["ending"],
                "pause_ms": item["pause_ms"],
                "start": round(offset + timing["start"], 4),
                "end": round(offset + timing["end"], 4),
                "duration": round(timing["duration"], 4),
                "audio_file": result.parts[i].relative_to(ctx.dir).as_posix(),
            })
        merged.append({
            "id": segment["id"],
            "index": segment["index"],
            "headline": segment["headline"],
            "text": segment["text"],
            "start": round(whole["start"], 4),
            "end": round(whole["end"], 4),
            "duration": round(whole["duration"], 4),
            "clip_duration": round(whole["duration"], 4),
            "utterance_count": len(meta["indexes"]),
            # 整段一个文件：云端数字人接口要送整段音频，不能只送第一口气
            "audio_file": segment_wavs[meta_index].relative_to(ctx.dir).as_posix(),
        })

    segments_json = {
        "provider": result.provider,
        "note": result.note,
        # 关键：明确记录「本次到底有没有用上用户上传的音色」，
        # 前端据此给出「你现在听到的不是你的声音」的提示。
        "cloned": bool(result.cloned),
        "voice_id": result.voice_id,
        "voice_name": result.voice_name,
        "sample_rate": int(cfg.get("sample_rate", 24000)),
        "breath_max_chars": max_chars,
        "gap_ms": int(cfg.get("gap_ms", 220)),
        "total_duration_s": round(M.wav_duration(full), 3),
        "utterances": utterances,
        "segments": merged,
    }
    seg_path = out / "voice_segments.json"
    seg_path.write_text(json.dumps(segments_json, ensure_ascii=False, indent=2), encoding="utf-8")

    ctx.add_artifact("voice", "voice", full, "audio")
    ctx.add_artifact("voice", "voice_segments", seg_path, "json")
    log(
        f"语音完成：provider={result.provider}，"
        f"总时长 {segments_json['total_duration_s']}s，"
        f"{len(merged)} 段 / {len(utterances)} 个气口句"
    )


def _load_voice_asset(ctx: RunContext, log: Logger):
    """按 project.voice_id 载入音色资产；没配或找不到就返回 None。"""
    voice_id = str(ctx.config.get("project.voice_id", "") or "").strip()
    if not voice_id:
        return None
    store = AssetStore(ctx.config)
    asset = store.get_voice(voice_id)
    if asset is None:
        log(f"  ⚠ 找不到音色资产 {voice_id}，本次用 provider 内置音色")
        return None
    return asset


def _load_avatar_portrait(ctx: RunContext, log: Logger):
    """按 project.avatar_id 载入形象主图；没配或找不到就返回 None。"""
    avatar_id = str(ctx.config.get("project.avatar_id", "") or "").strip()
    if not avatar_id:
        return None
    store = AssetStore(ctx.config)
    asset = store.get_avatar(avatar_id)
    if asset is None:
        log(f"  ⚠ 找不到形象资产 {avatar_id}，本次只用背景图")
        return None
    portrait = store.avatar_portrait(avatar_id)
    if portrait is None:
        log(f"  ⚠ 形象「{asset.name}」还没有上传照片，本次只用背景图")
        return None
    return portrait


# --------------------------------------------------------------------------- #
# 4. 背景图 / 封面底图
# --------------------------------------------------------------------------- #
def step_visuals(ctx: RunContext, state: dict, log: Logger) -> None:
    script = state["script"]
    platform = ctx.config.platform
    width = int(platform.get("width", 1080))
    height = int(platform.get("height", 1920))
    out = work_dir(ctx, "visuals")

    prompts = [s["visual_prompt"] for s in script["segments"]]
    paths, notes = P.image_generate(ctx.config, prompts, out, width, height, log)

    items = [
        {"id": s["id"], "index": s["index"], "prompt": s["visual_prompt"],
         "file": p.relative_to(ctx.dir).as_posix()}
        for s, p in zip(script["segments"], paths)
    ]
    data = {
        "provider": str(ctx.config.provider_of("visuals")),
        "width": width,
        "height": height,
        "images": items,
    }
    json_path = out / "visuals.json"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.add_artifact("visuals", "visuals", json_path, "json")
    for index, path in enumerate(paths):
        ctx.add_artifact("visuals", f"visual_{index:02d}", path, "image")


# --------------------------------------------------------------------------- #
# 5. 数字人驱动
# --------------------------------------------------------------------------- #
def step_avatar(ctx: RunContext, state: dict, log: Logger) -> None:
    script = state["script"]
    voice = state["voice_segments"]
    visuals_data = state["visuals"]
    platform = ctx.config.platform
    width = int(platform.get("width", 1080))
    height = int(platform.get("height", 1920))
    fps = int(platform.get("fps", 30))
    out = work_dir(ctx, "avatar")

    visuals = [ctx.path(v["file"]) for v in visuals_data["images"]]
    # 把音频绝对路径注入 segments，供云端数字人 provider 使用
    segments: list[dict[str, Any]] = []
    for segment in voice["segments"]:
        item = dict(segment)
        item["audio_path"] = str(ctx.path(segment["audio_file"]))
        segments.append(item)

    portrait = _load_avatar_portrait(ctx, log)
    clips, provider = P.avatar_render(
        ctx.config, segments, visuals, out, width, height, fps, log, portrait=portrait
    )

    items = [
        {"id": s["id"], "index": s["index"],
         "file": c.relative_to(ctx.dir).as_posix(),
         "duration": s["clip_duration"]}
        for s, c in zip(segments, clips)
    ]
    avatar_id = str(ctx.config.get("project.avatar_id", "") or "").strip()
    data = {
        "provider": provider,
        "width": width,
        "height": height,
        "fps": fps,
        "avatar_id": avatar_id or None,
        "used_portrait": portrait is not None,
        "clips": items,
    }
    json_path = out / "avatar.json"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.add_artifact("avatar", "avatar", json_path, "json")
    for index, clip in enumerate(clips):
        ctx.add_artifact("avatar", f"clip_{index:02d}", clip, "video")


# --------------------------------------------------------------------------- #
# 6. 字幕
# --------------------------------------------------------------------------- #
def _short_title(text: str, limit: int = 9) -> str:
    """取第一小句并截断，用于顶部关键词条。

    上屏标题必须短 —— 直接把整段 hook 挂上去会撑成四五行，非常难看。
    """
    cleaned = text.strip()
    for sep in "，。！？；：、,.!?;:":
        if sep in cleaned:
            cleaned = cleaned.split(sep)[0]
    return cleaned.strip()[:limit]


def _looks_duplicate(a: str, b: str, min_common: int = 6) -> bool:
    """判断两条上屏标题是否实质重复（靠公共前缀）。

    用 --script-file 时，hook 就是第一段的原文，而第一段的关键词又取自同一句，
    两者几乎一样 —— 直接都上屏会出现两条黄字叠在一起，非常难看。
    """
    if not a or not b:
        return False
    common = 0
    for left, right in zip(a, b):
        if left != right:
            break
        common += 1
    return common >= min_common


def _display_chunks(text: str, chunk_chars: int) -> list[str]:
    """把一段口播拆成适合逐句上屏的小块。

    按标点切成小句，再贪心打包到 chunk_chars 以内。单句超长就硬切。
    这样字幕是「跟读式」一块块推进，而不是整段文字一直挂在屏幕上。
    """
    clauses: list[str] = []
    current = ""
    for char in text.strip():
        current += char
        if char in "，。！？；：、,.!?;:":
            clauses.append(current)
            current = ""
    if current.strip():
        clauses.append(current)

    chunks: list[str] = []
    buffer = ""
    for clause in clauses:
        while len(clause) > chunk_chars:          # 单句过长，硬切
            if buffer:
                chunks.append(buffer)
                buffer = ""
            chunks.append(clause[:chunk_chars])
            clause = clause[chunk_chars:]
        if len(buffer) + len(clause) <= chunk_chars:
            buffer += clause
        else:
            if buffer:
                chunks.append(buffer)
            buffer = clause
    if buffer.strip():
        chunks.append(buffer)
    return [c for c in chunks if c.strip()]


def _subtitle_events_for(segment: dict[str, Any], max_chars: int) -> list[dict[str, Any]]:
    """把一段语音切成若干条字幕，时间按字数比例分配。

    段内的分配是估算（真正的逐字时间戳需要 TTS 返回 word boundary），
    但因为是按字数比例切，跟读观感已经足够对齐；而**段与段之间**的时间
    是精确的（来自 PCM 样本数）。
    """
    chunks = _display_chunks(str(segment["text"]), max(2, max_chars * 2 - 2))
    if not chunks:
        return []
    total = sum(len(c) for c in chunks) or 1
    start = float(segment["start"])
    speech = float(segment.get("duration", segment["end"] - start))
    events: list[dict[str, Any]] = []
    cursor = start
    for index, chunk in enumerate(chunks):
        share = len(chunk) / total
        end = start + speech if index == len(chunks) - 1 else cursor + speech * share
        events.append({
            "style": "Main", "start": round(cursor, 3), "end": round(end, 3),
            "text": chunk, "max_chars": max_chars,
        })
        cursor = end
    return events


def step_subtitles(ctx: RunContext, state: dict, log: Logger) -> None:
    script = state["script"]
    voice = state["voice_segments"]
    cfg = ctx.config.step_cfg("subtitles")
    platform = ctx.config.platform
    width = int(platform.get("width", 1080))
    height = int(platform.get("height", 1920))
    out = work_dir(ctx, "subtitles")

    font = str(cfg.get("font", "Microsoft YaHei"))
    main_style = M.make_style(
        "Main", font, int(cfg.get("font_size", 76)),
        primary=str(cfg.get("primary", "&H00FFFFFF")),
        outline_colour=str(cfg.get("outline_colour", "&H00202020")),
        back_colour=str(cfg.get("back_colour", "&H80000000")),
        bold=bool(cfg.get("bold", True)),
        outline=int(cfg.get("outline", 5)),
        shadow=int(cfg.get("shadow", 2)),
        alignment=2,
        margin_v=int(cfg.get("margin_v", 360)),
    )
    # 顶部关键词条：让每段有个上屏标题，观感更像成片
    title_style = M.make_style(
        "Keyword", font, int(cfg.get("keyword_size", 88)),
        primary=str(cfg.get("keyword_colour", "&H0000E5FF")),
        outline_colour=str(cfg.get("outline_colour", "&H00202020")),
        back_colour="&H80000000",
        bold=True, outline=int(cfg.get("outline", 5)) + 1, shadow=2,
        alignment=8, margin_v=int(cfg.get("keyword_margin_v", 210)),
    )

    max_chars = int(cfg.get("max_chars_per_line", 13))
    segments = voice["segments"]
    events: list[dict[str, Any]] = []

    # 开场标题卡：只占开头一小段，文字必须短
    hook_title = ""
    hook_end = 0.0
    if bool(cfg.get("show_hook", True)) and script.get("hook"):
        hook_title = _short_title(str(script["hook"]))
        hook_end = min(2.4, segments[0]["end"] if segments else 2.4)
        events.append({"style": "Keyword", "start": 0.0, "end": hook_end,
                       "text": hook_title, "max_chars": 10})

    for index, segment in enumerate(segments):
        headline = _short_title(str(segment["headline"]))
        start = float(segment["start"])
        end = float(segment["end"])
        if index == 0 and hook_title:
            if _looks_duplicate(headline, hook_title):
                headline = ""                    # 与开场标题重复，不再重复上屏
            else:
                start = max(start, hook_end)     # 顺延，避免两条黄字叠在一起
        if headline and start < end:
            events.append({"style": "Keyword", "start": round(start, 3),
                           "end": round(end, 3), "text": headline, "max_chars": 10})

    # 正文字幕直接吃「气口句」的时间轴：一条字幕 = 一口气，天然跟读。
    # 这比按整段估算切分准得多 —— 时间戳来自 PCM 样本数，不是猜的。
    utterances = voice.get("utterances") or []
    if utterances:
        for item in utterances:
            events.append({
                "style": "Main",
                "start": round(float(item["start"]), 3),
                "end": round(float(item["end"]), 3),
                "text": str(item["text"]),
                "max_chars": max_chars,
            })
    else:
        # 兼容早期没有 utterances 的运行记录
        for segment in segments:
            events.extend(_subtitle_events_for(segment, max_chars))

    ass_path = out / "subtitles.ass"
    M.write_ass(ass_path, M.build_ass([main_style, title_style], events, width, height,
                                      title=script.get("topic", "AutoVid")))
    ctx.add_artifact("subtitles", "subtitles", ass_path, "file")
    log(f"字幕完成：{len(events)} 条事件"
        f"（{len(segments)} 段 / {len(utterances) or '按段估算'} 个气口句）")


# --------------------------------------------------------------------------- #
# 7. 合成成片 + 封面
# --------------------------------------------------------------------------- #
def step_compose(ctx: RunContext, state: dict, log: Logger) -> None:
    script = state["script"]
    avatar = state["avatar"]
    cfg = ctx.config.step_cfg("subtitles")
    platform = ctx.config.platform
    width = int(platform.get("width", 1080))
    height = int(platform.get("height", 1920))
    fps = int(platform.get("fps", 30))
    out = work_dir(ctx, "compose")

    clips = [ctx.path(c["file"]) for c in avatar["clips"]]
    voice_wav = ctx.artifact_path("voice")
    ass_path = ctx.artifact_path("subtitles")
    if voice_wav is None:
        raise AutoVidError("[compose] 找不到 voice.wav 产物")
    if ass_path is None:
        raise AutoVidError("[compose] 找不到字幕产物")

    log("拼接视频轨...")
    vtrack = M.concat_clips(clips, out / "vtrack.mp4", cwd=out)

    log("合并音频轨...")
    av = M.mux_audio(vtrack, voice_wav, out / "av.mp4", cwd=out,
                     audio_bitrate=str(platform.get("audio_bitrate", "192k")))

    log("烧录字幕...")
    final = M.burn_subtitles(av, ass_path, out / "final.mp4", cwd=out,
                             crf=int(platform.get("crf", 20)))

    # 封面：复用 libass，中文渲染无坑；同时出 3:4 版本（部分场景推荐）
    log("渲染封面...")
    cover_bg = ctx.path(avatar["clips"][0]["file"])
    title_text = script.get("title_options", [script.get("topic", "")])[0]
    font = str(cfg.get("font", "Microsoft YaHei"))

    def _cover(height_px: int, name: str) -> Path:
        style = M.make_style("Cover", font, int(cfg.get("cover_size", 110)),
                             primary="&H00FFFFFF", outline_colour="&H00202020",
                             bold=True, outline=7, shadow=3, alignment=5,
                             margin_l=90, margin_r=90, margin_v=0)
        ass = M.write_ass(
            out / f"{name}.ass",
            M.build_ass([style], [{"style": "Cover", "start": 0.0, "end": 3.0,
                                   "text": str(title_text), "max_chars": 11}],
                        width, height_px, title=name),
        )
        return M.render_card(cover_bg, ass, out / f"{name}", width, height_px, cwd=out)

    cover = _cover(height, "cover.jpg")
    cover_3x4 = _cover(int(width * 4 / 3), "cover_3x4.jpg")

    ctx.add_artifact("compose", "video", final, "video")
    ctx.add_artifact("compose", "cover", cover, "image")
    ctx.add_artifact("compose", "cover_3x4", cover_3x4, "image")
    duration = M.probe_duration(final)
    log(f"成片完成：{final.name}，{duration:.2f}s，{final.stat().st_size / 1024 / 1024:.1f} MB")
    if duration < float(platform.get("min_duration_s", 0)):
        log(f"  ⚠ 时长 {duration:.1f}s 低于平台建议下限 {platform.get('min_duration_s')}s")


# --------------------------------------------------------------------------- #
# 8. 标题 / 标签 / 简介
# --------------------------------------------------------------------------- #
def step_metadata(ctx: RunContext, state: dict, log: Logger) -> None:
    script = state["script"]
    cfg = ctx.config.step_cfg("metadata")
    provider = str(cfg.get("provider", "offline"))
    title_count = int(cfg.get("title_count", 3))
    tag_count = int(cfg.get("tag_count", 6))

    titles = list(script.get("title_options") or [script.get("topic", "")])
    tags = list(script.get("tags") or [])
    description = str(script.get("description", ""))

    if provider == "llm":
        try:
            refined = _metadata_via_llm(ctx, script, title_count, tag_count, log)
            titles = refined.get("titles") or titles
            tags = refined.get("tags") or tags
            description = refined.get("description") or description
            log("标题/标签由 LLM 生成")
        except Exception as exc:  # noqa: BLE001 - 元数据不该阻断成片
            log(f"  ⚠ LLM 生成元数据失败，回退到脚本自带：{str(exc).splitlines()[0][:120]}")
    else:
        log("使用脚本自带的标题/标签（离线）")

    # 平台硬约束：抖音标题建议 ≤30 字
    titles = [t for t in titles if t][:title_count]
    tags = [t.lstrip("#") for t in tags if t][:tag_count]

    data = {
        "provider": provider,
        "titles": titles,
        "title_options": titles,
        "description": description,
        "tags": tags,
        "cta": script.get("cta", ""),
        "topic": script.get("topic", ""),
    }
    out = work_dir(ctx, "metadata") / "metadata.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.add_artifact("metadata", "metadata", out, "json")


def _metadata_via_llm(ctx: RunContext, script: dict, title_count: int, tag_count: int, log) -> dict:
    cfg = ctx.config.provider_cfg("llm")
    base = str(cfg.get("base_url", "")).rstrip("/")
    api_key = ctx.config.secret_for("llm", "AUTOVID_LLM_API_KEY")
    payload = {
        "model": cfg.get("model", "deepseek-chat"),
        "temperature": 0.9,
        "messages": [
            {"role": "system", "content": (
                "你是抖音标题优化师。标题必须≤30个汉字、有钩子、不夸大不承诺收益。"
                "只输出 JSON：{\"titles\":[...],\"tags\":[...],\"description\":\"...\"}，"
                "tags 不带 # 号，4-8 个。"
            )},
            {"role": "user", "content": json.dumps(
                {"topic": script.get("topic"), "hook": script.get("hook"),
                 "segments": [s["text"] for s in script.get("segments", [])]},
                ensure_ascii=False)},
        ],
    }
    data = P._http(f"{base}/chat/completions", payload,
                   headers=P._auth_header(api_key), timeout_s=int(cfg.get("timeout_s", 120)))
    content = data["choices"][0]["message"]["content"]
    parsed = P._extract_json(content)
    return {
        "titles": [str(t)[:30] for t in parsed.get("titles", [])][:title_count],
        "tags": [str(t).lstrip("#") for t in parsed.get("tags", [])][:tag_count],
        "description": str(parsed.get("description", "")),
    }


# --------------------------------------------------------------------------- #
# 9. 发布包
# --------------------------------------------------------------------------- #
def step_publish(ctx: RunContext, state: dict, log: Logger) -> None:
    metadata = state["metadata"]
    video = ctx.artifact_path("video")
    cover = ctx.artifact_path("cover")
    cover_3x4 = ctx.artifact_path("cover_3x4")
    for name, path in (("video", video), ("cover", cover), ("cover_3x4", cover_3x4)):
        if path is None:
            raise AutoVidError(f"[publish] 缺少产物 {name}")

    # 发布包放在 run 目录顶层（而不是 work/ 下），方便直接找到并上传
    out = ctx.dir / "publish"
    out.mkdir(parents=True, exist_ok=True)
    report = P.publish(ctx.config, video, cover, cover_3x4, metadata, out, log)
    report_path = out / "publish_report.json"
    ctx.add_artifact("publish", "publish_report", report_path, "json")
    ctx.add_artifact("publish", "caption", Path(report["caption_file"]), "text")
    ctx.add_artifact("publish", "checklist", Path(report["checklist_file"]), "text")
    # 复制进发布包的成片/封面也要登记，否则文件被删掉后缓存判定会误认为可复用
    if bool(ctx.config.step_cfg("publish").get("copy_media", True)):
        ctx.add_artifact("publish", "packaged_video", Path(report["video"]), "video")
        ctx.add_artifact("publish", "packaged_cover", Path(report["cover"]), "image")
        ctx.add_artifact("publish", "packaged_cover_3x4", Path(report["cover_3x4"]), "image")
