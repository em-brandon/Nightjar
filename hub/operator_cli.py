"""Create and maintain the console account. Run on the server, never over the network:

    docker compose exec hub python -m hub.operator_cli create
    docker compose exec hub python -m hub.operator_cli passwd
    docker compose exec hub python -m hub.operator_cli reset-2fa
    docker compose exec hub python -m hub.operator_cli sign-out-everywhere

There is deliberately no sign-up route: whoever can run this already controls the machine, and nobody else gets to ask
the hub for an account. `create` shows the authenticator secret and the recovery codes once and only saves the account
after you have typed a working code, so a mistyped scan cannot lock you out on the first day.

The authenticator secret is derived from SECRET_MASTER_KEY (hub/credentials.py), so this needs it set. Changing the
master key invalidates every authenticator: run `reset-2fa` afterwards.
"""

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit, credentials
from .config import settings
from .db import SessionLocal, engine
from .models import ConsoleSession, Operator


def _need_master_key() -> None:
    if len(settings.secret_master_key) < 32:
        sys.exit("SECRET_MASTER_KEY is not set (or is too short): the authenticator code is derived from it.")


def _ask_password(confirm: bool = True) -> str:
    while True:
        password = getpass.getpass("Password (at least %d characters): " % credentials.MIN_PASSWORD_LEN)
        problem = credentials.password_problem(password)
        if problem:
            print(f"  {problem}")
            continue
        if confirm and getpass.getpass("Again: ") != password:
            print("  those did not match")
            continue
        return password


def _show_enrolment(username: str, secret: bytes, codes: list[str]) -> None:
    print()
    print("Add this to your authenticator app (1Password, Google Authenticator, Aegis, ...):")
    print(f"  secret : {credentials.totp_base32(secret)}   (time-based, 6 digits, 30 seconds)")
    print(f"  or open: {credentials.totp_uri(secret, username)}")
    print()
    print("Recovery codes. Each works once, in place of an authenticator code. They are shown only now:")
    for code in codes:
        print(f"  {code}")
    print()


def _confirm_code(secret: bytes) -> None:
    while True:
        code = input("Type the 6-digit code your app now shows: ").strip()
        if credentials.totp_match(secret, code, 0) is not None:
            return
        print("  that is not the current code; check the app's clock and try again")


async def _revoke_all(db: AsyncSession, operator_id: int) -> int:
    result = await db.execute(
        update(ConsoleSession)
        .where(ConsoleSession.operator_id == operator_id, ConsoleSession.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    return result.rowcount


async def create(username: str) -> None:
    _need_master_key()
    username = username.strip().lower()
    if not username or len(username) > 60:
        sys.exit("a username is 1 to 60 characters")
    async with SessionLocal() as db:
        if await db.scalar(select(Operator).where(Operator.username == username)):
            sys.exit(f"{username!r} already exists. Use passwd or reset-2fa to change it.")
        password = _ask_password()
        op = Operator(
            username=username,
            password_hash=credentials.hash_password(password),
            totp_version=1,
            totp_last_step=0,
            recovery_hashes=[],
        )
        db.add(op)
        await db.flush()  # the id is part of the authenticator secret
        secret = credentials.totp_secret(settings.secret_master_key, op.id, op.totp_version)
        codes = credentials.new_recovery_codes()
        _show_enrolment(username, secret, codes)
        _confirm_code(secret)
        op.recovery_hashes = [credentials.hash_recovery(c) for c in codes]
        audit.record_login(db, "operator_created", "", actor=username)
        await db.commit()
    print(f"Account {username!r} created. Sign in to the console with it; the console token no longer signs in.")


async def _get(db: AsyncSession, username: str) -> Operator:
    op = await db.scalar(select(Operator).where(Operator.username == username.strip().lower()).with_for_update())
    if op is None:
        sys.exit(f"no account called {username!r}")
    return op


async def passwd(username: str) -> None:
    async with SessionLocal() as db:
        op = await _get(db, username)
        op.password_hash = credentials.hash_password(_ask_password())
        gone = await _revoke_all(db, op.id)
        audit.record_login(db, "password_changed", f"{gone} session(s) ended", actor=op.username)
        await db.commit()
    print(f"Password changed. {gone} signed-in session(s) ended.")


async def reset_2fa(username: str) -> None:
    _need_master_key()
    async with SessionLocal() as db:
        op = await _get(db, username)
        version = op.totp_version + 1
        secret = credentials.totp_secret(settings.secret_master_key, op.id, version)
        codes = credentials.new_recovery_codes()
        _show_enrolment(op.username, secret, codes)
        _confirm_code(secret)
        op.totp_version = version
        op.totp_last_step = 0
        op.recovery_hashes = [credentials.hash_recovery(c) for c in codes]
        gone = await _revoke_all(db, op.id)
        audit.record_login(db, "two_factor_reset", f"{gone} session(s) ended", actor=op.username)
        await db.commit()
    print(f"Authenticator and recovery codes replaced. {gone} signed-in session(s) ended.")


async def sign_out_everywhere(username: str) -> None:
    async with SessionLocal() as db:
        op = await _get(db, username)
        gone = await _revoke_all(db, op.id)
        audit.record_login(db, "sessions_revoked", f"{gone} session(s), from the command line", actor=op.username)
        await db.commit()
    print(f"{gone} session(s) ended.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m hub.operator_cli", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_ in (
        ("create", "make the console account (asks for a password, shows the authenticator secret)"),
        ("passwd", "change the password and sign every session out"),
        ("reset-2fa", "replace the authenticator and recovery codes and sign every session out"),
        ("sign-out-everywhere", "end every signed-in session"),
    ):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--username", default="operator" if name == "create" else None, required=name != "create")
    args = parser.parse_args(argv)

    run = {"create": create, "passwd": passwd, "reset-2fa": reset_2fa, "sign-out-everywhere": sign_out_everywhere}[args.command]

    async def go() -> None:
        try:
            await run(args.username)
        finally:
            await engine.dispose()

    asyncio.run(go())


if __name__ == "__main__":
    main()
