"""命令行入口。

    python -m autovid run --topic "为什么你越努力越焦虑"
    python -m autovid run --script-file 我的文案.txt
    python -m autovid show --run latest
    python -m autovid rerun --run latest --only compose,subtitles
    python -m autovid providers
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from . import __version__
from .config import Config
from .manifest import RunContext, list_runs, purge_runs
from .pipeline import STEP_NAMES, Runner, get_step_def


def _force_utf8_output() -> None:
    """Windows 控制台默认是 GBK，输出里含 ✓/⚠ 之类字符会直接抛异常。

    这里把编码保持为控制台原有编码，只把错误处理改成 replace，
    保证任何字符都不会让 CLI 崩掉。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _parse_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]


def _validate_steps(names: list[str], flag: str) -> None:
    for name in names:
        if name == "all":
            continue
        if name not in STEP_NAMES:
            raise SystemExit(f"{flag} 里的 '{name}' 不是有效步骤。可用：{', '.join(STEP_NAMES)}")


def _apply_inputs(ctx: RunContext, args: argparse.Namespace) -> None:
    """把 CLI 输入写进 manifest，让它们参与 input_hash（改选题会自动触发重跑）。"""
    inputs = ctx.manifest.setdefault("inputs", {})
    topic = (getattr(args, "topic", None) or "").strip()
    if topic:
        inputs["topic"] = topic
    script_file = (getattr(args, "script_file", None) or "").strip()
    if script_file:
        path = Path(script_file)
        if not path.exists():
            raise SystemExit(f"找不到文案文件：{path}")
        inputs["script_file"] = str(path.resolve())
        inputs["script_hash"] = hashlib.sha256(path.read_bytes()).hexdigest()
    ctx.save()


def _resolve_run(config: Config, run_id: str | None) -> RunContext:
    if not run_id or run_id == "latest":
        return RunContext.open_latest(config)
    direct = config.path(config.get("project.out_dir", "runs")) / run_id
    if direct.exists():
        return RunContext.open(direct, config)
    # 允许用前缀匹配
    matches = [r for r in list_runs(config) if r["run_id"].startswith(run_id)]
    if len(matches) == 1:
        return RunContext.open(matches[0]["dir"], config)
    if len(matches) > 1:
        raise SystemExit(f"'{run_id}' 匹配到多个运行：{[m['run_id'] for m in matches]}")
    raise SystemExit(f"找不到运行 '{run_id}'")


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_run(args: argparse.Namespace, config: Config) -> int:
    slug = (args.topic or (Path(args.script_file).stem if args.script_file else "run"))
    # 音色/形象资产按次指定，覆盖进配置（因此会参与 input_hash，换资产自动失效）
    overrides: dict[str, str] = {}
    if getattr(args, "voice_id", None):
        overrides["project.voice_id"] = args.voice_id
    if getattr(args, "avatar_id", None):
        overrides["project.avatar_id"] = args.avatar_id
    if overrides:
        config = config.with_overrides(overrides)
    # dry-run 不落盘、不抢 .latest 指针，保证「只看计划」没有副作用
    ctx = RunContext.create(config, slug=slug, run_id=args.run_id, persist=not args.dry_run)
    if not args.dry_run:
        _apply_inputs(ctx, args)
        print(f"运行目录：{ctx.dir}")

    only = _parse_list(args.only)
    force = _parse_list(args.force)
    _validate_steps(only, "--only")
    _validate_steps(force, "--force")

    ok = Runner(ctx).run(
        only=only, force=force, from_step=args.from_step, to_step=args.to_step,
        dry_run=args.dry_run, keep_going=args.keep_going,
    )
    if not args.dry_run:
        print()
        print(ctx.summary())
        print(f"\n成片：{ctx.artifact_path('video')}")
    return 0 if ok else 1


def cmd_resume(args: argparse.Namespace, config: Config) -> int:
    ctx = _resolve_run(config, args.run)
    print(f"继续运行：{ctx.dir}")
    ok = Runner(ctx).run(
        only=_parse_list(args.only), force=_parse_list(args.force),
        from_step=args.from_step, to_step=args.to_step,
        dry_run=args.dry_run, keep_going=args.keep_going,
    )
    return 0 if ok else 1


def cmd_rerun(args: argparse.Namespace, config: Config) -> int:
    ctx = _resolve_run(config, args.run)
    only = _parse_list(args.only) or list(STEP_NAMES[-3:])
    _validate_steps(only, "--only")
    print(f"局部重跑：{ctx.dir}  步骤={only}")
    ok = Runner(ctx).run(only=only, force=["all"], keep_going=args.keep_going)
    return 0 if ok else 1


def cmd_step(args: argparse.Namespace, config: Config) -> int:
    """只跑某一步。若该步所需上游产物不在，走正常依赖报错。"""
    ctx = _resolve_run(config, args.run)
    target = get_step_def(args.step)
    kind = "强制重跑" if args.force else "按需执行"
    print(f"单步运行：{target.name}（{target.title}）[{kind}]")
    ok = Runner(ctx).run(only=[target.name], force=["all"] if args.force else [])
    return 0 if ok else 1


def cmd_runs(args: argparse.Namespace, config: Config) -> int:
    runs = list_runs(config)
    if not runs:
        print("还没有任何运行记录。先执行：python -m autovid run --topic \"...\"")
        return 0
    print(f"{'RUN ID':<34} {'创建时间':<26} {'步骤':>4} {'成功':>4} {'失败':>4}")
    print("-" * 78)
    for item in runs:
        print(f"{item['run_id']:<34} {item['created']:<26} {item['steps']:>4} "
              f"{item['ok']:>4} {item['failed']:>4}")
    return 0


def cmd_show(args: argparse.Namespace, config: Config) -> int:
    ctx = _resolve_run(config, args.run)
    print(ctx.summary())
    inputs = ctx.manifest.get("inputs") or {}
    if inputs:
        print("\n输入参数：")
        for key, value in inputs.items():
            shown = str(value)
            print(f"  {key}: {shown if len(shown) < 100 else shown[:97] + '...'}")
    return 0


def cmd_providers(args: argparse.Namespace, config: Config) -> int:
    from .providers import provider_statuses

    print("Provider 可用性\n")
    current = {name: str(config.provider_of(name)) for name in
               ("topic", "script", "voice", "visuals", "avatar", "subtitles", "compose",
                "metadata", "publish")}
    print("当前配置：")
    for step, provider in current.items():
        print(f"  {step:<10} -> {provider}")
    print()
    kind_width = 8
    print(f"{'类型':<{kind_width}} {'名称':<18} {'可用':<6} 说明")
    print("-" * 90)
    for status in provider_statuses(config):
        mark = "是" if status.available else "否"
        print(f"{status.kind:<{kind_width}} {status.name:<18} {mark:<6} {status.detail}")
    print("\n提示：标注 [未实测] 的云 provider 在本机开发环境无法联网验证，"
          "首次使用请单步调试。")
    return 0


def cmd_purge(args: argparse.Namespace, config: Config) -> int:
    removed = purge_runs(config, keep=args.keep)
    if not removed:
        print(f"没有需要清理的运行（保留最近 {args.keep} 次）")
        return 0
    for path in removed:
        print(f"已删除 {path}")
    return 0


def cmd_graph(args: argparse.Namespace, config: Config) -> int:
    """LangGraph 编排：start -> 前置判断 -> 音色克隆 -> ... -> end。"""
    import json

    from .graph import DEFAULT_GATES, VideoFlow, parse_decision

    if args.action == "state":
        snapshot = VideoFlow(config, log=lambda _m: None).state_of(args.thread)
        print(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))
        return 0

    gates = () if getattr(args, "auto", False) else tuple(
        _parse_list(getattr(args, "gates", None)) or DEFAULT_GATES)

    overrides: dict[str, str] = {}
    if getattr(args, "voice_id", None):
        overrides["project.voice_id"] = args.voice_id
    if getattr(args, "avatar_id", None):
        overrides["project.avatar_id"] = args.avatar_id
    if overrides:
        config = config.with_overrides(overrides)

    flow = VideoFlow(config, gates=gates, log=print)
    flow.checkpointer()          # 先初始化，否则下面打印出来是 unknown
    print(f"检查点  ：{flow.checkpointer_kind}（sqlite 可跨进程恢复）")
    print(f"人工闸门：{'、'.join(gates) if gates else '全部关闭'}")

    if args.action == "run":
        slug = args.topic or (Path(args.script_file).stem if args.script_file else "flow")
        run_dir = VideoFlow.new_run_dir(config, slug)
        inputs: dict[str, object] = {
            "run_id": run_dir.name,
            "run_dir": str(run_dir),
            "topic": (args.topic or "").strip(),
            "script_text": "",
            "script_file": "",
            "voice_id": str(config.get("project.voice_id", "") or ""),
            "avatar_id": str(config.get("project.avatar_id", "") or ""),
            "scene_photo": str(Path(args.scene_photo).resolve()) if args.scene_photo else "",
            "revision": 0,
            "approvals": {},
            "trace": [],
        }
        if args.script_file:
            path = Path(args.script_file)
            if not path.exists():
                raise SystemExit(f"找不到文案文件：{path}")
            inputs["script_file"] = str(path.resolve())
        print(f"运行目录：{run_dir}\n")
        outcome = flow.run(inputs, thread_id=getattr(args, "thread", None)
                           or f"flow-{run_dir.name}")
    else:  # resume
        snapshot = VideoFlow(config, log=lambda _m: None).state_of(args.thread)
        values = snapshot.get("values") or {}
        if not values.get("run_dir"):
            raise SystemExit(
                f"线程 {args.thread} 没有可恢复的状态。\n"
                "  确认 --thread 是否正确，或该线程是否已经跑完。")
        run_dir = Path(str(values["run_dir"]))
        print(f"恢复线程：{args.thread}\n运行目录：{run_dir}\n"
              f"本次决定：{parse_decision(args.decision)}\n")
        outcome = flow.resume(parse_decision(args.decision), thread_id=args.thread)

    while True:
        thread = outcome["thread_id"]
        status = outcome["status"]

        if status == "finished":
            print("\n✓ 图执行完成")
            print(f"  成片    ：{outcome.get('video')}")
            print(f"  时长    ：{outcome.get('duration_s')}s")
            print(f"  发布包  ：{outcome.get('publish')}")
            print(f"  运行目录：{run_dir}")
            return 0

        if status == "failed":
            print("\n" + str(outcome.get("failure")))
            return 1

        payload = outcome.get("payload") or {}
        title = payload.get("title") or payload.get("gate") or "人工闸门"
        print(f"\n{'=' * 62}\n⏸  停在「{title}」\n{'=' * 62}")
        for key, value in payload.items():
            if key in ("gate", "title"):
                continue
            if isinstance(value, (list, dict)):
                body = json.dumps(value, ensure_ascii=False, indent=2)[:800]
                print(f"  {key}:\n    " + body.replace("\n", "\n    "))
            else:
                print(f"  {key}: {value}")

        if getattr(args, "interactive", False):
            answer = input("\n通过吗？[y=通过 / n=打回 / 回车=通过] ").strip().lower()
            outcome = flow.resume(
                {"action": "reject" if answer.startswith("n") else "approve"},
                thread_id=thread)
            continue

        print("\n继续执行：")
        print(f"  通过：python -m autovid graph resume --thread {thread} --decision approve")
        print(f"  打回：python -m autovid graph resume --thread {thread} --decision reject")
        return 3


def cmd_web(args: argparse.Namespace, config: Config) -> int:
    """启动本地 Web 工作台（零依赖，只用标准库）。"""
    from .web import serve

    serve(config, host=args.host, port=args.port, open_browser=args.open)
    return 0


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def _add_run_controls(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--only", help="只跑这些步骤，逗号分隔")
    parser.add_argument("--from", dest="from_step", help="从该步骤开始")
    parser.add_argument("--to", dest="to_step", help="到该步骤结束")
    parser.add_argument("--force", help="强制重跑（步骤名或 all）")
    parser.add_argument("--dry-run", action="store_true", help="只显示计划不执行")
    parser.add_argument("--keep-going", action="store_true", help="某步失败也继续后面的步骤")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autovid",
        description="短视频口播数字人自动化流水线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="步骤顺序：" + " -> ".join(STEP_NAMES),
    )
    parser.add_argument("--version", action="version", version=f"autovid {__version__}")
    parser.add_argument("--root", default=".", help="项目根目录（默认当前目录）")
    parser.add_argument("--config", help="配置文件路径（默认 config/pipeline.json）")
    parser.add_argument("--secrets", help="密钥文件路径（默认 config/secrets.json）")

    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="新建一次运行并执行")
    p_run.add_argument("--topic", help="指定选题")
    p_run.add_argument("--script-file", help="已有文案文件（会切段；配合 LLM provider 则做改写）")
    p_run.add_argument("--voice-id", help="使用资产库里的音色（assets/voices/<id>）")
    p_run.add_argument("--avatar-id", help="使用资产库里的形象（assets/avatars/<id>）")
    p_run.add_argument("--run-id", help="自定义运行目录名")
    _add_run_controls(p_run)
    p_run.set_defaults(func=cmd_run)

    p_resume = sub.add_parser("resume", help="继续某次运行（默认 latest）")
    p_resume.add_argument("--run", default="latest")
    _add_run_controls(p_resume)
    p_resume.set_defaults(func=cmd_resume)

    p_rerun = sub.add_parser("rerun", help="强制重跑指定步骤")
    p_rerun.add_argument("--run", default="latest")
    p_rerun.add_argument("--only", help="要重跑的步骤，逗号分隔")
    p_rerun.add_argument("--keep-going", action="store_true")
    p_rerun.set_defaults(func=cmd_rerun)

    p_step = sub.add_parser("step", help="运行单个步骤")
    p_step.add_argument("step", choices=STEP_NAMES)
    p_step.add_argument("--run", default="latest")
    p_step.add_argument("--force", action="store_true", help="即使输入未变也重跑")
    p_step.set_defaults(func=cmd_step)

    p_runs = sub.add_parser("runs", help="列出所有运行")
    p_runs.set_defaults(func=cmd_runs)

    # ---- LangGraph 编排 ----
    p_graph = sub.add_parser(
        "graph", help="LangGraph 编排：start -> 前置判断 -> 音色克隆 -> ... -> end")
    gsub = p_graph.add_subparsers(dest="action", required=True)

    g_run = gsub.add_parser("run", help="按图执行（默认一路跑完）")
    g_run.add_argument("--topic", help="选题（不给就得给 --script-file）")
    g_run.add_argument("--script-file", help="自有文案文件")
    g_run.add_argument("--voice-id", help="音色资产 ID")
    g_run.add_argument("--avatar-id", help="形象资产 ID")
    g_run.add_argument("--scene-photo", help="本次拍摄场景照；自然动作模式建议腰部以上、双手入镜")
    g_run.add_argument("--thread", help="自定义线程 ID")
    g_run.add_argument("--gates", help="启用人审闸门，如 script,voice（默认关闭）")
    g_run.add_argument("--auto", action="store_true", help="显式关闭全部闸门")
    g_run.add_argument("--interactive", action="store_true", help="在终端里交互式审批")
    g_run.set_defaults(func=cmd_graph)

    g_resume = gsub.add_parser("resume", help="从闸门处恢复执行")
    g_resume.add_argument("--thread", required=True)
    g_resume.add_argument("--decision", default="approve",
                          help="approve / reject，或 script=reject,voice=approve")
    g_resume.add_argument("--gates", help="闸门配置需与首次运行时一致")
    g_resume.add_argument("--auto", action="store_true")
    g_resume.add_argument("--interactive", action="store_true")
    g_resume.set_defaults(func=cmd_graph)

    g_state = gsub.add_parser("state", help="查看某个线程的状态快照")
    g_state.add_argument("--thread", required=True)
    g_state.set_defaults(func=cmd_graph)

    p_show = sub.add_parser("show", help="查看某次运行的清单")
    p_show.add_argument("--run", default="latest")
    p_show.set_defaults(func=cmd_show)

    p_prov = sub.add_parser("providers", help="查看各 provider 可用性")
    p_prov.set_defaults(func=cmd_providers)

    p_purge = sub.add_parser("purge", help="清理旧的运行目录")
    p_purge.add_argument("--keep", type=int, default=5)
    p_purge.set_defaults(func=cmd_purge)

    p_web = sub.add_parser("web", help="启动本地 Web 工作台（推荐）")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8899)
    p_web.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    p_web.set_defaults(func=cmd_web)

    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Config.load(root=args.root, config_path=args.config, secrets_path=args.secrets)
    try:
        return int(args.func(args, config))
    except KeyboardInterrupt:
        print("\n已中断。用 `python -m autovid resume` 继续（已完成的步骤不会重跑）。")
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"\n执行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
