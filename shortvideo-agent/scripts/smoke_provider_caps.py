"""回归测试：厂商能力声明 + 失败签名。

这一层存在的理由是两个真机事故：

  1. 硅基流动把「拒绝」伪装成 HTTP 200 + text/plain + 0 字节 + 0.18s 秒回。
     我们把它当网络抖动，退避重试 12 次，对着一个已明确拒收的接口磕了两分半钟。
  2. D-ID 的 /images 上传 3072×4096 返回 201 成功，到 /talks 才报
     "file size exceeded 10 MB" —— 而那张 JPEG 只有 738KB。
     真正的规则是像素总数（阈值 10×1024×1024），错误信息完全是误导的。

下面每一条都对应一个实测结论，不是想出来的。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autovid import provider_caps as CAP   # noqa: E402
from autovid.config import Config          # noqa: E402
from autovid.providers_registry import PRESETS, resolve  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


# --------------------------------------------------------------- 1. 能力声明
def test_capabilities() -> None:
    print("[1] 能力声明能从真实配置里读出来")
    config = Config.load(root=ROOT)

    did = resolve(config, "avatar:avatar-d-id") or {"cfg": PRESETS["d_id"]["config"]}
    check("D-ID 测试模板能解析", did is not None)
    cfg = did["cfg"]

    out = CAP.output_spec(cfg)
    check("D-ID 输出规格 = 512×512@25fps（实测值）",
          (out.width, out.height, out.fps) == (512, 512, 25),
          out.describe())

    img = CAP.image_limits(cfg)
    check("D-ID 图片最短边门槛 = 160（实测 InvalidImageResolutionError）",
          img.min_side == 160, str(img.min_side))
    check("D-ID 图片像素上限 = 10×1024×1024（实测阈值）",
          img.max_pixels == 10 * 1024 * 1024, f"{img.max_pixels / 1e6:.2f}Mpx")
    check("D-ID 上传响应带人脸检测（实测 faces[]）", img.reports_faces)

    aud = CAP.audio_limits(cfg)
    check("D-ID 音频时长上限 = 600s（官方 spec: talks 10 分钟）",
          aud.max_s == 600, f"{aud.max_s:.0f}s")
    check("D-ID 音频体积上限 = 15MB（官方 spec）", aud.max_mb == 15)

    sf = resolve(config, "voice:voice-cosyvoice2")
    check("硅基流动带 sf_empty_200 签名",
          any(s.get("name") == "sf_empty_200"
              for s in (sf["cfg"].get("failure_signatures") or [])))
    check("硅基流动有失败预算", int(sf["cfg"].get("reject_streak_limit") or 0) > 0,
          f"streak={sf['cfg'].get('reject_streak_limit')} "
          f"budget={sf['cfg'].get('reject_budget_s')}s")


# --------------------------------------------------------------- 2. 图片尺寸
def test_image_plan() -> None:
    print("\n[2] 图片尺寸协商（D-ID 的真实约束）")
    limits = CAP.ImageLimits(min_side=160, max_pixels=10 * 1024 * 1024)

    # 用户真实照片：3072×4096 = 12.58Mpx，实测会让 /talks 报 InvalidFileSizeError
    plan = CAP.plan_image(3072, 4096, limits, prefer_long_side=1280)
    px = plan.width * plan.height
    check("3072×4096 被压到长边 1280", max(plan.width, plan.height) == 1280,
          f"{plan.width}×{plan.height}")
    check("压完在像素上限内", px <= limits.max_pixels or plan.ok is False,
          f"{px / 1e6:.2f}Mpx / 上限 {limits.max_pixels / 1e6:.2f}Mpx")
    check("没有报问题（这是正常图）", plan.ok, str(plan.issues))

    # 不带 prefer_long_side 时，唯一目标就是不超上限
    plan2 = CAP.plan_image(3072, 4096, limits, prefer_long_side=0)
    check("只按上限压：结果 <= 10.49Mpx",
          plan2.width * plan2.height <= limits.max_pixels,
          f"{plan2.width}×{plan2.height} = {plan2.width * plan2.height / 1e6:.2f}Mpx")
    check("留了余量（不贴边，避免边界随机失败）",
          plan2.width * plan2.height < limits.max_pixels * 0.995,
          f"{plan2.width * plan2.height / limits.max_pixels:.3f} × 上限")

    # 实测通过的那档不能被改小到没意义
    plan3 = CAP.plan_image(2304, 3072, limits, prefer_long_side=0)
    check("2304×3072（实测通过的那档）不被无谓缩小",
          plan3.scale == 1.0, f"scale={plan3.scale:.3f}")

    # 太小的图：必须报出来，**不能偷偷放大**
    plan4 = CAP.plan_image(128, 128, limits, prefer_long_side=1280)
    check("128×128 被判为不合格", not plan4.ok, str(plan4.issues))
    check("没有偷偷放大糊弄校验", plan4.scale <= 1.0, f"scale={plan4.scale:.2f}")
    check("报错里给了人话建议",
          any("清晰" in i or "换一张" in i for i in plan4.issues),
          str(plan4.issues))

    # 不留任何限制时不该动图
    plan5 = CAP.plan_image(1000, 1000, CAP.ImageLimits(), prefer_long_side=0)
    check("无限制时不缩放", plan5.scale == 1.0 and plan5.width == 1000)


# --------------------------------------------------------------- 3. 画布协商
def test_canvas() -> None:
    print("\n[3] 画布协商：做不到就如实说")
    did = CAP.OutputSpec(width=512, height=512, fps=25, aspect="1:1")

    w, h, notes = CAP.negotiate_canvas(1080, 1920, did)
    check("目标画布仍是 1080×1920（放大在合成阶段做）", (w, h) == (1080, 1920))
    check("说了画幅不同", any("画幅不同" in n for n in notes), str(notes[:1]))
    check("说了这是放大、清晰度由厂商决定",
          any("放大" in n and "清晰度" in n for n in notes),
          next((n for n in notes if "放大" in n), "无"))
    check("算得出放大倍数是 7.9×(0.26Mpx -> 2.07Mpx)",
          any("7.9" in n for n in notes),
          next((n for n in notes if "7.9" in n), "无"))

    same = CAP.OutputSpec(width=1080, height=1920, fps=30)
    _, _, notes2 = CAP.negotiate_canvas(1080, 1920, same)
    check("规格一致时不出警告", notes2 == [], str(notes2))

    _, _, notes3 = CAP.negotiate_canvas(1080, 1920, CAP.OutputSpec())
    check("厂商未声明规格时不编造", notes3 == [], str(notes3))


# --------------------------------------------------------------- 4. 失败签名
def test_signatures() -> None:
    print("\n[4] 失败签名判定")
    config = Config.load(root=ROOT)
    sf = resolve(config, "voice:voice-cosyvoice2")["cfg"]
    did_entry = resolve(config, "avatar:avatar-d-id")
    did = did_entry["cfg"] if did_entry else PRESETS["d_id"]["config"]

    # 实测复刻：HTTP 200 + text/plain + 0 字节
    r = CAP.classify(200, "text/plain; charset=utf-8", 0, b"", sf)
    check("硅基流动空 200 被认出来", r is not None, str(r))
    check("判定为 rejected（重试没意义）", r is not None and r.kind == "rejected",
          r.kind if r else "-")
    check("明确说了「不重试」", r is not None and not r.worth_retrying)
    check("提示里点了 trace-id（提工单的凭据）",
          r is not None and "trace-id" in r.hint, r.hint if r else "")

    # 正常的空响应（比如别家真的返回空 body）不该被误判
    r2 = CAP.classify(200, "application/json", 0, b"", sf)
    check("application/json 的空响应不误判", r2 is None, str(r2))

    # 实测第二种形态：合法 audio/wav，但只有 7854 字节 ≈ 0.16s
    r2b = CAP.classify(200, "audio/wav", 7854, b"RIFF" + b"\0" * 7850, sf)
    check("近乎空的 WAV 被认出来（实测 7854 字节）", r2b is not None, str(r2b))
    check("但判为 transient —— 这是没出声，不是被拒绝",
          r2b is not None and r2b.kind == "transient",
          r2b.kind if r2b else "-")
    check("所以行为不变：仍然会重试", r2b is not None and r2b.worth_retrying)
    check("正常长度的 WAV 不误判",
          CAP.classify(200, "audio/wav", 274000, b"RIFF" + b"\0" * 273996, sf) is None)

    # D-ID 实测复刻：/talks 返回 InvalidFileSizeError
    body = b'{"kind":"InvalidFileSizeError","description":"file size exceeded 10 MB - the maximum size permitted"}'
    r3 = CAP.classify(400, "application/json", len(body), body, did)
    check("D-ID InvalidFileSizeError 被认出来", r3 is not None, str(r3))
    check("提示点破「其实是像素超限，不是文件体积」",
          r3 is not None and "像素" in r3.hint, r3.hint if r3 else "")
    check("提示给了实测阈值",
          r3 is not None and "10×1024×1024" in r3.hint, "")

    body2 = b'{"kind":"InvalidImageResolutionError","description":"image resolution is too low"}'
    r4 = CAP.classify(400, "application/json", len(body2), body2, did)
    check("D-ID 分辨率过低被认出来",
          r4 is not None and "分辨率" in r4.label, r4.label if r4 else "")

    r5 = CAP.classify(451, "application/json", 10, b'{"kind":"ImageModerationError"}', did)
    check("D-ID 451 审核拦截被认出来（撞车 451 而不是当 4xx）",
          r5 is not None and "审核" in r5.label, r5.label if r5 else "")

    fal_entry = resolve(config, "avatar:avatar-fal-ai-omnihuman-1-5")
    fal = fal_entry["cfg"] if fal_entry else PRESETS["fal_omnihuman"]["config"]
    locked = b'{"detail":"user is locked. reason: exhausted balance."}'
    r6 = CAP.classify(403, "application/json", len(locked), locked, fal)
    check("fal.ai 的 403 余额耗尽不再误报成认证失败",
          r6 is not None and "余额耗尽" in r6.label,
          r6.label if r6 else "未命中")
    check("body_has 匹配不受厂商大小写变化影响",
          r6 is not None and r6.matched == "fal_balance_exhausted",
          r6.matched if r6 else "未命中")

    dashscope = PRESETS["dashscope_s2v"]["config"]
    unpurchased = b'{"code":"AccessDenied.Unpurchased","message":"Access to model denied"}'
    r7 = CAP.classify(403, "application/json", len(unpurchased), unpurchased, dashscope)
    check("百炼未开通或欠费停服能给出明确原因",
          r7 is not None and r7.matched == "dashscope_unpurchased",
          r7.label if r7 else "未命中")

    # 通用兜底
    check("401 判为 fatal",
          CAP.classify(401, "", 0, b"", did).kind == "fatal")
    check("402 判为 fatal",
          CAP.classify(402, "", 0, b"", did).kind == "fatal")
    check("429 判为 transient",
          CAP.classify(429, "", 0, b"", did).kind == "transient")
    check("503 判为 transient",
          CAP.classify(503, "", 0, b"", did).kind == "transient")
    check("普通 400 判为 rejected",
          CAP.classify(400, "", 0, b"", did).kind == "rejected")
    check("200 且无签名 -> 不下结论",
          CAP.classify(200, "audio/wav", 5000, b"RIFF", did) is None)

    # 重试决策必须听签名的
    from autovid import providers as P   # noqa: PLC0415
    exc = P.ProviderError("x", status=200, content_type="text/plain", body=b"")
    check("_worth_retrying：硅基流动空 200 不重试", not P._worth_retrying(exc, sf))
    exc2 = P.ProviderError("x", status=503, body=b"", transient=True)
    check("_worth_retrying：503 重试", P._worth_retrying(exc2, did))
    exc3 = P.ProviderError("x", status=400, body=body)
    check("_worth_retrying：D-ID 文件过大不重试", not P._worth_retrying(exc3, did))


# --------------------------------------------------------------- 5. 证据抓取
def test_evidence() -> None:
    print("\n[5] 证据抓取（trace-id）")
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "x-siliconcloud-trace-id": "ti_2mbzarxowekopm6cs2",
        "X-Request-Id": "req_abc",
        "Date": "Sat, 13 Sep 2026 04:00:00 GMT",
    }
    traces = CAP.trace_ids(headers)
    check("抓到硅基流动的 trace-id",
          traces.get("x-siliconcloud-trace-id") == "ti_2mbzarxowekopm6cs2", str(traces))
    check("抓到通用 request-id", traces.get("x-request-id") == "req_abc")
    check("不把无关头抓进来", "date" not in traces)

    line = CAP.describe_evidence(200, "text/plain", 0, 0.18, headers)
    check("一行说全状态/类型/字节/耗时/trace",
          all(k in line for k in ("HTTP 200", "text/plain", "0 字节", "0.18s", "ti_")),
          line)
    check("没有响应头也不炸", "HTTP 500" in CAP.describe_evidence(500, "", 0, None, None))


# ------------------------------------------------- 6. 语音失败预算（真跑一遍）
def test_tts_budget() -> None:
    """复刻真机：一个只回「HTTP 200 + text/plain + 0 字节」的语音接口。

    改造前的行为：磕 12 次、退避最长 30 秒，对着一个已明确拒收的接口
    耗掉两分半钟（这是实测发生过的）。
    改造后应该：连着被拒到上限就承认这家不可用、立刻交回退链，
    并且报错里带签名和 trace-id。
    """
    print("\n[6] 语音失败预算：不硬磕，快速交给下一家")
    import json as _json
    import socket as _socket
    import threading
    import time as _time
    import urllib.request
    import wave
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state = {"calls": 0, "fail_until": 10 ** 9, "trace": "ti_sf_fake_9999"}

    def wav_bytes() -> bytes:
        import io
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            # 非静音：_audio_sane 要求 RMS >= 0.005
            frames = b"".join(int(8000 * ((i // 40) % 2 - 0.5) * 2).to_bytes(
                2, "little", signed=True) for i in range(24000))
            w.writeframes(frames)
        return buf.getvalue()

    good = wav_bytes()

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            size = int(self.headers.get("Content-Length") or 0)
            if size:
                self.rfile.read(size)
            state["calls"] += 1
            if state["calls"] <= state["fail_until"]:
                # 复刻实测：200 + text/plain + 0 字节 + 秒回 + 带 trace-id
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", "0")
                self.send_header("x-siliconcloud-trace-id", state["trace"])
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(good)))
            self.end_headers()
            self.wfile.write(good)

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = int(server.server_address[1])
    threading.Thread(target=server.serve_forever, daemon=True).start()

    from autovid import providers as P   # noqa: PLC0415
    config = Config.load(root=ROOT)
    work = ROOT / ".tmp" / "smoke_caps_tts"
    if work.exists():
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    cfg = {
        "url": f"http://127.0.0.1:{port}/v1/audio/speech",
        "method": "POST",
        "headers": {"Authorization": "Bearer {{api_key}}"},
        "body": {"model": "{{model}}", "input": "{{text}}", "voice": "{{voice_id}}"},
        "model": "fake", "voice_id": "fake-voice",
        "audio_encoding": "raw", "audio_path": "",
        "request_delay_s": 0,
        "retries": 12,
        "reject_streak_limit": 3,
        "reject_budget_s": 0,
        "failure_signatures": [{
            "name": "sf_empty_200",
            "when": {"status": 200, "ctype_has": "text/plain", "max_bytes": 0},
            "kind": "rejected",
            "label": "硅基流动假装成功：HTTP 200 + text/plain + 0 字节",
            "hint": "已记 trace-id，请换一家语音厂商。",
        }],
    }
    segments = [{"text": "测试一下这句话", "index": 0}]

    try:
        # --- A. 一直坏：应该在 streak 上限处就停，而不是磕满 12 次 ---
        state["calls"] = 0
        state["fail_until"] = 10 ** 9
        try:
            P._tts_cloud(config, segments, work / "a", lambda *_: None,
                         voice_asset=None, cfg=cfg, api_key="fake",
                         provider_name="voice:fake")
            check("一直坏时应当失败", False, "竟然成功了")
        except P.ProviderError as exc:
            text = str(exc)
            check("在预算处就放弃，没有磕满 12 次", state["calls"] == 3,
                  f"实际调用了 {state['calls']} 次")
            check("判为这家当前不可用", "判定这家当前不可用" in text,
                  text.splitlines()[0][:70])
            check("报错点出签名", "假装成功" in text)
            check("报错带上 trace-id", "ti_sf_fake_9999" in text,
                  next((l for l in text.splitlines() if "证据" in l), "无")[:80])
            check("明确说会交给下一家", "下一家" in text)
            check("标记为不可重试（transient=False）", exc.transient is False)

        # --- B. 前面坏几次、后面好：应该重试到成功 ---
        state["calls"] = 0
        state["fail_until"] = 2
        result = P._tts_cloud(config, segments, work / "b", lambda *_: None,
                              voice_asset=None, cfg=cfg, api_key="fake",
                              provider_name="voice:fake")
        check("抖动几次后能成功", state["calls"] == 3, f"调用了 {state['calls']} 次")
        check("产出了音频文件",
              bool(getattr(result, "parts", None)) and Path(result.parts[0]).exists(),
              str(getattr(result, "parts", None)))
    finally:
        server.shutdown()
        server.server_close()
        import shutil
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------- 7. 解耦护栏（静态）
def test_decoupling() -> None:
    """目标第 (1) 条：语音侧和数字人侧必须互不认识。

    这是**静态护栏**，不是文档口号：编排层里只要出现「语音厂商名」和
    「数字人厂商名」互相串门，就说明有人偷偷把两家绑在一起了。
    绑定一旦发生，「任意一家配任意一家」就变成了一句空话。
    """
    print("\n[7] 解耦护栏：两侧不许互相认识（静态检查）")
    import ast

    voice_vendors = {"siliconflow", "cosyvoice", "qwen3", "qwen_tts", "edge_native",
                     "sapi", "elevenlabs", "minimax_tts", "fish_audio", "dashscope",
                     "volcengine", "cloud_tts"}
    avatar_vendors = {"d_id", "d-id", "heygen", "wav2lip", "minimax_h3", "fal",
                      "replicate", "comfy", "http_job"}

    src = (ROOT / "autovid" / "graph.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    def literals(node: ast.AST) -> set[str]:
        found: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                found.add(sub.value.lower())
            elif isinstance(sub, ast.Attribute):
                found.add(sub.attr.lower())
        return found

    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    avatar_lits = literals(funcs["node_avatar"])
    tts_lits = literals(funcs["node_tts"])
    compose_lits = literals(funcs["node_compose"])

    hit_v = sorted(t for t in voice_vendors if any(t in s for s in avatar_lits))
    check("数字人节点里没有语音厂商的名字", not hit_v, str(hit_v))

    hit_a = sorted(t for t in avatar_vendors if any(t in s for s in tts_lits))
    check("语音节点里没有数字人厂商的名字", not hit_a, str(hit_a))

    hit_c = sorted(t for t in (voice_vendors | avatar_vendors)
                   if any(t in s for s in compose_lits))
    check("合成节点不认识任何具体厂商", not hit_c, str(hit_c))

    # 两侧之间的数据契约：只传音频文件路径，不传厂商身份
    avatar_src = ast.get_source_segment(src, funcs["node_avatar"]) or ""
    check("数字人只从语音阶段取音频路径",
          'state["voice"]' in avatar_src and '"segments"' in avatar_src)
    check("数字人没有从语音阶段取 provider 名字",
          '["provider"]' not in avatar_src.split('state["voice"]')[1][:200]
          if 'state["voice"]' in avatar_src else False)

    # providers 层：数字人入口不该出现语音厂商分支
    psrc = (ROOT / "autovid" / "providers.py").read_text(encoding="utf-8")
    ptree = ast.parse(psrc)
    pfuncs = {n.name: n for n in ast.walk(ptree) if isinstance(n, ast.FunctionDef)}
    render_lits = literals(pfuncs["avatar_render"])
    hit_r = sorted(t for t in voice_vendors if any(t in s for s in render_lits))
    check("avatar_render 里没有语音厂商分支", not hit_r, str(hit_r))

    # 数字人入口的参数里不许有音色相关的概念
    args = [a.arg for a in pfuncs["avatar_render"].args.args]
    check("avatar_render 的参数里没有 voice_id / voice_asset",
          not any("voice" in a for a in args), str(args))


# ------------------------------------- 8. 预设能力覆盖与「未实测」标注
def test_preset_coverage() -> None:
    """每一个预设都必须说清楚自己支持什么，或者明确说「我不知道」。

    留空最危险：数据上「0 = 没查过」和「0 = 确认无限制」长得一模一样，
    而含义相反。混淆这两者正是静默失败的温床。
    """
    print("\n[8] 预设能力覆盖：不许有「沉默的未知」")
    from autovid.providers_registry import PRESETS, capability_note, preset_kind

    check("预设数量只增不减", len(PRESETS) >= 14, str(len(PRESETS)))
    check("包含百炼自然讲解预设", "dashscope_s2v" in PRESETS)

    missing_kind = [n for n in PRESETS if preset_kind(n) not in ("voice", "avatar")]
    check("每个预设都标了用途（语音/数字人）", not missing_kind, str(missing_kind))

    no_caps = [n for n in PRESETS if not (PRESETS[n].get("config") or {}).get("capabilities")]
    check("每个预设都有 capabilities 块", not no_caps, str(no_caps))

    no_output = [n for n in PRESETS
                 if not (PRESETS[n]["config"]["capabilities"].get("output") or {}).get("note")
                 and not (PRESETS[n]["config"]["capabilities"].get("output") or {}).get("width")]
    check("每个预设都声明了输出（或写明不知道）", not no_output, str(no_output))

    silent = [n for n in PRESETS if not capability_note(n)]
    check("每个预设都能给出一句能力摘要", not silent, str(silent))

    # 我们自己实测过的两家必须标 verified=True，不能和没验过的混在一起
    d_id = PRESETS["d_id"]["config"]["capabilities"]
    check("D-ID 标为已实测（输出规格是我们量的）", d_id.get("verified") is True)
    check("D-ID 的来源说明区分了实测与 spec",
          "实测" in str(d_id.get("source")) and "spec" in str(d_id.get("source")),
          str(d_id.get("source"))[:60])

    sf = PRESETS["siliconflow"]["config"]["capabilities"]
    check("硅基流动标为已实测", sf.get("verified") is True)

    heygen = PRESETS["heygen"]["config"]["capabilities"]
    check("HeyGen 标为未实测（只有文档）", heygen.get("verified") is False)
    check("HeyGen 声明了原生 1080×1920（这正是 D-ID 做不到的）",
          (heygen["output"]["width"], heygen["output"]["height"]) == (1080, 1920),
          str(heygen["output"]))
    check("未实测的会在摘要里显式标注",
          "未实测" in capability_note("heygen"), capability_note("heygen")[:60])

    unverified = [n for n in PRESETS
                  if not (PRESETS[n]["config"]["capabilities"].get("verified"))
                  and preset_kind(n) == "avatar"
                  and "尚未核实" not in capability_note(n)
                  and "未实测" not in capability_note(n)]
    check("没标已实测的数字人厂商都会显示「未实测」", not unverified, str(unverified))


# ------------------------------------- 9. 声明的音频上限真的会约束行为
def test_declared_audio_limit_drives_behavior() -> None:
    print("\n[9] 声明的音频上限必须真的约束行为（不能只写不做）")
    import shutil
    import wave

    from autovid import providers as P  # noqa: PLC0415

    work = ROOT / ".tmp" / "smoke_caps_limit"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    segs = []
    for i in range(4):
        path = work / f"s{i}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x01" * 16000 * 20)     # 每段 20s
        segs.append({"audio_path": str(path), "index": i})

    # 厂商只收 45s，而我们自己的 merge_max_s 写的是 150s
    cfg = {"merge_max_s": 150, "capabilities": {"audio": {"max_s": 45}}}
    jobs = P._merge_avatar_jobs(segs, cfg, work, lambda *_: None)
    durs = [P._safe_wav_duration(j["audio_path"]) for j in jobs]
    check("厂商的 45s 上限压过了我们自己的 150s",
          all(d <= 45.0001 for d in durs), str([round(d, 1) for d in durs]))
    check("于是 4×20s 被拆成 2 个任务（20+20 / 20+20，都 ≤45s）",
          len(jobs) == 2, f"{len(jobs)} 个，时长 {[round(d, 1) for d in durs]}")
    check("总时长没有丢", abs(sum(durs) - 80.0) < 0.05, f"{sum(durs):.2f}s")

    # 单句本身就超上限 -> 必须明确报错，而不是硬提交
    cfg2 = {"merge_max_s": 150, "capabilities": {"audio": {"max_s": 10}}}
    try:
        P._merge_avatar_jobs(segs, cfg2, work, lambda *_: None)
        check("单句超上限时应当报错", False, "竟然没报错")
    except P.ProviderError as exc:
        check("单句超上限时明确报错（合并救不了）",
              "超过该厂商的上限" in str(exc), str(exc).splitlines()[0][:70])

    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    test_capabilities()
    test_image_plan()
    test_canvas()
    test_signatures()
    test_evidence()
    test_tts_budget()
    test_decoupling()
    test_preset_coverage()
    test_declared_audio_limit_drives_behavior()
    print()
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败：{FAILS}")
        sys.exit(1)
    print("✓ 全部通过 —— 能力声明与失败签名层正常")
