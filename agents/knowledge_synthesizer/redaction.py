"""PII / secret redaction — runs before anything is persisted or published.

Step 4 of the Knowledge Synthesizer process: scrub a drafted postmortem / KB
article / runbook of secrets and personal data *before* it enters the KB store
or the HITL review queue. A published KB article is read by other agents and
humans, so a leaked token or private key there is a real exposure.

Design: a pure, dependency-free regex redactor. No new dependency (the brief's
"PII-redaction lib" is deferred post-POC) and no I/O — this module is trivially
unit-testable and safe to call on every synthesis. It is intentionally
conservative about false positives: it preserves service names, flag names
(e.g. ``productCatalogFailure``), latency numbers and dates, and only rewrites
things that match a specific secret/PII shape.

⚠️ BEST-EFFORT, NOT COMPLIANCE-GRADE. This catches common secret/PII *shapes*
(emails, IPv4, bearer tokens, ``key=secret`` pairs, AWS keys, JWTs, PEM private
keys). It will miss novel formats and is not a substitute for a real DLP /
compliance scrubber — do not treat it as the sole control for regulated data.

Returns both the scrubbed text and a :class:`RedactionReport` (counts per
category) so the synthesizer can record *what* was redacted in its audit trail
without recording the secret values themselves.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

# ─── placeholders ────────────────────────────────────────────────────────────

_EMAIL = "[REDACTED_EMAIL]"
_IP = "[REDACTED_IP]"
_TOKEN = "[REDACTED_TOKEN]"
_SECRET = "[REDACTED_SECRET]"
_AWS_KEY = "[REDACTED_AWS_KEY]"
_JWT = "[REDACTED_JWT]"
_PRIVATE_KEY = "[REDACTED_PRIVATE_KEY]"


# ─── rules ───────────────────────────────────────────────────────────────────
#
# Order matters. The most specific / structural patterns run first so a value
# is labelled by its real shape (private key, AWS key, JWT) before a broader
# rule (key=value secret) can claim it. Each rule's replacement may be a string
# or a callable (used to preserve the key name while redacting only its value).

_Replacement = str | Callable[[re.Match[str]], str]


def _redact_secret_assignment(m: re.Match[str]) -> str:
    # Keep the key + separator, redact the value: ``password=hunter2`` →
    # ``password=[REDACTED_SECRET]``. Keeping the key makes the redaction
    # auditable ("a password was here") without leaking the value.
    return f"{m.group(1)}{m.group(2)}{_SECRET}"


_RULES: list[tuple[str, re.Pattern[str], _Replacement]] = [
    # PEM private key blocks (multi-line) — redact the whole block.
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.S,
        ),
        _PRIVATE_KEY,
    ),
    # AWS access key id.
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), _AWS_KEY),
    # key=value / key: value secrets — preserve the key, redact the value.
    (
        "secret_assignment",
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|client[_-]?secret|token|"
            r"api[_-]?key|apikey|access[_-]?key)\b(\s*[=:]\s*)((?!\[REDACTED)\S+)"
        ),
        _redact_secret_assignment,
    ),
    # JSON Web Tokens (header.payload.signature, base64url).
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"),
        _JWT,
    ),
    # Bearer tokens in an Authorization header.
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), f"Bearer {_TOKEN}"),
    # Email addresses (PII).
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), _EMAIL),
    # IPv4 addresses — octet-validated so version strings ("0.0.1") and
    # latency values ("5.2") don't match.
    (
        "ipv4",
        re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
        _IP,
    ),
]


# ─── public API ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RedactionReport:
    """What redaction found — counts per category, never the values."""

    findings: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.findings.values())

    @property
    def redacted(self) -> bool:
        return self.total > 0

    def summary(self) -> str:
        """One-line audit string, e.g. ``redacted 3 item(s): email=2, ipv4=1``."""
        if not self.findings:
            return "no PII/secrets detected"
        parts = ", ".join(f"{k}={v}" for k, v in sorted(self.findings.items()))
        return f"redacted {self.total} item(s): {parts}"


@dataclass(frozen=True)
class RedactionResult:
    text: str
    report: RedactionReport


def redact(text: str) -> RedactionResult:
    """Scrub ``text`` of PII/secrets. Returns the redacted text + a report.

    Pure and idempotent: running it on already-redacted text is a no-op (the
    placeholders match none of the rules), so it is safe to call defensively.
    """
    if not text:
        return RedactionResult(text=text or "", report=RedactionReport({}))
    findings: dict[str, int] = {}
    out = text
    for category, pattern, repl in _RULES:
        out, n = pattern.subn(repl, out)
        if n:
            findings[category] = findings.get(category, 0) + n
    return RedactionResult(text=out, report=RedactionReport(findings))


def scrub(text: str) -> str:
    """Convenience wrapper returning only the redacted text."""
    return redact(text).text


__all__ = ["RedactionReport", "RedactionResult", "redact", "scrub"]
