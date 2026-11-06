"""The contract between each server-side reporter and the hub, against a real throwaway database.

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest tests.test_server_reporters -v

Each reporter (client/node, client/php, client/java) is run for real. Its signed request is then given to
the hub with VERIFY_SIGNATURES on, so a mismatch in how a language builds the JSON or the HMAC fails here,
rather than on the first production error. A runtime that is not installed is skipped by name, and the
skip says so; it does not pass silently.
"""

import asyncio
import glob
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import httpx  # noqa: E402
from sqlalchemy import pool, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from hub import migrate  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.db import get_session  # noqa: E402
from hub.main import app  # noqa: E402

HUB = "https://hub.test"  # https: the reporters refuse plain http to anywhere but this machine


def _admin_url() -> str:
    return make_url(settings.database_url).set(database="postgres").render_as_string(hide_password=False)


async def _admin(sql: str) -> None:
    engine = create_async_engine(_admin_url(), isolation_level="AUTOCOMMIT", poolclass=pool.NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


def _reachable() -> bool:
    try:
        asyncio.run(_admin("SELECT 1"))
        return True
    except Exception:
        return False


# ---- how to run each reporter -----------------------------------------------------------------
# Every runner takes the credentials and a padding count (blank lines placed above the failing line, so
# its line number differs) and returns the prepared request: {"url", "headers", "body"}.


DEFAULT_MESSAGE = "x is undefined"


def reporter_env(key: str, secret: str, message: str) -> dict:
    # The message travels in the environment so no language needs to quote it inside source code.
    return {**os.environ, "NIGHTJAR_URL": HUB, "NIGHTJAR_KEY": key, "NIGHTJAR_SECRET": secret, "NJ_TEST_MESSAGE": message}


def run_node(key: str, secret: str, pad: int = 0, message: str = DEFAULT_MESSAGE) -> dict:
    lib = os.path.join(ROOT, "client", "node", "nightjar.ts").replace("\\", "/")
    script = (
        f'import {{ prepare }} from "file:///{lib}";\n'
        + "\n" * pad
        + 'function render() { throw new TypeError(process.env.NJ_TEST_MESSAGE); }\n'
        "try { render(); } catch (e) {\n"
        '  console.log(JSON.stringify(prepare({}, e, "error", { method: "GET", path: "/cart?token=SECRET" })));\n'
        "}\n"
    )
    done = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env=reporter_env(key, secret, message),
    )
    if done.returncode != 0:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout.strip().splitlines()[-1])


def find_php() -> list[str] | None:
    """A PHP on the PATH, else the one winget installs on Windows."""
    found = shutil.which("php")
    if found:
        return [found]
    pattern = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\PHP.PHP*\php.exe")
    matches = sorted(glob.glob(pattern))
    return [matches[-1]] if matches else None


def docker_php(version: str) -> list[str] | None:
    """The same PHP in Docker, to test the oldest version supported. Needs the image already pulled."""
    if not shutil.which("docker"):
        return None
    image = f"php:{version}-cli-alpine"
    try:
        have = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=30).returncode == 0
    except Exception:
        return None
    if not have:
        return None
    return ["docker", "run", "--rm", "-v", f"{ROOT}:/app:ro", "-w", "/app", image, "php"]


def make_run_php(cmd: list[str], lib: str):
    def run(key: str, secret: str, pad: int = 0, message: str = DEFAULT_MESSAGE) -> dict:
        code = (
            f'require "{lib}"; '
            + "\n" * pad
            + 'function render() { throw new TypeError(getenv("NJ_TEST_MESSAGE")); }\n'
            "try { render(); } catch (Throwable $e) {\n"
            "  echo json_encode(Nightjar\\Reporter::prepare([], $e, 'error', ['method' => 'GET', 'path' => '/cart?token=SECRET']));\n"
            "}\n"
        )
        env = reporter_env(key, secret, message)
        # Docker only passes through the variables it is told to
        passthrough = [] if cmd[0] != "docker" else [
            a for name in ("NIGHTJAR_URL", "NIGHTJAR_KEY", "NIGHTJAR_SECRET", "NJ_TEST_MESSAGE") for a in ("-e", name)
        ]
        cmd_full = cmd[:2] + passthrough + cmd[2:] if passthrough else cmd
        done = subprocess.run(cmd_full + ["-r", code], capture_output=True, text=True, timeout=120, env=env, encoding="utf-8")
        if done.returncode != 0:
            raise AssertionError(done.stderr + done.stdout)
        return json.loads(done.stdout.strip().splitlines()[-1])

    return run


_java_classes: list[str] = []


def java_classes() -> str:
    """Compile the reporter once, for Java 11 exactly, and keep the output for the whole run."""
    if not _java_classes:
        out = tempfile.mkdtemp(prefix="nj_java_")
        lib = os.path.join(ROOT, "client", "java", "Nightjar.java")
        built = subprocess.run(["javac", "--release", "11", "-d", out, lib], capture_output=True, text=True, timeout=120)
        if built.returncode != 0:
            raise AssertionError(built.stderr)
        _java_classes.append(out)
    return _java_classes[0]


def run_java(key: str, secret: str, pad: int = 0, message: str = DEFAULT_MESSAGE) -> dict:
    driver = (
        "import nightjar.Nightjar;\n"
        "public class Driver {\n"
        + "\n" * pad
        + "  static void render() { throw new IllegalStateException(System.getenv(\"NJ_TEST_MESSAGE\")); }\n"
        "  public static void main(String[] a) {\n"
        "    try { render(); } catch (Exception e) {\n"
        '      Nightjar.Prepared p = Nightjar.prepare(new Nightjar.Options(), e, Nightjar.Level.ERROR, "GET", "/cart?token=SECRET");\n'
        '      System.out.println(p.url + "\\n" + p.headers.get("X-Hub-Timestamp") + "\\n" + p.headers.get("X-Hub-Signature") + "\\n" + p.body);\n'
        "    }\n"
        "  }\n"
        "}\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "Driver.java")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(driver)
        done = subprocess.run(
            ["java", "-Dfile.encoding=UTF-8", "-Dstdout.encoding=UTF-8", "-cp", java_classes(), path],
            capture_output=True, text=True, timeout=120, env=reporter_env(key, secret, message), encoding="utf-8",
        )
    if done.returncode != 0:
        raise AssertionError(done.stderr)
    url, timestamp, signature, body = done.stdout.strip().split("\n", 3)
    headers = {"Content-Type": "application/json", "X-Hub-Timestamp": timestamp, "X-Hub-Signature": signature}
    return {"url": url, "headers": headers, "body": body}


PHP = find_php()
PHP74 = docker_php("7.4")
PHP83 = docker_php("8.3")
PHP_LIB = os.path.join(ROOT, "client", "php", "Nightjar.php")

RUNNERS = {"node": (run_node, shutil.which("node") is not None, "node is not installed")}
RUNNERS["java"] = (run_java, bool(shutil.which("javac") and shutil.which("java")), "a JDK is not installed")
RUNNERS["php (native)"] = (make_run_php(PHP or ["php"], PHP_LIB), PHP is not None, "php is not installed")
RUNNERS["php 7.4 (docker)"] = (make_run_php(PHP74 or ["docker"], "/app/client/php/Nightjar.php"), PHP74 is not None, "docker image php:7.4-cli-alpine is not pulled")
RUNNERS["php 8.3 (docker)"] = (make_run_php(PHP83 or ["docker"], "/app/client/php/Nightjar.php"), PHP83 is not None, "docker image php:8.3-cli-alpine is not pulled")


@unittest.skipUnless(_reachable(), "Postgres is not reachable (start the dev database container)")
class ServerReporters(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_rep_{uuid.uuid4().hex[:10]}"
        asyncio.run(_admin(f'CREATE DATABASE "{cls.name}"'))
        cls.url = make_url(settings.database_url).set(database=cls.name).render_as_string(hide_password=False)
        migrate.upgrade(cls.url)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(get_session, None)
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))

    async def asyncSetUp(self):
        for p in (
            patch.object(settings, "console_token", ""),
            patch.object(settings, "operator_login", False),  # the console is left open here; signatures are what is under test
            patch.object(settings, "verify_signatures", True),  # the point of this file
        ):
            p.start()
            self.addCleanup(p.stop)
        self.engine = create_async_engine(self.url, pool_size=5)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async def override():
            async with self.maker() as session:
                yield session

        app.dependency_overrides[get_session] = override
        async with self.engine.begin() as conn:
            await conn.execute(text("TRUNCATE clients, sites RESTART IDENTITY CASCADE"))
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=HUB)
        made = (await self.http.post("/api/sites", json={"name": "shop", "client": "Shop"})).json()
        self.key, self.secret = made["ingest_key"], made["ingest_secret"]

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.engine.dispose()

    async def post(self, prepared: dict, *, body: bytes | None = None, headers: dict | None = None):
        path = prepared["url"].removeprefix(HUB)
        return await self.http.post(
            path, content=body if body is not None else prepared["body"].encode(), headers=headers or prepared["headers"]
        )

    async def issues(self):
        async with self.engine.connect() as conn:
            return (await conn.execute(text("SELECT title, culprit, event_count, level, severity FROM issues"))).all()

    def prepared(self, runner, pad=0, message=DEFAULT_MESSAGE):
        return runner(self.key, self.secret, pad, message)

    # ---- one set of checks, run once per language ---------------------------------------------

    async def each(self, check):
        ran = 0
        for name, (runner, available, why) in RUNNERS.items():
            with self.subTest(language=name):
                if not available:
                    self.skipTest(f"{name}: {why}")
                async with self.engine.begin() as conn:
                    await conn.execute(text("TRUNCATE issues, events RESTART IDENTITY CASCADE"))
                await check(name, runner)
                ran += 1
        return ran

    async def test_a_signed_report_is_accepted_and_becomes_an_issue(self):
        async def check(name, runner):
            r = await self.post(self.prepared(runner))
            self.assertEqual(r.status_code, 200, f"{name}: {r.text}")
            self.assertEqual(r.json()["state_change"], "new")
            rows = await self.issues()
            self.assertEqual(len(rows), 1, name)
            self.assertIn("x is undefined", rows[0].title, f"{name}: {rows[0].title}")
            self.assertEqual(rows[0].severity, "P2")

        await self.each(check)

    async def test_a_tampered_body_or_a_wrong_secret_is_refused(self):
        async def check(name, runner):
            p = self.prepared(runner)
            tampered = p["body"].replace("x is undefined", "x is defined").encode()
            self.assertEqual((await self.post(p, body=tampered)).status_code, 401, f"{name}: tampered body")
            wrong = runner(self.key, "not-the-secret", 0)
            self.assertEqual((await self.post(wrong)).status_code, 401, f"{name}: wrong secret")
            self.assertEqual((await self.post(p, headers={"Content-Type": "application/json"})).status_code, 401, f"{name}: no signature")
            self.assertEqual(await self.issues(), [], name)

        await self.each(check)

    async def test_the_signature_is_over_the_exact_bytes_sent(self):
        async def check(name, runner):
            p = self.prepared(runner)
            ts = p["headers"]["X-Hub-Timestamp"]
            self.assertLess(abs(time.time() - int(ts)), 5, f"{name}: a timestamp, and it is now")
            signed = ts.encode() + b"." + p["body"].encode()
            expected = "sha256=" + hmac.new(self.secret.encode(), signed, hashlib.sha256).hexdigest()
            self.assertEqual(p["headers"]["X-Hub-Signature"], expected, name)

        await self.each(check)

    async def test_moving_the_failing_line_is_still_the_same_issue(self):
        # the whole point of the fingerprint: edit above the bug, and it is still one issue
        async def check(name, runner):
            for pad in (0, 7, 31):
                r = await self.post(self.prepared(runner, pad))
                self.assertEqual(r.status_code, 200, f"{name}: {r.text}")
            rows = await self.issues()
            self.assertEqual((len(rows), rows[0].event_count), (1, 3), f"{name}: {rows}")

        await self.each(check)

    async def test_awkward_text_arrives_exactly_as_written(self):
        # every language builds its JSON a different way, and hand-written JSON is where escaping goes wrong
        awkward = 'quote" back\\slash and a\nnew line,\ttab, "nested \\"quotes\\"", emoji \U0001F600, accents café, </script>'

        async def check(name, runner):
            r = await self.post(self.prepared(runner, message=awkward))
            self.assertEqual(r.status_code, 200, f"{name}: {r.text}")
            async with self.engine.connect() as conn:
                payload = (await conn.execute(text("SELECT payload FROM events ORDER BY id DESC LIMIT 1"))).scalar()
            self.assertEqual(payload["message"], awkward, name)

        await self.each(check)

    async def test_the_query_string_never_reaches_the_hub(self):
        async def check(name, runner):
            await self.post(self.prepared(runner))
            async with self.engine.connect() as conn:
                payload = (await conn.execute(text("SELECT payload FROM events"))).scalar()
            self.assertNotIn("SECRET", json.dumps(payload), name)

        await self.each(check)


class Receiver:
    """A tiny stand-in for the hub that records what a crashing process manages to send."""

    def __init__(self):
        self.got: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.got.append(
                    {
                        "path": self.path,
                        "sig": self.headers.get("X-Hub-Signature"),
                        "ts": self.headers.get("X-Hub-Timestamp"),
                        "body": body,
                    }
                )
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@unittest.skipUnless(PHP, "php is not installed")
class PhpCrashes(unittest.TestCase):
    """A real PHP process that dies, sending over a real socket. No database needed: the signature is
    checked here with the same HMAC the hub uses."""

    def setUp(self):
        self.rx = Receiver()
        self.addCleanup(self.rx.close)

    def run_php(self, code: str, hub: str | None = None, transport: str = "auto", configure: bool = True, before: str = ""):
        # A real script file, as PHP is really run. (`php -r` never calls exception handlers on Windows,
        # so it would test PHP's quirk rather than the reporter.)
        install = (
            "Nightjar\\Reporter::install(["
            f'"hub" => "{hub or self.rx.url}", "key" => "KEY1", "secret" => "s3cret", "transport" => "{transport}"]);\n'
            if configure
            else "Nightjar\\Reporter::install();\n"
        )
        script = f'<?php\nrequire "{PHP_LIB}";\n' + before + install + code + "\n"
        env =os.environ if configure else {k: v for k, v in os.environ.items() if not k.startswith("NIGHTJAR_")}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "app.php")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(script)
            started = time.monotonic()
            done = subprocess.run(PHP + [path], capture_output=True, text=True, timeout=60, env=env)
            return done, time.monotonic() - started

    def check_signed(self, sent: dict):
        self.assertIsNotNone(sent["ts"], "a timestamp was sent")
        signed = sent["ts"].encode() + b"." + sent["body"]
        expected = "sha256=" + hmac.new(b"s3cret", signed, hashlib.sha256).hexdigest()
        self.assertEqual(sent["sig"], expected)
        self.assertEqual(sent["path"], "/ingest/KEY1/event")

    def test_an_uncaught_exception_is_reported_and_php_still_fails_as_it_does_without_us(self):
        for transport in ("curl", "stream"):
            with self.subTest(transport=transport):
                self.rx.got.clear()
                done, _ = self.run_php('function boom() { throw new RuntimeException("kaboom"); } boom();', transport=transport)
                self.assertEqual(done.returncode, 255, "PHP's exit code for an uncaught exception")
                self.assertIn("Uncaught RuntimeException: kaboom", done.stdout + done.stderr, "and it still says so")
                self.assertEqual(len(self.rx.got), 1, "and the report arrived")
                self.check_signed(self.rx.got[0])
                body = json.loads(self.rx.got[0]["body"])
                self.assertEqual((body["exception_type"], body["message"], body["level"]), ("RuntimeException", "kaboom", "error"))
                self.assertEqual(body["frames"][-1]["function"], "boom")

    def test_a_fatal_error_that_no_handler_can_catch_is_still_reported(self):
        done, _ = self.run_php('trigger_error("custom fatal", E_USER_ERROR);')
        self.assertEqual(done.returncode, 255)
        self.assertEqual(len(self.rx.got), 1)
        body = json.loads(self.rx.got[0]["body"])
        self.assertEqual((body["exception_type"], body["message"]), ("FatalError", "custom fatal"))

    def test_an_uncaught_exception_is_reported_once_not_twice(self):
        # PHP ends it as an E_ERROR too; the shutdown check must not report that a second time
        self.run_php('throw new Exception("once");')
        self.assertEqual(len(self.rx.got), 1)

    def test_a_caught_exception_is_reported_and_the_script_carries_on(self):
        done, _ = self.run_php('try { throw new LogicException("handled"); } catch (Exception $e) { Nightjar\\Reporter::captureException($e, "warning"); } echo "still running";')
        self.assertEqual(done.returncode, 0)
        self.assertIn("still running", done.stdout)
        self.assertEqual(json.loads(self.rx.got[0]["body"])["level"], "warning")

    def test_a_hub_that_is_down_does_not_change_what_a_crash_does(self):
        for transport in ("curl", "stream"):
            with self.subTest(transport=transport):
                done, seconds = self.run_php('throw new Exception("hub is down");', hub="http://127.0.0.1:9", transport=transport)
                self.assertEqual(done.returncode, 255)
                self.assertIn("Uncaught Exception: hub is down", done.stdout + done.stderr)
                self.assertLess(seconds, 8, "a dead hub costs at most the timeout")

    def test_a_script_with_no_configuration_runs_exactly_as_it_did(self):
        done, _ = self.run_php('echo "ok"; throw new Exception("no config");', configure=False)
        self.assertEqual(done.returncode, 255)
        self.assertIn("ok", done.stdout)
        self.assertIn("Uncaught Exception: no config", done.stdout + done.stderr)

    def test_an_app_with_its_own_exception_handler_keeps_it(self):
        # compare with the same script run WITHOUT the reporter, rather than guess PHP's exit code
        app = 'set_exception_handler(function ($e) { echo "app handled: " . $e->getMessage(); }); '
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plain.php")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("<?php\n" + app + 'throw new Exception("mine");\n')
            plain = subprocess.run(PHP + [path], capture_output=True, text=True, timeout=60)
        done, _ = self.run_php('throw new Exception("mine");', before=app + "\n")
        self.assertEqual((done.returncode, done.stdout), (plain.returncode, plain.stdout), "behaves exactly as without us")
        self.assertIn("app handled: mine", done.stdout)
        self.assertEqual(len(self.rx.got), 1, "and it was still reported")

    def test_known_limit_a_handler_the_app_sets_after_install_replaces_ours(self):
        # PHP has one exception handler and the last one set wins. Install last, or call captureException()
        # from the app's own handler. This pins the limit so it is a documented fact, not a surprise.
        app = 'set_exception_handler(function ($e) { echo "app handled"; }); '
        done, _ = self.run_php(app + 'throw new Exception("mine");')
        self.assertIn("app handled", done.stdout)
        self.assertEqual(len(self.rx.got), 0)


if __name__ == "__main__":
    unittest.main()
