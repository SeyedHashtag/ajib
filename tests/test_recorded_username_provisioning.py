import ast
import importlib.util
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "core" / "scripts" / "telegrambot"
USERNAME_UTILS_PATH = BOT_DIR / "utils" / "username_utils.py"
USERNAME_SPEC = importlib.util.spec_from_file_location(
    "username_utils_provisioning_under_test",
    USERNAME_UTILS_PATH,
)
username_utils = importlib.util.module_from_spec(USERNAME_SPEC)
USERNAME_SPEC.loader.exec_module(username_utils)


def source_function(relative_path, function_name):
    source = (BOT_DIR / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )


class FakeClient:
    server_id = "server-1"

    def __init__(self, calls):
        self.calls = calls

    def add_user(self, username, traffic_limit, expiration_days, **kwargs):
        self.calls.append((username, traffic_limit, expiration_days, kwargs))
        return {"created": True}

    def get_user_uri(self, username):
        return {"normal_sub": f"https://example.test/{username}"}


class FakeMultiServerAPI:
    def __init__(self, calls):
        self.calls = calls
        self.client = FakeClient(calls)

    def create_user_with_retry(self, allocator, creator, fallback_client=None):
        username = allocator(set())
        result = creator(self.client, username)
        return username, result, self.client


def compile_function(relative_path, function_name, namespace):
    module = ast.Module(
        body=[source_function(relative_path, function_name)],
        type_ignores=[],
    )
    exec(
        compile(ast.fix_missing_locations(module), str(relative_path), "exec"),
        namespace,
    )
    return namespace[function_name]


class RecordedUsernameProvisioningTests(unittest.TestCase):
    def creator_namespace(self, recorded_usernames, calls):
        return {
            "MultiServerAPI": lambda: FakeMultiServerAPI(calls),
            "allocate_username": username_utils.allocate_username,
            "build_user_note": lambda **kwargs: "note",
            "load_recorded_usernames": lambda: set(recorded_usernames),
            "RecordedUsernameLoadError": username_utils.RecordedUsernameLoadError,
            "logging": logging,
        }

    def test_standard_sale_skips_a_recorded_deleted_username(self):
        calls = []
        namespace = self.creator_namespace({"s123"}, calls)
        create_sale_user = compile_function(
            Path("utils/purchase_plan.py"),
            "create_sale_user_with_note",
            namespace,
        )

        username, result, _client = create_sale_user(
            None,
            123,
            5,
            30,
            False,
        )

        self.assertEqual(username, "s123a")
        self.assertEqual(result, {"created": True})
        self.assertEqual(calls[0][0], "s123a")

    def test_reseller_creation_skips_a_recorded_deleted_username(self):
        calls = []
        namespace = self.creator_namespace({"r321"}, calls)
        create_reseller_user = compile_function(
            Path("utils/reseller_handlers.py"),
            "_create_reseller_user_with_note",
            namespace,
        )

        username, result, _client = create_reseller_user(
            None,
            321,
            5,
            30,
            "customer",
        )

        self.assertEqual(username, "r321a")
        self.assertEqual(result, {"created": True})
        self.assertEqual(calls[0][0], "r321a")

    def test_sale_history_failure_stops_before_vpn_creation(self):
        calls = []

        def fail_load():
            raise username_utils.RecordedUsernameLoadError("damaged payments.json")

        namespace = self.creator_namespace(set(), calls)
        namespace["load_recorded_usernames"] = fail_load
        create_sale_user = compile_function(
            Path("utils/purchase_plan.py"),
            "create_sale_user_with_note",
            namespace,
        )

        result = create_sale_user(None, 123, 5, 30, False)

        self.assertEqual(result, (None, None, None))
        self.assertEqual(calls, [])

    def test_individual_test_creation_merges_recorded_history(self):
        calls = []
        released = []
        marked = []
        namespace = {
            "is_test_creation_disabled": lambda: False,
            "_claim_test_config_creation": lambda user_id: True,
            "_release_test_config_creation": lambda user_id: released.append(user_id),
            "load_recorded_usernames": lambda: {"t123"},
            "RecordedUsernameLoadError": username_utils.RecordedUsernameLoadError,
            "MultiServerAPI": lambda: FakeMultiServerAPI(calls),
            "allocate_username": username_utils.allocate_username,
            "build_user_note": lambda **kwargs: "note",
            "TEST_TRAFFIC_GB": 1,
            "TEST_DAYS": 30,
            "mark_test_config_used": lambda *args, **kwargs: marked.append((args, kwargs)),
            "_send_created_test_config": lambda *args, **kwargs: None,
            "logging": logging,
            "bot": SimpleNamespace(send_message=lambda *args, **kwargs: None),
        }
        create_test = compile_function(
            Path("utils/test_config.py"),
            "create_test_config",
            namespace,
        )

        self.assertTrue(create_test(123, 456))
        self.assertEqual(calls[0][0], "t123a")
        self.assertEqual(marked[0][1]["username"], "t123a")
        self.assertEqual(released, [])

    def test_test_history_failure_releases_claim_without_creating(self):
        released = []
        multi_created = []

        def fail_load():
            raise username_utils.RecordedUsernameLoadError("damaged test_configs.json")

        namespace = {
            "is_test_creation_disabled": lambda: False,
            "_claim_test_config_creation": lambda user_id: True,
            "_release_test_config_creation": lambda user_id: released.append(user_id),
            "load_recorded_usernames": fail_load,
            "RecordedUsernameLoadError": username_utils.RecordedUsernameLoadError,
            "MultiServerAPI": lambda: multi_created.append(True),
            "logging": logging,
            "bot": SimpleNamespace(send_message=lambda *args, **kwargs: None),
        }
        create_test = compile_function(
            Path("utils/test_config.py"),
            "create_test_config",
            namespace,
        )

        self.assertFalse(create_test(123, 456))
        self.assertEqual(released, [123])
        self.assertEqual(multi_created, [])

    def test_bulk_test_snapshot_starts_with_recorded_history(self):
        class BulkMultiServerAPI:
            def iter_clients(self, include_disabled=True):
                client = SimpleNamespace(get_users=lambda: {})
                yield {"enabled": True, "weight": 1}, client

            def extract_usernames(self, users):
                return set()

            def active_user_count(self, users):
                return 0

        namespace = {
            "MultiServerAPI": BulkMultiServerAPI,
            "load_recorded_usernames": lambda: {"t123"},
            "_safe_server_weight": lambda value: 1.0,
        }
        build_state = compile_function(
            Path("utils/test_config.py"),
            "_build_bulk_test_config_state",
            namespace,
        )

        usernames, server_states = build_state()

        self.assertEqual(usernames, {"t123"})
        self.assertEqual(len(server_states), 1)


if __name__ == "__main__":
    unittest.main()
