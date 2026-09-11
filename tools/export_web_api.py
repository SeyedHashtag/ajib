"""Export the OpenAPI contract without connecting to production services."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("AJIB_BOT_ROLE", "api")
# A factory builds routes only; lifespan database initialization is not entered.
from core.web.app import create_app
from core.web.settings import Settings

if __name__ == "__main__":
    schema = create_app(settings=Settings()).openapi()
    path = ROOT / "docs" / "web-openapi.json"
    path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {len(schema['paths'])} paths")
