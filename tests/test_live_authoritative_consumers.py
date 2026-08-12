import ast
import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = ROOT / "core" / "scripts" / "telegrambot" / "hosted_worker.py"
ACCOUNT_STATE_PATH = ROOT / "core" / "scripts" / "telegrambot" / "utils" / "account_state.py"


def _load_account_state():
    spec = importlib.util.spec_from_file_location(
        "live_authoritative_account_state", ACCOUNT_STATE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _worker_function(name):
    tree = ast.parse(WORKER_PATH.read_text(encoding="utf-8"))
    return next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_hosted_onboarding_ignores_old_issuance_after_connection():
    account_state = _load_account_state()
    live = {
        "status": "Online",
        "blocked": False,
        "account_creation_date": "2026-08-01T00:00:00+00:00",
        "expiration_days": 60,
        "max_download_bytes": 100,
        "upload_bytes": 1,
        "download_bytes": 0,
    }
    cycle = account_state.resolve_service_cycle({
        "old": {
            "username": "r1",
            "server_id": "s1",
            "days": 1,
            "status": "completed",
            "created_at": "2020-01-01T00:00:00+00:00",
        }
    }, username="r1", server_id="s1", source="hosted_customer")

    class FakeMultiServerAPI:
        def find_user(self, username, preferred_server_id=None):
            return object(), live

    namespace = {
        "_find_customer_configs": lambda _user_id: [
            {"username": "r1", "server_id": "s1"}
        ],
        "MultiServerAPI": FakeMultiServerAPI,
        "_hosted_service_cycle": lambda _config: cycle,
        "inspect_account": account_state.inspect_account,
        "PanelState": account_state.PanelState,
        "EntitlementState": account_state.EntitlementState,
        "_test_record": lambda _user_id: {},
    }
    module = ast.Module(body=[_worker_function("_customer_onboarding_state")], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "hosted_worker.py", "exec"), namespace)

    state, _configs = namespace["_customer_onboarding_state"](123)

    assert state == "paid"


def test_hosted_notifications_use_canonical_service_fields():
    source = ast.get_source_segment(
        WORKER_PATH.read_text(encoding="utf-8"),
        _worker_function("_customer_notification_monitor"),
    )

    assert "account.service_days_remaining" in source
    assert "account.service_duration_days" in source
    assert "account.service_marker" in source
    assert "account.entitlement_days_remaining" not in source
