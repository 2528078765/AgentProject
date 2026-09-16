"""烟雾测试：网络抖动的重试与画布适配。

覆盖两件刚修的事故：
  1. D-ID 轮询时 SSL 断流（UNEXPECTED_EOF_WHILE_READING）不该终结整条工作流；
  2. 云端数字人回 512×512 方片，拼接前必须适配到 1080×1920。

第 2 项会真的调 ffmpeg 生成一个 512×512 片段再做适配，所以是真实端到端验证，
不是 mock。
"""
from __future__ import annotations

import socket
import ssl
import subprocess
import sys
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autovid import media as M            # noqa: E402
from autovid import providers as P        # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


# --------------------------------------------------------------------- 1. 分类
def test_classification() -> None:
    print("[1] 异常分类：可重试 vs 不可重试")
    check("SSL 断流算可重试",
          P._is_transient(ssl.SSLError("UNEXPECTED_EOF_WHILE_READING")))
    check("socket 超时算可重试", P._is_transient(socket.timeout("timed out")))
    check("HTTP 429 算可重试", P._is_transient(P.ProviderError("限流", transient=True)))
    check("HTTP 400 不算可重试", not P._is_transient(P.ProviderError("参数错")))
    check("业务报错不算可重试", not P._is_transient(P.ProviderError("任务失败", transient=False)))
    check("ProviderError 默认不可重试", not P.ProviderError("x").transient)


# --------------------------------------------------------------------- 2. 重试
def test_retry() -> None:
    print("[2] _retry：抖动后成功 / 立刻放弃 / 耗尽抛出")
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise P.ProviderError("断流", transient=True)
        return "ok"

    got = P._retry(flaky, what="测试", log=lambda *_: None,
                   attempts=5, base_delay=0.01, max_delay=0.02)
    check("第 3 次成功返回", got == "ok" and calls["n"] == 3, f"调用 {calls['n']} 次")

    hard = {"n": 0}

    def hard_fail():
        hard["n"] += 1
        raise P.ProviderError("Key 无效")

    try:
        P._retry(hard_fail, what="测试", log=lambda *_: None, attempts=5, base_delay=0.01)
        check("不可重试错误立刻抛出", False)
    except P.ProviderError:
        check("不可重试错误立刻抛出", hard["n"] == 1, f"只调了 {hard['n']} 次")

    tried = {"n": 0}

    def always():
        tried["n"] += 1
        raise P.ProviderError("一直断", transient=True)

    try:
        P._retry(always, what="测试", log=lambda *_: None,
                 attempts=4, base_delay=0.01, max_delay=0.02)
        check("耗尽后抛出", False)
    except P.ProviderError:
        check("耗尽后抛出", tried["n"] == 4, f"共 {tried['n']} 次")


# ----------------------------------------------------------------- 3. 画布适配
def test_canvas() -> None:
    print("[3] 画布适配：512×512 -> 1080×1920")
    # 用工作区里的临时目录：系统 Temp 在沙箱里不可写
    work = ROOT / ".tmp" / "smoke_retry"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    src = work / "square.mp4"
    subprocess.run(
        [M.ffmpeg_exe(), "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc=size=512x512:rate=30:duration=1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True)
    check("源片段是 512×512", M.probe_video_size(src) == (512, 512),
          str(M.probe_video_size(src)))

    out = work / "fitted.mp4"
    got = M.fit_to_canvas(src, out, 1080, 1920, 30, cwd=work)
    check("输出是 1080×1920", M.probe_video_size(got) == (1080, 1920),
          str(M.probe_video_size(got)))
    check("时长没被改变", abs(M.probe_duration(got) - 1.0) < 0.35,
          f"{M.probe_duration(got):.2f}s")

    # 已经是目标尺寸时不该重编码，应原样返回
    same = M.fit_to_canvas(got, work / "again.mp4", 1080, 1920, 30, cwd=work)
    check("已适配的片段原样返回（不重编码）", same == got, str(same.name))
    check("也没有多生成文件", not (work / "again.mp4").exists())

    # 两个不同尺寸的片段必须能拼起来（这就是事故现场）
    square2 = work / "square2.mp4"
    subprocess.run(
        [M.ffmpeg_exe(), "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=size=512x512:rate=30:duration=1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(square2)],
        check=True, capture_output=True)
    a = M.fit_to_canvas(src, work / "a.mp4", 1080, 1920, 30, cwd=work)
    b = M.fit_to_canvas(square2, work / "b.mp4", 1080, 1920, 30, cwd=work)
    joined = M.concat_clips([a, b], work / "join.mp4", cwd=work)
    check("适配后能流拷贝拼接", M.probe_video_size(joined) == (1080, 1920),
          str(M.probe_video_size(joined)))
    check("拼接后时长约 2s", abs(M.probe_duration(joined) - 2.0) < 0.5,
          f"{M.probe_duration(joined):.2f}s")


# ------------------------------------------------------------- 4. 云端任务合并
def test_merge() -> None:
    print("[4] 云端数字人任务合并：少提交但一秒不差")
    work = ROOT / ".tmp" / "smoke_retry" / "merge"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    # 造 5 段各 8s 的 wav（16k/16bit/单声道）
    import wave
    segs = []
    for i in range(5):
        path = work / f"seg_{i:02d}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x01" * 16000 * 8)
        # 故意用 audio_file 命名：voice.json 里就是这个名字，必须也认
        segs.append({"audio_file": str(path), "index": i, "duration": 8.0})

    total_in = sum(M.wav_duration(s["audio_file"]) for s in segs)
    check("原始总时长 40s", abs(total_in - 40.0) < 0.05, f"{total_in:.2f}s")

    # 每段是原子的 8s，cap=20 时装得下 2 段（16s），装不下 3 段（24s）
    # 所以是 16 + 16 + 8 三个任务 —— 贪心装箱在这个粒度下已经是最优
    for cap, want in ((0, 5), (20, 3), (100, 1)):
        jobs = P._merge_avatar_jobs(segs, {"merge_max_s": cap}, work, lambda *_: None)
        total_out = sum(M.wav_duration(j["audio_path"]) for j in jobs)
        check(f"cap={cap} -> {want} 个任务", len(jobs) == want, f"实际 {len(jobs)}")
        check(f"cap={cap} 时长守住了", abs(total_out - total_in) < 0.05,
              f"{total_out:.2f}s vs {total_in:.2f}s")
        check(f"cap={cap} 每个任务都有 audio_path",
              all(j.get("audio_path") for j in jobs))

    # 合并出来的必须还带原来的元信息，下游才会认
    jobs = P._merge_avatar_jobs(segs, {"merge_max_s": 100}, work, lambda *_: None)
    check("合并后保留了元信息", jobs[0].get("index") == 0 and "duration" in jobs[0],
          str(sorted(jobs[0].keys())))


# ------------------------------------------------------------- 5. 严格 API
def test_strict_provider() -> None:
    print("[5] 严格 API：失败后不能换成本地或默认音色")
    calls: list[str] = []

    class StrictConfig:
        @staticmethod
        def provider_of(_step: str) -> str:
            return "test_api"

        @staticmethod
        def step_cfg(_step: str) -> dict:
            return {"strict": True, "fallback": ["test_local"]}

        @staticmethod
        def get(_key: str, default=None):
            return default

    def fail_api(*_args, **_kwargs):
        calls.append("api")
        raise P.ProviderError("厂商返回空音频")

    def local_fallback(*_args, **_kwargs):
        calls.append("local")
        raise AssertionError("严格模式不应调用本地回退")

    P._TTS_PROVIDERS["test_api"] = fail_api
    P._TTS_PROVIDERS["test_local"] = local_fallback
    try:
        P.tts_synthesize(
            StrictConfig(), [{"text": "测试"}],
            ROOT / ".tmp" / "smoke_retry" / "strict", lambda _m: None)
        check("API 失败应直接报错", False, "竟然返回成功")
    except P.ProviderError as exc:
        check("只调用 API，不调用本地回退", calls == ["api"], str(calls))
        check("错误点明 API 失败", "语音 API" in str(exc) and "厂商返回空音频" in str(exc),
              str(exc))
    finally:
        P._TTS_PROVIDERS.pop("test_api", None)
        P._TTS_PROVIDERS.pop("test_local", None)


if __name__ == "__main__":
    test_classification()
    test_retry()
    test_canvas()
    test_merge()
    test_strict_provider()
    print()
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败：{FAILS}")
        sys.exit(1)
    print("✓ 全部通过")
