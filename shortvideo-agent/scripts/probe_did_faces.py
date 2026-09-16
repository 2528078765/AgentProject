"""免费测试：/images 的 detect_faces 能不能当「图合格」的前置闸门。

价值：我们现在的上传只拿到一个 url，**根本不知道那张图里有没有能被识别的人脸**。
实测纯色图也能上传成功（201），所以「图里没有人」这种错误会一路拖到生成阶段才爆 ——
而那时候已经花了额度。

spec 里 /images 的 form 字段有 `detect_faces`，响应里有 faces[] 数组，每张脸带：
    size / top_left / overlap / detect_confidence / sharpness / face_occluded / detection
如果能用，就能在**上传当场**判定「这张图能不能用」，一分钱不花。

上传图片不消耗额度，所以这个可以放心跑。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autovid.config import Config              # noqa: E402
from autovid.providers_registry import resolve  # noqa: E402

WORK = ROOT / ".tmp" / "did_faces"
PHOTO = ROOT / "assets" / "avatars" / "avatar-your-id" / "photos" / "形象1.jpg"


def upload(url: str, key: str, blob: bytes, name: str, ctype: str,
           extra: dict[str, str] | None = None) -> dict:
    b = "----autovid" + uuid.uuid4().hex
    chunks = []
    for k, v in (extra or {}).items():
        chunks += [f"--{b}\r\n".encode(),
                   f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode(),
                   f"{v}\r\n".encode()]
    chunks += [
        f"--{b}\r\n".encode(),
        f'Content-Disposition: form-data; name="image"; filename="{name}"\r\n'.encode(),
        f"Content-Type: {ctype}\r\n\r\n".encode(),
        blob, b"\r\n", f"--{b}--\r\n".encode()]
    body = b"".join(chunks)
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": f"Basic {key}",
                 "Content-Type": f"multipart/form-data; boundary={b}"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return {"error": exc.code, "body": exc.read().decode("utf-8", "replace")[:300]}


def main() -> int:
    config = Config.load(root=ROOT)
    entry = resolve(config, "avatar:avatar-d-id")
    cfg, key = entry["cfg"], entry["api_key"]
    url = str(cfg["upload"]["image"]["url"])
    WORK.mkdir(parents=True, exist_ok=True)

    from PIL import Image  # noqa: PLC0415

    cases: list[tuple[str, bytes]] = []

    # 1) 用户真实照片，长边 1280（我们流水线实际会传的尺寸）
    with Image.open(PHOTO) as im:
        img = im.convert("RGB")
        scale = 1280 / max(img.size)
        img = img.resize((int(img.size[0] * scale), int(img.size[1] * scale)),
                         Image.LANCZOS)
        p = WORK / "person.jpg"
        img.save(p, "JPEG", quality=88)
        cases.append(("真人照片 960×1280（+detect_faces）", p.read_bytes()))

    # 2) 纯色图 —— 必然没人脸
    blank = WORK / "blank.jpg"
    Image.new("RGB", (960, 1280), (40, 90, 160)).save(blank, "JPEG", quality=88)
    cases.append(("纯色图 960×1280（+detect_faces）", blank.read_bytes()))

    # 3) 真人照片但不要求检测 —— 看默认行为
    cases.append(("真人照片（不带 detect_faces）", (WORK / "person.jpg").read_bytes()))

    for label, blob in cases:
        want = "detect_faces" in label
        extra = {"detect_faces": "true"} if want else None
        r = upload(url, key, blob, "autovid.jpg", "image/jpeg", extra)
        print(f"\n--- {label} ---")
        faces = r.get("faces")
        if faces is None:
            print(f"  响应里没有 faces 字段：{json.dumps(r, ensure_ascii=False)[:300]}")
            continue
        print(f"  检测到 {len(faces)} 张脸")
        for i, f in enumerate(faces[:3]):
            print(f"    #{i}: size={f.get('size')} top_left={f.get('top_left')} "
                  f"conf={f.get('detect_confidence')} sharp={f.get('sharpness')} "
                  f"occluded={f.get('face_occluded')} overlap={f.get('overlap')}")
            det = f.get("detection") or {}
            if det:
                w = det.get("right", 0) - det.get("left", 0)
                h = det.get("bottom", 0) - det.get("top", 0)
                print(f"        face box ≈ {w:.0f}×{h:.0f} px"
                      f"  占比 ≈ {w * h / (960 * 1280) * 100:.1f}% 画面")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
