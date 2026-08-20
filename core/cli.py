#!/usr/bin/env python3
"""ajib operator CLI with compatibility aliases for pre-2.3 automation."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import click

import ajib_operator as operator
import cli_api


def pretty_print(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _echo_error(error: Exception) -> click.ClickException:
    return click.ClickException(str(error))


def _read_json_config(source: str) -> dict[str, Any]:
    if source == "-":
        raw = sys.stdin.read()
    else:
        path = Path(source)
        if not path.is_file():
            raise click.ClickException(f"Configuration file not found: {source}")
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise click.ClickException(
                f"Configuration file {source} is group/world accessible; run chmod 600 first."
            )
        raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise click.ClickException(f"Invalid JSON configuration: {error}") from error
    if not isinstance(payload, dict):
        raise click.ClickException("Configuration input must contain one JSON object.")
    return payload


def _secret_prompt(label: str, current: str = "") -> str:
    suffix = " (leave blank to keep current)" if current else ""
    while True:
        value = click.prompt(f"{label}{suffix}", default="", show_default=False, hide_input=True)
        if value:
            return value
        if current:
            return current
        click.echo(f"{label} cannot be empty.", err=True)


def _prompt_weight(default: float) -> float:
    while True:
        value = click.prompt("Balancing weight (0 pauses new placement)", default=default, type=float)
        if math.isfinite(value) and value >= 0:
            return 0.0 if value == 0 else value
        click.echo("Weight must be a finite non-negative number.", err=True)


def _prompt_inbound_ids(default: list[int], required: bool) -> list[int]:
    default_text = "|".join(str(item) for item in default)
    while True:
        value = click.prompt(
            "Default inbound IDs (pipe-separated)", default=default_text,
            show_default=bool(default_text),
        ).strip()
        if not value and not required:
            return []
        try:
            result = [int(item) for item in value.split("|") if item.strip()]
        except ValueError:
            result = []
        if result and all(item > 0 for item in result):
            return list(dict.fromkeys(result))
        click.echo("Use positive inbound IDs separated by |.", err=True)


def _prompt_server(existing: dict[str, Any] | None = None, *, immutable_id: str | None = None) -> dict[str, Any]:
    current = existing or {}
    server_id = immutable_id or click.prompt("Server ID", default=current.get("id") or "primary")
    name = click.prompt("Display name", default=current.get("name") or server_id)
    url = click.prompt("Panel API URL", default=current.get("url") or "")
    token = _secret_prompt("Panel API token", str(current.get("token") or ""))
    panel = click.prompt(
        "Panel type", default=current.get("panel") or "blitz",
        type=click.Choice(["blitz", "3x-ui"], case_sensitive=False),
    )
    weight = _prompt_weight(float(current.get("weight", 1)))
    enabled = click.confirm("Enable this server?", default=bool(current.get("enabled", True)))
    inbound_ids: list[int] = []
    limit_ip = 0
    if panel == "3x-ui":
        inbound_ids = _prompt_inbound_ids(
            list(current.get("default_inbound_ids") or []), True
        )
        limit_ip = click.prompt(
            "Default client IP limit (0 = unlimited)",
            default=int(current.get("default_limit_ip", 0)), type=click.IntRange(min=0),
        )
    return operator.normalize_server({
        "id": server_id, "name": name, "url": url, "token": token,
        "panel": panel, "weight": weight, "enabled": enabled,
        "default_inbound_ids": inbound_ids, "default_limit_ip": limit_ip,
    })


def _interactive_setup() -> dict[str, Any]:
    current = operator.load_config()
    current_telegram = (current or {}).get("telegram", {})
    token = _secret_prompt("Telegram bot token", str(current_telegram.get("token") or ""))
    current_admins = ",".join(str(item) for item in current_telegram.get("admin_ids", []))
    admin_ids = click.prompt("Administrator IDs (comma-separated)", default=current_admins)
    current_servers = list((current or {}).get("servers", []))
    count = click.prompt(
        "Number of VPN servers", default=max(1, len(current_servers)),
        type=click.IntRange(min=1),
    )
    servers = []
    for index in range(count):
        click.echo(f"\nVPN server {index + 1} of {count}")
        existing = current_servers[index] if index < len(current_servers) else None
        servers.append(_prompt_server(existing))
    return operator.normalize_config({
        "schema_version": 1,
        "telegram": {"token": token, "admin_ids": admin_ids},
        "servers": servers,
    })


def _show_diff(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, list[str]]:
    diff = operator.configuration_diff(old, new)
    click.echo("\nConfiguration changes")
    for label in ("added", "changed", "removed"):
        values = diff[label]
        click.echo(f"  {label.title()}: {', '.join(values) if values else '(none)'}")
    click.echo(f"  Telegram token: {operator.mask_secret(new['telegram']['token'])}")
    click.echo(f"  Administrator IDs: {', '.join(str(item) for item in new['telegram']['admin_ids'])}")
    for server in new["servers"]:
        placement = "disabled" if not server["enabled"] else "paused (weight 0)" if server["weight"] == 0 else "enabled"
        click.echo(
            f"  - {server['id']}: {server['panel']} at {server['url']} | {placement} | "
            f"weight {server['weight']} | token {operator.mask_secret(server['token'])}"
        )
    return diff


def _show_preflight(result: dict[str, Any]) -> None:
    click.echo("\nLive checks")
    telegram = result["telegram"]
    click.echo(f"  {'OK' if telegram['ok'] else 'WARN'} Telegram: {telegram['message']}")
    for server in result["servers"]:
        click.echo(
            f"  {'OK' if server['ok'] else 'WARN'} {server['id']}: {server['message']} "
            f"({server['latency_ms']}ms)"
        )


def _validate_transition(
    old: dict[str, Any] | None, new: dict[str, Any], *,
    forced_panel_ids: set[str] | None = None,
) -> None:
    if old is None:
        return
    forced = {item.casefold() for item in (forced_panel_ids or set())}
    old_servers = {item["id"].casefold(): item for item in old["servers"]}
    new_servers = {item["id"].casefold(): item for item in new["servers"]}
    removed = [item["id"] for key, item in old_servers.items() if key not in new_servers]
    if removed:
        raise click.ClickException(
            "Server identities cannot be removed through setup or a bulk configuration edit. "
            f"Use 'ajib server remove {removed[0]}' so live accounts, records, migrations, and backup safety are checked."
        )
    for key, previous in old_servers.items():
        candidate = new_servers[key]
        if candidate["id"] != previous["id"]:
            raise click.ClickException(
                f"Server ID '{previous['id']}' is immutable, including its letter case."
            )
        active = operator.active_transfer_for_server(previous["id"])
        protected_changed = any(
            candidate[field] != previous[field]
            for field in ("url", "token", "panel", "default_inbound_ids")
        )
        paused = (
            not candidate["enabled"] and previous["enabled"]
        ) or (candidate["weight"] == 0 and previous["weight"] != 0)
        if active and (protected_changed or paused):
            raise click.ClickException(
                f"Server '{previous['id']}' participates in active migration "
                f"{active.get('job_id')}; endpoint and placement changes are blocked."
            )
        if (
            candidate["panel"] != previous["panel"]
            and operator.database_server_references(previous["id"])
            and key not in forced
        ):
            raise click.ClickException(
                f"Referenced server '{previous['id']}' cannot change panel type without migration or --force."
            )


def _apply_candidate(
    candidate: dict[str, Any], *, yes: bool, allow_unverified: bool,
    interactive: bool, restore_prompt: bool = True,
    forced_panel_ids: set[str] | None = None,
) -> int:
    old = operator.load_config()
    expected_fingerprint = operator.config_fingerprint(old) if old else ""
    diff = _show_diff(old, candidate)
    _validate_transition(old, candidate, forced_panel_ids=forced_panel_ids)
    if not yes and not interactive:
        raise click.ClickException("Non-interactive configuration changes require --yes.")
    preflight = operator.preflight_config(candidate)
    _show_preflight(preflight)
    if not preflight["ok"] and not allow_unverified:
        if not interactive or not click.confirm("Some live checks failed. Apply anyway?", default=False):
            raise click.ClickException(
                "Configuration was not changed. Retry the checks or use --allow-unverified."
            )
    risky = bool(diff["removed"]) or not preflight["ok"]
    if not yes and not (interactive and click.confirm("Apply these changes?", default=not risky)):
        click.echo("Configuration was not changed.")
        return 0
    result = operator.apply_config(
        candidate, preflight=preflight, expected_fingerprint=expected_fingerprint
    )
    click.echo(result.message)
    if (
        result.status == "degraded" and not result.ready and restore_prompt
        and result.had_previous and interactive
    ):
        if click.confirm("Restore the previous working configuration?", default=True):
            restored = operator.rollback_config()
            click.echo(restored.message)
            return 1
    if result.status == "degraded" and not result.ready and not result.had_previous:
        try:
            operator.service_action("stop")
            click.echo("No previous configuration exists; the unready service was stopped and your settings were preserved.")
        except operator.OperatorError as error:
            click.echo(f"Warning: {error}", err=True)
    return result.exit_code


def _server_from_json(source: str) -> dict[str, Any]:
    payload = _read_json_config(source)
    if "server" in payload:
        payload = payload["server"]
    return operator.normalize_server(payload)


def _find_server(config: dict[str, Any], server_id: str) -> tuple[int, dict[str, Any]]:
    for index, server in enumerate(config["servers"]):
        if server["id"].casefold() == server_id.casefold():
            return index, server
    raise click.ClickException(f"VPN server '{server_id}' is not configured.")


def _require_config() -> dict[str, Any]:
    try:
        config = operator.load_config()
    except operator.OperatorError as error:
        raise _echo_error(error) from error
    if config is None:
        raise click.ClickException("The bot is not configured. Run 'ajib setup' first.")
    return config


def _apply_server_config(
    config: dict[str, Any], *, yes: bool, allow_unverified: bool,
    forced_panel_ids: set[str] | None = None,
) -> int:
    return _apply_candidate(
        operator.normalize_config(config), yes=yes, allow_unverified=allow_unverified,
        interactive=sys.stdin.isatty(), restore_prompt=True,
        forced_panel_ids=forced_panel_ids,
    )


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Securely configure, inspect, and operate the ajib Telegram VPN bot."""


@cli.command("setup")
@click.option("--config", "config_source", type=str, help="Read schema-v1 JSON from a mode-600 file or '-' for stdin.")
@click.option("--yes", is_flag=True, help="Apply without the final confirmation.")
@click.option("--allow-unverified", is_flag=True, help="Allow failed advisory live checks.")
def setup_command(config_source: str | None, yes: bool, allow_unverified: bool) -> None:
    """Run secure first-time setup or reconfigure the complete installation."""
    prompt_capable = sys.stdin.isatty() and config_source != "-"
    if config_source is None and not prompt_capable:
        raise click.ClickException("Interactive setup requires a terminal; use --config PATH or --config -.")
    try:
        candidate = operator.normalize_config(
            _read_json_config(config_source) if config_source else _interactive_setup()
        )
        code = _apply_candidate(
            candidate, yes=yes, allow_unverified=allow_unverified,
            interactive=prompt_capable,
        )
    except (operator.OperatorError, click.Abort) as error:
        if isinstance(error, click.Abort):
            click.echo("Setup cancelled.")
            return
        raise _echo_error(error) from error
    if code:
        raise click.exceptions.Exit(code)


@cli.command("status")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON.")
def status_command(json_output: bool) -> None:
    """Show installed, configured, service, and bot-readiness state."""
    try:
        status = operator.service_status()
    except operator.OperatorError as error:
        raise _echo_error(error) from error
    if json_output:
        pretty_print(status)
    else:
        click.echo(f"ajib status: {status['status']}")
        click.echo(f"Installed: {'yes' if status['installed'] else 'no'}")
        click.echo(f"Configured: {'yes' if status['configured'] else 'no'}")
        click.echo(f"Service: {'active' if status['service_active'] else 'inactive'}")
        click.echo(f"Bot readiness: {'confirmed' if status['ready'] else 'not confirmed'}")
    if status["status"] == "degraded":
        raise click.exceptions.Exit(2)


@cli.command("doctor")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON.")
@click.option("--no-live", is_flag=True, help="Skip Telegram and VPN network checks.")
def doctor_command(json_output: bool, no_live: bool) -> None:
    """Run installation, permissions, service, Telegram, and VPN diagnostics."""
    try:
        report = operator.doctor(live=not no_live)
    except operator.OperatorError as error:
        raise _echo_error(error) from error
    if json_output:
        pretty_print(report)
    else:
        click.echo(f"ajib doctor: {report['status']}")
        for check in report["checks"]:
            click.echo(f"  {'OK' if check.get('ok') else 'WARN'} {check['name']}: {check['message']}")
    if report["status"] != "healthy":
        raise click.exceptions.Exit(1 if report["status"] == "broken" else 2)


@cli.command("logs")
@click.option("--lines", default=100, show_default=True, type=click.IntRange(1, 1000))
def logs_command(lines: int) -> None:
    """Show recent systemd journal entries for the ajib service."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", "ajib-telegram-bot.service", "-n", str(lines), "--no-pager"],
            text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise click.ClickException(f"Unable to read service logs: {error}") from error
    raise click.exceptions.Exit(result.returncode)


def _simple_service_action(action: str) -> None:
    try:
        click.echo(operator.service_action(action))
    except operator.OperatorError as error:
        raise _echo_error(error) from error


@cli.command("restart")
def restart_command() -> None:
    """Restart the configured bot and verify the systemd action."""
    _simple_service_action("restart")


@cli.command("stop")
def stop_command() -> None:
    """Stop and disable the bot while preserving configuration."""
    _simple_service_action("stop")


@cli.command("rollback-config")
def rollback_config_command() -> None:
    """Restore the previous protected configuration and restart the bot."""
    try:
        result = operator.rollback_config()
    except operator.OperatorError as error:
        raise _echo_error(error) from error
    click.echo(result.message)
    if result.exit_code:
        raise click.exceptions.Exit(result.exit_code)


@click.group("server")
def server_group() -> None:
    """Add, inspect, test, migrate, and safely remove VPN servers."""


cli.add_command(server_group)


def _render_server(server: dict[str, Any], probe: dict[str, Any] | None = None) -> None:
    placement = "disabled" if not server["enabled"] else "paused (weight 0)" if server["weight"] == 0 else "accepting placement"
    click.echo(f"{server['id']} ({server['name']})")
    click.echo(f"  Panel: {server['panel']} | URL: {server['url']}")
    click.echo(f"  State: {placement} | Weight: {server['weight']}")
    click.echo(f"  API token: {operator.mask_secret(server['token'])}")
    if server["panel"] == "3x-ui":
        click.echo(f"  Inbounds: {server['default_inbound_ids'] or '(none)'} | IP limit: {server['default_limit_ip']}")
    if probe:
        click.echo(f"  Health: {'healthy' if probe['healthy'] else 'unhealthy'} | Accounts: {probe['account_count'] if probe['account_count'] is not None else 'unknown'}")
        click.echo(f"  Readiness: {probe['message']} ({probe['latency_ms']}ms)")


@server_group.command("list")
@click.option("--json", "json_output", is_flag=True)
@click.option("--live", is_flag=True, help="Probe every panel before displaying it.")
def server_list(json_output: bool, live: bool) -> None:
    """List configured servers with masked credentials and placement state."""
    config = _require_config()
    records = []
    probes = operator.probe_servers(config["servers"]) if live else [None] * len(config["servers"])
    for server, probe in zip(config["servers"], probes):
        records.append({"server": operator.public_server(server), "probe": probe})
    if json_output:
        pretty_print(records)
    else:
        for index, record in enumerate(records):
            if index:
                click.echo()
            _render_server(config["servers"][index], record["probe"])


@server_group.command("show")
@click.argument("server_id", required=False)
@click.option("--json", "json_output", is_flag=True)
@click.option("--live", is_flag=True)
def server_show(server_id: str | None, json_output: bool, live: bool) -> None:
    """Show one server, or all servers, without exposing API tokens."""
    config = _require_config()
    if server_id is None:
        records = []
        probes = operator.probe_servers(config["servers"]) if live else [None] * len(config["servers"])
        for server, probe in zip(config["servers"], probes):
            records.append({
                "server": operator.public_server(server), "probe": probe,
                "references": operator.database_server_references(server["id"]),
            })
        if json_output:
            pretty_print(records)
        else:
            for index, record in enumerate(records):
                if index:
                    click.echo()
                _render_server(config["servers"][index], record["probe"])
                click.echo(f"  Local references: {sum(record['references'].values())}")
        return
    _index, server = _find_server(config, server_id)
    probe = operator.probe_server(server) if live else None
    references = operator.database_server_references(server["id"])
    if json_output:
        pretty_print({"server": operator.public_server(server), "probe": probe, "references": references})
    else:
        _render_server(server, probe)
        click.echo(f"  Local references: {sum(references.values())}")


@server_group.command("test")
@click.argument("server_id", required=False)
@click.option("--json", "json_output", is_flag=True)
def server_test(server_id: str | None, json_output: bool) -> None:
    """Run read-only panel health and account-placement checks."""
    config = _require_config()
    servers = config["servers"]
    if server_id:
        servers = [_find_server(config, server_id)[1]]
    results = operator.probe_servers(servers)
    if json_output:
        pretty_print(results)
    else:
        for result in results:
            click.echo(f"{'OK' if result['ok'] else 'WARN'} {result['id']}: {result['message']} ({result['latency_ms']}ms)")
    if not all(item["ok"] for item in results):
        raise click.exceptions.Exit(2)


def _server_candidate(
    source: str | None, existing: dict[str, Any] | None = None,
    requested_id: str | None = None,
) -> dict[str, Any]:
    if source:
        candidate = _server_from_json(source)
        if existing and candidate["id"].casefold() != existing["id"].casefold():
            raise click.ClickException("Server IDs are immutable; add, migrate, and remove instead.")
        return {**candidate, "id": existing["id"]} if existing else candidate
    if not sys.stdin.isatty():
        raise click.ClickException("Interactive server input requires a terminal; use --config PATH or --config -.")
    return _prompt_server(
        existing, immutable_id=existing["id"] if existing else requested_id
    )


@server_group.command("add")
@click.argument("server_id", required=False)
@click.option("--config", "config_source", type=str, help="Single server JSON file or '-' for stdin.")
@click.option("--yes", is_flag=True)
@click.option("--allow-unverified", is_flag=True)
def server_add(server_id: str | None, config_source: str | None, yes: bool, allow_unverified: bool) -> None:
    """Add and validate a VPN server; tokens are prompted securely."""
    config = _require_config()
    candidate = _server_candidate(config_source, requested_id=server_id)
    if server_id and candidate["id"].casefold() != server_id.casefold():
        raise click.ClickException(
            f"Command ID '{server_id}' does not match configuration ID '{candidate['id']}'."
        )
    if any(item["id"].casefold() == candidate["id"].casefold() for item in config["servers"]):
        raise click.ClickException(f"Server ID '{candidate['id']}' is already configured.")
    config["servers"].append(candidate)
    code = _apply_server_config(config, yes=yes, allow_unverified=allow_unverified)
    if code:
        raise click.exceptions.Exit(code)


@server_group.command("edit")
@click.argument("server_id")
@click.option("--config", "config_source", type=str)
@click.option("--yes", is_flag=True)
@click.option("--allow-unverified", is_flag=True)
@click.option("--force", "--force-panel-change", "force_panel_change", is_flag=True, help="Allow a referenced server's panel type to change.")
def server_edit(server_id: str, config_source: str | None, yes: bool, allow_unverified: bool, force_panel_change: bool) -> None:
    """Edit one logical server; its ID cannot be changed."""
    config = _require_config()
    index, existing = _find_server(config, server_id)
    active = operator.active_transfer_for_server(existing["id"])
    candidate = _server_candidate(config_source, existing)
    protected_changed = any(candidate[key] != existing[key] for key in ("url", "token", "panel", "default_inbound_ids"))
    if active and protected_changed:
        raise click.ClickException(f"Server is used by active transfer {active.get('job_id')}; endpoint changes are blocked.")
    if candidate["panel"] != existing["panel"] and operator.database_server_references(existing["id"]) and not force_panel_change:
        raise click.ClickException("Referenced server panel type cannot change without --force-panel-change or account migration.")
    config["servers"][index] = candidate
    code = _apply_server_config(
        config, yes=yes, allow_unverified=allow_unverified,
        forced_panel_ids={existing["id"]} if force_panel_change else None,
    )
    if code:
        raise click.exceptions.Exit(code)


def _mutate_server(server_id: str, mutation, *, yes: bool, allow_unverified: bool = True) -> None:
    config = _require_config()
    index, existing = _find_server(config, server_id)
    active = operator.active_transfer_for_server(existing["id"])
    candidate = mutation(dict(existing))
    if active and (not candidate.get("enabled", True) or candidate.get("weight") == 0):
        raise click.ClickException(f"Server is used by active transfer {active.get('job_id')}; pausing it is blocked.")
    config["servers"][index] = operator.normalize_server(candidate, index)
    code = _apply_server_config(config, yes=yes, allow_unverified=allow_unverified)
    if code:
        raise click.exceptions.Exit(code)


@server_group.command("enable")
@click.argument("server_id")
@click.option("--yes", is_flag=True)
@click.option("--allow-unverified", is_flag=True)
def server_enable(server_id: str, yes: bool, allow_unverified: bool) -> None:
    """Administratively enable a server; weight 0 still pauses placement."""
    _mutate_server(server_id, lambda item: {**item, "enabled": True}, yes=yes, allow_unverified=allow_unverified)


@server_group.command("disable")
@click.argument("server_id")
@click.option("--yes", is_flag=True)
def server_disable(server_id: str, yes: bool) -> None:
    """Disable new placement without deleting server identity or history."""
    _mutate_server(server_id, lambda item: {**item, "enabled": False}, yes=yes)


@server_group.command("weight")
@click.argument("server_id")
@click.argument("value", type=float)
@click.option("--yes", is_flag=True)
def server_weight(server_id: str, value: float, yes: bool) -> None:
    """Set balancing weight; zero pauses automatic placement."""
    if not math.isfinite(value) or value < 0:
        raise click.BadParameter("must be finite and non-negative", param_hint="VALUE")
    _mutate_server(server_id, lambda item: {**item, "weight": value}, yes=yes)


@server_group.command("remove")
@click.argument("server_id")
@click.option("--force", is_flag=True, help="Break-glass removal after disable + weight 0.")
@click.option("--yes", is_flag=True, help="Skip typing the server ID.")
def server_remove(server_id: str, force: bool, yes: bool) -> None:
    """Remove an empty, unreferenced server after a safety backup."""
    try:
        report = operator.server_removal_report(server_id)
    except operator.OperatorError as error:
        raise _echo_error(error) from error
    click.echo(f"Removal preflight for {report['server']['id']}")
    click.echo(f"  Live accounts: {(report.get('probe') or {}).get('account_count', 'unknown')}")
    click.echo(f"  Local references: {sum(report['references'].values())}")
    for blocker in report["blockers"]:
        click.echo(f"  BLOCKER: {blocker}")
    if report["blockers"] and not force:
        raise click.ClickException("Removal blocked. Migrate accounts first or use the documented break-glass --force flow.")
    if not yes:
        if not sys.stdin.isatty():
            raise click.ClickException("Non-interactive removal requires --yes.")
        typed = click.prompt(f"Type {report['server']['id']} to confirm removal", default="", show_default=False)
        if typed != report["server"]["id"]:
            raise click.ClickException("Confirmation did not match; server was not removed.")
    try:
        result = operator.remove_server(server_id, force=force)
        try:
            operator.service_action("start")
        except operator.OperatorError:
            operator.restore_previous_config()
            try:
                operator.service_action("start")
            except operator.OperatorError:
                pass
            raise
    except operator.OperatorError as error:
        raise _echo_error(error) from error
    click.echo(f"Removed server {result['removed']}. Safety backup: {result['backup']}")


def _load_transfer_module():
    path = str(operator.bot_dir())
    if path not in sys.path:
        sys.path.insert(0, path)
    previous_role = os.environ.get("AJIB_BOT_ROLE")
    os.environ["AJIB_BOT_ROLE"] = "supervisor"
    try:
        from utils import bulk_transfer  # type: ignore
    finally:
        if previous_role is None:
            os.environ.pop("AJIB_BOT_ROLE", None)
        else:
            os.environ["AJIB_BOT_ROLE"] = previous_role
    return bulk_transfer


@server_group.command("migrate")
@click.argument("source")
@click.argument("destination")
@click.option("--mode", type=click.Choice(["copy", "migrate"]), required=True)
@click.option("--notify", type=click.Choice(["send", "deferred", "disabled"]), help="Required for migration; copy always disables notifications.")
@click.option("--inbound-id", "inbound_ids", multiple=True, type=click.IntRange(min=1))
@click.option("--yes", is_flag=True)
def server_migrate(source: str, destination: str, mode: str, notify: str | None, inbound_ids: tuple[int, ...], yes: bool) -> None:
    """Queue a resumable, verified copy or migration between VPN servers."""
    if mode == "copy":
        notification_policy = "disabled"
    elif notify:
        notification_policy = notify
    elif sys.stdin.isatty():
        notification_policy = click.prompt(
            "Customer notification policy", type=click.Choice(["send", "deferred", "disabled"])
        )
    else:
        raise click.ClickException("Non-interactive migration requires --notify.")
    module = _load_transfer_module()
    spec = module.BulkUserTransferSpec(
        mode=mode, source_server_id=source, destination_server_id=destination,
        inbound_ids=tuple(inbound_ids), requesting_admin="cli", notification_policy=notification_policy,
    )
    try:
        preflight = module.preflight_transfer(spec)
    except ValueError as error:
        raise click.ClickException(f"Migration input is invalid: {error}") from error
    if not preflight.get("ok"):
        raise click.ClickException(f"Migration preflight failed: {preflight.get('error', 'unknown_error')}")
    click.echo(
        f"Eligible: {preflight['eligible']} / {preflight['total']} | "
        f"Collisions: {preflight['collisions']} | Rejections: {preflight['rejections']}"
    )
    if mode == "migrate" and not yes:
        if not sys.stdin.isatty() or not click.confirm(
            "Destination records will be verified before each source account is deleted. Start migration?",
            default=False,
        ):
            click.echo("Migration was not started.")
            return
    result = module.create_transfer_job(spec, preflight)
    if not result.get("ok"):
        raise click.ClickException(f"Could not queue migration: {result.get('error', 'unknown_error')}")
    click.echo(f"Queued {mode} job {result['job_id']}. The running bot will process it.")


@click.group("migration")
def migration_group() -> None:
    """Inspect and control resumable server migration jobs."""


server_group.add_command(migration_group)


def _job_or_latest(module, job_id: str | None):
    job = module.get_job(job_id, include_items=True) if job_id else module.get_latest_job()
    if not job:
        raise click.ClickException("No migration job was found.")
    return job


@migration_group.command("status")
@click.argument("job_id", required=False)
@click.option("--json", "json_output", is_flag=True)
def migration_status(job_id: str | None, json_output: bool) -> None:
    """Show the latest or selected migration job and item counts."""
    module = _load_transfer_module()
    job = _job_or_latest(module, job_id)
    report = {"job": job, "counts": module.job_counts(job["job_id"]), "notifications": module.notification_counts(job["job_id"])}
    if json_output:
        pretty_print(report)
    else:
        click.echo(f"Job {job['job_id']}: {job['status']} ({job['mode']})")
        click.echo(f"Route: {job['source_server_id']} -> {job['destination_server_id']}")
        click.echo(f"Counts: {report['counts']}")
        click.echo(f"Notifications: {report['notifications']}")


@migration_group.command("cancel")
@click.argument("job_id", required=False)
def migration_cancel(job_id: str | None) -> None:
    """Request cancellation of a queued or running CLI migration."""
    module = _load_transfer_module()
    job = _job_or_latest(module, job_id)
    if not module.request_cancel(job["job_id"], "cli"):
        raise click.ClickException("This CLI migration cannot be cancelled in its current state.")
    click.echo(f"Cancellation requested for {job['job_id']}.")


@migration_group.command("resume")
@click.argument("job_id", required=False)
def migration_resume(job_id: str | None) -> None:
    """Resume a failed or cancelled CLI migration."""
    module = _load_transfer_module()
    job = _job_or_latest(module, job_id)
    if not module.resume_job(job["job_id"], "cli"):
        raise click.ClickException("This CLI migration cannot be resumed or another job is active.")
    click.echo(f"Migration {job['job_id']} queued for resume.")


@migration_group.command("export")
@click.argument("job_id", required=False)
@click.option("--output", type=click.Path(dir_okay=False, writable=True), help="Write CSV to this path; default is stdout.")
def migration_export(job_id: str | None, output: str | None) -> None:
    """Export a migration item report as CSV."""
    module = _load_transfer_module()
    job = _job_or_latest(module, job_id)
    content = module.export_job_csv(job["job_id"])
    if not content:
        raise click.ClickException("Migration report is unavailable.")
    if output:
        Path(output).write_bytes(content)
        click.echo(f"Wrote {output}.")
    else:
        click.echo(content.decode("utf-8"), nl=False)


@server_group.command("manage")
def server_manage() -> None:
    """Open a compact interactive server-management menu."""
    if not sys.stdin.isatty():
        raise click.ClickException("Interactive server management requires a terminal.")
    while True:
        try:
            _server_manage_loop()
            return
        except click.Abort:
            click.echo("Server management cancelled.")
            return
        except (click.ClickException, operator.OperatorError) as error:
            click.echo(f"Error: {error}", err=True)


def _server_manage_loop() -> None:
    while True:
        config = _require_config()
        click.echo("\nConfigured VPN servers")
        for index, server in enumerate(config["servers"], 1):
            placement = "disabled" if not server["enabled"] else "paused" if server["weight"] == 0 else "enabled"
            click.echo(f"  {index}. {server['id']} ({server['panel']}, {placement}, weight {server['weight']})")
        click.echo("  A. Add  E. Edit  T. Test  D. Disable/enable  W. Weight")
        click.echo("  M. Migrate/copy  J. Migration job  R. Remove  Q. Back")
        action = click.prompt("Action", type=str).strip().casefold()
        if action in {"q", "quit", "back"}:
            return
        if action == "a":
            config["servers"].append(_prompt_server())
            _apply_server_config(config, yes=False, allow_unverified=False)
            continue
        if action == "j":
            _manage_migration_job()
            continue
        if action not in {"e", "t", "d", "w", "m", "r"}:
            click.echo("Unknown action.", err=True)
            continue
        server_id = click.prompt("Server ID")
        if action == "e":
            index, existing = _find_server(config, server_id)
            config["servers"][index] = _prompt_server(existing, immutable_id=existing["id"])
            _apply_server_config(config, yes=False, allow_unverified=False)
        elif action == "t":
            result = operator.probe_server(_find_server(config, server_id)[1])
            click.echo(f"{'OK' if result['ok'] else 'WARN'} {result['id']}: {result['message']}")
        elif action == "d":
            _index, server = _find_server(config, server_id)
            _mutate_server(server_id, lambda item: {**item, "enabled": not server["enabled"]}, yes=False)
        elif action == "w":
            value = _prompt_weight(_find_server(config, server_id)[1]["weight"])
            _mutate_server(server_id, lambda item: {**item, "weight": value}, yes=False)
        elif action == "m":
            destination = click.prompt("Destination server ID")
            mode = click.prompt(
                "Transfer mode", type=click.Choice(["copy", "migrate"])
            )
            inbound_text = click.prompt(
                "Destination inbound IDs (pipe-separated; blank for panel default)",
                default="", show_default=False,
            ).strip()
            try:
                inbound_ids = tuple(
                    int(item.strip()) for item in inbound_text.split("|") if item.strip()
                )
            except ValueError as error:
                raise click.ClickException("Inbound IDs must be positive integers.") from error
            if any(item <= 0 for item in inbound_ids):
                raise click.ClickException("Inbound IDs must be positive integers.")
            click.get_current_context().invoke(
                server_migrate, source=server_id, destination=destination,
                mode=mode, notify=None, inbound_ids=inbound_ids, yes=False,
            )
        elif action == "r":
            report = operator.server_removal_report(server_id)
            if report["blockers"]:
                click.echo("Removal blocked: " + " ".join(report["blockers"]), err=True)
            else:
                typed = click.prompt(f"Type {report['server']['id']} to confirm", default="", show_default=False)
                if typed == report["server"]["id"]:
                    result = operator.remove_server(server_id)
                    try:
                        operator.service_action("start")
                    except operator.OperatorError:
                        operator.restore_previous_config()
                        operator.service_action("start")
                        raise
                    click.echo(f"Removed {result['removed']}; backup {result['backup']}.")


def _manage_migration_job() -> None:
    job_action = click.prompt(
        "Job action", type=click.Choice(["status", "cancel", "resume", "export"])
    )
    job_id = click.prompt(
        "Job ID (blank selects latest)", default="", show_default=False
    ).strip() or None
    context = click.get_current_context()
    if job_action == "status":
        context.invoke(migration_status, job_id=job_id, json_output=False)
    elif job_action == "cancel":
        context.invoke(migration_cancel, job_id=job_id)
    elif job_action == "resume":
        context.invoke(migration_resume, job_id=job_id)
    else:
        output = click.prompt(
            "Output CSV path (blank prints CSV)", default="", show_default=False
        ).strip() or None
        context.invoke(migration_export, job_id=job_id, output=output)


@cli.command("backup")
def backup_command() -> None:
    """Create a private, versioned bot-state backup."""
    try:
        click.echo(cli_api.backup_ajib())
    except Exception as error:
        raise _echo_error(error) from error


@cli.command("restore")
@click.argument("backup_file_path", required=False, type=click.Path(exists=True, file_okay=True, dir_okay=False, readable=True))
@click.option("--yes", is_flag=True, help="Restore without the final confirmation.")
def restore_command(backup_file_path: str | None, yes: bool) -> None:
    """Restore bot state from a validated backup ZIP with rollback protection."""
    if backup_file_path is None:
        candidates = sorted(
            operator.backup_dir().glob("ajib_bot_backup_*.zip"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise click.ClickException(f"No ajib backup archives were found in {operator.backup_dir()}.")
        backup_file_path = str(candidates[0])
    if not yes:
        if not sys.stdin.isatty():
            raise click.ClickException("Non-interactive restore requires --yes.")
        if not click.confirm(f"Restore bot state from {backup_file_path}?", default=False):
            click.echo("Restore cancelled.")
            return
    try:
        click.echo(cli_api.restore_ajib(backup_file_path))
    except Exception as error:
        raise _echo_error(error) from error


@cli.command("version")
@click.option("--check", is_flag=True, help="Also check the stable release channel.")
def version_command(check: bool) -> None:
    """Display the installed version and optionally check for an update."""
    try:
        output = cli_api.check_version() if check else cli_api.show_version()
        click.echo(output or "Version unavailable.")
    except Exception as error:
        raise _echo_error(error) from error


@cli.command("upgrade")
@click.option("--channel", type=click.Choice(["stable", "main"]), default="stable", show_default=True)
@click.option("--version", "target_version", type=str, help="Install an explicit release tag.")
@click.option("--yes", is_flag=True)
def upgrade_command(channel: str, target_version: str | None, yes: bool) -> None:
    """Upgrade through the transactional stable-release upgrader."""
    command = ["bash", str(operator.install_dir() / "upgrade.sh"), "--channel", channel]
    if target_version:
        command.extend(["--version", target_version])
    if yes:
        command.append("--yes")
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise click.ClickException(f"Upgrade failed: {error}") from error


@cli.command("uninstall")
@click.option("--yes", is_flag=True)
def uninstall_command(yes: bool) -> None:
    """Back up state, then remove ajib while preserving backup archives."""
    if not yes:
        if not sys.stdin.isatty():
            raise click.ClickException("Non-interactive uninstall requires --yes.")
        typed = click.prompt("Type uninstall to confirm", default="", show_default=False)
        if typed != "uninstall":
            click.echo("Uninstall cancelled.")
            return
    try:
        backup = operator.safety_backup()
        click.echo(f"Safety backup created: {backup}")
        subprocess.run(["bash", str(operator.install_dir() / "core/scripts/ajib/uninstall.sh"), "--yes"], check=True)
    except (operator.OperatorError, OSError, subprocess.CalledProcessError) as error:
        raise _echo_error(error) from error


@cli.command("server-info")
@click.option(
    "--section", type=click.Choice(["overview", "business", "customers", "tech", "traffic", "alerts", "full"], case_sensitive=False),
    default="full", show_default=True,
)
def server_info(section: str) -> None:
    """Render a business and technical dashboard section."""
    try:
        click.echo(cli_api.server_info(section=section) or "Server information not available.")
    except Exception as error:
        raise _echo_error(error) from error


# Compatibility commands retained until the next major release.
def _legacy_warning(old: str, new: str) -> None:
    click.echo(f"Warning: '{old}' is deprecated; use '{new}'.", err=True)


@cli.command("backup-ajib")
def backup_ajib_legacy() -> None:
    """Deprecated alias for `ajib backup`."""
    _legacy_warning("backup-ajib", "backup")
    try:
        click.echo(cli_api.backup_ajib())
    except Exception as error:
        raise _echo_error(error) from error


@cli.command("restore-ajib")
@click.argument("backup_file_path", type=click.Path(exists=True, file_okay=True, dir_okay=False, readable=True))
def restore_ajib_legacy(backup_file_path: str) -> None:
    """Deprecated alias for `ajib restore`."""
    _legacy_warning("restore-ajib", "restore --yes")
    try:
        click.echo(cli_api.restore_ajib(backup_file_path))
    except Exception as error:
        raise _echo_error(error) from error


@cli.command("show-version")
def show_version_legacy() -> None:
    """Deprecated alias for `ajib version`."""
    _legacy_warning("show-version", "version")
    try:
        click.echo(cli_api.show_version() or "Version unavailable.")
    except Exception as error:
        raise _echo_error(error) from error


@cli.command("check-version")
def check_version_legacy() -> None:
    """Check whether a newer stable version is available."""
    _legacy_warning("check-version", "version --check")
    try:
        click.echo(cli_api.check_version() or "Version information unavailable.")
    except Exception as error:
        raise _echo_error(error) from error


@cli.command("get-services-status")
def services_status_legacy() -> None:
    """Deprecated machine-oriented service status command."""
    _legacy_warning("get-services-status", "status --json")
    try:
        pretty_print(cli_api.get_services_status() or {})
    except Exception as error:
        raise _echo_error(error) from error


@cli.command("telegram")
@click.option("--action", "-a", required=True, type=click.Choice(["start", "stop"], case_sensitive=False))
@click.option("--token", "-t", type=str)
@click.option("--adminid", "-aid", type=str)
@click.option("--api-url", "-u", type=str)
@click.option("--api-key", "-k", type=str)
@click.option("--server", multiple=True, type=str)
def telegram_legacy(action: str, token: str | None, adminid: str | None, api_url: str | None, api_key: str | None, server: tuple[str, ...]) -> None:
    """Deprecated lifecycle interface; use setup/restart/stop instead."""
    click.echo("Warning: 'telegram' is deprecated; secret flags can be exposed by process listings. Use 'ajib setup'.", err=True)
    if action.casefold() == "stop":
        _simple_service_action("stop")
        return
    if not token or not adminid or ((not api_url or not api_key) and not server):
        raise click.UsageError("--token and --adminid are required; provide --api-url/--api-key or --server.")
    try:
        commands: list[list[str]] = []
        original = cli_api.run_cmd
        cli_api.run_cmd = lambda command: commands.append(command) or ""  # type: ignore[assignment]
        try:
            cli_api.start_telegram_bot(token, adminid, api_url or "", api_key or "", servers=server)
        finally:
            cli_api.run_cmd = original
        parsed_servers = json.loads(commands[0][-1]) if server else [{
            "id": "primary", "name": "Primary", "url": api_url,
            "token": api_key, "enabled": True, "weight": 1, "panel": "blitz",
            "default_inbound_ids": [], "default_limit_ip": 0,
        }]
        candidate = operator.normalize_config({
            "schema_version": 1,
            "telegram": {"token": token, "admin_ids": adminid},
            "servers": parsed_servers,
        })
        preflight = operator.preflight_config(candidate)
        result = operator.apply_config(candidate, preflight=preflight)
    except (operator.OperatorError, cli_api.ajibError, ValueError, IndexError) as error:
        raise _echo_error(error) from error
    click.echo(result.message)
    if result.exit_code:
        raise click.exceptions.Exit(result.exit_code)


if __name__ == "__main__":
    cli()
