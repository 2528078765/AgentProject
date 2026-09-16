"""实测：D-ID 的 config.stitch 能不能把成片分辨率从 512×512 提到原图尺寸。

这是「分辨率必须是对应厂商能支持的」这条要求的核心问题。

spec 里 TalksConfig.stitch 的原文是：
    "Stitch back the animated result to the original image"
听起来就是「把动完的脸贴回原图」→ 输出应该跟着原图走。
但同一个 spec 里 403 的示例写着：
    {"kind": "PermissionError", "description": "user has no permission for stitch"}
说明它可能是付费权限。

所以只能实测。用同一张图、同一段 5 秒音频，跑 stitch=false / stitch=true 各一次，
把回来的 mp4 分辨率量出来。

成本：约 1~2 个 D-ID 额度。
"""
from __future__ import annotations

import json
import subprocess
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

WORK = ROOT / ".tmp" / "did_stitch"
PHOTO = ROOT / "assets" / "avatars" / "avatar-your-id" / "photos" / "形象1.jpg"


def api(url: str, key: str, payload: dict | None = None, method: str = "POST") -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Basic {key}", "Content-Type": "application/json"},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return {"status": resp.status, "json": json.loads(resp.read().decode() or "{}")}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "json": exc.read().decode("utf-8", "replace")[:400]}


def upload(url: str, key: str, blob: bytes, name: str, ctype: str, field: str) -> dict:
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


def run_talk(key: str, image_url: str, audio_url: str, stitch: bool) -> dict:
    payload = {
        "source_url": image_url,
        "script": {"type": "audio", "audio_url": audio_url},
        "config": {"stitch": stitch},
    }
    print(f"  提交（stitch={stitch}）…")
    created = api("https://api.d-id.com/talks", key, payload)
    if created["status"] >= 400:
        return {"ok": False, "submit": created}
    job = created["json"].get("id")
    print(f"    id={job}")
    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(8)
        got = api(f"https://api.d-id.com/talks/{job}", key, method="GET")
        st = (got["json"].get("status") if isinstance(got["json"], dict) else "?")
        print(f"    状态={st}")
        if st == "done":
            return {"ok": True, "result": got["json"].get("result_url"), "raw": got["json"]}
        if st in ("error", "rejected"):
            return {"ok": False, "poll": got["json"]}
    return {"ok": False, "poll": "超时"}


def main() -> int:
    config = Config.load(root=ROOT)
    entry = resolve(config, "avatar:avatar-d-id")
    cfg, key = entry["cfg"], entry["api_key"]
    WORK.mkdir(parents=True, exist_ok=True)

    creds = api("https://api.d-id.com/credits", key, method="GET")
    print(f"额度：{json.dumps(creds['json'], ensure_ascii=False)[:200]}\n")

    # 5 秒测试音频：用 ffmpeg 生成一段真人声以外的白噪不合适，直接用现成的
    # 第一句语音片段（真实、短）
    src = ROOT / "runs" / "20260913-115519-langchain" / "voice" / "utt" / "utt_00.wav"
    if not src.exists():
        src = next((ROOT / "runs").glob("*/voice/utt/utt_00.wav"), None)
    if src is None:
        print("找不到测试音频")
        return 2
    short = WORK / "short.wav"
    subprocess.run([M.ffmpeg_exe(), "-y", "-v", "error", "-i", str(src),
                    "-t", "5", "-ar", "24000", "-ac", "1", str(short)],
                   check=True, capture_output=True)
    print(f"测试音频 = {short.name}  {M.wav_duration(short):.2f}s")

    up_cfg = cfg["upload"]
    img = upload(str(up_cfg["image"]["url"]), key, PHOTO.read_bytes(),
                 "autovid.jpg", "image/jpeg", "image")
    print(f"图上传 -> {str(img)[:120]}")
    aud = upload(str(up_cfg["audio"]["url"]), key, short.read_bytes(),
                 "autovid.wav", "audio/wav", "audio")
    print(f"音上传 -> {str(aud)[:120]}")
    if "url" not in img or "url" not in aud:
        print("上传失败，停")
        return 2

    results = {}
    for stitch in (False, True):
        print(f"\n=== stitch={stitch} ===")
        r = run_talk(key, img["url"], aud["url"], stitch)
        results[stitch] = r
        if not r["ok"]:
            print(f"  失败：{json.dumps(r, ensure_ascii=False)[:400]}")
            continue
        out = WORK / f"stitch_{stitch}.mp4"
        req = urllib.request.Request(r["result"])
        with urllib.request.urlopen(req, timeout=300) as resp:
            out.write_bytes(resp.read())
        print(f"  成片：{out.name}  {out.stat().st_size/1e6:.2f}MB  "
              f"分辨率={M.probe_video_size(out)}  时长={M.probe_duration(out):.2f}s")

    print("\n=== 结论 ===")
    for stitch, r in results.items():
        if r["ok"]:
            print(f"  stitch={stitch}: 成功")
        else:
            print(f"  stitch={stitch}: 失败 -> {json.dumps(r, ensure_ascii=False)[:200]}")
    creds2 = api("https://api.d-id.com/credits", key, method="GET")
    print(f"  额度：{json.dumps(creds2['json'], ensure_ascii=False)[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
