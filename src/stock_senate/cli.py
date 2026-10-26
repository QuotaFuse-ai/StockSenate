"""StockSenate command line interface.

``ssenate`` is a thin, stable surface in front of the Python API. Anything that
touches an LLM, a data provider or a long-running job is imported lazily so
``ssenate --help`` stays fast and keeps working when an optional extra is not
installed.

Exit codes follow the product spec (chapter 11):
0 success · 2 bad arguments · 3 bad configuration · 4 data unavailable ·
7 rate limited · 8 partial failure.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import typer

from stock_senate.version import __version__

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_DATA = 4
EXIT_RATE_LIMIT = 7
EXIT_PARTIAL = 8

CONFIG_FILENAMES = (
    "agent_roles.toml",
    "workflow.toml",
    "output_template.toml",
    "data_source.toml",
    "llm_config.toml",
    "alert_rules.toml",
    "budget.toml",
)

app = typer.Typer(
    name="ssenate",
    help="StockSenate — 多角色投研合议系统。只做投研分析与回测推演，不输出交易指令。",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)

role_app = typer.Typer(help="角色清单、详情、校验与启停。", no_args_is_help=True)
alert_rules_app = typer.Typer(help="告警规则校验。", no_args_is_help=True)
alert_app = typer.Typer(help="告警规则查看、试跑与常驻服务。", no_args_is_help=True)
backtest_app = typer.Typer(help="回测推演。", no_args_is_help=True)
report_app = typer.Typer(help="报告读取、字段级对比与多格式导出。", no_args_is_help=True)
queue_app = typer.Typer(help="批量任务队列。", no_args_is_help=True)
mcp_app = typer.Typer(help="MCP Server（stdio / Streamable HTTP）。", no_args_is_help=True)
config_app = typer.Typer(help="配置查看、校验、初始化与迁移。", no_args_is_help=True)

app.add_typer(role_app, name="role")
app.add_typer(alert_app, name="alert")
alert_app.add_typer(alert_rules_app, name="rules")
app.add_typer(backtest_app, name="backtest")
app.add_typer(report_app, name="report")
app.add_typer(queue_app, name="queue")
app.add_typer(mcp_app, name="mcp")
app.add_typer(config_app, name="config")


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"stocksenate {__version__}")
        raise typer.Exit(EXIT_OK)


@app.callback()
def _root(
    show_version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_print_version,
        is_eager=True,
        help="打印版本号并退出。",
    ),
) -> None:
    """StockSenate — 多角色投研合议系统。只做投研分析与回测推演，不输出交易指令。"""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _fail(message: str, code: int = EXIT_USAGE) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code)


def _exit_code_for(exc: BaseException) -> int:
    """Map engine exceptions onto the documented exit codes.

    Order matters: ``RoleError``, ``NodeConfigError``, ``ConfigEditError`` and
    ``LLMResponseInvalid`` all derive from ``ValueError``, and
    ``LLMRateLimited``/``LLMConfigError`` derive from ``LLMUnavailable``.
    """
    from stock_senate.config.edit import ConfigEditError
    from stock_senate.data.providers.base import ProviderError, ProviderNotAvailable
    from stock_senate.roles.llm import LLMConfigError, LLMRateLimited, LLMResponseInvalid
    from stock_senate.roles.registry import RoleError
    from stock_senate.workflow.budget import BudgetExceeded
    from stock_senate.workflow.nodes.base import NodeConfigError

    if isinstance(exc, LLMRateLimited):
        return EXIT_RATE_LIMIT
    if isinstance(exc, (LLMConfigError, RoleError, NodeConfigError, ConfigEditError)):
        return EXIT_CONFIG
    if isinstance(exc, (LLMResponseInvalid, ProviderNotAvailable, ProviderError)):
        return EXIT_DATA
    if isinstance(exc, BudgetExceeded):
        return EXIT_PARTIAL
    if isinstance(exc, ValueError):
        return EXIT_USAGE
    return 1


@contextmanager
def _mapped_errors(prefix: str) -> Iterator[None]:
    """Translate engine exceptions into the documented exit codes.

    Unknown exceptions keep their traceback (exit 1) so defects stay visible.
    """
    try:
        yield
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - classified by contract
        code = _exit_code_for(exc)
        if code == 1:
            raise
        raise _fail(f"{prefix}：{exc}", code) from exc


def _config_dir() -> Path:
    override = os.environ.get("STOCKSENATE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    legacy = os.environ.get("TRADINGAGENTS_CONFIG_DIR") or os.environ.get("QF_COUNCIL_CONFIG_DIR")
    if legacy:
        typer.secho(
            f"提示：检测到旧环境变量，配置目录仍读 {legacy}；请迁移到 STOCKSENATE_CONFIG_DIR。",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return Path(legacy).expanduser()
    return Path.home() / ".stock_senate"


def _require(module: str, milestone: str) -> Any:
    """Import an engine module, or fail loudly with the owning milestone."""
    import importlib

    try:
        return importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise _fail(
            f"{module} 不可用：{exc}。该能力计划在 {milestone} 交付；"
            f"先执行 `pip install -e .[all]` 并检查配置。",
            EXIT_CONFIG,
        ) from exc


def _emit(payload: Any, output: str, rows: list[dict[str, Any]] | None = None) -> None:
    if output == "json":
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    if output == "csv":
        import csv
        import io

        data = rows if rows is not None else (payload if isinstance(payload, list) else [payload])
        if not data:
            return
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(data[0].keys()))
        writer.writeheader()
        writer.writerows(data)
        typer.echo(buffer.getvalue().rstrip())
        return
    if isinstance(payload, (dict, list)):
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        typer.echo(str(payload))


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


def _load_config_dir(path: Path | None) -> dict[str, Any]:
    """Read the toml files a user actually has. Missing files are not an error."""
    import toml

    directory = Path(path).expanduser() if path else _config_dir()
    merged: dict[str, Any] = {"config_dir": str(directory)}
    for name in CONFIG_FILENAMES:
        file = directory / name
        if not file.exists():
            continue
        try:
            merged[name.removesuffix(".toml")] = toml.load(file)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            raise _fail(f"配置文件解析失败 {file}: {exc}", EXIT_CONFIG) from exc
    return merged


# --------------------------------------------------------------------------- #
# top level
# --------------------------------------------------------------------------- #


@app.command()
def version() -> None:
    """打印版本号。"""
    typer.echo(f"stocksenate {__version__}")


@app.command()
def analyze(
    code: str = typer.Argument(..., help="标的代码，如 600519。"),
    date: str = typer.Option(None, "--date", "-d", help="分析日期 YYYY-MM-DD，默认今天。"),
    config: Path = typer.Option(None, "--config", "-c", help="配置目录，默认 ~/.stock_senate。"),
    output: str = typer.Option("table", "--output", "-o", help="table | json | csv"),
    enable: list[str] = typer.Option(None, "--enable", help="临时启用自定义角色，可重复。"),
    disable: list[str] = typer.Option(None, "--disable", help="临时停用角色，可重复。"),
    weight: list[str] = typer.Option(
        None, "--weight", help="角色权重，形如 custom_macro=0.6，可重复。"
    ),
    no_hitl: bool = typer.Option(False, "--no-hitl", help="跳过人工介入检查点（会在报告中留痕）。"),
) -> None:
    """运行一次完整的多角色合议分析。"""
    from stock_senate.graph.senate_graph import SenateGraph

    overrides = {"enable": enable or [], "disable": disable or [], "weights": weight or []}
    with _mapped_errors("analyze"):
        graph = SenateGraph(config=_load_config_dir(config))
        state, decision = graph.propagate(
            code, date or _today(), hitl=not no_hitl, overrides=overrides
        )
    payload = {
        "code": code,
        "date": date or _today(),
        "decision": decision,
        "state_keys": sorted(state),
    }
    _emit(
        payload,
        output,
        rows=[{"code": code, "rating": decision.get("rating"), "action": decision.get("action")}],
    )
    if (decision.get("data") or {}).get("status") == "missing":
        raise typer.Exit(EXIT_DATA)
    if decision.get("partial"):
        raise typer.Exit(EXIT_PARTIAL)


@app.command()
def performance(
    run_id: str = typer.Option(None, "--run-id", help="指定运行；缺省统计全部历史。"),
    output: str = typer.Option("table", "--output", "-o", help="table | json | csv"),
) -> None:
    """已保存决策的事后归因统计（不是回测）。"""
    engine = _require("stock_senate.report.performance", "M2")
    _emit(engine.summarize(run_id), output)


@app.command("sync-news")
def sync_news_cmd(
    since: str = typer.Option(None, "--since", help="YYYY-MM-DD，缺省 7 天前。"),
    output: str = typer.Option("table", "--output", "-o", help="table | json | csv"),
) -> None:
    """通过 TickHub-CN 拉取并过滤资讯。"""
    provider = _require("stock_senate.data.providers.tickhub", "M3")
    try:
        payload = provider.sync(since=since)
    except provider.ProviderNotAvailable as exc:
        # stockdata 是可选依赖：未安装时如实报告，不抛出裸 traceback。
        _emit({"ok": False, "since": since, "reason": str(exc)}, output)
        raise typer.Exit(EXIT_DATA) from exc
    _emit(payload, output)


# --------------------------------------------------------------------------- #
# role
# --------------------------------------------------------------------------- #


def _roles(config: Path | None = None) -> dict[str, Any]:
    return _load_config_dir(config).get("agent_roles", {}).get("roles", {})


@role_app.command("list")
def role_list(
    config: Path = typer.Option(None, "--config", "-c"),
    output: str = typer.Option("table", "--output", "-o", help="table | json | csv"),
) -> None:
    """列出全部角色及其启用状态。"""
    rows = [
        {
            "role": key,
            "display_name": (val or {}).get("display_name", ""),
            "enabled": (val or {}).get("enabled", True),
            "model": (val or {}).get("model", "default"),
        }
        for key, val in _roles(config).items()
    ]
    _emit(rows, output, rows=rows)


@role_app.command("show")
def role_show(
    name: str = typer.Argument(..., help="角色键，如 market / bull / portfolio_manager。"),
    config: Path = typer.Option(None, "--config", "-c"),
    output: str = typer.Option("table", "--output", "-o"),
) -> None:
    """显示单个角色的配置。"""
    from stock_senate.config.edit import canonical_name

    role = _roles(config).get(canonical_name(name))
    if role is None:
        raise _fail(f"未知角色: {name}", EXIT_CONFIG)
    _emit(role, output)


@role_app.command("validate")
def role_validate(
    config: Path = typer.Option(None, "--config", "-c"),
    output: str = typer.Option("table", "--output", "-o"),
) -> None:
    """校验角色配置（数量上限、合法键、工具白名单）。"""
    validator = _require("stock_senate.config.schema", "M0")
    report = validator.validate_roles(_roles(config))
    _emit(report, output)
    if not report.get("ok", False):
        raise typer.Exit(EXIT_CONFIG)


@role_app.command("enable")
def role_enable(
    name: str = typer.Argument(..., help="角色键。"),
    config: Path = typer.Option(None, "--config", "-c"),
) -> None:
    """启用角色（写回 agent_roles.toml，变更需审计）。"""
    editor = _require("stock_senate.config.edit", "M1")
    editor.set_enabled(name, True, config_dir=config or _config_dir())
    typer.echo(f"已启用 {name}")


@role_app.command("disable")
def role_disable(
    name: str = typer.Argument(..., help="角色键。"),
    config: Path = typer.Option(None, "--config", "-c"),
) -> None:
    """停用角色（写回 agent_roles.toml，变更需审计）。"""
    editor = _require("stock_senate.config.edit", "M1")
    editor.set_enabled(name, False, config_dir=config or _config_dir())
    typer.echo(f"已停用 {name}")


# --------------------------------------------------------------------------- #
# backtest
# --------------------------------------------------------------------------- #


@backtest_app.command("run")
def backtest_run(
    code: list[str] = typer.Option(None, "--code", help="标的代码，可重复。"),
    codes: str = typer.Option(None, "--codes", help="逗号分隔的标的列表。"),
    start: str = typer.Option(..., "--start", help="YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="YYYY-MM-DD"),
    window: int = typer.Option(5, "--window", help="每个决策窗口持有交易日数。"),
    slippage: float = typer.Option(0.0, "--slippage", help="滑点，按成交价比例。"),
    fee: float = typer.Option(0.0, "--fee", help="单边手续费率。"),
    out_dir: Path = typer.Option(None, "--out-dir", help="结果输出目录。"),
    config: Path = typer.Option(None, "--config", "-c", help="配置目录，默认 ~/.stock_senate。"),
    output: str = typer.Option("table", "--output", "-o", help="table | json | csv"),
) -> None:
    """回测推演（与 performance 事后归因是两件事）。"""
    targets = list(code or [])
    if codes:
        targets += [c.strip() for c in codes.split(",") if c.strip()]
    if not targets:
        raise _fail("backtest run 需要 --code 或 --codes")
    engine = _require("stock_senate.backtest.engine", "M3")
    with _mapped_errors("backtest run"):
        result = engine.run(
            codes=targets,
            start=start,
            end=end,
            window=window,
            slippage=slippage,
            fee=fee,
            out_dir=out_dir,
            config=config,
        )
    _emit(result, output)
    if (result.get("data") or {}).get("status") == "missing":
        raise typer.Exit(EXIT_DATA)
    if result.get("partial"):
        raise typer.Exit(EXIT_PARTIAL)


# --------------------------------------------------------------------------- #
# alert
# --------------------------------------------------------------------------- #


@alert_app.command("list")
def alert_list(
    config: Path = typer.Option(None, "--config", "-c"),
    output: str = typer.Option("table", "--output", "-o"),
) -> None:
    """列出告警规则。"""
    rules = _load_config_dir(config).get("alert_rules", {}).get("rules", [])
    _emit(rules, output, rows=rules)


@alert_rules_app.command("validate")
def alert_rules_validate(
    config: Path = typer.Option(None, "--config", "-c"),
    output: str = typer.Option("table", "--output", "-o"),
) -> None:
    """校验告警规则文件。"""
    validator = _require("stock_senate.config.schema", "M0")
    report = validator.validate_alert_rules(_load_config_dir(config))
    _emit(report, output)
    if not report.get("ok", False):
        raise typer.Exit(EXIT_CONFIG)


@alert_app.command("test")
def alert_test(
    rule: str = typer.Option(None, "--rule", help="只试跑指定规则 id。"),
    output: str = typer.Option("table", "--output", "-o"),
) -> None:
    """在最近一次数据上试跑告警规则，不推送。"""
    engine = _require("stock_senate.alerts.engine", "M2")
    report = engine.dry_run(rule)
    _emit(report, output)
    if report.get("ok") is False:
        raise typer.Exit(EXIT_USAGE)


@alert_app.command("serve")
def alert_serve(
    interval: int = typer.Option(60, "--interval", help="轮询间隔秒数。"),
) -> None:
    """常驻告警服务。"""
    engine = _require("stock_senate.alerts.engine", "M2")
    engine.serve(interval_seconds=interval)


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


@report_app.command("list")
def report_list(
    code: str = typer.Option(None, "--code", help="按标的过滤。"),
    output: str = typer.Option("table", "--output", "-o"),
) -> None:
    """列出已保存的报告快照。"""
    store = _require("stock_senate.report.store", "M2")
    _emit(store.list_runs(code=code), output)


@report_app.command("show")
def report_show(
    run_id: str = typer.Argument(..., help="运行 ID。"),
    output: str = typer.Option("table", "--output", "-o"),
) -> None:
    """读取一份报告。"""
    store = _require("stock_senate.report.store", "M2")
    _emit(store.load(run_id), output)


@report_app.command("diff")
def report_diff(
    code: str = typer.Option(None, "--code", help="标的代码。"),
    from_date: str = typer.Option(None, "--from", help="起始日期 YYYY-MM-DD。"),
    to_date: str = typer.Option(None, "--to", help="结束日期 YYYY-MM-DD。"),
    output: str = typer.Option("table", "--output", "-o"),
) -> None:
    """对比两次运行的字段级差异。"""
    if not (code and from_date and to_date):
        raise _fail("report diff 需要 --code、--from、--to")
    store = _require("stock_senate.report.store", "M2")
    _emit(store.diff(code=code, from_date=from_date, to_date=to_date), output)


@report_app.command("export")
def report_export(
    run_id: str = typer.Argument(..., help="运行 ID。"),
    fmt: str = typer.Option("md", "--format", "-f", help="md | json | pdf | docx"),
    out: Path = typer.Option(None, "--out", help="输出文件路径。"),
) -> None:
    """按指定格式导出报告。"""
    store = _require("stock_senate.report.store", "M2")
    path = store.export(run_id, fmt, out_path=out)
    typer.echo(str(path))


# --------------------------------------------------------------------------- #
# queue
# --------------------------------------------------------------------------- #


@queue_app.command("status")
def queue_status(
    job_id: str = typer.Option(None, "--job-id"),
    output: str = typer.Option("table", "--output", "-o"),
) -> None:
    """查看队列状态。"""
    manager = _require("stock_senate.queue.manager", "M2")
    _emit(manager.status(job_id), output)


@queue_app.command("submit")
def queue_submit(
    file: Path = typer.Option(..., "--file", help="CSV，列名 code[,date]。"),
    concurrency: int = typer.Option(1, "--concurrency"),
    output: str = typer.Option("table", "--output", "-o"),
) -> None:
    """从 CSV 批量提交任务。"""
    manager = _require("stock_senate.queue.manager", "M2")
    _emit(manager.submit_file(file, concurrency=concurrency), output)


@queue_app.command("cancel")
def queue_cancel(
    job_id: str = typer.Argument(..., help="任务 ID。"),
) -> None:
    """取消任务。"""
    manager = _require("stock_senate.queue.manager", "M2")
    manager.cancel(job_id)
    typer.echo(f"已取消 {job_id}")


@queue_app.command("pause")
def queue_pause(
    job_id: str = typer.Argument(..., help="任务 ID。"),
) -> None:
    """暂停任务。"""
    manager = _require("stock_senate.queue.manager", "M2")
    manager.pause(job_id)
    typer.echo(f"已暂停 {job_id}")


@queue_app.command("resume")
def queue_resume(
    job_id: str = typer.Argument(..., help="任务 ID。"),
) -> None:
    """恢复任务。"""
    manager = _require("stock_senate.queue.manager", "M2")
    manager.resume(job_id)
    typer.echo(f"已恢复 {job_id}")


# --------------------------------------------------------------------------- #
# mcp / webui
# --------------------------------------------------------------------------- #


@mcp_app.command("stdio")
def mcp_stdio() -> None:
    """以 stdio 启动 MCP Server；stdout 只输出 MCP 报文。"""
    server = _require("stock_senate.mcp_server.server", "M3")
    server.run(transport="stdio")


@mcp_app.command("http")
def mcp_http(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port"),
) -> None:
    """以 Streamable HTTP 启动 MCP Server，默认仅本机并强制鉴权。"""
    server = _require("stock_senate.mcp_server.server", "M3")
    server.run(transport="http", host=host, port=port)


@app.command()
def webui(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8788, "--port"),
) -> None:
    """启动独立 WebUI（与 MCP Server、TickHub Admin 分进程分端口）。"""
    launcher = _require("stock_senate.webui.launch", "M3")
    launcher.run(host=host, port=port)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


@config_app.command("show")
def config_show(
    config: Path = typer.Option(None, "--config", "-c"),
    output: str = typer.Option("table", "--output", "-o"),
) -> None:
    """打印当前生效配置（密钥与绝对路径已打码）。"""
    from stock_senate.observability.logging import redact

    _emit(redact(_load_config_dir(config), mask_paths=True), output)


@config_app.command("path")
def config_path() -> None:
    """打印配置目录。"""
    typer.echo(str(_config_dir()))


@config_app.command("init")
def config_init(
    config: Path = typer.Option(None, "--config", "-c"),
    output: str = typer.Option("table", "--output", "-o"),
) -> None:
    """创建配置目录并报告缺失文件。"""
    target = Path(config).expanduser() if config else _config_dir()
    target.mkdir(parents=True, exist_ok=True)
    missing = [name for name in CONFIG_FILENAMES if not (target / name).exists()]
    _emit({"config_dir": str(target), "missing": missing}, output)


@config_app.command("validate")
def config_validate(
    config: Path = typer.Option(None, "--config", "-c"),
    strict: bool = typer.Option(False, "--strict", help="把未知配置键视为错误，而不只是告警。"),
    output: str = typer.Option("table", "--output", "-o"),
) -> None:
    """校验七个配置文件；失败时定位到文件与键。"""
    validator = _require("stock_senate.config.schema", "M0")
    target = Path(config).expanduser() if config else _config_dir()
    report = validator.validate_dir(target, strict=strict)
    _emit(report, output)
    if not report.get("ok", False):
        raise typer.Exit(EXIT_CONFIG)


@config_app.command("migrate")
def config_migrate(
    dry_run: bool = typer.Option(False, "--dry-run", help="只显示将要迁移的内容。"),
    output: str = typer.Option("table", "--output", "-o"),
) -> None:
    """把 ~/.tradingagents 只读迁移到 ~/.stock_senate。"""
    from stock_senate.config.loader import migrate_legacy

    report = migrate_legacy(_config_dir(), dry_run=dry_run)
    _emit(report, output)
    if report.get("error"):
        raise typer.Exit(EXIT_CONFIG)


def _ensure_utf8_stdio() -> None:
    """Make redirected output UTF-8; Windows otherwise uses the ANSI code page."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):  # detached or closed stream
            pass


def _load_dotenv() -> None:
    """Read ``./.env`` without overriding anything already exported.

    dotenv stays silent: ``mcp stdio`` writes JSON-RPC to stdout, so a banner
    or a warning here would corrupt the transport.
    """
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False, verbose=False)


def main() -> None:
    _ensure_utf8_stdio()
    _load_dotenv()
    app()


def legacy_main() -> None:
    """Entry point for the deprecated ``tradingagents`` command."""
    _ensure_utf8_stdio()
    _load_dotenv()
    typer.secho(
        "警告：`tradingagents` 已更名为 `ssenate`，本命令将在下个大版本移除。",
        fg=typer.colors.YELLOW,
        err=True,
    )
    app()


if __name__ == "__main__":
    main()
