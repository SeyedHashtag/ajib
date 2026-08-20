import json
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "core" / "scripts" / "telegrambot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))
os.environ.setdefault("AJIB_BOT_ROLE", "supervisor")

from utils import database  # noqa: E402
from utils.api_client import BLITZ_PANEL, BulkUserTransferSpec  # noqa: E402
from utils.bulk_transfer import (  # noqa: E402
    create_transfer_job,
    decide_deferred_notifications,
    deliver_notifications,
    export_job_csv,
    get_job,
    job_counts,
    notification_counts,
    preflight_transfer,
    request_cancel,
    resume_job,
    run_transfer_job,
)
import utils.bulk_transfer as bulk_transfer  # noqa: E402
from utils.translations import get_message_text  # noqa: E402


def user(username, *, password="secret", total=10 * 1024**3, used=0):
    return {
        "username": username,
        "password": password,
        "max_download_bytes": total,
        "upload_bytes": used,
        "download_bytes": 0,
        "expiration_days": 30,
        "blocked": False,
        "unlimited_user": False,
        "delayed_start": True,
        "status": "on hold",
        "note": None,
    }


class FakeClient:
    def __init__(self, server_id, users=None, panel=BLITZ_PANEL, inbounds=None):
        self.server_id = server_id
        self.server_name = server_id.title()
        self.panel_type = panel
        self.users = dict(users or {})
        self.deleted = []
        self.inbounds = list(inbounds or [])
        self.delete_fails = False

    def get_users(self):
        return [dict(value) for value in self.users.values()]

    def get_user_result(self, username):
        if username in self.users:
            return {"status": "found", "data": dict(self.users[username])}
        return {"status": "missing", "data": None}

    def delete_user(self, username):
        if self.delete_fails:
            return None
        if username not in self.users:
            return None
        self.deleted.append(username)
        self.users.pop(username)
        return {"message": "deleted"}

    def get_user_uri(self, username):
        if username not in self.users:
            return None
        return {"normal_sub": f"https://sub.example/{self.server_id}/{username}"}

    def get_inbound_options(self):
        return list(self.inbounds)


class FakeMulti:
    def __init__(self, source, destination):
        self.clients = {source.server_id: source, destination.server_id: destination}
        self.servers = [
            {"id": source.server_id, "name": source.server_name, "panel": source.panel_type},
            {"id": destination.server_id, "name": destination.server_name, "panel": destination.panel_type},
        ]

    def get_client(self, server_id=None):
        return self.clients.get(server_id)

    def copy_user(self, spec):
        source = self.clients[spec.source.server_id]
        destination = self.clients[spec.destination_server_id]
        if spec.source.username in destination.users:
            return {"ok": False, "error": "destination_exists"}
        copied = dict(source.users[spec.source.username])
        destination.users[spec.source.username] = copied
        return {
            "ok": True,
            "source_server_id": source.server_id,
            "source_panel_type": source.panel_type,
            "destination_server_id": destination.server_id,
            "destination_server_name": destination.server_name,
            "panel_type": destination.panel_type,
            "expiry_rounded": False,
            "expiry_extension_seconds": 0,
            "normal_sub": "must-not-be-persisted",
        }


def spec(mode="copy", policy="disabled"):
    return BulkUserTransferSpec(
        mode=mode,
        source_server_id="source",
        destination_server_id="destination",
        requesting_admin="42",
        notification_policy=policy,
    )


def create_from_preflight(path, multi, transfer_spec):
    preview = preflight_transfer(transfer_spec, multi)
    assert preview["ok"]
    created = create_transfer_job(transfer_spec, preview, path=path)
    assert created["ok"]
    return created["job_id"]


def create_pending_migration_notice(
    path,
    *,
    username="alice",
    recipient_id="123",
    route_scope="main",
    policy="send",
):
    connection = database.get_connection(path)
    connection.execute(
        """INSERT INTO payments(scope,payment_id,user_id,status,payload_json)
           VALUES (?,?,?,?,?)""",
        (
            route_scope,
            f"payment-{username}",
            str(recipient_id),
            "completed",
            json.dumps({
                "user_id": int(recipient_id),
                "username": username,
                "server_id": "source",
                "status": "completed",
            }),
        ),
    )
    source = FakeClient("source", {username: user(username)})
    destination = FakeClient("destination")
    destination.server_name = "Internal Destination Name"
    multi = FakeMulti(source, destination)
    job_id = create_from_preflight(path, multi, spec("migrate", policy))
    assert run_transfer_job(job_id, multi_api=multi, path=path)
    return job_id, multi, destination


def test_schema_v5_has_transfer_tables(tmp_path):
    path = str(tmp_path / "state.db")
    connection = database.get_connection(path)
    assert database.schema_version(path) == 5
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "bulk_transfer_jobs", "bulk_transfer_items", "bulk_transfer_notifications"
    } <= tables


def test_mass_copy_uses_fixed_snapshot_and_never_changes_records(tmp_path):
    path = str(tmp_path / "state.db")
    source = FakeClient("source", {"alice": user("alice"), "bob": user("bob")})
    destination = FakeClient("destination")
    multi = FakeMulti(source, destination)
    job_id = create_from_preflight(path, multi, spec())

    source.users["late"] = user("late")
    run_transfer_job(job_id, multi_api=multi, path=path)

    assert set(source.users) == {"alice", "bob", "late"}
    assert set(destination.users) == {"alice", "bob"}
    assert job_counts(job_id, path=path)["completed"] == 2
    assert notification_counts(job_id, path=path) == {}
    persisted = database.get_connection(path).execute(
        "SELECT result_json FROM bulk_transfer_items WHERE job_id=? LIMIT 1", (job_id,)
    ).fetchone()[0]
    assert "must-not-be-persisted" not in persisted


def test_migrate_rehomes_payment_then_deletes_and_releases_notice(tmp_path):
    path = str(tmp_path / "state.db")
    connection = database.get_connection(path)
    payment = {
        "user_id": 123,
        "username": "alice",
        "server_id": "source",
        "status": "completed",
    }
    connection.execute(
        """INSERT INTO payments(scope,payment_id,user_id,status,payload_json)
           VALUES ('main','p1','123','completed',?)""",
        (json.dumps(payment),),
    )
    source = FakeClient("source", {"alice": user("alice")})
    destination = FakeClient("destination")
    multi = FakeMulti(source, destination)
    job_id = create_from_preflight(path, multi, spec("migrate", "send"))

    assert run_transfer_job(job_id, multi_api=multi, path=path)
    assert "alice" not in source.users
    assert "alice" in destination.users
    stored = json.loads(connection.execute(
        "SELECT payload_json FROM payments WHERE scope='main' AND payment_id='p1'"
    ).fetchone()[0])
    assert stored["server_id"] == "destination"
    assert notification_counts(job_id, path=path) == {"pending": 1}

    sent = []
    assert deliver_notifications(
        "main", lambda recipient, text: sent.append((recipient, text)),
        path=path, multi_api=multi,
    ) == 1
    assert sent[0][0] == 123
    assert "https://sub.example/destination/alice" in sent[0][1]
    assert notification_counts(job_id, path=path) == {"sent": 1}


@pytest.mark.parametrize("language", ("en", "fa", "ru", "tk"))
@pytest.mark.parametrize("direct", (False, True))
def test_customer_notice_is_localized_neutral_and_link_type_agnostic(
    tmp_path, language, direct
):
    path = str(tmp_path / "state.db")
    _job_id, multi, destination = create_pending_migration_notice(path)
    link = "https://new.example/alice"
    destination.get_user_uri = lambda _username: {
        "normal_sub": link,
        "direct": direct,
    }
    sent = []

    assert deliver_notifications(
        "main",
        lambda recipient, text: sent.append((recipient, text)),
        path=path,
        multi_api=multi,
        language_resolver=lambda _recipient: language,
    ) == 1

    expected = get_message_text(language, "migration_connection_updated").format(
        username="alice",
        link=link,
    )
    assert sent == [(123, expected)]
    lowered = sent[0][1].casefold()
    for internal_detail in (
        "internal destination name",
        "migrat",
        "blitz",
        "3x-ui",
        "expiry",
        "quota",
        "counter",
        "blocked",
    ):
        assert internal_detail not in lowered


def test_deferred_notice_uses_language_preference_at_delivery_time(tmp_path):
    path = str(tmp_path / "state.db")
    job_id, multi, destination = create_pending_migration_notice(
        path,
        policy="deferred",
    )
    languages = {123: "en"}
    assert notification_counts(job_id, path=path) == {"held": 1}
    assert decide_deferred_notifications(job_id, "42", "send", path=path)
    languages[123] = "ru"
    link = "https://new.example/alice"
    destination.get_user_uri = lambda _username: {"normal_sub": link}
    sent = []

    assert deliver_notifications(
        "main",
        lambda recipient, text: sent.append((recipient, text)),
        path=path,
        multi_api=multi,
        language_resolver=lambda recipient: languages[recipient],
    ) == 1
    assert sent[0][1] == get_message_text(
        "ru", "migration_connection_updated"
    ).format(username="alice", link=link)


@pytest.mark.parametrize("resolver_kind", ("invalid", "error"))
def test_customer_notice_language_lookup_falls_back_to_english(tmp_path, resolver_kind):
    path = str(tmp_path / "state.db")
    _job_id, multi, destination = create_pending_migration_notice(path)
    link = "https://new.example/alice"
    destination.get_user_uri = lambda _username: {"normal_sub": link}

    def resolve_language(_recipient):
        if resolver_kind == "error":
            raise OSError("language store unavailable")
        return "unsupported"

    sent = []
    assert deliver_notifications(
        "main",
        lambda recipient, text: sent.append((recipient, text)),
        path=path,
        multi_api=multi,
        language_resolver=resolve_language,
    ) == 1
    assert sent[0][1] == get_message_text(
        "en", "migration_connection_updated"
    ).format(username="alice", link=link)


def test_main_and_hosted_monitors_use_their_live_language_stores():
    main_source = (BOT_DIR / "tbot.py").read_text(encoding="utf-8")
    hosted_source = (BOT_DIR / "hosted_worker.py").read_text(encoding="utf-8")

    assert "language_resolver=get_user_language" in main_source
    assert "language_resolver=_language" in hosted_source


def test_deferred_notifications_require_explicit_send_or_discard(tmp_path):
    path = str(tmp_path / "state.db")
    connection = database.get_connection(path)
    connection.execute(
        """INSERT INTO payments(scope,payment_id,user_id,status,payload_json)
           VALUES ('main','p1','123','completed',?)""",
        (json.dumps({
            "user_id": 123, "username": "alice", "server_id": "source",
            "status": "completed",
        }),),
    )
    source = FakeClient("source", {"alice": user("alice")})
    destination = FakeClient("destination")
    multi = FakeMulti(source, destination)
    job_id = create_from_preflight(path, multi, spec("migrate", "deferred"))
    run_transfer_job(job_id, multi_api=multi, path=path)
    assert notification_counts(job_id, path=path) == {"held": 1}
    assert deliver_notifications("main", lambda *_: None, path=path, multi_api=multi) == 0
    assert decide_deferred_notifications(job_id, "42", "send", path=path)
    assert notification_counts(job_id, path=path) == {"pending": 1}

    source2 = FakeClient("source", {"bob": user("bob")})
    destination2 = FakeClient("destination")
    multi2 = FakeMulti(source2, destination2)
    connection.execute(
        """INSERT INTO payments(scope,payment_id,user_id,status,payload_json)
           VALUES ('main','p2','456','completed',?)""",
        (json.dumps({
            "user_id": 456, "username": "bob", "server_id": "source",
            "status": "completed",
        }),),
    )
    second = create_from_preflight(path, multi2, spec("migrate", "deferred"))
    run_transfer_job(second, multi_api=multi2, path=path)
    assert decide_deferred_notifications(second, "42", "discard", path=path)
    assert notification_counts(second, path=path) == {"discarded": 1}
    assert "bob" in destination2.users and "bob" not in source2.users


def test_collision_is_snapshotted_as_skipped_and_csv_has_no_secrets(tmp_path):
    path = str(tmp_path / "state.db")
    source = FakeClient("source", {"alice": user("alice", password="private")})
    destination = FakeClient("destination", {"alice": user("alice", password="other")})
    multi = FakeMulti(source, destination)
    preview = preflight_transfer(spec(), multi)
    assert preview["collisions"] == 1
    assert preview["eligible"] == 0
    assert create_transfer_job(spec(), preview, path=path)["error"] == "no_eligible_users"

    destination.users.clear()
    job_id = create_from_preflight(path, multi, spec())
    run_transfer_job(job_id, multi_api=multi, path=path)
    report = export_job_csv(job_id, path=path).decode("utf-8-sig")
    assert "alice" in report
    assert "private" not in report
    assert "https://" not in report


def test_hysteria_reverse_preflight_rejects_missing_auth_and_allowance(tmp_path):
    valid = user("valid", total=5 * 1024**3)
    valid.update({
        "unlimited_ip": True,
        "panel_type": "3x-ui",
        "credential_metadata": {"fields_present": ["auth"], "selected_field": "auth"},
        "inbound_ids": [7],
    })
    missing_auth = dict(valid, username="missing", password="")
    missing_auth["credential_metadata"] = {"fields_present": [], "selected_field": None}
    unlimited_traffic = dict(valid, username="unlimited", max_download_bytes=0)
    exhausted = dict(valid, username="exhausted", upload_bytes=5 * 1024**3)
    source = FakeClient(
        "source",
        {item["username"]: item for item in (valid, missing_auth, unlimited_traffic, exhausted)},
        panel="3x-ui",
        inbounds=[{"id": 7, "protocol": "hysteria2", "remark": "hy2"}],
    )
    destination = FakeClient("destination")
    preview = preflight_transfer(spec(), FakeMulti(source, destination))
    assert preview["eligible"] == 1
    assert preview["rejections"] == {
        "blitz_allowance_exhausted": 1,
        "blitz_unlimited_not_representable": 1,
        "source_auth_missing": 1,
    }


def test_record_update_failure_rolls_back_owned_destination(tmp_path, monkeypatch):
    path = str(tmp_path / "state.db")
    source = FakeClient("source", {"alice": user("alice")})
    destination = FakeClient("destination")
    multi = FakeMulti(source, destination)
    job_id = create_from_preflight(path, multi, spec("migrate", "disabled"))

    monkeypatch.setattr(
        bulk_transfer,
        "_rehome_records_and_hold_recipients",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("database failed")),
    )
    run_transfer_job(job_id, multi_api=multi, path=path)
    assert "alice" in source.users
    assert "alice" not in destination.users
    assert job_counts(job_id, path=path)["failed"] == 1
    item = get_job(job_id, path=path, include_items=True)["items"][0]
    assert item["error_code"] == "record_update_failed"


def test_source_delete_failure_keeps_destination_records_and_held_notice(tmp_path):
    path = str(tmp_path / "state.db")
    connection = database.get_connection(path)
    connection.execute(
        """INSERT INTO payments(scope,payment_id,user_id,status,payload_json)
           VALUES ('main','p1','123','completed',?)""",
        (json.dumps({
            "user_id": 123, "username": "alice", "server_id": "source",
            "status": "completed",
        }),),
    )
    source = FakeClient("source", {"alice": user("alice")})
    source.delete_fails = True
    destination = FakeClient("destination")
    multi = FakeMulti(source, destination)
    job_id = create_from_preflight(path, multi, spec("migrate", "send"))
    run_transfer_job(job_id, multi_api=multi, path=path)

    assert "alice" in source.users and "alice" in destination.users
    stored = json.loads(connection.execute(
        "SELECT payload_json FROM payments WHERE payment_id='p1'"
    ).fetchone()[0])
    assert stored["server_id"] == "destination"
    item = get_job(job_id, path=path, include_items=True)["items"][0]
    assert item["stage"] == "manual_review"
    assert item["delete_attempts"] == 3
    assert item["error_code"] == "source_delete_failed"
    assert notification_counts(job_id, path=path) == {"held": 1}


def test_rehomes_hosted_reseller_test_and_cleanup_references_without_audit_history(tmp_path):
    path = str(tmp_path / "state.db")
    connection = database.get_connection(path)
    config = {
        "username": "alice", "server_id": "source", "customer_telegram_id": 900,
        "historical_configs": [{"username": "alice", "server_id": "source"}],
    }
    connection.execute(
        "INSERT INTO resellers(reseller_id,status,payload_json) VALUES ('7','approved',?)",
        (json.dumps({"status": "approved", "configs": [config]}),),
    )
    connection.execute(
        """INSERT INTO reseller_configs(
               reseller_id,config_index,username,server_id,payload_json
           ) VALUES ('7',0,'alice','source',?)""",
        (json.dumps(config),),
    )
    hosted_payment = {
        "user_id": 900, "username": "alice", "server_id": "source",
        "status": "completed",
    }
    connection.execute(
        """INSERT INTO payments(scope,payment_id,user_id,status,payload_json)
           VALUES ('hosted:7','hp','900','completed',?)""",
        (json.dumps(hosted_payment),),
    )
    test_record = {
        "telegram_id": 901, "username": "alice", "server_id": "source",
        "historical_configs": [{"username": "alice", "server_id": "source"}],
    }
    cleanup = {
        "username": "alice", "server_id": "source", "cleanup_status": "pending",
    }
    connection.execute(
        """INSERT INTO kv_state(namespace,scope,state_key,value_json)
           VALUES ('test_configs','main','901',?)""",
        (json.dumps(test_record),),
    )
    connection.execute(
        """INSERT INTO kv_state(namespace,scope,state_key,value_json)
           VALUES ('expired_cleanup','main','source:alice',?)""",
        (json.dumps(cleanup),),
    )
    source = FakeClient("source", {"alice": user("alice")})
    destination = FakeClient("destination")
    multi = FakeMulti(source, destination)
    job_id = create_from_preflight(path, multi, spec("migrate", "deferred"))
    run_transfer_job(job_id, multi_api=multi, path=path)

    config_after = json.loads(connection.execute(
        "SELECT payload_json FROM reseller_configs WHERE reseller_id='7'"
    ).fetchone()[0])
    assert config_after["server_id"] == "destination"
    assert config_after["historical_configs"][0]["server_id"] == "source"
    parent_after = json.loads(connection.execute(
        "SELECT payload_json FROM resellers WHERE reseller_id='7'"
    ).fetchone()[0])
    assert parent_after["configs"][0]["server_id"] == "destination"
    assert parent_after["configs"][0]["historical_configs"][0]["server_id"] == "source"
    test_after = json.loads(connection.execute(
        "SELECT value_json FROM kv_state WHERE namespace='test_configs'"
    ).fetchone()[0])
    assert test_after["server_id"] == "destination"
    assert test_after["historical_configs"][0]["server_id"] == "source"
    cleanup_row = connection.execute(
        "SELECT state_key,value_json FROM kv_state WHERE namespace='expired_cleanup'"
    ).fetchone()
    assert cleanup_row["state_key"] == "destination:alice"
    assert json.loads(cleanup_row["value_json"])["server_id"] == "destination"
    # Hosted payment and reseller config identify the same hosted customer;
    # the outbox deduplicates that recipient while retaining the main test user.
    assert notification_counts(job_id, path=path) == {"held": 2}
    routes = {
        (row[0], row[1]) for row in connection.execute(
            "SELECT route_scope,recipient_id FROM bulk_transfer_notifications WHERE job_id=?",
            (job_id,),
        )
    }
    assert routes == {("hosted:7", "900"), ("main", "901")}


def test_only_one_active_job_and_sqlite_backup_preserves_journal(tmp_path):
    path = str(tmp_path / "state.db")
    source = FakeClient("source", {"alice": user("alice")})
    destination = FakeClient("destination")
    multi = FakeMulti(source, destination)
    preview = preflight_transfer(spec(), multi)
    first = create_transfer_job(spec(), preview, path=path)
    assert first["ok"]
    assert create_transfer_job(spec(), preview, path=path) == {
        "ok": False, "error": "active_job_exists"
    }

    backup = str(tmp_path / "backup.db")
    database.backup_database(backup, source=path)
    assert get_job(first["job_id"], path=backup, include_items=True)["items"][0]["username"] == "alice"


def test_restart_recovery_adopts_only_a_strictly_matching_interrupted_copy(tmp_path):
    path = str(tmp_path / "state.db")
    source = FakeClient("source", {"alice": user("alice")})
    destination = FakeClient("destination")
    multi = FakeMulti(source, destination)
    job_id = create_from_preflight(path, multi, spec())
    destination.users["alice"] = dict(source.users["alice"])
    database.get_connection(path).execute(
        "UPDATE bulk_transfer_items SET stage='copying' WHERE job_id=?", (job_id,)
    )
    assert run_transfer_job(job_id, multi_api=multi, path=path)
    assert job_counts(job_id, path=path)["completed"] == 1
    metadata = json.loads(get_job(job_id, path=path, include_items=True)["items"][0]["result_json"])
    assert metadata["recovered_after_restart"] is True

    source2 = FakeClient("source", {"bob": user("bob")})
    destination2 = FakeClient("destination")
    multi2 = FakeMulti(source2, destination2)
    second = create_from_preflight(path, multi2, spec())
    destination2.users["bob"] = dict(source2.users["bob"], password="collision")
    database.get_connection(path).execute(
        "UPDATE bulk_transfer_items SET stage='copying' WHERE job_id=?", (second,)
    )
    run_transfer_job(second, multi_api=multi2, path=path)
    item = get_job(second, path=path, include_items=True)["items"][0]
    assert item["stage"] == "manual_review"
    assert item["error_code"] == "interrupted_copy_ambiguous"


def test_unlimited_duration_is_eligible_and_restart_recovery_matches_without_expiry(tmp_path):
    path = str(tmp_path / "state.db")
    unlimited = user("alice")
    unlimited.update({
        "expiration_days": 0,
        "delayed_start": False,
        "status": "Offline",
        "account_creation_date": None,
    })
    source = FakeClient("source", {"alice": unlimited})
    destination = FakeClient("destination")
    multi = FakeMulti(source, destination)

    preview = preflight_transfer(spec(), multi)
    assert preview["eligible"] == 1
    assert preview["rejections"] == {}
    job_id = create_from_preflight(path, multi, spec())
    destination.users["alice"] = dict(unlimited)
    database.get_connection(path).execute(
        "UPDATE bulk_transfer_items SET stage='copying' WHERE job_id=?", (job_id,)
    )

    assert run_transfer_job(job_id, multi_api=multi, path=path)
    assert job_counts(job_id, path=path)["completed"] == 1
    metadata = json.loads(
        get_job(job_id, path=path, include_items=True)["items"][0]["result_json"]
    )
    assert metadata["expiry_extension_seconds"] == 0
    assert metadata["recovered_after_restart"] is True


def test_unlimited_duration_migration_deletes_source_after_verified_copy(tmp_path):
    path = str(tmp_path / "state.db")
    unlimited = user("alice")
    unlimited.update({
        "expiration_days": 0,
        "delayed_start": False,
        "status": "Offline",
        "account_creation_date": None,
    })
    source = FakeClient("source", {"alice": unlimited})
    destination = FakeClient("destination")
    multi = FakeMulti(source, destination)
    job_id = create_from_preflight(path, multi, spec("migrate"))

    assert run_transfer_job(job_id, multi_api=multi, path=path)

    assert source.deleted == ["alice"]
    assert destination.users["alice"]["expiration_days"] == 0
    assert job_counts(job_id, path=path)["completed"] == 1


def test_cancel_after_current_state_is_resumable(tmp_path, monkeypatch):
    path = str(tmp_path / "state.db")
    source = FakeClient("source", {"alice": user("alice")})
    destination = FakeClient("destination")
    multi = FakeMulti(source, destination)
    job_id = create_from_preflight(path, multi, spec())
    assert request_cancel(job_id, "42", path=path)
    assert run_transfer_job(job_id, multi_api=multi, path=path)
    assert get_job(job_id, path=path)["status"] == "cancelled"
    assert job_counts(job_id, path=path)["remaining"] == 1

    monkeypatch.setattr(bulk_transfer, "start_transfer_worker", lambda **_kwargs: None)
    assert resume_job(job_id, "42", path=path)
    assert get_job(job_id, path=path)["status"] == "queued"
    assert run_transfer_job(job_id, multi_api=multi, path=path)
    assert get_job(job_id, path=path)["status"] == "completed"


def test_notification_failures_never_roll_back_completed_migration(tmp_path):
    path = str(tmp_path / "state.db")
    connection = database.get_connection(path)
    connection.execute(
        """INSERT INTO payments(scope,payment_id,user_id,status,payload_json)
           VALUES ('main','p1','123','completed',?)""",
        (json.dumps({
            "user_id": 123, "username": "alice", "server_id": "source",
            "status": "completed",
        }),),
    )
    source = FakeClient("source", {"alice": user("alice")})
    destination = FakeClient("destination")
    multi = FakeMulti(source, destination)
    job_id = create_from_preflight(path, multi, spec("migrate", "send"))
    run_transfer_job(job_id, multi_api=multi, path=path)

    def fail_sender(_recipient, _text):
        raise RuntimeError("Telegram unavailable")

    for _ in range(5):
        deliver_notifications("main", fail_sender, path=path, multi_api=multi)
        connection.execute(
            """UPDATE bulk_transfer_notifications SET next_attempt_at=NULL
               WHERE job_id=? AND status='pending'""",
            (job_id,),
        )
    assert notification_counts(job_id, path=path) == {"permanent_failed": 1}
    assert "alice" in destination.users and "alice" not in source.users
    assert get_job(job_id, path=path)["status"] == "completed"
