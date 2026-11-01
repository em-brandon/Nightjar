"""Secrets mounted as files, not environment variables, in production.

    python -m unittest tests.test_config_secrets -v

No database. Each test builds its own Settings from a temporary directory, with the project's .env and the process
environment kept out of it, so a developer's real values can neither hide a failure nor leak into one.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import auth, config  # noqa: E402

SECRET_NAMES = ("CONSOLE_TOKEN", "DATABASE_URL", "PUSHOVER_TOKEN", "PUSHOVER_USER", "PUSHOVER_CALLBACK_TOKEN", "DEADMAN_URL", "PRESENCE_TOKEN", "SECRET_MASTER_KEY")


class SecretsFromFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        # the environment without any of the settings under test
        env = {k: v for k, v in os.environ.items() if k.upper() not in SECRET_NAMES}
        p = patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def write(self, name: str, value: str) -> None:
        Path(self.dir.name, name).write_text(value, encoding="utf-8")

    def settings(self, **kw):
        # _env_file=None: the developer's own .env must not take part
        return config.Settings(_env_file=None, _secrets_dir=self.dir.name, **kw)

    def test_a_value_in_a_file_is_used_and_its_trailing_newline_is_not(self):
        self.write("console_token", "token-from-a-file-0123456789\n")
        self.assertEqual(self.settings().console_token, "token-from-a-file-0123456789")

    def test_every_secret_the_deploy_stack_mounts_is_read_by_its_own_name(self):
        values = {
            "console_token": "console-value-0123456789",
            "database_url": "postgresql+asyncpg://u:p@db:5432/n",
            "pushover_token": "push-token",
            "pushover_user": "push-user",
            "pushover_callback_token": "callback-secret",
            "deadman_url": "https://hc-ping.com/uuid",
        }
        for name, value in values.items():
            self.write(name, value)
        s = self.settings()
        for name, value in values.items():
            self.assertEqual(getattr(s, name), value, name)

    def test_an_environment_variable_beats_a_file(self):
        # local development keeps using .env; production sets no variable for a setting it mounts
        self.write("console_token", "from-the-file-0123456789")
        with patch.dict(os.environ, {"CONSOLE_TOKEN": "from-the-environment-0123"}):
            self.assertEqual(self.settings().console_token, "from-the-environment-0123")

    def test_an_empty_file_is_an_empty_setting_not_an_error(self):
        # optional secrets are created empty by deploy/make-secrets.sh so Compose can mount them
        self.write("pushover_token", "")
        self.assertEqual(self.settings().pushover_token, "")

    def test_a_setting_with_no_file_keeps_its_default(self):
        self.assertEqual(self.settings().pushover_token, "")
        self.assertEqual(self.settings().quiet_tz, "Africa/Nairobi")

    def test_a_missing_directory_is_ignored_not_warned_about(self):
        self.assertIsNone(config.secrets_dir(os.path.join(self.dir.name, "nope")))
        self.assertEqual(config.secrets_dir(self.dir.name), self.dir.name)

    def test_the_directory_can_be_moved_with_an_environment_variable(self):
        with patch.dict(os.environ, {"NIGHTJAR_SECRETS_DIR": self.dir.name}):
            self.assertEqual(config.secrets_dir(), self.dir.name)


class StartupGuardStillHolds(unittest.TestCase):
    """The hub refuses to run in production without a console token. That must still be true when the token is a file."""

    def settings_with(self, token_file_contents: str | None, **kw):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dirs = getattr(self, "dirs", []) + [d]
        if token_file_contents is not None:
            Path(d.name, "console_token").write_text(token_file_contents, encoding="utf-8")
        Path(d.name, "secret_master_key").write_text("a-master-key-from-a-file-0123456789abcdef\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k.upper() not in SECRET_NAMES}
        with patch.dict(os.environ, env, clear=True):
            return config.Settings(_env_file=None, _secrets_dir=d.name, **kw)

    def check(self, s):
        with patch.object(auth, "settings", s):
            auth.check_config()

    def test_a_long_enough_token_from_a_file_lets_production_start(self):
        self.check(self.settings_with("a-console-token-from-a-file-0123456789\n", verify_signatures=True))

    def test_production_refuses_to_start_when_the_token_file_is_missing(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.check(self.settings_with(None, verify_signatures=True))
        self.assertIn("/run/secrets/console_token", str(ctx.exception), "the message says where the file goes")

    def test_production_refuses_to_start_when_the_token_file_is_empty(self):
        with self.assertRaises(RuntimeError):
            self.check(self.settings_with("", verify_signatures=True))

    def test_a_token_file_that_is_too_short_is_refused_even_in_development(self):
        with self.assertRaises(RuntimeError):
            self.check(self.settings_with("short"))

    def test_the_master_key_comes_from_a_file_and_production_refuses_without_it(self):
        s = self.settings_with("a-console-token-from-a-file-0123456789\n", verify_signatures=True)
        self.assertEqual(s.secret_master_key, "a-master-key-from-a-file-0123456789abcdef")
        (Path(self.dirs[-1].name) / "secret_master_key").unlink()
        with self.assertRaises(RuntimeError) as ctx:
            self.check(config.Settings(_env_file=None, _secrets_dir=self.dirs[-1].name, verify_signatures=True))
        self.assertIn("/run/secrets/secret_master_key", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
