"""下载 Wav2Lip 数字人需要的两个模型文件。

    wav2lip_gan.pth  436 MB   Wav2Lip 生成器（口型合成）
    s3fd.pth          90 MB   S3FD 人脸检测器（定位人脸框）

只下代码不需要的东西：视觉质量判别器、唇同步专家、训练数据集都不下载。

可重复运行（已存在就跳过），断点续传，多镜像回退。

    python scripts/deploy_wav2lip.py
    python scripts/deploy_wav2lip.py --status
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autovid import wav2lip as W  # noqa: E402

TARGETS = [
    {
        "name": "wav2lip_gan.pth",
        "target": W.CKPT_PATH,
        "min_mb": 400,
        "urls": [
            "https://huggingface.co/Nekochu/Wav2Lip/resolve/main/wav2lip_gan.pth",
            "https://hf-mirror.com/Nekochu/Wav2Lip/resolve/main/wav2lip_gan.pth",
        ],
    },
    {
        "name": "s3fd.pth",
        "target": W.S3FD_PATH,
        "min_mb": 80,
        "urls": [
            "https://www.adrianbulat.com/downloads/python-fan/s3fd-619a316812.pth",
        ],
    },
]

_pass, _fail = "[通过]", "[失败]"
problems: list[str] = []


def log(message: str) -> None:
    print(message, flush=True)


def download(urls: list[str], target: Path, name: str, min_mb: int,
             attempts: int = 4) -> bool:
    """带续传与镜像回退的下载。"""
    if target.exists() and target.stat().st_size >= min_mb * 1e6:
        log(f"  已存在，跳过：{name}（{target.stat().st_size / 1e6:.0f} MB）")
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    last: Exception | None = None

    for attempt in range(1, attempts + 1):
        for url in urls:
            try:
                have = part.stat().st_size if part.exists() else 0
                headers = {"User-Agent": "autovid-deploy"}
                if have:
                    headers["Range"] = f"bytes={have}-"
                request = urllib.request.Request(url, headers=headers)
                started = time.time()
                with urllib.request.urlopen(request, timeout=1800) as response:
                    resuming = have > 0 and response.status == 206
                    if not resuming:
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
                                log(f"  {name}: {done / 1e6:.0f} / {total / 1e6:.0f} MB "
                                    f"({pct}, {speed:.1f} MB/s)")
                if total and part.stat().st_size < total:
                    raise IOError(f"只下到 {part.stat().st_size} / {total} 字节")
                part.replace(target)
                size_mb = target.stat().st_size / 1e6
                if size_mb < min_mb:
                    raise IOError(f"文件偏小（{size_mb:.0f} MB < {min_mb} MB），可能下错了")
                log(f"  完成：{name}（{size_mb:.0f} MB）")
                return True
            except Exception as exc:  # noqa: BLE001
                last = exc
                kept = part.stat().st_size / 1e6 if part.exists() else 0
                log(f"  失败（{type(exc).__name__}）: {url[:64]}"
                    f"{f'，已保留 {kept:.0f} MB 下次续传' if kept else ''}")
        if attempt < attempts:
            time.sleep(3 * attempt)
    problems.append(f"{name}: {last}")
    return False


def show_status() -> None:
    print("Wav2Lip 模型状态\n")
    for item in TARGETS:
        target = item["target"]
        if target.exists():
            print(f"  {item['name']:<20} 存在（{target.stat().st_size / 1e6:.0f} MB）"
                  f"  {'✓ 可用' if target.stat().st_size >= item['min_mb'] * 1e6 else '⚠ 偏小'}")
        else:
            print(f"  {item['name']:<20} 缺失  {target}")
    print(f"\n就绪：{'是' if W.models_ready() else '否'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 Wav2Lip 数字人模型")
    parser.add_argument("--status", action="store_true", help="只看状态")
    args = parser.parse_args()
    if args.status:
        show_status()
        return 0

    print("Wav2Lip 数字人模型下载")
    print("=" * 70)
    print(f"目标目录：{W.W2L_DIR}\n")
    for item in TARGETS:
        log(f"· {item['name']}（约 {item['min_mb']}+ MB）")
        download(item["urls"], item["target"], item["name"], item["min_mb"])

    print("\n" + "=" * 70)
    show_status()
    if problems or not W.models_ready():
        print(f"\n{_fail} 还有问题：{problems or '文件不齐'}")
        return 1
    print(f"\n{_pass} 模型就绪 —— 可以跑 scripts/smoke_wav2lip.py 验证")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
