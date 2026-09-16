"""实测：数字人厂商对上传图片到底有什么要求。

拿你真实的照片（3072×4096）压成各种分辨率，分别打到 D-ID 的 /images 上传接口，
把它返回的东西全部打出来 —— 尤其是人脸检测结果。

上传图片不消耗生成额度，所以这个可以放心跑。

同时测几张「故意不合格」的图（纯色无人脸 / 极小图），看它到底怎么报错 ——
把它的失败签名摸清楚，才知道以后怎么自动判断。
"""
from __future__ import annotations

import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autovid.config import Config              # noqa: E402
from autovid.providers_registry import resolve  # noqa: E402

PHOTO = ROOT / "assets" / "avatars" / "avatar-your-id" / "photos" / "形象1.jpg"
WORK = ROOT / ".tmp" / "did_image"


def upload(url: str, key: str, blob: bytes, name: str, ctype: str) -> dict:
    import uuid
    boundary = "----autovid" + uuid.uuid4().hex
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="image"; filename="{name}"\r\n'.encode(),
        f"Content-Type: {ctype}\r\n\r\n".encode(),
        blob, b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": f"Basic {key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            text = resp.read().decode("utf-8", "replace")
            return {"status": resp.status, "body": text, "elapsed": time.time() - t0}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code,
                "body": exc.read().decode("utf-8", "replace")[:600],
                "elapsed": time.time() - t0}
    except Exception as exc:  # noqa: BLE001
        return {"status": type(exc).__name__, "body": str(exc)[:300],
                "elapsed": time.time() - t0}


def make_variant(src: Path, out: Path, size: tuple[int, int] | None) -> bytes:
    from PIL import Image  # noqa: PLC0415
    image = Image.open(src).convert("RGB")
    if size:
        image = image.resize(size, Image.LANCZOS)
    image.save(out, "JPEG", quality=88)
    return out.read_bytes()


def make_blank(size: tuple[int, int]) -> bytes:
    from PIL import Image  # noqa: PLC0415
    out = WORK / f"blank_{size[0]}.jpg"
    Image.new("RGB", size, (40, 90, 160)).save(out, "JPEG", quality=88)
    return out.read_bytes()


def main() -> int:
    config = Config.load(root=ROOT)
    entry = resolve(config, "avatar:avatar-d-id")
    cfg, key = entry["cfg"], entry["api_key"]
    url = str((cfg.get("upload") or {}).get("image", {}).get("url"))
    print(f"endpoint = {url}")
    print(f"源图     = {PHOTO.name}  {PHOTO.stat().st_size / 1024:.0f} KB")
    try:
        from PIL import Image  # noqa: PLC0415
        with Image.open(PHOTO) as im:
            print(f"源图尺寸 = {im.size[0]}×{im.size[1]}")
    except Exception as exc:  # noqa: BLE001
        print(f"读源图失败：{exc}")
        return 2

    WORK.mkdir(parents=True, exist_ok=True)

    cases: list[tuple[str, bytes]] = []
    for label, size in [
        ("原始 3072×4096", None),
        ("长边 1920", (1440, 1920)),
        ("长边 1280（我们当前用的）", (960, 1280)),
        ("长边 512", (384, 512)),
        ("256×256 极小", (256, 256)),
        ("128×128 极小", (128, 128)),
        ("64×64 极小", (64, 64)),
    ]:
        out = WORK / f"v_{size[0] if size else 'orig'}.jpg"
        cases.append((label, make_variant(PHOTO, out, size)))
    cases.append(("纯色图（无人脸） 960×1280", make_blank((960, 1280))))

    print(f"\n{'用例':<26} {'HTTP':<6} {'上传KB':<8} {'耗时':<7} 结果")
    print("-" * 100)
    for label, blob in cases:
        r = upload(url, key, blob, "autovid.jpg", "image/jpeg")
        body = r["body"]
        try:
            data = json.loads(body)
            brief = json.dumps(data, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            brief = body.replace("\n", " ")[:220]
        print(f"{label:<26} {r['status']:<6} {len(blob)/1024:<8.0f} "
              f"{r['elapsed']:<7.2f} {brief[:200]}")
        time.sleep(0.6)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
