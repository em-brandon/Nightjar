"""Turn a stack trace into a stable hash.

The whole system depends on this. Get it wrong in one direction and 1,284
copies of the same crash become 1,284 rows. Get it wrong in the other and two
unrelated bugs get merged and you only ever hear about one of them.

Rule of thumb: hash the things that identify the *bug*, normalise away the
things that change between *occurrences* (ids, addresses, user input, counts).
"""

import hashlib
import re

# Order matters: UUIDs are matched before the generic number rule eats them.
_NOISE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"0x[0-9a-fA-F]+"), "<addr>"),
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
            r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "<uuid>",
    ),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "<email>"),
    (re.compile(r"'[^']*'"), "'<s>'"),
    (re.compile(r'"[^"]*"'), '"<s>"'),
    (re.compile(r"\b\d[\d,._-]*\b"), "<n>"),
]

FRAME_DEPTH = 3

# A bundler puts a content hash in the file name (Vite: index-3fa9c1B2.js, vendor.CxZ9aBq1.js;
# webpack: main.3f9a1c2e.js), and the hash changes on every build. Left in the fingerprint, every
# deploy would turn every browser error into a "new" issue. Two shapes are stripped, before the
# extension and after a - or .: exactly 8 characters that are not all lowercase letters (Vite),
# or 8 to 32 hex digits (webpack). "all lowercase letters" is spared so a real name such as
# index-database.js keeps its meaning; a Vite hash is all lowercase about once in 1,500 builds.
_BUNDLE_HASH = re.compile(
    r"[-.](?:(?![a-z]{8}\.)[A-Za-z0-9_-]{8}|[0-9a-f]{8,32})(?=\.(?:m?js|css)$)"
)


def frame_module(module: str) -> str:
    """The file name of a frame with the query string and the bundler's hash removed."""
    name = module.split("?", 1)[0].split("#", 1)[0].rsplit("/", 1)[-1]
    return _BUNDLE_HASH.sub("", name)


def normalize(text: str) -> str:
    """Strip per-occurrence detail out of an exception message."""
    out = text.strip()
    for pattern, replacement in _NOISE:
        out = pattern.sub(replacement, out)
    return re.sub(r"\s+", " ", out)


def culprit_of(frames: list[dict]) -> str:
    """Human-readable 'where it broke', for the issue list."""
    if not frames:
        return ""
    innermost = frames[-1]
    module = innermost.get("module") or innermost.get("file") or "?"
    return f"{module}:{innermost.get('lineno', '?')}"


def compute(site_id: int, exception_type: str, message: str, frames: list[dict]) -> str:
    """Fingerprint = site + exception type + normalised message + innermost frames.

    Line numbers are deliberately excluded — they shift every time you edit the
    file above the bug, and you do not want a fingerprint that changes on
    reformat. Function and module names are stable enough to identify the site
    of the failure.
    """
    parts: list[str] = [
        str(site_id),
        exception_type.strip(),
        normalize(message),
    ]
    for frame in frames[-FRAME_DEPTH:]:
        module = frame_module(frame.get("module") or frame.get("file") or "?")
        function = frame.get("function") or "?"
        parts.append(f"{module}:{function}")

    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]
