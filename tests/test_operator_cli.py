import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys

from click.testing import CliRunner
import pytest


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

import ajib_operator as operator  # noqa: E402
import cli as operator_cli  # noqa: E402


def server(server_id="primary", **changes):
    value = {
        "id": server_id,
        "name": server_id.title(),
        "url": f"https://{server_id}.example.com",
        "token": f"{server_id}-panel-secret-token",
        "panel": "blitz",
        "weight": 1,
        "enabled": True,
        "default_inbound_ids": [],
        "default_limit_ip": 0,
    }
    value.update(changes)
    return value


def config(*servers, telegram_token="123456:telegram-super-secret-token"):
    return {
        "schema_version": 1,
        "telegram": {"token": telegram_token, "admin_ids": [123, 456]},
        "servers": list(servers or (server(),)),
    }


@pytest.fixture
def operator_paths(tmp_path, monkeypatch):
    install = tmp_path / "install"
    bot = install / "core" / "scripts" / "telegrambot"
    bot.mkdir(parents=True)
    monkeypatch.setenv("AJIB_INSTALL_DIR", str(install))
    monkeypatch.setenv("AJIB_BOT_DIR", str(bot))
    monkeypatch.setenv("AJIB_TELEGRAM_ENV", str(bot / ".env"))
    monkeypatch.setenv("AJIB_PREVIOUS_ENV", str(bot / ".env.previous"))
    monkeypatch.setenv("AJIB_DB_PATH", str(bot / "ajib.db"))
    monkeypatch.setenv("AJIB_READY_FILE", str(tmp_path / "run" / "main.ready"))
    monkeypatch.setenv("AJIB_BACKUP_DIR", str(tmp_path / "backups"))
    sqlite3.connect(bot / "ajib.db").close()
    return {"install": install, "bot": bot, "env": bot / ".env", "db": bot / "ajib.db"}


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("id", "has space", "Server ID"),
        ("url", "ftp://vpn.example.com", r"HTTP\(S\)"),
        ("url", "https://user:pass@vpn.example.com", "embedded credentials"),
        ("weight", float("nan"), "finite"),
        ("weight", -1, "non-negative"),
        ("default_limit_ip", -1, "non-negative"),
    ],
)
def test_server_validation_rejects_unsafe_values(field, value, message):
    with pytest.raises(operator.ValidationError, match=message):
        operator.normalize_server(server(**{field: value}))


def test_config_normalizes_admins_panels_and_rejects_duplicates():
    normalized = operator.normalize_config({
        "telegram": {"token": "secret", "admin_ids": " 123, 456,123 "},
        "servers": [server("hy2", panel="3xui", default_inbound_ids="4|7|4")],
    })
    assert normalized["telegram"]["admin_ids"] == [123, 456]
    assert normalized["servers"][0]["panel"] == "3x-ui"
    assert normalized["servers"][0]["default_inbound_ids"] == [4, 7]

    with pytest.raises(operator.ValidationError, match="case-insensitive"):
        operator.normalize_config(config(server("Primary"), server("primary")))


def test_every_three_x_server_requires_inbounds():
    with pytest.raises(operator.ValidationError, match="requires default inbound"):
        operator.normalize_server(server("hy2", panel="3x-ui", enabled=False, weight=0))


def test_atomic_config_preserves_unrelated_values_and_private_copies(operator_paths):
    operator_paths["env"].write_text("CRYPTO_API_KEY=keep-me\n# operator note\n", encoding="utf-8")
    fingerprint = operator.save_config(config(server(), server("west")))
    text = operator_paths["env"].read_text(encoding="utf-8")

    assert "CRYPTO_API_KEY=keep-me" in text
    assert "# operator note" in text
    assert fingerprint == operator.config_fingerprint(operator.load_config())
    assert operator.previous_env_path().read_text(encoding="utf-8") == "CRYPTO_API_KEY=keep-me\n# operator note\n"
    if os.name != "nt":
        assert stat.S_IMODE(operator_paths["env"].stat().st_mode) == 0o600
        assert stat.S_IMODE(operator.previous_env_path().stat().st_mode) == 0o600


def test_stale_full_edit_is_rejected_without_losing_atomic_server_change(operator_paths):
    original = config(server(), server("west"))
    original_fingerprint = operator.save_config(original, keep_previous=False)
    servers, _fingerprint = operator.update_server("west", {"weight": 4})
    assert next(item for item in servers if item["id"] == "west")["weight"] == 4

    stale = config(server(), server("west", enabled=False))
    with pytest.raises(operator.ConfigurationConflictError, match="changed while"):
        operator.save_config(stale, expected_fingerprint=original_fingerprint)

    current = operator.load_config()
    assert next(item for item in current["servers"] if item["id"] == "west")["weight"] == 4
    assert next(item for item in current["servers"] if item["id"] == "west")["enabled"] is True


def test_update_server_merges_fields_and_keeps_id_immutable(operator_paths):
    operator.save_config(config(server(), server("west")), keep_previous=False)
    servers, _fingerprint = operator.update_server("west", {"enabled": False, "weight": 0})
    changed = next(item for item in servers if item["id"] == "west")
    assert changed["enabled"] is False
    assert changed["weight"] == 0
    assert changed["token"] == "west-panel-secret-token"
    with pytest.raises(operator.ValidationError, match="immutable"):
        operator.update_server("west", {"id": "east"})


def test_configuration_diff_reports_added_changed_and_removed():
    old = operator.normalize_config(config(server(), server("old")))
    new = operator.normalize_config(config(server(weight=2), server("new")))
    diff = operator.configuration_diff(old, new)
    assert diff == {"added": ["new"], "changed": ["primary"], "removed": ["old"]}


def _create_reference_database(path, *, active=False):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE payments(server_id TEXT, payload_json TEXT)")
    connection.execute(
        "INSERT INTO payments VALUES (?, ?)",
        ("west", json.dumps({"renewal_server_id": "west"})),
    )
    connection.execute(
        "CREATE TABLE bulk_transfer_jobs(job_id TEXT, source_server_id TEXT, destination_server_id TEXT, status TEXT, created_at TEXT)"
    )
    if active:
        connection.execute(
            "INSERT INTO bulk_transfer_jobs VALUES ('job-1','west','primary','running','2026-01-01')"
        )
    connection.commit()
    connection.close()


def test_removal_report_blocks_references_live_accounts_and_active_jobs(operator_paths, monkeypatch):
    operator.save_config(config(server(), server("west")), keep_previous=False)
    _create_reference_database(operator_paths["db"], active=True)
    monkeypatch.setattr(operator, "probe_server", lambda _server: {
        "healthy": True, "account_count": 3, "ok": True,
    })
    report = operator.server_removal_report("west")
    assert report["removable"] is False
    assert report["references"]["payments"] == 2
    assert report["active_transfer"]["job_id"] == "job-1"
    assert any("3 account" in item for item in report["blockers"])


def test_removal_report_blocks_unavailable_and_final_servers(operator_paths, monkeypatch):
    operator.save_config(config(server()), keep_previous=False)
    monkeypatch.setattr(operator, "probe_server", lambda _server: {
        "healthy": False, "account_count": None, "ok": False,
    })
    report = operator.server_removal_report("primary")
    assert any("final configured" in item for item in report["blockers"])
    assert any("unavailable" in item for item in report["blockers"])
    with pytest.raises(operator.UnsafeRemovalError, match="final configured"):
        operator.remove_server("primary", force=True, backup=False)


def test_malformed_local_records_block_reference_scan_and_removal(operator_paths, monkeypatch):
    operator.save_config(config(server(), server("west")), keep_previous=False)
    connection = sqlite3.connect(operator_paths["db"])
    connection.execute("CREATE TABLE payments(payload_json TEXT)")
    connection.execute("INSERT INTO payments VALUES ('{broken')")
    connection.commit()
    connection.close()
    monkeypatch.setattr(operator, "probe_server", lambda _server: {
        "healthy": True, "account_count": 0, "ok": True,
    })
    with pytest.raises(operator.OperatorError, match="Malformed JSON"):
        operator.server_removal_report("west")


def test_ordinary_empty_removal_creates_backup(operator_paths, monkeypatch):
    operator.save_config(config(server(), server("west")), keep_previous=False)
    monkeypatch.setattr(operator, "probe_server", lambda _server: {
        "healthy": True, "account_count": 0, "ok": True,
    })
    monkeypatch.setattr(operator, "safety_backup", lambda: "/safe/backup.zip")
    result = operator.remove_server("west")
    assert result["backup"] == "/safe/backup.zip"
    assert [item["id"] for item in operator.load_config()["servers"]] == ["primary"]


def test_force_removal_requires_disabled_zero_weight_and_backup(operator_paths, monkeypatch):
    operator.save_config(config(server(), server("west")), keep_previous=False)
    monkeypatch.setattr(operator, "probe_server", lambda _server: {
        "healthy": False, "account_count": None, "ok": False,
    })
    with pytest.raises(operator.UnsafeRemovalError, match="disabled with weight 0"):
        operator.remove_server("west", force=True, backup=False)

    operator.update_server("west", {"enabled": False, "weight": 0})
    monkeypatch.setattr(operator, "safety_backup", lambda: "/safe/break-glass.zip")
    result = operator.remove_server("west", force=True)
    assert result["backup"] == "/safe/break-glass.zip"


def test_active_migration_blocks_server_pause_and_endpoint_edit(operator_paths):
    operator.save_config(config(server(), server("west")), keep_previous=False)
    _create_reference_database(operator_paths["db"], active=True)
    with pytest.raises(operator.OperatorError, match="active transfer"):
        operator.update_server("west", {"enabled": False})
    with pytest.raises(operator.OperatorError, match="active transfer"):
        operator.update_server("west", {"url": "https://rotated.example.com"})


def test_systemd_failure_rolls_back_previous_configuration(operator_paths, monkeypatch):
    original = config(server())
    operator.save_config(original, keep_previous=False)
    changed = config(server(token="rotated-panel-secret"))
    actions = []

    def fail_once(action):
        actions.append(action)
        if len(actions) == 1:
            raise operator.OperatorError("fake systemd failure")
        return "restored"

    monkeypatch.setattr(operator, "service_action", fail_once)
    with pytest.raises(operator.OperatorError, match="not applied"):
        operator.apply_config(changed)
    assert operator.load_config()["servers"][0]["token"] == original["servers"][0]["token"]
    assert actions == ["start", "start"]


def test_readiness_timeout_preserves_first_install_and_reports_degraded(operator_paths, monkeypatch):
    monkeypatch.setattr(operator, "service_action", lambda _action: "started")
    monkeypatch.setattr(operator, "wait_for_readiness", lambda _fingerprint: False)
    result = operator.apply_config(
        config(server()),
        preflight={"ok": True},
        expected_fingerprint="",
    )
    assert result.status == "degraded"
    assert result.exit_code == 2
    assert result.had_previous is False
    assert operator.load_config() is not None


def test_first_install_systemd_failure_preserves_config_but_stops_service(operator_paths, monkeypatch):
    actions = []

    def service(action):
        actions.append(action)
        if action == "start":
            raise operator.OperatorError("fake start failure")
        return "stopped"

    monkeypatch.setattr(operator, "service_action", service)
    with pytest.raises(operator.OperatorError, match="not applied"):
        operator.apply_config(config(server()), expected_fingerprint="")
    assert operator.load_config() is not None
    assert actions == ["start", "stop"]


def test_rollback_keeps_failed_copy_and_restores_previous(operator_paths, monkeypatch):
    original = config(server(token="original-panel-token"))
    changed = config(server(token="changed-panel-token"))
    operator.save_config(original, keep_previous=False)
    operator.save_config(changed)
    monkeypatch.setattr(operator, "service_action", lambda _action: "started")
    monkeypatch.setattr(operator, "wait_for_readiness", lambda _fingerprint: True)
    result = operator.rollback_config()
    assert result.status == "healthy"
    assert operator.load_config()["servers"][0]["token"] == "original-panel-token"
    failed = operator.env_path().with_name(".env.failed")
    assert "changed-panel-token" in failed.read_text(encoding="utf-8")


def test_service_actions_never_put_credentials_in_subprocess_arguments(operator_paths, monkeypatch):
    calls = []

    def completed(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok")

    monkeypatch.setattr(operator.subprocess, "run", completed)
    assert operator.service_action("start") == "ok"
    flattened = " ".join(calls[0])
    assert "telegram-super-secret" not in flattened
    assert "panel-secret-token" not in flattened
    assert calls[0][-1] == "start"


def test_cli_setup_from_stdin_masks_secrets_and_returns_degraded(operator_paths, monkeypatch):
    candidate = config(server())
    monkeypatch.setattr(operator, "preflight_config", lambda _config: {
        "ok": False,
        "telegram": {"ok": False, "message": "unreachable"},
        "servers": [{"id": "primary", "ok": False, "message": "unreachable", "latency_ms": 1}],
    })
    monkeypatch.setattr(operator, "apply_config", lambda *args, **kwargs: operator.ApplyResult(
        "degraded", "saved with warning", False, "fingerprint", ready=True, verified=False
    ))
    service_actions = []
    monkeypatch.setattr(operator, "service_action", lambda action: service_actions.append(action) or "stopped")
    result = CliRunner().invoke(
        operator_cli.cli,
        ["setup", "--config", "-", "--yes", "--allow-unverified"],
        input=json.dumps(candidate),
    )
    assert result.exit_code == 2
    assert "saved with warning" in result.output
    assert candidate["telegram"]["token"] not in result.output
    assert candidate["servers"][0]["token"] not in result.output
    assert "1234...oken" in result.output
    assert service_actions == []


def test_cli_setup_refuses_unverified_noninteractive_input(operator_paths, monkeypatch):
    monkeypatch.setattr(operator, "preflight_config", lambda _config: {
        "ok": False,
        "telegram": {"ok": False, "message": "unreachable"},
        "servers": [{"id": "primary", "ok": False, "message": "unreachable", "latency_ms": 1}],
    })
    result = CliRunner().invoke(
        operator_cli.cli, ["setup", "--config", "-", "--yes"],
        input=json.dumps(config(server())),
    )
    assert result.exit_code == 1
    assert "--allow-unverified" in result.output
    assert operator.load_config() is None


def test_cli_server_json_never_exposes_token(operator_paths):
    operator.save_config(config(server(), server("west")), keep_previous=False)
    result = CliRunner().invoke(operator_cli.cli, ["server", "list", "--json"])
    assert result.exit_code == 0
    assert "primary-panel-secret-token" not in result.output
    assert "prim...oken" in result.output
    assert "paused" not in result.output


def test_cli_server_crud_uses_transactional_apply(operator_paths, monkeypatch, tmp_path):
    operator.save_config(config(server()), keep_previous=False)
    monkeypatch.setenv("AJIB_SKIP_SERVICE_ACTIONS", "1")

    def healthy(candidate):
        return {
            "ok": True,
            "telegram": {"ok": True, "message": "verified"},
            "servers": [
                {"id": item["id"], "ok": True, "message": "ready", "latency_ms": 1}
                for item in candidate["servers"]
            ],
        }

    monkeypatch.setattr(operator, "preflight_config", healthy)
    input_file = tmp_path / "server.json"
    input_file.write_text(json.dumps(server("west")), encoding="utf-8")
    input_file.chmod(0o600)
    runner = CliRunner()

    added = runner.invoke(operator_cli.cli, [
        "server", "add", "west", "--config", str(input_file), "--yes",
    ])
    assert added.exit_code == 0, added.output
    assert [item["id"] for item in operator.load_config()["servers"]] == ["primary", "west"]

    rotated = server("west", url="https://rotated.example.com", token="rotated-secret-token")
    input_file.write_text(json.dumps(rotated), encoding="utf-8")
    edited = runner.invoke(operator_cli.cli, [
        "server", "edit", "west", "--config", str(input_file), "--yes",
    ])
    assert edited.exit_code == 0, edited.output
    assert next(item for item in operator.load_config()["servers"] if item["id"] == "west")["url"] == "https://rotated.example.com"

    disabled = runner.invoke(operator_cli.cli, ["server", "disable", "west", "--yes"])
    weighted = runner.invoke(operator_cli.cli, ["server", "weight", "west", "0", "--yes"])
    enabled = runner.invoke(operator_cli.cli, ["server", "enable", "west", "--yes"])
    assert (disabled.exit_code, weighted.exit_code, enabled.exit_code) == (0, 0, 0)
    changed = next(item for item in operator.load_config()["servers"] if item["id"] == "west")
    assert changed["enabled"] is True
    assert changed["weight"] == 0


def test_cli_blocks_bulk_config_server_removal(operator_paths, monkeypatch):
    operator.save_config(config(server(), server("west")), keep_previous=False)
    monkeypatch.setattr(operator, "preflight_config", lambda _config: {
        "ok": True,
        "telegram": {"ok": True, "message": "ok"},
        "servers": [{"id": "primary", "ok": True, "message": "ok", "latency_ms": 1}],
    })
    result = CliRunner().invoke(
        operator_cli.cli,
        ["setup", "--config", "-", "--yes"],
        input=json.dumps(config(server())),
    )
    assert result.exit_code == 1
    assert "ajib server remove west" in result.output
    assert len(operator.load_config()["servers"]) == 2


def test_legacy_commands_warn_and_public_help_is_complete(monkeypatch):
    monkeypatch.setattr(operator_cli.cli_api, "show_version", lambda: "Bot Version: 2.3.0")
    runner = CliRunner()
    legacy = runner.invoke(operator_cli.cli, ["show-version"])
    assert legacy.exit_code == 0
    assert "deprecated" in legacy.output
    assert "2.3.0" in legacy.output

    root_help = runner.invoke(operator_cli.cli, ["--help"])
    server_help = runner.invoke(operator_cli.cli, ["server", "--help"])
    migration_help = runner.invoke(operator_cli.cli, ["server", "migration", "--help"])
    for name in ("setup", "status", "doctor", "rollback-config", "uninstall"):
        assert name in root_help.output
    for name in ("manage", "list", "show", "add", "edit", "enable", "disable", "weight", "test", "remove", "migrate", "migration"):
        assert name in server_help.output
    for name in ("status", "cancel", "resume", "export"):
        assert name in migration_help.output


def test_noninteractive_migration_requires_policy_and_queues_verified_job(monkeypatch):
    captured = {}

    class Spec:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            captured["spec"] = self

    class TransferModule:
        BulkUserTransferSpec = Spec

        @staticmethod
        def preflight_transfer(spec):
            return {"ok": True, "eligible": 2, "total": 2, "collisions": 0, "rejections": {}}

        @staticmethod
        def create_transfer_job(spec, preflight):
            captured["preflight"] = preflight
            return {"ok": True, "job_id": "job-123"}

    monkeypatch.setattr(operator_cli, "_load_transfer_module", lambda: TransferModule)
    runner = CliRunner()
    missing = runner.invoke(operator_cli.cli, [
        "server", "migrate", "old", "new", "--mode", "migrate", "--yes",
    ])
    assert missing.exit_code == 1
    assert "requires --notify" in missing.output

    queued = runner.invoke(operator_cli.cli, [
        "server", "migrate", "old", "new", "--mode", "migrate",
        "--notify", "deferred", "--yes",
    ])
    assert queued.exit_code == 0
    assert "job-123" in queued.output
    assert captured["spec"].notification_policy == "deferred"


def test_probe_reports_health_readiness_count_and_latency(monkeypatch):
    class FakeClient:
        def get_users(self):
            return [{"username": "a"}, {"username": "b"}]

        def is_creation_ready(self, verify_remote=False):
            assert verify_remote is True
            return True, None

    monkeypatch.setattr(operator, "_load_api_client_module", lambda: type(
        "Module", (), {"create_panel_client": staticmethod(lambda _server: FakeClient())}
    ))
    result = operator.probe_server(server())
    assert result["ok"] is True
    assert result["healthy"] is True
    assert result["creation_ready"] is True
    assert result["account_count"] == 2
    assert isinstance(result["latency_ms"], int)


@pytest.mark.skipif(os.name == "nt", reason="runbot integration is exercised by Linux CI")
def test_runbot_uses_deterministic_systemd_commands_and_propagates_failures(tmp_path):
    install = tmp_path / "ajib"
    bot = install / "core" / "scripts" / "telegrambot"
    bot.mkdir(parents=True)
    shutil.copy2(ROOT / "core" / "scripts" / "utils.sh", install / "core" / "scripts" / "utils.sh")
    shutil.copy2(ROOT / "core" / "scripts" / "path.sh", install / "core" / "scripts" / "path.sh")
    shutil.copy2(ROOT / "core" / "scripts" / "telegrambot" / "runbot.sh", bot / "runbot.sh")
    (bot / ".env").write_text("API_TOKEN=do-not-log-this-secret\n", encoding="utf-8")
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    fake = tmp_path / "fake-systemctl"
    fake.write_text(
        "#!/bin/bash\n"
        "echo \"$*\" >> \"$FAKE_SYSTEMD_LOG\"\n"
        "case \"$1\" in\n"
        "  is-active) [ -f \"$FAKE_SYSTEMD_STATE\" ];;\n"
        "  start|restart) [ \"${FAKE_SYSTEMD_FAIL:-0}\" = 1 ] && exit 1; touch \"$FAKE_SYSTEMD_STATE\";;\n"
        "  stop) rm -f \"$FAKE_SYSTEMD_STATE\";;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    journal = tmp_path / "fake-journalctl"
    journal.write_text("#!/bin/bash\necho fake-journal >&2\n", encoding="utf-8")
    journal.chmod(0o755)
    log = tmp_path / "systemd.log"
    environment = {
        **os.environ,
        "AJIB_INSTALL_DIR": str(install),
        "AJIB_SYSTEMD_DIR": str(systemd_dir),
        "AJIB_SYSTEMCTL": str(fake),
        "AJIB_JOURNALCTL": str(journal),
        "FAKE_SYSTEMD_LOG": str(log),
        "FAKE_SYSTEMD_STATE": str(tmp_path / "active"),
    }
    success = subprocess.run(
        ["bash", str(bot / "runbot.sh"), "start"],
        env=environment, text=True, capture_output=True, check=False,
    )
    assert success.returncode == 0
    assert (systemd_dir / "ajib-telegram-bot.service").is_file()
    assert "do-not-log-this-secret" not in log.read_text(encoding="utf-8")

    environment["FAKE_SYSTEMD_FAIL"] = "1"
    failed = subprocess.run(
        ["bash", str(bot / "runbot.sh"), "restart"],
        env=environment, text=True, capture_output=True, check=False,
    )
    assert failed.returncode != 0
