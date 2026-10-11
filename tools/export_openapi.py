"""Write the hub's OpenAPI schema to console/src/api/openapi.json.

The console's API types are generated from this file (`npm run types` in console/). It is read
from the app object rather than a running hub, because the hub turns /openapi.json off when auth
is on. tests/test_openapi_snapshot.py fails if the committed file is stale.

    python tools/export_openapi.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.main import app  # noqa: E402

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "console", "src", "api", "openapi.json")


def render() -> str:
    # sorted and indented so a diff shows exactly what changed, and the same schema is always the same bytes
    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    with open(PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(render())
    print(f"wrote {PATH}")
