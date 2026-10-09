"""Fingerprint stability, with no database.

    python -m unittest tests.test_fingerprint -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import fingerprint as fp  # noqa: E402


def frames(*modules, function="render"):
    return [{"module": m, "function": function, "lineno": 10} for m in modules]


class BundleHashes(unittest.TestCase):
    def test_a_rebuild_does_not_change_the_fingerprint(self):
        before = fp.compute(1, "TypeError", "x is undefined", frames("index-3fa9c1B2.js"))
        after = fp.compute(1, "TypeError", "x is undefined", frames("index-Zq81mK0p.js"))
        self.assertEqual(before, after)

    def test_every_common_bundler_shape_is_stripped(self):
        for name, stripped in {
            "index-3fa9c1B2.js": "index.js",  # vite
            "vendor.CxZ9aBq1.js": "vendor.js",  # vite, dot form
            "main.3f9a1c2e.js": "main.js",  # webpack, 8 hex
            "main.3f9a1c2e4b5d6a7c8e9f.js": "main.js",  # webpack, 20 hex
            "app.Bx7_dQ-2.css": "app.css",
            "chunk-A1b2C3d4.mjs": "chunk.mjs",
        }.items():
            with self.subTest(name=name):
                self.assertEqual(fp.frame_module(name), stripped)

    def test_a_query_string_or_url_is_reduced_to_the_file(self):
        self.assertEqual(fp.frame_module("https://x.example/assets/index-3fa9c1B2.js?v=12#a"), "index.js")

    def test_real_names_are_left_alone(self):
        for name in ("index-database.js", "main.py", "checkout.js", "react-dom.production.min.js",
                     "use-fetch.js", "views/Cart.jsx", "app-header.js"):
            with self.subTest(name=name):
                self.assertEqual(fp.frame_module(name), name.rsplit("/", 1)[-1])

    def test_different_files_still_differ(self):
        a = fp.compute(1, "TypeError", "x", frames("cart-3fa9c1B2.js"))
        b = fp.compute(1, "TypeError", "x", frames("checkout-3fa9c1B2.js"))
        self.assertNotEqual(a, b)

    def test_server_fingerprints_are_unchanged(self):
        # a regression guard: a Python frame must hash exactly as it did before bundle stripping
        got = fp.compute(7, "KeyError", "'abc'", [{"module": "orders.py", "function": "load", "lineno": 3}])
        self.assertEqual(got, fp.compute(7, "KeyError", "'zzz'", [{"module": "orders.py", "function": "load", "lineno": 99}]))
        self.assertEqual(len(got), 32)


if __name__ == "__main__":
    unittest.main()
