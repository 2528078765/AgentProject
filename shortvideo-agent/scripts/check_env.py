"""环境自检：把「能不能跑」这件事一次性查清楚。

    python scripts/check_env.py

每一项都给出明确的结论和修复建议，不确定的地方会真的去试一次而不是猜。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = "[通过]"
WARN = "[注意]"
FAIL = "[失败]"

problems: list[str] = []


def title(text: str) -> None:
    print(f"\n{text}")
    print("-" * 64)


def main() -> int:
    print("AutoVid 环境自检")
    print("=" * 64)

    # ---------------------------------------------------------------- Python
    title("1. Python")
    version = sys.version_info
    print(f"  解释器: {sys.executable}")
    print(f"  版本  : {sys.version.split()[0]}")
    if version >= (3, 10):
        print(f"  {PASS} 版本满足要求（>= 3.10）")
    else:
        print(f"  {FAIL} 需要 Python 3.10 及以上")
        problems.append("升级 Python 到 3.10+")

    # ---------------------------------------------------------------- FFmpeg
    title("2. FFmpeg / FFprobe")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        print(f"  {FAIL} 没有找到 ffmpeg/ffprobe")
        print("    修复： winget install Gyan.FFmpeg   （装完重开终端）")
        problems.append("安装 FFmpeg 并加入 PATH")
    else:
        print(f"  ffmpeg : {ffmpeg}")
        print(f"  ffprobe: {ffprobe}")
        proc = subprocess.run([ffmpeg, "-hide_banner", "-version"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(f"  版本   : {(proc.stdout or '').splitlines()[0] if proc.stdout else '未知'}")
        filters = subprocess.run([ffmpeg, "-hide_banner", "-filters"],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace").stdout or ""
        required = {"ass": "ASS 字幕烧录（libass）", "zoompan": "静图推镜",
                    "gradients": "离线渐变背景", "subtitles": "字幕滤镜"}
        for name, why in required.items():
            if f" {name} " in filters or f" {name}\n" in filters:
                print(f"  {PASS} 滤镜 {name:<10} {why}")
            else:
                print(f"  {FAIL} 缺少滤镜 {name}（{why}）")
                problems.append(f"当前 ffmpeg 缺少 {name} 滤镜，建议换 full build")

    # ---------------------------------------------------------------- 字体
    title("3. 中文字体")
    fonts = ["msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc", "Deng.ttf"]
    found = [f for f in fonts if (Path("C:/Windows/Fonts") / f).exists()]
    if found:
        print(f"  {PASS} 找到 {len(found)} 个中文字体：{', '.join(found)}")
        print("       字幕用 Microsoft YaHei（msyh.ttc），渲染中文没问题")
    else:
        print(f"  {WARN} 没找到常见中文字体，字幕可能显示成方块")
        problems.append("安装中文字体（控制面板 -> 语言 -> 中文）")

    # ---------------------------------------------------------------- 语音
    title("4. 语音合成（TTS）")
    try:
        from autovid import media as M
        voices = M.list_sapi_voices()
        zh = [v for v in voices if v["culture"].lower().startswith("zh")]
        print(f"  系统语音列表: {', '.join(v['name'] + '/' + v['culture'] for v in voices) or '（空）'}")
        if not voices:
            print(f"  {FAIL} 没有安装任何语音包")
            problems.append("安装中文语音包")
        else:
            # 关键：光看列表不算数，必须真的合成一次才知道能不能用。
            # 探针文件放在项目内的 .tmp 下 —— 系统临时目录在受限环境里可能不可写，
            # 那样探针会先自己失败，得出错误结论。
            probe_dir = ROOT / ".tmp" / "sapi_probe"
            probe_dir.mkdir(parents=True, exist_ok=True)
            probe = probe_dir / "probe.wav"
            try:
                M.sapi_synthesize([(probe, "语音合成测试。")],
                                  voice=(zh[0]["name"] if zh else voices[0]["name"]),
                                  sample_rate=24000, log=lambda _m: None)
                size = probe.stat().st_size
                print(f"  {PASS} SAPI 实际合成成功（{size} 字节）—— 本机可以用真实语音")
            except Exception as exc:  # noqa: BLE001
                print(f"  {WARN} SAPI 列表有语音，但实际合成被拒绝：")
                print(f"        {str(exc).splitlines()[0][:120]}")
                print("        这在受限/非交互会话（如某些 CI、沙箱、服务账户）下很常见。")
                print("        请在**自己的终端窗口**里直接运行本脚本复测。")
                print("        若仍失败，改用 edge-tts 或云端 TTS：")
                print("          pip install edge-tts   然后设 steps.voice.provider = \"edge\"")
                problems.append("TTS 需要确认：安装 edge-tts 或配置云端 TTS")
    except Exception as exc:  # noqa: BLE001
        print(f"  {FAIL} 检测 TTS 时出错：{exc}")
        problems.append("TTS 检测异常")

    # ---------------------------------------------------------------- 可选依赖
    title("5. 可选 Python 包")
    for module, why, fix in (
        ("edge_tts", "免费中文 TTS（推荐）", "pip install edge-tts"),
    ):
        try:
            __import__(module)
            print(f"  {PASS} {module:<12} {why}")
        except Exception:  # noqa: BLE001
            print(f"  {WARN} {module:<12} 未安装（{why}）-> {fix}")

    # ---------------------------------------------------------------- 显卡
    title("6. 显卡提示（本机不需要 GPU）")
    print("  本流水线离线模式不需要 GPU：出图用 ffmpeg、数字人用静图推镜。")
    print("  要接真实模型时注意：")
    print("    - NVIDIA + CUDA：HeyGem / MuseTalk / FLUX / GPT-SoVITS 都能本地跑")
    print("    - AMD 显卡 + Windows：主流开源模型基本不可用（缺 ROCm，DirectML 带不动），")
    print("      建议数字人和出图走云 API，本地只做编排、字幕和合成")

    # ---------------------------------------------------------------- 结论
    title("结论")
    if not problems:
        print(f"  {PASS} 环境就绪，可以直接跑：")
        print("       python -m autovid run --topic \"你的选题\"")
        return 0
    print(f"  发现 {len(problems)} 项需要处理：")
    for item in problems:
        print(f"    - {item}")
    print("\n  即使不处理，也可以用离线兜底跑通全链（语音为静音占位）：")
    print("       python -m autovid run --topic \"你的选题\"")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
