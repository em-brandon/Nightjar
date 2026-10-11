"""deploy/make-secrets.sh: it makes the secret files the production stack mounts, exactly and without clobbering anything.

    python -m unittest tests.test_make_secrets -v

Runs the real script with bash and openssl in a temporary directory. Skipped, by name, where either is missing.
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "make-secrets.sh"
BASH = shutil.which("bash")
OPENSSL = shutil.which("openssl")


@unittest.skipUnless(BASH and OPENSSL, "needs bash and openssl")
class MakeSecrets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # the script finds the project root as the parent of its own folder, so give it a root of its own
        self.root = Path(self.tmp.name)
        (self.root / "deploy").mkdir()
        shutil.copy(SCRIPT, self.root / "deploy" / "make-secrets.sh")
        (self.root / ".env").write_text("POSTGRES_USER=nightjar\nPOSTGRES_DB=nightjar\n", encoding="utf-8")
        self.dir = self.root / "secrets"

    def run_script(self, *args, env=None):
        full_env = {k: v for k, v in os.environ.items() if k not in ("POSTGRES_USER", "POSTGRES_DB")}
        full_env.update(env or {})
        return subprocess.run([BASH, str(self.root / "deploy" / "make-secrets.sh"), *args], capture_output=True, text=True, env=full_env)

    def read(self, name: str) -> str:
        return (self.dir / name).read_bytes().decode("utf-8")

    def test_it_creates_every_file_the_compose_file_mounts(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = {"postgres_password", "database_url", "console_token", "pushover_callback_token", "pushover_token", "pushover_user", "deadman_url", "presence_token"}
        self.assertEqual({p.name for p in self.dir.iterdir()}, expected)

    def test_no_file_has_a_carriage_return_or_a_newline_anywhere(self):
        # openssl on Windows ends its output with CRLF; a stray \r in a token or a URL is a bug to chase for an evening
        self.run_script()
        for path in self.dir.iterdir():
            raw = path.read_bytes()
            self.assertNotIn(b"\r", raw, path.name)
            self.assertNotIn(b"\n", raw, path.name)

    def test_the_generated_values_have_the_shape_the_hub_needs(self):
        self.run_script()
        self.assertRegex(self.read("postgres_password"), r"^[0-9a-f]{64}$", "hex, so it is safe inside a database URL")
        self.assertGreaterEqual(len(self.read("console_token")), 16, "the hub refuses a shorter token")
        self.assertRegex(self.read("pushover_callback_token"), r"^[0-9a-f]{48}$")

    def test_the_database_url_is_built_from_the_password_and_the_settings(self):
        self.run_script()
        self.assertEqual(self.read("database_url"), f"postgresql+asyncpg://nightjar:{self.read('postgres_password')}@db:5432/nightjar")

    def test_the_ones_you_fill_in_are_created_empty_so_compose_can_mount_them(self):
        self.run_script()
        for name in ("pushover_token", "pushover_user", "deadman_url", "presence_token"):
            self.assertEqual(self.read(name), "", name)

    def test_a_second_run_changes_nothing_and_says_what_it_kept(self):
        self.run_script()
        before = {p.name: p.read_bytes() for p in self.dir.iterdir()}
        (self.dir / "pushover_token").write_text("a-real-pushover-token", encoding="utf-8")  # filled in by hand
        before["pushover_token"] = b"a-real-pushover-token"
        again = self.run_script()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual({p.name: p.read_bytes() for p in self.dir.iterdir()}, before, "never overwrites a secret, least of all one you typed")
        self.assertIn("left alone", again.stdout)
        self.assertNotIn("created:", again.stdout)

    def test_it_fills_in_only_what_is_missing(self):
        self.run_script()
        (self.dir / "console_token").unlink()
        before_password = self.read("postgres_password")
        self.run_script()
        self.assertEqual(self.read("postgres_password"), before_password, "the existing database password is kept")
        self.assertGreaterEqual(len(self.read("console_token")), 16, "the missing token is made")

    @unittest.skipIf(os.name == "nt", "file modes are not meaningful on Windows")
    def test_the_directory_is_private_to_its_owner(self):
        self.run_script()
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)

    def test_it_refuses_to_guess_the_database_name_or_user(self):
        (self.root / ".env").write_text("HUB_DOMAIN=x\n", encoding="utf-8")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("POSTGRES_USER", result.stderr)
        self.assertFalse(self.dir.exists() and any(self.dir.iterdir()), "and writes nothing")

    def test_the_names_match_the_secrets_the_compose_file_declares(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        declared = set(re.findall(r"file: \./secrets/(\w+)", compose))
        self.run_script()
        self.assertEqual({p.name for p in self.dir.iterdir()}, declared, "the script and the compose file must agree on every name")


if __name__ == "__main__":
    unittest.main()
