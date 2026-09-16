"""定位 InvalidFileSizeError：到底是图太大还是音太大。

线索：
  - /images 上传原图 3072×4096（738KB 的 JPEG）-> 201 成功
  - 拿它去 /talks 提交 -> 400 InvalidFileSizeError "file size exceeded 10 MB"
  - 但我们的流水线一直是把图压到长边 1280 再传，之前是能跑通的

两个假设：
  A. 限制算的是**解码后的像素体积**（3072×4096×3 = 37.7MB > 10MB），
     而不是 738KB 的 JPEG 文件大小。→ 那 1280（960×1280×3 = 3.7MB）正好安全。
  B. 限制算的是音频，跟图无关。

用 D-ID 自己的公开图（必然合格）配我们的音频提交一次就能分开：
  - 还是报 InvalidFileSizeError -> 是音频的问题（B）
  - 不报 -> 是图的问题（A）

这个测试如果在 400 就结束，不花额度；只有成功才花。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autovid import media as M                 # noqa: E402
from autovid.config import Config              # noqa: E402
from autovid.providers_registry import resolve  # noqa: E402

WORK = ROOT / ".tmp" / "did_size"
PHOTO = ROOT / "assets" / "avatars" / "avatar-your-id" / "photos" / "形象1.jpg"
PUBLIC = "https://d-id-public-bucket.s3.us-west-2.amazonaws.com/alice.jpg"


def api(url, key, payload=None, method="POST"):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Basic {key}", "Content-Type": "application/json"},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except Exception:  # noqa: BLE001
            return exc.code, body[:400]


def upload(url, key, blob, name, ctype, field):
    import uuid
    b = "----autovid" + uuid.uuid4().hex
    body = b"".join([
        f"--{b}\r\n".encode(),
        f'Content-Disposition: form-data; name="{field}"; filename="{name}"\r\n'.encode(),
        f"Content-Type: {ctype}\r\n\r\n".encode(),
        blob, b"\r\n", f"--{b}--\r\n".encode()])
    req = urllib.request.Request(url, data=body,
                                 headers={"Authorization": f"Basic {key}",
                                          "Content-Type": f"multipart/form-data; boundary={b}"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return {"error": exc.code, "body": exc.read().decode("utf-8", "replace")[:300]}


def main() -> int:
    config = Config.load(root=ROOT)
    entry = resolve(config, "avatar:avatar-d-id")
    cfg, key = entry["cfg"], entry["api_key"]
    WORK.mkdir(parents=True, exist_ok=True)

    src = next((ROOT / "runs").glob("*/voice/utt/utt_00.wav"), None)
    short = WORK / "short.wav"
    import subprocess
    subprocess.run([M.ffmpeg_exe(), "-y", "-v", "error", "-i", str(src),
                    "-t", "3", "-ar", "24000", "-ac", "1", str(short)],
                   check=True, capture_output=True)
    audio_blob = short.read_bytes()
    print(f"音频 {M.wav_duration(short):.2f}s  {len(audio_blob)/1024:.0f}KB "
          f"（解码后 {M.wav_duration(short)*24000*2/1e6:.2f}MB）")

    aud = upload(str(cfg["upload"]["audio"]["url"]), key, audio_blob,
                 "autovid.wav", "audio/wav", "audio")
    print(f"音频上传 -> {str(aud)[:110]}")
    audio_url = aud.get("url")
    if not audio_url:
        return 2

    # --- 关键实验：D-ID 自己的公开图 + 我们的音频 ---
    print(f"\n[A] 用 D-ID 公开图提交（图必然合格）—— 分离图/音")
    code, data = api("https://api.d-id.com/talks", key,
                     {"source_url": PUBLIC,
                      "script": {"type": "audio", "audio_url": audio_url}})
    print(f"    HTTP {code}  {json.dumps(data, ensure_ascii=False)[:220]}")
    if code >= 400 and isinstance(data, dict) and data.get("kind") == "InvalidFileSizeError":
        print("    => 音频有问题（假设 B 成立），与图无关")
        return 0
    if code >= 400:
        print("    => 既不是图也不是音，是别的错，需要看详情")
        return 0
    print("    => 音频没问题，问题在**我们的图**（假设 A 成立）")

    job = data.get("id")
    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(8)
        _, got = api(f"https://api.d-id.com/talks/{job}", key, method="GET")
        st = got.get("status") if isinstance(got, dict) else "?"
        print(f"    基线任务状态={st}")
        if st in ("done", "error", "rejected"):
            break

    # --- 用各种尺寸的图去试，找出阈值（400 免费，成功才花钱）---
    print("\n[B] 我们用不同尺寸的图提交，找阈值")
    from PIL import Image  # noqa: PLC0415
    with Image.open(PHOTO) as im:
        print(f"    原图 {im.size[0]}×{im.size[1]}  "
              f"解码后 {im.size[0]*im.size[1]*3/1e6:.1f}MB  "
              f"JPEG {PHOTO.stat().st_size/1024:.0f}KB")
        for long_side in (3072, 2400, 1920, 1600, 1280):
            scaled = im.convert("RGB").copy()
            scale = long_side / max(scaled.size)
            size = (max(1, int(scaled.size[0] * scale)),
                    max(1, int(scaled.size[1] * scale)))
            scaled = scaled.resize(size, Image.LANCZOS)
            out = WORK / f"w{long_side}.jpg"
            scaled.save(out, "JPEG", quality=88)
            blob = out.read_bytes()
            up = upload(str(cfg["upload"]["image"]["url"]), key, blob,
                        "autovid.jpg", "image/jpeg", "image")
            iurl = up.get("url")
            if not iurl:
                print(f"    {size[0]}×{size[1]}  上传失败 {str(up)[:80]}")
                continue
            code, data = api("https://api.d-id.com/talks", key,
                             {"source_url": iurl,
                              "script": {"type": "audio", "audio_url": audio_url}})
            kind = data.get("kind") if isinstance(data, dict) else "-"
            desc = (data.get("description", "") if isinstance(data, dict) else str(data))[:60]
            print(f"    {size[0]}×{size[1]}  {len(blob)/1024:>5.0f}KB  "
                  f"解码后 {size[0]*size[1]*3/1e6:>5.1f}MB  HTTP {code}  {kind} {desc}")
            if code < 400:
                # 成功了：删掉别再花钱
                tid = data.get("id")
                if tid:
                    api(f"https://api.d-id.com/talks/{tid}", key, method="DELETE")
                print("      （这一档通过了，已删除任务避免继续计费）")
            time.sleep(1)

    _, creds = api("https://api.d-id.com/credits", key, method="GET")
    print(f"\n额度：{json.dumps(creds, ensure_ascii=False)[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
