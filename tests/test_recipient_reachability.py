import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "core/scripts/telegrambot/utils/recipient_reachability.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location(
        "recipient_reachability_under_test",
        MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecipientReachabilityTests(unittest.TestCase):
    def test_registry_load_mark_clear_and_reset_are_idempotent(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "broadcast_failed_users.json"
            module.UNREACHABLE_RECIPIENTS_PATH = str(path)
            module._state_helpers = lambda: None

            self.assertEqual(module.load_unreachable_recipients(), set())
            self.assertTrue(module.mark_recipient_unreachable(123))
            self.assertFalse(module.mark_recipient_unreachable("123"))
            self.assertEqual(
                module.mark_recipients_unreachable((123, 456, 789)),
                {"456", "789"},
            )
            self.assertEqual(
                module.load_unreachable_recipients(),
                {"123", "456", "789"},
            )
            self.assertTrue(module.clear_recipient_unreachable(123))
            self.assertFalse(module.clear_recipient_unreachable(123))
            self.assertEqual(module.load_unreachable_recipients(), {"456", "789"})

            module.reset_unreachable_recipients()
            self.assertFalse(path.exists())
