import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BotOnlyRepositoryTests(unittest.TestCase):
    def test_local_vpn_management_scripts_are_absent(self):
        legacy_scripts = {
            "change_port.sh",
            "ip.sh",
            "restart.sh",
            "server_info.sh",
            "update.sh",
        }
        script_dir = ROOT / "core" / "scripts" / "ajib"

        self.assertTrue(legacy_scripts.isdisjoint(path.name for path in script_dir.iterdir()))

    def test_runtime_files_do_not_reference_removed_local_vpn_service(self):
        forbidden = (
            "ajib-server.service",
            ".configs.env",
            "get.hy2.sh",
            "127.0.0.1:25413",
        )

        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or "tests" in path.parts:
                continue
            if path.name == "changelog" or "__pycache__" in path.parts:
                continue
            if path.suffix not in {".py", ".sh", ".md"} and path.name != "README.md":
                continue
            text = path.read_text(errors="ignore")
            for value in forbidden:
                self.assertNotIn(value, text, f"{value!r} remains in {path.relative_to(ROOT)}")

    def test_documentation_describes_an_external_api_bot(self):
        readme = (ROOT / "README.md").read_text()

        self.assertIn("does not install or run a VPN server", readme)
        self.assertIn("external VPN servers", readme)
        self.assertIn("License-GPLv3", readme)


if __name__ == "__main__":
    unittest.main()
