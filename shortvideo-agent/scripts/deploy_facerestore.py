"""下载 CodeFormer 人脸修复模型（解决 Wav2Lip 嘴部糊的问题）。

    codeformer.onnx   337 MB   ONNX 格式，用 onnxruntime 直接跑

为什么不用官方的 gfpgan / basicsr 包：
    它们依赖旧版 torch + 需要编译的扩展，Python 3.14 上基本装不了
    （本项目已在 librosa/numba、sox 上踩过同样的坑）。
    onnxruntime 项目里已经有了（Qwen3-TTS 在用），拿 ONNX 模型最稳。

可重复运行，带断点续传与镜像回退。

    python scripts/deploy_facerestore.py
    python scripts/deploy_facerestore.py --status
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autovid import face_restore as F  # noqa: E402

MIN_MB = 300
URLS = [
    "https://huggingface.co/yuvraj108c/facerestore-onnx/resolve/main/codeformer.onnx",
    "https://hf-mirror.com/yuvraj108c/facerestore-onnx/resolve/main/codeformer.onnx",
]


def log(message: str) -> None:
    print(message, flush=True)


def download(attempts: int = 4) -> bool:
    target = F.MODEL_PATH
    if F.model_ready():
        log(f"  已存在，跳过：{target.name}（{target.stat().st_size / 1e6:.0f} MB）")
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(".part")
    last: Exception | None = None

    for attempt in range(1, attempts + 1):
        for url in URLS:
            try:
                have = part.stat().st_size if part.exists() else 0
                headers = {"User-Agent": "autovid-deploy"}
                if have:
                    headers["Range"] = f"bytes={have}-"
                request = urllib.request.Request(url, headers=headers)
                started = time.time()
                with urllib.request.urlopen(request, timeout=1800) as response:
                    resuming = have > 0 and response.status == 206
                    if resuming:
                        log(f"  续传：从 {have / 1e6:.0f} MB 继续")
                    else:
                        have = 0
                    total = int(response.headers.get("Content-Length") or 0) + have
                    done = have
                    last_log = 0.0
                    with open(part, "ab" if resuming else "wb") as handle:
                        while chunk := response.read(1 << 20):
                            handle.write(chunk)
                            done += len(chunk)
                            now = time.time()
                            if now - last_log > 5:
                                last_log = now
                                speed = (done - have) / max(0.001, now - started) / 1e6
                                pct = f"{done / total * 100:.0f}%" if total else "?"
                                log(f"  codeformer.onnx: {done / 1e6:.0f} / "
                                    f"{total / 1e6:.0f} MB ({pct}, {speed:.1f} MB/s)")
                if total and part.stat().st_size < total:
                    raise IOError(f"只下到 {part.stat().st_size} / {total} 字节")
                part.replace(target)
                log(f"  完成：{target.name}（{target.stat().st_size / 1e6:.0f} MB）")
                return True
            except Exception as exc:  # noqa: BLE001
                last = exc
                kept = part.stat().st_size / 1e6 if part.exists() else 0
                log(f"  失败（{type(exc).__name__}）: {url[:60]}"
                    f"{f'（保留 {kept:.0f} MB 待续传）' if kept else ''}")
        if attempt < attempts:
            time.sleep(3 * attempt)
    log(f"  ✗ 多次重试仍失败：{last}")
    return False


def show_status() -> None:
    print("CodeFormer 人脸修复模型\n")
    if F.model_ready():
        print(f"  codeformer.onnx  存在（{F.MODEL_PATH.stat().st_size / 1e6:.0f} MB）✓ 可用")
    else:
        print(f"  codeformer.onnx  缺失  {F.MODEL_PATH}")
    print(f"\n就绪：{'是' if F.model_ready() else '否'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 CodeFormer 人脸修复模型")
    parser.add_argument("--status", action="store_true", help="只看状态")
    args = parser.parse_args()
    if args.status:
        show_status()
        return 0

    print("CodeFormer 人脸修复模型下载")
    print("=" * 70)
    print(f"目标：{F.MODEL_PATH}\n")
    ok = download()
    print("\n" + "=" * 70)
    show_status()
    if not ok or not F.model_ready():
        print("\n[失败] 模型不完整")
        return 1
    print("\n[通过] 就绪 —— 生成时会自动对脸部做修复，嘴部会清晰很多")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
