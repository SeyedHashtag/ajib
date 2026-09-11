"""Generate a reviewable transport-entrypoint inventory without importing the bot."""
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def collect():
    rows = []
    for path in sorted((ROOT / "core").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = [ast.unparse(item) for item in node.decorator_list]
            entrypoints = [item for item in decorators if any(word in item for word in (
                "message_handler", "callback_query_handler", ".command(", ".group("))]
            if entrypoints:
                rows.append({"source": path.relative_to(ROOT).as_posix(),
                             "line": node.lineno, "function": node.name,
                             "entrypoints": entrypoints})
    return rows


if __name__ == "__main__":
    target = ROOT / "docs" / "web-entrypoints.json"
    target.write_text(json.dumps(collect(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Recorded {len(collect())} entrypoints in {target.relative_to(ROOT)}")
