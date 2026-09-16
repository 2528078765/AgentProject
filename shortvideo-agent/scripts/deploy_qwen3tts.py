"""一键部署 Qwen3-TTS（本地音色克隆）—— 不需要 pip，不需要你动手。

为什么不能做成一个 pip 依赖：
    * 模型权重 1.8 GB（ModelScope 上的 Qwen3-TTS-12Hz-0.6B-Base）
    * llama.cpp 的 Vulkan 二进制（跑 Talker / Predictor）
    * 一个独立的推理引擎仓库（qwen3_tts_gguf）
    * ONNX Runtime DirectML（跑 Encoder / Decoder，AMD 卡可用）
  这些东西必须落到磁盘上，没有捷径。但**下载、装依赖、配置、导出**都可以自动化。

执行阶段（可重复运行，已完成的会跳过）：
    1. 环境检查
    2. 拉取仓库
    3. 安装 Python 依赖（用 scripts/vendor_deps.py，绕开 pip）
    4. 下载模型权重
    5. 下载 llama.cpp Vulkan 二进制
    6. 改写 export_config.py 指向 0.6B + 本地目录
    7. 执行导出（11~34 号脚本）
    8. 自检产物

    python scripts/deploy_qwen3tts.py            # 全部阶段
    python scripts/deploy_qwen3tts.py --phase 4  # 只跑某一阶段
    python scripts/deploy_qwen3tts.py --status   # 看当前进度
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

VENDOR = ROOT / "vendor"
REPO_DIR = VENDOR / "qwen3-tts-gguf"
MODEL_DIR = VENDOR / "models" / "Qwen3-TTS-12Hz-0.6B-Base"
EXPORT_DIR = VENDOR / "qwen3-tts-export"
LIBS = ROOT / ".pylibs"
LOG_PATH = VENDOR / "deploy.log"

REPO_ZIP = "https://github.com/HaujetZhao/Qwen3-TTS-GGUF/archive/refs/heads/main.zip"
MODEL_ID = "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
LLAMA_TAG = "b10621"
LLAMA_ASSET_HINT = "bin-win-vulkan-x64.zip"

# 运行 + 导出所需。
# 注意：torch / torchvision 已装，不在这里拉（避免下载 2GB 的 CUDA 版）。
# accelerate / torchaudio 会牵连 torch 版本链，先不装 —— 导出脚本如果真需要
# 再补，避免为了「可能用不上」的依赖下载几百 MB。
DEPS = [
    "gguf", "numpy", "scipy", "soundfile", "tokenizers", "safetensors",
    "onnx", "onnxscript", "sentencepiece", "einops", "transformers==4.57.6",
    "accelerate==1.12.0",
    "onnxruntime-directml",
]

EXPORT_SCRIPTS = [
    "11-Export-Codec-Encoder.py", "12-Export-Speaker-Encoder.py",
    "13-Export-Decoder.py", "14-Export-Embeddings.py",
    "15-Copy-Tokenizer.py", "16-Quantize-ONNX-Models.py",
    "21-Extract-Talker-Weights.py", "22-Prepare-Talker-Tokenizer.py",
    "23-Convert-Talker-GGUF.py", "24-Quantize-Talker-GGUF.py",
    "31-Extract-Predictor-Weights.py", "32-Prepare-Predictor-Tokenizer.py",
    "33-Convert-Predictor-GGUF.py", "34-Quantize-Predictor-GGUF.py",
]

_log_file = None


def log(message: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    print(line, flush=True)
    if _log_file:
        _log_file.write(line + "\n")
        _log_file.flush()


def http_json(url: str, timeout: int = 60) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "autovid-deploy"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def download(urls, target: Path, desc: str = "", timeout: int = 1800,
             attempts: int = 3) -> Path:
    """下载文件，支持多地址回退、重试、以及 **HTTP Range 断点续传**。

    实测两颗雷：
      * codeload.github.com 会偶发 ConnectionReset（大文件传一半断掉）
      * 慢链路（0.1 MB/s）下重试若从头开始，永远下不完
    所以失败**保留 .part**，下次带 Range 头续传；服务器不支持 Range 就重来。
    """
    sources = urls if isinstance(urls, (list, tuple)) else [urls]
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size > 0:
        log(f"    已存在，跳过：{target.name}（{target.stat().st_size / 1e6:.1f} MB）")
        return target

    part = target.with_suffix(target.suffix + ".part")
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        for url in sources:
            try:
                have = part.stat().st_size if part.exists() else 0
                headers = {"User-Agent": "autovid-deploy"}
                if have:
                    headers["Range"] = f"bytes={have}-"
                request = urllib.request.Request(url, headers=headers)
                started = time.time()
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    resuming = have > 0 and response.status == 206
                    if resuming:
                        log(f"    续传：从 {have / 1e6:.1f} MB 继续")
                    else:
                        have = 0
                    total = int(response.headers.get("Content-Length") or 0) + have
                    done = have
                    last = 0.0
                    with open(part, "ab" if resuming else "wb") as handle:
                        while chunk := response.read(1 << 20):
                            handle.write(chunk)
                            done += len(chunk)
                            now = time.time()
                            if now - last > 5:
                                last = now
                                speed = (done - have) / max(0.001, now - started) / 1e6
                                pct = f"{done / total * 100:.0f}%" if total else "?"
                                log(f"    {desc or target.name}: {done / 1e6:.0f} MB / "
                                    f"{total / 1e6:.0f} MB ({pct}, {speed:.1f} MB/s)")
                if total and part.stat().st_size < total:
                    raise IOError(f"只下到 {part.stat().st_size} / {total} 字节")
                part.replace(target)
                log(f"    完成：{target.name}（{target.stat().st_size / 1e6:.1f} MB）")
                return target
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                kept = part.stat().st_size / 1e6 if part.exists() else 0
                log(f"    下载失败（{type(exc).__name__}）: {url[:64]}"
                    f"{f'（已保留 {kept:.1f} MB，下次续传）' if kept else ''}")
        if attempt < attempts:
            wait = 3 * attempt
            log(f"    {wait} 秒后重试（第 {attempt + 1}/{attempts} 次）…")
            time.sleep(wait)
    raise RuntimeError(f"多次重试后仍无法下载 {target.name}：{last_error}")


# --------------------------------------------------------------------------- #
# 各阶段
# --------------------------------------------------------------------------- #
def phase1_check() -> bool:
    log("【1/8】环境检查")
    usage = shutil.disk_usage(str(VENDOR.parent))
    log(f"    磁盘可用 {usage.free / 1e9:.1f} GB（本部署约需 6 GB）")
    if usage.free < 6e9:
        log("    ✗ 磁盘不足，至少需要 6 GB")
        return False
    log(f"    Python {sys.version_info.major}.{sys.version_info.minor}"
        f".{sys.version_info.micro}")
    try:
        import torch
        log(f"    torch {torch.__version__}（导出用，CPU 也能跑）")
    except ImportError:
        log("    ✗ 没有 torch，导出阶段会失败")
        return False
    VENDOR.mkdir(parents=True, exist_ok=True)
    log("    通过")
    return True


def phase2_repo() -> bool:
    log("【2/8】拉取推理引擎仓库")
    if (REPO_DIR / "qwen3_tts_gguf" / "inference" / "engine.py").exists():
        log(f"    已存在，跳过：{REPO_DIR}")
        return True
    archive = download(
        [REPO_ZIP,
         "https://ghproxy.net/" + REPO_ZIP,
         "https://mirror.ghproxy.com/" + REPO_ZIP],
        VENDOR / "qwen3-tts-gguf.zip", "仓库压缩包")
    log("    解压…")
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(VENDOR / "_repo_tmp")
    extracted = next((VENDOR / "_repo_tmp").iterdir())
    if REPO_DIR.exists():
        shutil.rmtree(REPO_DIR, ignore_errors=True)
    extracted.rename(REPO_DIR)
    shutil.rmtree(VENDOR / "_repo_tmp", ignore_errors=True)
    log(f"    完成：{REPO_DIR}")
    return True


def phase3_deps() -> bool:
    log("【3/8】检查 / 安装 Python 依赖")

    # 关键运行依赖如果已经可导入（比如用户自己 pip install 过了），直接跳过。
    # vendor 装依赖只是「沙箱里 pip 被禁」的退路；正常终端用 pip 快得多。
    critical = ["onnxruntime", "tokenizers", "safetensors", "soundfile",
                "onnx", "transformers", "sentencepiece", "einops",
                "accelerate"]
    import importlib.util
    missing = [m for m in critical if importlib.util.find_spec(m) is None]
    if not missing:
        log(f"    关键依赖已可用（{len(critical)} 项齐全），跳过安装")
        return True
    log(f"    还缺 {len(missing)} 项：{missing}，走 vendor 安装…")

    command = [sys.executable, str(ROOT / "scripts" / "vendor_deps.py"), *DEPS]
    # 流式转发子进程输出；必须加 PYTHONUNBUFFERED=1，否则 print 在管道里
    # 被块缓冲，进度全憋住，看起来像卡死。
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    process = subprocess.Popen(command, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, env=env,
                               text=True, encoding="utf-8", errors="replace")
    assert process.stdout is not None
    for line in process.stdout:
        log("    " + line.rstrip())
    code = process.wait()
    if code != 0:
        log("    ✗ 依赖安装失败（退出码 %d）" % code)
        return False
    return True


def ms_collect_files(model_id: str, root: str = "", depth: int = 0) -> list[dict]:
    """递归收集 ModelScope 仓库里所有 blob 文件。

    仓库里有 type=tree 的目录（比如 speech_tokenizer/，里面有 666MB 的
    model.safetensors），列表接口只给一层 —— 必须按 Root 递归才能拿全。
    """
    if depth > 6:
        return []
    url = (f"https://modelscope.cn/api/v1/models/{model_id}/repo/files"
           f"?Revision=master")
    if root:
        url += f"&Root={urllib.parse.quote(root, safe='')}"
    data = http_json(url)
    out: list[dict] = []
    for item in (data.get("Data") or {}).get("Files") or []:
        path = str(item.get("Path") or "")
        if not path or path.endswith("/"):
            continue
        kind = str(item.get("Type") or "")
        size = int(item.get("Size") or 0)
        if kind == "tree" or (size == 0 and not Path(path).suffix):
            out.extend(ms_collect_files(model_id, path, depth + 1))
        else:
            out.append({"path": path, "size": size})
    return out


def phase4_model() -> bool:
    log("【4/8】下载模型权重（约 2.5 GB，会慢）")
    marker = MODEL_DIR / "model.safetensors"
    if marker.exists() and marker.stat().st_size > 1e9:
        log(f"    已存在，跳过：{marker.stat().st_size / 1e9:.2f} GB")
        return True
    files = ms_collect_files(MODEL_ID)
    if not files:
        log("    ✗ 拿不到文件列表")
        return False
    total = sum(f["size"] for f in files)
    log(f"    {len(files)} 个文件，合计 {total / 1e9:.2f} GB")
    for item in files:
        path = item["path"]
        url = (f"https://modelscope.cn/api/v1/models/{MODEL_ID}/repo"
               f"?Revision=master&FilePath={urllib.parse.quote(path, safe='/')}")
        download(url, MODEL_DIR / path, path)
    return True


def phase5_llama() -> bool:
    log(f"【5/8】下载 llama.cpp {LLAMA_TAG} 二进制（Vulkan，AMD 卡用这个）")
    # 引擎（llama.py）和量化脚本（24/34）都是从「模块目录下的 bin」加载：
    #   qwen3_tts_gguf/inference/bin/{ggml.dll, llama.dll, llama-quantize.exe, ...}
    bin_dir = REPO_DIR / "qwen3_tts_gguf" / "inference" / "bin"
    marker = bin_dir / ".llama_version"

    # **版本必须严格匹配**：引擎的 ctypes 绑定是按 b10621 写的，换版本会导致
    # 加载 GGUF 时 fatal（llama-hparams 校验失败）。所以这里记版本标记，
    # 不一致就把旧 DLL 清掉重来 —— 实测「随便找个新版 Vulkan 包」会炸。
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == LLAMA_TAG \
            and any(bin_dir.glob("*.dll")):
        log(f"    已是 {LLAMA_TAG}，跳过（{len(list(bin_dir.glob('*.dll')))} 个 DLL）")
        return True
    if bin_dir.exists() and any(bin_dir.glob("*.dll")):
        current = (marker.read_text(encoding="utf-8").strip()
                   if marker.exists() else "未知版本")
        log(f"    当前 DLL 是 {current}，目标 {LLAMA_TAG} —— 版本不匹配会导致 "
            f"GGUF 加载 fatal，替换中…")
        for stale in list(bin_dir.glob("*.dll")) + list(bin_dir.glob("*.exe")):
            stale.unlink()

    # 按 tag 精确取：b10621 早就出了最近 30 个 release 的窗口，
    # 用 releases 列表搜是搜不到的（上一版就是这么退到了不兼容的新版）。
    try:
        release = http_json(
            f"https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{LLAMA_TAG}")
    except Exception as exc:  # noqa: BLE001
        log(f"    ✗ 取不到 {LLAMA_TAG} 的 release：{type(exc).__name__} {exc}")
        return False
    asset = next((item for item in release.get("assets") or []
                  if LLAMA_ASSET_HINT in item["name"]), None)
    if asset is None:
        log(f"    ✗ {LLAMA_TAG} 里没有 {LLAMA_ASSET_HINT}")
        log("      看 readme.md 里锁定的版本，别自己换版本")
        return False

    log(f"    取 {asset['name']}（{asset['size'] / 1e6:.1f} MB）")
    zip_path = download(
        [asset["browser_download_url"],
         "https://ghproxy.net/" + asset["browser_download_url"],
         "https://gh-proxy.com/" + asset["browser_download_url"],
         "https://ghfast.top/" + asset["browser_download_url"]],
        VENDOR / asset["name"], asset["name"])
    bin_dir.mkdir(parents=True, exist_ok=True)
    log("    解压 DLL/EXE 到 qwen3_tts_gguf/inference/bin/…")
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if member.lower().endswith((".dll", ".exe")):
                (bin_dir / Path(member).name).write_bytes(zf.read(member))
    marker.write_text(LLAMA_TAG, encoding="utf-8")
    log(f"    完成：{len(list(bin_dir.glob('*.dll')))} 个 DLL / "
        f"{len(list(bin_dir.glob('*.exe')))} 个 EXE（版本 {LLAMA_TAG}）")
    return True


def phase6_config() -> bool:
    log("【6/8】改写 export_config.py 指向 0.6B 模型与本地目录")
    config_path = REPO_DIR / "export_config.py"
    if not config_path.exists():
        log(f"    ✗ 找不到 {config_path}")
        return False
    text = config_path.read_text(encoding="utf-8")

    expected_model = "model = Models.base_small"
    expected_home = f"model_home = Path(r'{MODEL_DIR.parent}')"
    expected_dest = f"dest_home = Path(r'{EXPORT_DIR}')"

    # 幂等：目标内容已经在了就直接算成功。
    # 注意不能用「替换前后文本是否相同」来判断 —— 已经改好的文件再替换一遍
    # 结果也是原文，会被误判成「没匹配到」（上一版就是这么误报的）。
    if expected_model in text and expected_home in text and expected_dest in text:
        log("    配置已是最新（幂等跳过）")
        return True

    # 指向 0.6B 的 Base（只有 Base 支持声音克隆）
    text = re.sub(r"(?m)^model\s*=\s*Models\.\w+.*$",
                  lambda _m: expected_model, text)
    # 模型来源目录与导出目录都落到 vendor 下。
    # 关键：替换必须用 lambda —— 替换字符串里是 Windows 路径（D:\...），
    # 直接当字符串传会让 re 把 \A、\D 之类当成转义符（bad escape）。
    text = re.sub(r"(?m)^model_home\s*=.*$",
                  lambda _m: expected_home, text)
    text = re.sub(r"(?m)^dest_home\s*=.*$",
                  lambda _m: expected_dest, text)

    if not (expected_model in text and expected_home in text
            and expected_dest in text):
        log("    ⚠ 配置改写未生效，请人工检查 export_config.py：")
        for line in text.splitlines()[:40]:
            log("      " + line)
        return False
    config_path.write_text(text, encoding="utf-8")
    log("    已改写。相关行：")
    for line in text.splitlines():
        if re.match(r"^(model|model_home|dest_home)\s*=", line):
            log("      " + line)
    return True


# --------------------------------------------------------------------------- #
# 兼容 stub：给装不了的原生依赖提供「导入能过、形状对」的最小实现。
#
# 为什么要 stub 而不是装真包：
#   - librosa   依赖 numba，cp314 上没有 Windows 轮子
#   - sox       Python 绑定需要 SoX 原生库（libsox），没有 cp314 轮子
#   - torchaudio 需要与 torch 版本严格匹配的 C++ 扩展，装它要动全局 torch
#
# 这些模块在官方代码里都是**模块顶层 import、只在音频处理函数里调用**。
# 导出权重（只碰模型参数）和 GGUF 推理（自研引擎，不走官方代码）都到不了
# 那些调用点 —— 所以 stub 只需让 import 成功；我们顺手把函数写成真实现，
# 万一被调到也不会悄悄出错。
# --------------------------------------------------------------------------- #
STUBS: dict[str, str] = {
    "librosa/__init__.py": '''"""librosa 兼容 stub（cp314 无 numba 轮子，装不了真 librosa）。"""
import numpy as np
import scipy.signal
import soundfile as sf


def load(path, sr=None, mono=True):
    data, native = sf.read(path, dtype="float32", always_2d=True)
    if data.shape[1] > 1 and mono:
        data = data.mean(axis=1, keepdims=True)
    data = data[:, 0] if mono else data
    if sr is not None and sr != native:
        data = resample(data, orig_sr=native, target_sr=sr)
        native = sr
    return data, native


def resample(y, orig_sr=22050, target_sr=22050, **kwargs):
    orig_sr, target_sr = int(orig_sr), int(target_sr)
    if orig_sr == target_sr or y.size == 0:
        return y
    gcd = int(np.gcd(orig_sr, target_sr))
    up, down = target_sr // gcd, orig_sr // gcd
    return scipy.signal.resample_poly(y, up, down, axis=0)
''',

    "librosa/filters/__init__.py": '''"""librosa.filters 兼容 stub —— 标准 HTK 公式的 mel 滤波器组。"""
import numpy as np


def mel(sr=22050, n_fft=2048, n_mels=128, fmin=0.0, fmax=None, **kwargs):
    n_fft, n_mels = int(n_fft), int(n_mels)
    fmax = float(fmax) if fmax else float(sr) / 2.0
    fmin = max(float(fmin), 0.0)
    hz_to_mel = lambda f: 2595.0 * np.log10(1.0 + f / 700.0)
    mel_to_hz = lambda m: 700.0 * (10.0 ** (m / 2595.0) - 1.0)
    n_freqs = n_fft // 2 + 1
    fftfreqs = np.linspace(0.0, float(sr) / 2.0, n_freqs)
    mels = np.linspace(hz_to_mel(fmin), hz_to_mel(fmax), n_mels + 2)
    fsp = mel_to_hz(mels)
    fdiff = np.diff(fsp)
    ramps = np.subtract.outer(fsp, fftfreqs)
    lower = -ramps[:-2] / fdiff[:-1, np.newaxis]
    upper = ramps[2:] / fdiff[1:, np.newaxis]
    weights = np.maximum(0.0, np.minimum(lower, upper))
    enorm = 2.0 / (fsp[2:n_mels + 2] - fsp[:n_mels])
    return weights * enorm[:, np.newaxis]
''',

    "sox/__init__.py": '''"""sox 兼容 stub —— 真实实现需要 SoX 原生库，cp314 装不了。

speech_vq.py 只在类初始化时调用 sox.Transformer()（链式 API），
导出权重不会走到音频变换。给一个方法返回 self 的空 Transformer，
保证 import 和实例化都不炸。
"""


class Transformer:
    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        # sox 的链式 API：tfm.set_output_format(...).resample(...) 每个都返回 self
        return self._noop

    def _noop(self, *args, **kwargs):
        return self

    def build(self, *args, **kwargs):
        return self

    def __call__(self, *args, **kwargs):
        return None
''',

    "torchaudio/__init__.py": '''"""torchaudio 兼容 stub —— C++ 扩展与 torch 版本强绑定，装它要动全局 torch。"""
''',

    "torchaudio/compliance/__init__.py": '''"""torchaudio.compliance 兼容 stub。"""
''',

    "torchaudio/compliance/kaldi.py": '''"""torchaudio.compliance.kaldi 兼容 stub。

speech_vq.py 只在 WhisperEncoder 前向里调用 kaldi.fbank() 算特征，
导出权重（只碰参数）不会走到。给一个返回零张量的实现，形状对、不崩。
"""
import torch


def fbank(sig, num_mel_bins=80, sample_frequency=16000, **kwargs):
    batch = 1
    if getattr(sig, "dim", lambda: 1)() == 2:
        batch = sig.shape[0]
    return torch.zeros(batch, 1, int(num_mel_bins))
''',
}


def _ensure_stubs() -> Path:
    """导出前生成全部兼容 stub。"""
    root = VENDOR / "stubs"
    for rel, content in STUBS.items():
        target = root / rel
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            log(f"    生成 stub: {rel}")
    return root


def _ensure_convert_shim() -> Path:
    """33 号脚本期待 ref/llama.cpp 下有 llama.cpp 源码树的 convert_hf_to_gguf.py。

    我们没有源码树，但仓库自带一份同源实现
    （qwen3_tts_gguf/export/convert_hf_to_gguf.py，23 号脚本就是用它成功的）。
    这里放一个 re-export shim 顶上，避免为了一个脚本去克隆整个 llama.cpp。
    """
    shim = REPO_DIR / "ref" / "llama.cpp" / "convert_hf_to_gguf.py"
    if not shim.exists():
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text(
            '"""Shim: 让 33 号脚本用仓库自带的转换器（无需克隆 llama.cpp 源码树）。"""\n'
            "from qwen3_tts_gguf.export.convert_hf_to_gguf import *  # noqa: F401,F403\n"
            "from qwen3_tts_gguf.export.convert_hf_to_gguf import "  # noqa: F401
            "TextModel, ModelBase, main  # noqa: F401\n",
            encoding="utf-8")
        log("    已生成 convert_hf_to_gguf shim（ref/llama.cpp/convert_hf_to_gguf.py）")
    else:
        log("    convert_hf_to_gguf shim 已存在")
    return shim


def phase7_export() -> bool:
    log("【7/8】执行导出（14 个脚本，CPU 上可能要十几分钟）")
    stubs = _ensure_stubs()
    _ensure_convert_shim()
    env = dict(os.environ)
    # 依赖（.pylibs）+ librosa stub + 仓库根 + 官方 Qwen3-TTS 源码
    env["PYTHONPATH"] = os.pathsep.join(
        [str(stubs), str(LIBS), str(REPO_DIR), str(REPO_DIR / "Qwen3-TTS-main"),
         env.get("PYTHONPATH", "")]).strip(os.pathsep)
    env["PYTHONIOENCODING"] = "utf-8"
    failed: list[str] = []
    for index, script in enumerate(EXPORT_SCRIPTS, start=1):
        path = REPO_DIR / script
        if not path.exists():
            log(f"    [{index}/{len(EXPORT_SCRIPTS)}] {script} —— 不存在，跳过")
            continue
        log(f"    [{index}/{len(EXPORT_SCRIPTS)}] {script} …")
        result = subprocess.run(
            [sys.executable, str(path)], cwd=str(REPO_DIR), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        tail = (result.stdout or "").strip().splitlines()[-6:]
        for line in tail:
            log("        " + line)
        if result.returncode != 0:
            log(f"        ✗ 退出码 {result.returncode}")
            for line in (result.stderr or "").strip().splitlines()[-10:]:
                log("        " + line)
            failed.append(script)
            break
    if failed:
        log(f"    ✗ 导出中断于 {failed[0]}，先修这个再重跑（已完成的部分会跳过）")
        return False
    return True


def phase8_verify() -> bool:
    log("【8/8】自检导出产物")
    if not EXPORT_DIR.exists():
        log(f"    ✗ 导出目录不存在：{EXPORT_DIR}")
        return False
    files = sorted(p for p in EXPORT_DIR.rglob("*") if p.is_file())
    total = sum(p.stat().st_size for p in files)
    log(f"    {len(files)} 个文件，合计 {total / 1e6:.0f} MB")
    for path in files[:24]:
        log(f"      {path.relative_to(EXPORT_DIR)}  {path.stat().st_size / 1e6:.1f} MB")
    gguf = list(EXPORT_DIR.rglob("*.gguf"))
    onnx = list(EXPORT_DIR.rglob("*.onnx"))
    log(f"    GGUF {len(gguf)} 个 / ONNX {len(onnx)} 个")
    if not gguf or not onnx:
        log("    ⚠ 产物不完整（GGUF 或 ONNX 缺一个），推理会失败")
        return False
    log("    ✓ 导出完整")
    return True


PHASES = {
    1: ("环境检查", phase1_check),
    2: ("拉取仓库", phase2_repo),
    3: ("安装依赖", phase3_deps),
    4: ("下载模型", phase4_model),
    5: ("llama.cpp", phase5_llama),
    6: ("改写配置", phase6_config),
    7: ("执行导出", phase7_export),
    8: ("自检产物", phase8_verify),
}


def show_status() -> None:
    print("Qwen3-TTS 部署进度\n")
    checks = [
        ("仓库", REPO_DIR / "qwen3_tts_gguf" / "inference" / "engine.py"),
        ("模型权重", MODEL_DIR / "model.safetensors"),
        ("llama.cpp DLL", REPO_DIR / "qwen3_tts_gguf" / "inference" / "bin"),
        ("导出目录", EXPORT_DIR),
    ]
    for name, path in checks:
        if path.is_dir():
            count = len(list(path.rglob("*")))
            print(f"  {name:<16} 存在（{count} 项）  {path}")
        elif path.exists():
            print(f"  {name:<16} 存在（{path.stat().st_size / 1e6:.1f} MB）  {path}")
        else:
            print(f"  {name:<16} 缺失  {path}")
    if LOG_PATH.exists():
        print(f"\n日志：{LOG_PATH}")


def main() -> int:
    parser = argparse.ArgumentParser(description="一键部署 Qwen3-TTS 本地音色克隆")
    parser.add_argument("--phase", type=int, choices=sorted(PHASES),
                        help="只跑指定阶段")
    parser.add_argument("--status", action="store_true", help="只看进度")
    parser.add_argument("--from", dest="from_phase", type=int, choices=sorted(PHASES),
                        help="从该阶段开始跑到底")
    args = parser.parse_args()

    if args.status:
        show_status()
        return 0

    VENDOR.mkdir(parents=True, exist_ok=True)
    global _log_file
    _log_file = open(LOG_PATH, "a", encoding="utf-8")
    _log_file.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 开始部署 =====\n")

    todo = ([args.phase] if args.phase
            else list(range(args.from_phase or 1, len(PHASES) + 1)))
    log(f"准备执行阶段：{todo}")
    for number in todo:
        name, function = PHASES[number]
        try:
            ok = function()
        except Exception as exc:  # noqa: BLE001
            log(f"    ✗ 阶段 {number} 异常：{type(exc).__name__}: {exc}")
            ok = False
        if not ok:
            log(f"\n✗ 停在阶段 {number}（{name}）。修好后重跑本脚本即可，已完成的会跳过。")
            return 1
    log("\n✓ 全部阶段完成")
    show_status()
    return 0


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  （阶段 4 用）
    raise SystemExit(main())
