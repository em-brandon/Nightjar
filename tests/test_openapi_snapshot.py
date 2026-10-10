"""The committed OpenAPI file must match the hub, or the console's generated types are stale.

If this fails: python tools/export_openapi.py && (cd console && npm run types), then commit both.
Needs no database and no Node.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import export_openapi  # noqa: E402


class OpenApiSnapshot(unittest.TestCase):
    def test_the_committed_schema_matches_the_hub(self):
        with open(export_openapi.PATH, encoding="utf-8", newline="") as f:
            committed = f.read()
        self.assertEqual(
            committed,
            export_openapi.render(),
            "console/src/api/openapi.json is stale: run python tools/export_openapi.py and npm run types",
        )


if __name__ == "__main__":
    unittest.main()
