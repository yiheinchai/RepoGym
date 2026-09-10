"""Best-effort secret redaction.

The gym is built from real engineering work, which means real prompts and real diffs.
Anything that looks like a credential is replaced before it is written to disk.
This is defence in depth, not a guarantee: pair it with `exclude_globs` in config.
"""
from __future__ import annotations

import fnmatch
import re
from typing import Iterable, List, Tuple

REDACTED = "[REDACTED]"

# (name, compiled pattern). Patterns are applied line-by-line; the whole match is replaced.
_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("anthropic", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai", re.compile(r"sk-(?:proj-|live-|test-)?[A-Za-z0-9_\-]{20,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("aws_secret", re.compile(r"(?i)aws(.{0,20})?(secret|private)?(.{0,20})?['\"][0-9a-zA-Z/+]{40}['\"]")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("github_fine_grained", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9\-_]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    ("stripe", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b")),
    ("google_api", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("private_key_line", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*")),
    ("url_basic_auth", re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/:@]+:[^\s/@]+@")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{20,}=*")),
    ("generic_assignment", re.compile(
        r"(?i)\b(api[_\-]?key|apikey|secret[_\-]?key|access[_\-]?token|auth[_\-]?token|client[_\-]?secret|"
        r"password|passwd|pwd|private[_\-]?key|token)\b(\s*[:=]\s*|\s*[\"']?\s*:\s*)[\"']?([^\s\"',;]{8,})[\"']?")),
]


def scrub_text(text: str) -> str:
    """Redact anything credential-shaped in `text`."""
    if not text:
        return text
    out = text
    for name, pat in _PATTERNS:
        if name == "url_basic_auth":
            out = pat.sub(lambda m: m.group(1) + REDACTED + "@", out)
        elif name == "generic_assignment":
            out = pat.sub(lambda m: m.group(1) + m.group(2) + REDACTED, out)
        else:
            out = pat.sub(REDACTED, out)
    return out


def scrub_obj(obj):
    """Recursively scrub strings inside JSON-like data."""
    if isinstance(obj, str):
        return scrub_text(obj)
    if isinstance(obj, list):
        return [scrub_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub_obj(v) for k, v in obj.items()}
    return obj


def path_excluded(path: str, globs: Iterable[str]) -> bool:
    """True when a file path matches any exclusion glob (matched against the path and its basename)."""
    base = path.rsplit("/", 1)[-1]
    for g in globs:
        if fnmatch.fnmatch(path, g) or fnmatch.fnmatch(base, g):
            return True
        # allow directory-style globs such as "secrets/*"
        if g.endswith("/*") and path.startswith(g[:-1]):
            return True
    return False


def has_secret(text: str) -> bool:
    return scrub_text(text) != text
