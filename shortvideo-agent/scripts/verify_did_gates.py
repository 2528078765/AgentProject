"""用真实 D-ID 验证新增的图片闸门（上传免费，不消耗额度）。

只验证**在上传阶段就拦下**的路径，所以一次 /talks 都不会发出去，零成本：

  1. 用真实 D-ID 的能力声明去规划用户那张 3072×4096 —— 应压到安全尺寸
  2. 把压好的图真的传到 D-ID /images，读回 faces[] —— 应是 1 张脸
  3. 拿一张纯色图走完整 _avatar_template —— 人脸闸门应在**上传后、提交前**拦下

第 3 步是关键：D-ID 的 /images 对纯色图返回 201，所以只有读了 faces[]
才发现问题。这正是改造前会一路走到提交、把额度花掉的那个洞。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autovid import provider_caps as CAP   # noqa: E402
from autovid import providers as P         # noqa: E402
from autovid.config import Config          # noqa: E402
from autovid.providers_registry import resolve  # noqa: E402

PHOTO = ROOT / "assets" / "avatars" / "avatar-your-id" / "photos" / "形象1.jpg"
WORK = ROOT / ".tmp" / "did_gates"
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


def main() -> int:
    config = Config.load(root=ROOT)
    entry = resolve(config, "avatar:avatar-d-id")
    cfg, key = entry["cfg"], entry["api_key"]
    limits = CAP.image_limits(cfg)
    print(f"D-ID 图片要求：{limits.describe()}\n")
    WORK.mkdir(parents=True, exist_ok=True)

    print("[1] 按真实能力声明规划用户那张 3072×4096")
    blob, fmt, plan = P._prepare_cloud_image(PHOTO, 1280, limits)
    print(f"  {plan.describe(3072, 4096)}  编码后 {len(blob) / 1024:.0f}KB（{fmt}）")
    check("尺寸规划没有报问题", plan.ok, str(plan.issues))
    check("控制在像素上限内",
          plan.width * plan.height <= limits.max_pixels,
          f"{plan.width * plan.height / 1e6:.2f}Mpx <= {limits.max_pixels / 1e6:.2f}Mpx")
    check("长边被压到 1280（省流量的默认值）", max(plan.width, plan.height) == 1280,
          f"{plan.width}×{plan.height}")

    print("\n[2] 真的传到 D-ID /images，读回 faces[]（上传免费）")
    import json as _json
    import uuid
    import urllib.error
    import urllib.request

    boundary = "----autovid" + uuid.uuid4().hex
    # 复刻代码里的做法：显式要求人脸检测（不传的话 D-ID 会回 faces: null）
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="detect_faces"\r\n\r\ntrue\r\n',
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="image"; filename="autovid.jpg"\r\n',
        b"Content-Type: image/jpeg\r\n\r\n", blob, b"\r\n",
        f"--{boundary}--\r\n".encode()])
    req = urllib.request.Request(
        str(cfg["upload"]["image"]["url"]), data=body,
        headers={"Authorization": f"Basic {key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            uploaded = _json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        print(f"  上传失败 HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}")
        return 2
    faces = uploaded.get("faces")
    print(f"  faces 字段：{_json.dumps(faces, ensure_ascii=False)[:200]}")
    check("真实响应里带 faces[]", isinstance(faces, list), str(type(faces)))
    check("真人照片检测到人脸", bool(faces), f"{len(faces or [])} 张")
    if faces:
        best = max(faces, key=lambda f: float(f.get("size") or 0))
        print(f"    置信度={best.get('detect_confidence')} "
              f"清晰度={best.get('sharpness')} 遮挡={best.get('face_occluded')}")
        check("置信度为满分（这张图是合格的）",
              float(best.get("detect_confidence") or 0) >= 99,
              str(best.get("detect_confidence")))

    print("\n[3] 纯色图走完整流程：应在提交前被拦下（零额度）")
    from PIL import Image
    blank = WORK / "blank.jpg"
    Image.new("RGB", (960, 1280), (40, 90, 160)).save(blank, "JPEG", quality=88)
    # 音频必须是真音频：上一版这里误传了 jpg，结果卡在音频上传 415，
    # 反而没验证到人脸闸门（测试自己的错，记在这里免得下次再犯）。
    import subprocess
    from autovid import media as M  # noqa: PLC0415
    audio = WORK / "probe.wav"
    if not audio.exists():
        subprocess.run([M.ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi",
                        "-i", "sine=frequency=220:duration=2", "-ar", "24000",
                        "-ac", "1", str(audio)], check=True, capture_output=True)
    segs = [{"audio_path": str(audio), "index": 0}]
    logs: list[str] = []
    try:
        P._avatar_template(config, segs, [blank], WORK / "t3", logs.append,
                           cfg=cfg, api_key=key,
                           provider_name="avatar:avatar-d-id", portrait=blank)
        check("纯色图应当被拦下", False, "竟然没报错 —— 说明闸门没生效")
    except P.ProviderError as exc:
        text = str(exc)
        check("纯色图被拦下", "没检测到人脸" in text, text.splitlines()[0][:70])
        check("说明了没有产生费用", "没有产生费用" in str(exc))
    except Exception as exc:  # noqa: BLE001
        check("纯色图被拦下", False, f"{type(exc).__name__}: {exc}"[:120])
    check("日志里有人脸检测结果",
          any("人脸" in line for line in logs),
          next((l for l in logs if "人脸" in l), "无"))

    print()
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败：{FAILS}")
        return 1
    print("✓ 全部通过 —— 真实 D-ID 上的图片闸门有效，且没有消耗任何额度")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
