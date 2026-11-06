"""Password hashing and one-time codes for the console login. Pure functions, standard library only: a login needs no
new dependency, and nothing here touches the database.

Passwords use scrypt (memory-hard, in hashlib). Second factor is TOTP, RFC 6238 (the code an authenticator app shows).
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

MIN_PASSWORD_LEN = 12

# scrypt cost: 2**15 * 8 * 128 bytes = 32 MiB per guess and about 100 ms. Stored in the hash, so it can be raised
# later without locking anyone out (verify reads the cost from the stored value).
_N, _R, _P = 2**15, 8, 1
_MAXMEM = 128 * 1024 * 1024

TOTP_LABEL = "nightjar-totp-v1"
TOTP_STEP_S = 30
TOTP_DIGITS = 6


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, expected = stored.split("$")
        if kind != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), maxmem=_MAXMEM, dklen=32
        )
        return hmac.compare_digest(digest, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


# A hash to verify against when the username is unknown, so a wrong username and a wrong password take the same time and
# the response does not say which one was wrong.
DUMMY_HASH = hash_password("not-a-real-password-just-for-timing")


def password_problem(password: str) -> str | None:
    """Why a new password is refused, or None. Length is what matters; composition rules only push people to
    predictable substitutions."""
    if len(password) < MIN_PASSWORD_LEN:
        return f"use at least {MIN_PASSWORD_LEN} characters"
    if len(set(password)) < 5:
        return "that is too repetitive"
    return None


def totp_secret(master_key: str, operator_id: int, version: int) -> bytes:
    """20 bytes derived from the master key. Not stored anywhere: see models.Operator."""
    message = f"{TOTP_LABEL}|{operator_id}|{version}".encode()
    return hmac.new(master_key.encode(), message, hashlib.sha256).digest()[:20]


def totp_base32(secret: bytes) -> str:
    return base64.b32encode(secret).decode().rstrip("=")


def totp_uri(secret: bytes, username: str, issuer: str = "Nightjar") -> str:
    label = quote(f"{issuer}:{username}")
    return f"otpauth://totp/{label}?secret={totp_base32(secret)}&issuer={quote(issuer)}&digits={TOTP_DIGITS}&period={TOTP_STEP_S}"


def _code(secret: bytes, step: int) -> str:
    mac = hmac.new(secret, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = (struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF) % 10**TOTP_DIGITS
    return str(value).zfill(TOTP_DIGITS)


def totp_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // TOTP_STEP_S)


def totp_match(secret: bytes, code: str, last_step: int, now: float | None = None) -> int | None:
    """The step this code is valid for, or None. Accepts one step either side for clock drift, and never a step at or
    before `last_step`, so a code that was already used (or an older one) is refused."""
    code = code.strip().replace(" ", "")
    # ascii first: str.isdigit() is true for digits of other scripts, and compare_digest raises (a 500) on non-ASCII text
    if len(code) != TOTP_DIGITS or not (code.isascii() and code.isdigit()):
        return None
    current = totp_step(now)
    for step in (current - 1, current, current + 1):
        if step > last_step and hmac.compare_digest(_code(secret, step), code):
            return step
    return None


def new_recovery_codes(count: int = 8) -> list[str]:
    """Shown once. Ten characters from an alphabet without look-alikes, grouped for reading aloud."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return ["".join(secrets.choice(alphabet) for _ in range(10)) for _ in range(count)]


def _norm_recovery(code: str) -> str:
    return code.strip().lower().replace("-", "").replace(" ", "")


def hash_recovery(code: str) -> str:
    # High-entropy random codes, so a plain SHA-256 is enough (no password-style stretching needed)
    return hashlib.sha256(_norm_recovery(code).encode()).hexdigest()


def use_recovery(code: str, hashes: list[str]) -> list[str] | None:
    """The hashes left after spending this code, or None if it is not one of them."""
    wanted = hash_recovery(code)
    for h in hashes:
        if hmac.compare_digest(h, wanted):
            return [x for x in hashes if x != h]
    return None


def new_session_token() -> tuple[str, str]:
    """(token for the cookie, hash for the database)"""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


DEVICE_PREFIX = "njd_"


def new_device_token() -> tuple[str, str, str]:
    """(token, hash for the database, prefix to show in a list). The prefix is a handful of the token's own
    characters, enough to tell two apart and nowhere near enough to guess the rest."""
    token = DEVICE_PREFIX + secrets.token_urlsafe(32)
    return token, hash_token(token), token[: len(DEVICE_PREFIX) + 6]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
