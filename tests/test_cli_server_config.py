import ast
import json
import math
import types
import unittest
from pathlib import Path


CLI_API_PATH = Path(__file__).resolve().parents[1] / "core" / "cli_api.py"


class InvalidInputError(Exception):
    pass


def load_start_telegram_bot():
    tree = ast.parse(CLI_API_PATH.read_text(encoding="utf-8"))
    target = next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "start_telegram_bot"
    )
    module = ast.Module(body=[target], type_ignores=[])
    commands = []
    namespace = {
        "json": json,
        "math": math,
        "InvalidInputError": InvalidInputError,
        "Command": types.SimpleNamespace(
            INSTALL_TELEGRAMBOT=types.SimpleNamespace(value="runbot.sh")
        ),
        "run_cmd": lambda command: commands.append(command),
    }
    exec(compile(module, str(CLI_API_PATH), "exec"), namespace)
    return namespace["start_telegram_bot"], commands


class CLIServerConfigTests(unittest.TestCase):
    def test_legacy_server_format_defaults_to_blitz(self):
        start, commands = load_start_telegram_bot()

        start("bot", "1", "", "", servers=["primary=https://b.example,t,2,false"])

        servers = json.loads(commands[0][-1])
        self.assertEqual(servers[0]["panel"], "blitz")
        self.assertEqual(servers[0]["default_inbound_ids"], [])
        self.assertEqual(servers[0]["default_limit_ip"], 0)

    def test_three_x_fields_round_trip(self):
        start, commands = load_start_telegram_bot()

        start("bot", "1", "", "", servers=[
            "hy2=https://x.example,bearer-token,1,true,3x-ui,4|7,2"
        ])

        server = json.loads(commands[0][-1])[0]
        self.assertEqual(server["panel"], "3x-ui")
        self.assertEqual(server["default_inbound_ids"], [4, 7])
        self.assertEqual(server["default_limit_ip"], 2)

    def test_enabled_three_x_requires_default_inbounds(self):
        start, _commands = load_start_telegram_bot()

        with self.assertRaisesRegex(InvalidInputError, "require default inbound"):
            start("bot", "1", "", "", servers=[
                "hy2=https://x.example,bearer-token,1,true,3x-ui"
            ])

    def test_disabled_three_x_can_be_copy_only(self):
        start, commands = load_start_telegram_bot()

        start("bot", "1", "", "", servers=[
            "hy2=https://x.example,bearer-token,1,false,3x-ui,,1"
        ])

        server = json.loads(commands[0][-1])[0]
        self.assertFalse(server["enabled"])
        self.assertEqual(server["default_inbound_ids"], [])

    def test_zero_weight_is_preserved_and_three_x_inbounds_are_optional(self):
        start, commands = load_start_telegram_bot()

        start("bot", "1", "", "", servers=[
            "hy2=https://x.example,bearer-token,0,true,3x-ui,,1"
        ])

        server = json.loads(commands[0][-1])[0]
        self.assertEqual(server["weight"], 0)
        self.assertTrue(server["enabled"])
        self.assertEqual(server["default_inbound_ids"], [])

    def test_negative_and_non_finite_weights_are_rejected(self):
        start, _commands = load_start_telegram_bot()

        for weight in ("-1", "nan", "inf"):
            with self.subTest(weight=weight):
                with self.assertRaisesRegex(InvalidInputError, "finite non-negative"):
                    start("bot", "1", "", "", servers=[
                        f"primary=https://b.example,t,{weight},true"
                    ])


if __name__ == "__main__":
    unittest.main()
