"""媒体层：FFmpeg 封装、ASS 字幕生成、Windows SAPI 语音合成、离线素材生成。

两个关键设计决定：

1. **不用 drawtext，所有文字都走 ASS 层。**
   libass 天生处理中文、字体、换行、描边，而 drawtext 在 Windows 上要跟
   `C\\:/path/...` 这种转义搏斗。少一个脆弱点。

2. **所有 ffmpeg 调用都带 cwd，使用相对文件名。**
   同样是绕开 Windows 盘符冒号在 filter 参数里的转义问题（`ass=sub.ass` 永远安全）。

3. **TTS 音频拼接用标准库 wave 直接拼 PCM，不经过 ffmpeg。**
   音色克隆那一步因此完全不依赖 ffmpeg，且时间轴精确到样本。
"""

from __future__ import annotations

import json
import random
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path
from typing import Any, Iterable, Sequence

from .errors import AutoVidError

# --------------------------------------------------------------------------- #
# FFmpeg / FFprobe
# --------------------------------------------------------------------------- #
_FFMPEG: str | None = None
_FFPROBE: str | None = None


def _media_binary(name: str) -> str | None:
    """查找系统或项目自带的媒体工具。

    桌面应用、IDE 和普通终端经常拿到不同的 PATH。把 FFmpeg 放在项目
    ``vendor`` 下时也应当能稳定启动，不能要求用户每次重开终端。
    """
    found = shutil.which(name)
    if found:
        return found
    root = Path(__file__).resolve().parent.parent
    executable = f"{name}.exe" if sys.platform == "win32" else name
    candidates = [
        root / "vendor" / "ffmpeg" / "bin" / executable,
        *sorted((root / "vendor" / "ffmpeg-package").glob(f"*/bin/{executable}")),
    ]
    return str(next((path for path in candidates if path.is_file()), "")) or None


def ffmpeg_exe() -> str:
    global _FFMPEG
    if _FFMPEG is None:
        found = _media_binary("ffmpeg")
        if not found:
            raise AutoVidError(
                "[media] 找不到 ffmpeg。请安装并加入 PATH：\n"
                "        winget install Gyan.FFmpeg\n"
                "        安装后重开终端，用 `python scripts/check_env.py` 自检。"
            )
        _FFMPEG = found
    return _FFMPEG


def ffprobe_exe() -> str:
    global _FFPROBE
    if _FFPROBE is None:
        found = _media_binary("ffprobe")
        if not found:
            raise AutoVidError("[media] 找不到 ffprobe（通常与 ffmpeg 一起安装）")
        _FFPROBE = found
    return _FFPROBE


def has_ffmpeg() -> bool:
    return _media_binary("ffmpeg") is not None


class FFmpegError(AutoVidError):
    """ffmpeg / ffprobe 调用失败。继承 AutoVidError，与 provider 错误同源。"""


def run_ffmpeg(
    args: Sequence[str],
    cwd: Path | str | None = None,
    desc: str = "ffmpeg",
    timeout_s: int = 900,
) -> subprocess.CompletedProcess:
    """执行 ffmpeg，失败时抛出带 stderr 尾部的异常。"""
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"{desc} 超时（{timeout_s}s）") from exc
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-12:])
        raise FFmpegError(f"{desc} 失败 (exit {proc.returncode})\n{tail}")
    return proc


def localize_for_filter(path: Path | str, cwd: Path | str | None) -> str:
    """把文件放到 cwd 下，并返回纯文件名。

    libass / 滤镜参数里的 Windows 盘符冒号必须转义成 `C\\:/...`，而转义规则
    在不同 ffmpeg 版本上并不一致，是个反复踩的坑。直接把文件复制到 cwd、
    只用纯文件名，就彻底绕开了这个问题。
    """
    path = Path(path)
    if cwd is None:
        return path.name
    cwd_path = Path(cwd)
    if path.parent.resolve() != cwd_path.resolve():
        shutil.copy2(path, cwd_path / path.name)
    return path.name


def probe_media(path: Path | str) -> dict[str, Any]:
    """用 ffprobe 读媒体信息，用于上传校验。

    返回 {ok, has_audio, has_video, duration, width, height, codec, error}
    音频/图片/视频都能读 —— 音色和形象上传都靠它做体检。
    """
    try:
        proc = subprocess.run(
            [
                ffprobe_exe(), "-v", "error",
                "-show_entries", "stream=codec_type,codec_name,width,height,duration",
                "-show_entries", "format=duration",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"无法调用 ffprobe: {exc}"}
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or "").strip()[:200] or "ffprobe 读取失败"}

    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "ffprobe 输出无法解析"}

    streams = data.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    video = next((s for s in streams if s.get("codec_type") == "video"), None)

    def _num(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    duration = _num((data.get("format") or {}).get("duration"))
    if duration is None and audio:
        duration = _num(audio.get("duration"))
    if duration is None and video:
        duration = _num(video.get("duration"))

    return {
        "ok": bool(audio or video),
        "has_audio": audio is not None,
        "has_video": video is not None,
        "duration": duration,
        "width": video.get("width") if video else None,
        "height": video.get("height") if video else None,
        "codec": (audio or video or {}).get("codec_name"),
        "error": None if (audio or video) else "文件里没有音频流也没有视频流",
    }


def probe_video_spec(path: Path | str) -> tuple[int, int, float]:
    """读视频的 (宽, 高, 帧率)。读不到的部分为 0。

    帧率必须一起看：D-ID 实测输出 512×512 @ **25fps**，而正片是 30fps。
    只比尺寸会漏掉帧率不一致 —— 流拷贝拼接时同样会出问题。
    """
    proc = subprocess.run(
        [
            ffprobe_exe(), "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-of", "json",
            str(path),
        ],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        return (0, 0, 0.0)
    try:
        stream = (json.loads(proc.stdout or "{}").get("streams") or [{}])[0]
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        num, _, den = str(stream.get("r_frame_rate") or "0/1").partition("/")
        fps = float(num) / float(den) if float(den or 0) else 0.0
        return (width, height, round(fps, 3))
    except Exception:  # noqa: BLE001
        return (0, 0, 0.0)


def probe_video_size(path: Path | str) -> tuple[int, int]:
    """用 ffprobe 读视频宽高。读不到返回 (0, 0)。"""
    width, height, _ = probe_video_spec(path)
    return (width, height)


def fit_to_canvas(
    clip: Path,
    out: Path,
    width: int,
    height: int,
    fps: int = 30,
    blur: int = 24,
    cwd: Path | str | None = None,
) -> Path:
    """把任意尺寸的片段适配到竖屏画布（等比放大铺底 + 居中放正片）。

    为什么需要它：付费云端数字人（实测 D-ID）回的是 **512×512 方片**，
    而正片是 1080×1920。直接 concat 会因参数不一致拼出花屏；
    硬拉伸会把脸拉成马脸。这里用「模糊铺底 + 原始比例居中」——抖音上
    很常见的处理，观感是刻意的，不是事故。

    已经是目标尺寸的片段直接返回原文件，不重编码（省时间也不掉画质）。
    尺寸和帧率都一致才算「已经是」—— D-ID 实测出的是 512×512 @25fps，
    帧率对不上同样会导致流拷贝拼接出问题。
    """
    src_w, src_h, src_fps = probe_video_spec(clip)
    if (src_w, src_h) == (width, height) and (not src_fps or abs(src_fps - fps) < 0.01):
        return clip
    vf = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},gblur=sigma={int(blur)}[bg];"
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p,fps={int(fps)}[v]"
    )
    run_ffmpeg(
        [
            "-i", str(clip),
            "-filter_complex", vf,
            "-map", "[v]", "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            str(out),
        ],
        cwd=cwd, desc=f"适配画布 {width}x{height} -> {out.name}",
    )
    return out


def probe_duration(path: Path | str) -> float:
    """用 ffprobe 读时长（秒）。"""
    proc = subprocess.run(
        [
            ffprobe_exe(), "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe 读取时长失败: {path}\n{proc.stderr.strip()[:400]}")
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise FFmpegError(f"ffprobe 返回了无法解析的时长: {proc.stdout!r}") from exc


# --------------------------------------------------------------------------- #
# WAV 工具（纯标准库，不依赖 ffmpeg）
# --------------------------------------------------------------------------- #
def wav_info(path: Path | str) -> tuple[int, int, int, int]:
    """返回 (声道数, 采样位宽字节, 采样率, 帧数)。"""
    with wave.open(str(path), "rb") as reader:
        return reader.getnchannels(), reader.getsampwidth(), reader.getframerate(), reader.getnframes()


def wav_duration(path: Path | str) -> float:
    channels, width, rate, frames = wav_info(path)
    return frames / float(rate)


def silence_pcm(sample_rate: int, ms: int, channels: int = 1, sampwidth: int = 2) -> bytes:
    frames = int(sample_rate * ms / 1000.0)
    return b"\x00" * (frames * channels * sampwidth)


def concat_wav_pcm(
    parts: Sequence[Path],
    out_path: Path,
    gap_ms: int = 0,
    trailing_gap: bool = True,
    gaps_ms: Sequence[int] | None = None,
) -> list[dict[str, float]]:
    """把若干 WAV 片段按顺序拼成一个 WAV，返回每段的精确时间轴。

    直接操作 PCM 样本，所以时间轴是精确的（这也是字幕能天然对齐口型的原因）。

    `gaps_ms` 允许给每个片段指定各自的停顿 —— 这是「气口」的关键：
    逗号后停 180ms、句号后停 420ms、段落之间停 650ms，听起来才像人在说话，
    而不是机器均匀地念。不传则退化为统一的 `gap_ms`。
    """
    if not parts:
        raise ValueError("concat_wav_pcm: parts 为空")

    # 只比较「格式」三元组：声道/位宽/采样率。
    # 注意不能把帧数也算进去 —— 每段帧数天然不同，那样永远判定为格式不一致。
    params = {wav_info(p)[:3] for p in parts}
    if len(params) != 1:
        detail = "\n".join(
            f"  {p.name}: ch={i[0]} width={i[1]} rate={i[2]}"
            for p, i in ((p, wav_info(p)) for p in parts)
        )
        raise ValueError(
            "待拼接的 WAV 格式不一致，无法直接拼 PCM。\n"
            f"各片段参数：\n{detail}\n"
            "请让 TTS 输出统一格式（推荐 24000Hz / 16bit / 单声道）。"
        )
    channels, sampwidth, rate = params.pop()

    if gaps_ms is None:
        per_gap = [gap_ms] * len(parts)
    else:
        per_gap = list(gaps_ms) + [gap_ms] * max(0, len(parts) - len(gaps_ms))
    if len(per_gap) < len(parts):
        per_gap += [gap_ms] * (len(parts) - len(per_gap))

    timeline: list[dict[str, float]] = []
    cursor = 0.0

    with wave.open(str(out_path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sampwidth)
        writer.setframerate(rate)

        for index, part in enumerate(parts):
            with wave.open(str(part), "rb") as reader:
                data = reader.readframes(reader.getnframes())
                frames = reader.getnframes()
            duration = frames / float(rate)

            this_gap = max(0, int(per_gap[index]))
            # 最后一段也补停顿，这样视频轨与音频轨等长（避免结尾被切掉几帧）
            pad = (silence_pcm(rate, this_gap, channels, sampwidth)
                   if (trailing_gap or index < len(parts) - 1) and this_gap > 0 else b"")
            writer.writeframes(data)
            if pad:
                writer.writeframes(pad)

            clip_duration = duration + (this_gap / 1000.0 if pad else 0.0)
            timeline.append(
                {
                    "start": round(cursor, 4),
                    "end": round(cursor + duration, 4),
                    "duration": round(duration, 4),
                    "pause_ms": this_gap,
                    "clip_duration": round(clip_duration, 4),
                }
            )
            cursor += clip_duration

    return timeline


def build_multipart(
    fields: dict[str, Any], file_field: str, filename: str,
    file_bytes: bytes, file_ctype: str = "application/octet-stream",
) -> tuple[bytes, str]:
    """手搓 multipart/form-data。

    标准库没有现成的 multipart 构造器，而云厂商的克隆接口、ComfyUI 的
    /upload/image 都是表单上传。自己拼一下只要十几行，比被迫引依赖划算。
    """
    boundary = f"----AutoVidBoundary{random.getrandbits(64):016x}"
    chunks: list[bytes] = []
    for name, value in (fields or {}).items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )
    chunks.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
        f'filename="{filename}"\r\nContent-Type: {file_ctype}\r\n\r\n'.encode("utf-8")
    )
    chunks.append(file_bytes)
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


# --------------------------------------------------------------------------- #
# 断句与气口
#
# 真实说话不是均匀的：逗号后轻顿，句号后换气，段落之间停得久一点。
# 而且一口气说不完 40 个字 —— 不分句地丢给 TTS，它会越读越快、越读越糊。
# 所以先把文本切成「一口气能说完」的小句，再按标点级别给不同长度的停顿。
# --------------------------------------------------------------------------- #
PAUSE_LEVELS: dict[str, str] = {
    "comma": "，,、",          # 轻顿
    "clause": "；;：:",        # 半句
    "sentence": "。！？!?…",   # 换气
}
_CLOSERS = "”’』」）)》】\"'"

# 停顿基准（毫秒）。句末 > 半句 > 逗号，这是中文口播比较自然的比例。
DEFAULT_PAUSES = {
    "comma": 180,
    "clause": 300,
    "sentence": 430,
    "none": 120,        # 硬切出来的碎句（原本没有标点）
    "segment": 650,     # 段落之间
}


def _punct_class(char: str) -> str | None:
    for name, chars in PAUSE_LEVELS.items():
        if char in chars:
            return name
    return None


def _hard_cut(head: str, max_chars: int) -> int:
    """长句硬切的位置：尽量别从词中间切。

    原来直接切 head[:max_chars] —— 中文没有空格，一个 20 字的窗口
    可能正好卡在词中间，把「入门」切成了「入|门」（实测踩过：
    「欢迎来到本期 LangChain 快速入|门」）。
    现在：
      1. 优先取窗口内**最后一个空格**（保住 LangChain 这类英文/数字词）；
      2. 没有空格就在常见连接词 / 语气词后面切（粗粒度，总比拆词强）；
      3. 都找不到才退回字符边界。
    """
    window = head[:max_chars]
    space = window.rfind(" ")
    if space > 0:
        return space
    # 中文里的弱边界：在这些字**之后**切，基本不会影响语义
    for sep in "的了是和在就要我来也出把被吧吗呢又着过什":
        idx = window.rfind(sep)
        if idx > 0:
            return idx + 1
    # 都找不到，退回等长硬切（至少保证进度）
    return max(1, max_chars)


def split_into_breaths(text: str, max_chars: int = 20) -> list[tuple[str, str]]:
    """把一段口播拆成「气口单位」，返回 [(文本, 结束标点级别)]。

    规则（顺序很重要）：
      1. 按标点切成小句，标点跟着前一小句走；
      2. 单句超过一口气的长度就硬切，硬切出来的碎片没有标点；
      3. **只在标点处断句，不跨越标点合并** ——
         否则逗号处的气口就丢了，而那正是「气口」的意义。
         只有硬切出来的无标点碎片才会往后并。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    max_chars = max(6, int(max_chars))

    clauses: list[str] = []
    current = ""
    for char in cleaned:
        current += char
        if _punct_class(char):
            clauses.append(current)
            current = ""
    if current.strip():
        clauses.append(current)

    # 每一片记录「自己是否带结束标点」——带标点的必须独占一组，停顿才不会丢
    pieces: list[tuple[str, bool]] = []
    for clause in clauses:
        stripped = clause.rstrip()
        has_punct = bool(stripped) and _punct_class(stripped[-1]) is not None
        head = clause
        while len(head) > max_chars:
            cut = _hard_cut(head, max_chars)
            pieces.append((head[:cut], False))
            head = head[cut:]
        if head:
            pieces.append((head, has_punct))

    groups: list[str] = []
    buffer = ""
    for text_piece, has_punct in pieces:
        # 合并前先看会不会超长。硬切出来的碎片各自都 <= max_chars，但两片一合
        # 就可能超（实测 10 + 14 = 24 字一条字幕）。超了就先结算手里的，
        # 不能等合完再判断 —— 那时候已经晚了。
        if buffer and len(buffer) + len(text_piece) > max_chars:
            if buffer.strip():
                groups.append(buffer)
            buffer = ""
        buffer += text_piece
        if has_punct or len(buffer) >= max_chars:
            if buffer.strip():
                groups.append(buffer)
            buffer = ""
    if buffer.strip():
        groups.append(buffer)

    result: list[tuple[str, str]] = []
    for group in groups:
        stripped = group.strip()
        if not stripped:
            continue
        index = len(stripped) - 1
        while index >= 0 and stripped[index] in _CLOSERS:   # 跳过右引号/右括号
            index -= 1
        ending = _punct_class(stripped[index]) if index >= 0 else None
        result.append((stripped, ending or "none"))
    return result


def pause_for_punctuation(
    ending: str,
    cfg: dict[str, Any] | None = None,
    rng: Any = None,
) -> int:
    """按标点级别给出停顿毫秒数，带一点随机抖动避免机械感。"""
    cfg = cfg or {}
    base = int(cfg.get(f"pause_{ending}_ms", DEFAULT_PAUSES.get(ending, 120)))
    jitter = int(cfg.get("pause_jitter_ms", 40))
    if jitter > 0 and rng is not None:
        base += rng.randint(-jitter, jitter)
    return max(0, base)


# --------------------------------------------------------------------------- #
# Windows SAPI 语音合成（离线、零依赖）
# --------------------------------------------------------------------------- #
def powershell_exe() -> str:
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    raise AutoVidError("[media] 找不到 PowerShell，无法使用 SAPI 语音合成")


def list_sapi_voices() -> list[dict[str, str]]:
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.GetInstalledVoices() | ForEach-Object { "
        "'{0}|{1}' -f $_.VoiceInfo.Name, $_.VoiceInfo.Culture };"
        "$s.Dispose()"
    )
    proc = subprocess.run(
        [powershell_exe(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
    )
    voices: list[dict[str, str]] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if "|" in line:
            name, _, culture = line.partition("|")
            voices.append({"name": name.strip(), "culture": culture.strip()})
    return voices


def sapi_synthesize(
    jobs: Sequence[tuple[Path, str]],
    voice: str = "",
    rate: int = 0,
    volume: int = 100,
    sample_rate: int = 24000,
    log=print,
) -> None:
    """用 Windows SAPI 批量合成语音。

    文本通过 UTF-8 的 JSON 文件传给 PowerShell，脚本本体保持纯 ASCII ——
    这样彻底避开 PowerShell 的编码与引号问题（中文文本不会被搞坏）。
    """
    if not jobs:
        return
    work_dir = Path(jobs[0][0]).parent
    work_dir.mkdir(parents=True, exist_ok=True)

    json_path = work_dir / "_sapi_jobs.json"
    json_path.write_text(
        json.dumps(
            [{"path": str(p.resolve()), "text": t} for p, t in jobs],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ps1_path = work_dir / "_sapi_run.ps1"
    ps1_path.write_text(
        "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                "Add-Type -AssemblyName System.Speech",
                "$jobsPath = $args[0]",
                f"$voiceName = $args[1]",
                f"$rate = {int(rate)}",
                f"$volume = {int(volume)}",
                f"$sampleRate = {int(sample_rate)}",
                "$raw = [System.IO.File]::ReadAllText($jobsPath, [System.Text.Encoding]::UTF8)",
                "$jobs = $raw | ConvertFrom-Json",
                "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer",
                "if ($voiceName -and $voiceName.Trim().Length -gt 0) {",
                "  try { $synth.SelectVoice($voiceName) }",
                "  catch { Write-Warning ('voice not found, using default: ' + $voiceName) }",
                "}",
                "$synth.Rate = $rate",
                "$synth.Volume = $volume",
                "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(",
                "  $sampleRate,",
                "  [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,",
                "  [System.Speech.AudioFormat.AudioChannel]::Mono)",
                "$synth.SetOutputToNull()",
                "foreach ($job in $jobs) {",
                "  $synth.SetOutputToWaveFile($job.path, $fmt)",
                "  $synth.Speak($job.text)",
                "  $synth.SetOutputToNull()",
                "}",
                "$synth.Dispose()",
                "Write-Output ('synthesized ' + $jobs.Count + ' segments')",
                "",
            ]
        ),
        encoding="utf-8",
    )

    cmd = [
        powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(ps1_path), str(json_path), voice or "",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800,
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-10:])
        raise RuntimeError(f"SAPI 语音合成失败 (exit {proc.returncode})\n{tail}")

    missing = [str(p) for p, _ in jobs if not Path(p).exists()]
    if missing:
        raise RuntimeError(
            "SAPI 没有产出以下文件（可能是文本为空或语音包缺失）：\n  " + "\n  ".join(missing)
        )
    log(f"SAPI 合成完成：{len(jobs)} 段，voice={voice or '(默认)'}")


# --------------------------------------------------------------------------- #
# 离线素材生成（渐变背景 / 封面）
# --------------------------------------------------------------------------- #
def make_gradient(
    out: Path,
    width: int,
    height: int,
    colors: Sequence[str],
    seed: int = 0,
    cwd: Path | str | None = None,
) -> Path:
    """生成一张渐变背景图（纯 ffmpeg lavfi，不需要任何模型）。"""
    padded = list(colors) + [colors[-1]] * max(0, 3 - len(colors))
    spec = (
        f"gradients=s={width}x{height}"
        f":c0={padded[0]}:c1={padded[1]}:c2={padded[2]}"
        f":n=3:seed={seed}:d=1"
    )
    run_ffmpeg(
        ["-f", "lavfi", "-i", spec, "-frames:v", "1", "-q:v", "2", str(out)],
        cwd=cwd, desc=f"生成渐变背景 {out.name}",
    )
    return out


def kenburns_clip(
    image: Path,
    out: Path,
    duration: float,
    width: int,
    height: int,
    fps: int = 30,
    zoom: float = 0.12,
    cwd: Path | str | None = None,
    crf: int = 20,
    preset: str = "veryfast",
) -> Path:
    """把一张静图变成带缓慢推镜的视频片段（模拟数字人镜头的呼吸感）。"""
    duration = max(duration, 1.0 / fps)
    frames = max(1, int(round(duration * fps)))
    zmax = 1.0 + max(zoom, 0.0)
    # 关键点：z 表达式里坚决不用 min(a,b)——逗号是 filtergraph 的分隔符，
    # 转义与否在不同 ffmpeg 版本上行为不一致。这里让 rate 在最后一帧刚好到 zmax，
    # 于是根本不需要截断，表达式里一个逗号都没有。
    rate = (zmax - 1.0) / max(1, frames - 1)
    vf = (
        f"scale={width * 2}:{height * 2}:flags=lanczos,"
        f"zoompan=z='1+{rate:.10f}*on'"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d=1:s={width}x{height}:fps={fps},"
        f"format=yuv420p"
    )
    run_ffmpeg(
        [
            "-loop", "1", "-i", str(image),
            "-vf", vf,
            "-frames:v", str(frames),
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-an",
            str(out),
        ],
        cwd=cwd, desc=f"生成镜头片段 {out.name}",
    )
    return out


def person_over_background(
    background: Path,
    portrait: Path,
    out: Path,
    duration: float,
    width: int,
    height: int,
    fps: int = 30,
    zoom: float = 0.10,
    person_width_ratio: float = 0.62,
    bottom_ratio: float = 0.30,
    cwd: Path | str | None = None,
    crf: int = 20,
) -> Path:
    """把人物照片合成到背景之上，做成一个「口播镜头」。

    这是接入真人数字人之前的可用形态：背景做缓慢推镜，人物用**用户自己的照片**。
    换成会动嘴的数字人后，只要把这张静图换成人物片段，本函数的合成思路不变。
    """
    duration = max(duration, 1.0 / fps)
    frames = max(1, int(round(duration * fps)))
    zmax = 1.0 + max(zoom, 0.0)
    rate = (zmax - 1.0) / max(1, frames - 1)      # 最后一帧刚好到 zmax，表达式里无需逗号
    person_w = max(2, int(width * person_width_ratio))

    # 背景可能是图片也可能是视频，-loop 1 只对图片解复用器有效
    bg_is_image = Path(background).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
    inputs: list[str] = (["-loop", "1"] if bg_is_image else []) + ["-i", str(background)]
    inputs += ["-loop", "1", "-i", str(portrait)]

    filter_complex = (
        f"[0:v]scale={width * 2}:{height * 2}:flags=lanczos,"
        f"zoompan=z='1+{rate:.10f}*on':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d=1:s={width}x{height}:fps={fps}[bg];"
        f"[1:v]scale={person_w}:-2:flags=lanczos[ps];"
        f"[bg][ps]overlay=x=(W-w)/2:y=H-h-H*{bottom_ratio:.4f}[v]"
    )
    run_ffmpeg(
        inputs + [
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-frames:v", str(frames),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-an",
            str(out),
        ],
        cwd=cwd, desc=f"合成人物镜头 {out.name}",
    )
    return out


def concat_clips(
    clips: Sequence[Path],
    out: Path,
    cwd: Path | str | None = None,
    reencode: bool = False,
    fps: int = 30,
) -> Path:
    """按顺序拼接视频片段。同参数时走流拷贝（快）。"""
    if not clips:
        raise ValueError("concat_clips: clips 为空")
    work_dir = Path(cwd) if cwd else out.parent
    list_path = work_dir / "_concat_list.txt"
    # 用绝对 POSIX 路径：片段可能不在 cwd 下，相对路径会解析失败；
    # 反斜杠会被 concat 解复用器当成转义字符，所以统一转成正斜杠。
    list_path.write_text(
        "\n".join(f"file '{Path(c).resolve().as_posix()}'" for c in clips) + "\n",
        encoding="utf-8",
    )

    if reencode:
        args = [
            "-f", "concat", "-safe", "0", "-i", list_path.name,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", str(fps), "-an", str(out),
        ]
    else:
        args = [
            "-f", "concat", "-safe", "0", "-i", list_path.name,
            "-c", "copy", "-an", str(out),
        ]
    run_ffmpeg(args, cwd=work_dir, desc=f"拼接 {len(clips)} 个片段 -> {out.name}")
    return out


def mux_audio(
    video: Path,
    audio: Path,
    out: Path,
    cwd: Path | str | None = None,
    audio_bitrate: str = "192k",
) -> Path:
    """给视频轨接上音频（音频略短时用 apad 补齐，避免最后几帧被切掉）。"""
    run_ffmpeg(
        [
            "-i", str(video), "-i", str(audio),
            "-filter_complex", "[1:a]apad[a]",
            "-map", "0:v:0", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", audio_bitrate,
            "-shortest", str(out),
        ],
        cwd=cwd, desc=f"合并音视频 -> {out.name}",
    )
    return out


def burn_subtitles(
    video: Path,
    ass_file: Path,
    out: Path,
    cwd: Path | str | None = None,
    crf: int = 20,
    preset: str = "medium",
) -> Path:
    """把 ASS 字幕烧进视频（libass）。ASS 会被复制到 cwd，避免路径转义问题。"""
    ass_name = localize_for_filter(ass_file, cwd)
    run_ffmpeg(
        [
            "-i", str(video),
            "-vf", f"ass={ass_name}",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-c:a", "copy",
            str(out),
        ],
        cwd=cwd, desc=f"烧录字幕 -> {out.name}",
    )
    return out


def render_card(
    background: Path,
    ass_file: Path,
    out: Path,
    width: int,
    height: int,
    cwd: Path | str | None = None,
) -> Path:
    """用一张背景图 + 一份 ASS 渲染出封面图（复用 libass，中文无忧）。

    输入既可以是图片也可以是视频（视频取首帧）。`-loop 1` 只对图片解复用器
    有效，对 mp4 会报 "Option loop not found"，所以这里要按类型区分。
    """
    ass_name = localize_for_filter(ass_file, cwd)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},ass={ass_name}"
    )
    is_image = Path(background).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
    args = (["-loop", "1"] if is_image else []) + [
        "-i", str(background), "-vf", vf, "-frames:v", "1", "-q:v", "3", str(out),
    ]
    run_ffmpeg(args, cwd=cwd, desc=f"渲染封面 {out.name}")
    return out


def extract_frame(video: Path, out: Path, at_s: float = 1.0) -> Path:
    run_ffmpeg(["-ss", f"{at_s:.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "3", str(out)],
               desc=f"抽帧 -> {out.name}")
    return out


# --------------------------------------------------------------------------- #
# ASS 字幕
# --------------------------------------------------------------------------- #
ASS_STYLE_FORMAT = (
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
    "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
    "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
)


def ass_time(seconds: float) -> str:
    """ASS 时间格式 H:MM:SS.cc"""
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def ass_escape(text: str) -> str:
    """转义 ASS 文本：花括号会被当成特效标签。"""
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


LINE_BREAK = "\x00"  # 占位符：先排版，再整体转义，最后把占位符换成 ASS 的 \N


def wrap_text(text: str, max_chars: int) -> str:
    """中文没有空格，按字数硬换行，用占位符标记断点（避免与 ASS 转义打架）。"""
    text = text.strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    lines: list[str] = []
    # 优先在标点处断开，读起来更自然
    breaks = "，。！？；：、,.!?;: "
    current = ""
    for char in text:
        current += char
        if len(current) >= max_chars:
            pivot = -1
            for i in range(len(current) - 1, max(0, len(current) - max_chars // 2), -1):
                if current[i] in breaks:
                    pivot = i + 1
                    break
            if pivot <= 0:
                pivot = len(current)
            lines.append(current[:pivot].strip())
            current = current[pivot:]
    if current.strip():
        lines.append(current.strip())
    # 避免出现只有标点的孤儿行（"。" 单独占一行很难看）
    while len(lines) > 1 and lines[-1] and all(ch in breaks for ch in lines[-1]):
        lines[-2] = lines[-2] + lines[-1]
        lines.pop()
    return LINE_BREAK.join(lines)


def make_style(
    name: str,
    font: str,
    size: int,
    primary: str = "&H00FFFFFF",
    outline_colour: str = "&H00202020",
    back_colour: str = "&H80000000",
    bold: bool = True,
    outline: int = 5,
    shadow: int = 2,
    alignment: int = 2,
    margin_l: int = 70,
    margin_r: int = 70,
    margin_v: int = 360,
    border_style: int = 1,
) -> str:
    return (
        f"Style: {name},{font},{size},{primary},&H000000FF,{outline_colour},{back_colour},"
        f"{-1 if bold else 0},0,0,0,100,100,0,0,{border_style},{outline},{shadow},"
        f"{alignment},{margin_l},{margin_r},{margin_v},134"
    )


def build_ass(
    styles: Sequence[str],
    events: Sequence[dict[str, Any]],
    width: int,
    height: int,
    title: str = "AutoVid",
) -> str:
    header = [
        "[Script Info]",
        f"Title: {title}",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        ASS_STYLE_FORMAT,
        *styles,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    body = [
        "Dialogue: 0,{start},{end},{style},,0,0,0,,{text}".format(
            start=ass_time(float(ev["start"])),
            end=ass_time(float(ev["end"])),
            style=ev.get("style", "Main"),
            text=render_ass_text(str(ev["text"]), int(ev.get("max_chars", 0))),
        )
        for ev in events
    ]
    return "\n".join(header + body) + "\n"


def render_ass_text(text: str, max_chars: int = 0) -> str:
    """把原始文本渲染成可安全放进 Dialogue 的 ASS 文本。

    顺序很重要：先排版（插入占位符）-> 再转义 -> 最后把占位符换成 \\N。
    反过来做的话，转义会把 \\N 里的反斜杠也转义掉，断行就失效了。
    """
    wrapped = wrap_text(text, max_chars) if max_chars > 0 else text.strip()
    escaped = ass_escape(wrapped)
    return escaped.replace(LINE_BREAK, "\\N")


def write_ass(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path
