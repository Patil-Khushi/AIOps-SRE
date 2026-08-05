"""Secret scrubbing for source content returned by the SCM seam.

Why this exists separately from ``agents/knowledge_synthesizer/redaction.py``:

1. **Layering.** That module lives under ``agents/``. The platform (``aiops/``)
   must not import an agent — the dependency runs the other way. Consolidating
   the two into a shared ``aiops/redaction/`` is the right end state; it is a
   refactor of an existing agent plus its tests, so it is deliberately not
   bundled into this change.
2. **Different shapes.** The knowledge synthesizer redacts PII in prose
   (emails, IPs, names) before publishing a KB article. This redacts
   credentials in *code and config* — API keys, tokens, connection strings,
   private keys. Running prose rules over source would mangle legitimate
   content (every ``user@host`` in a comment), and prose rules miss
   ``AWS_SECRET_ACCESS_KEY=...`` entirely.

This is defence in depth, not a guarantee. The real protection is a read-only
token scoped to one repository. Never rely on scrubbing alone to make a private
repo safe to expose to an LLM.
"""

from __future__ import annotations

import re

_PLACEHOLDER = "[REDACTED_SECRET]"

# Ordered most-specific first: a provider-shaped token should be reported as
# that provider rather than matching the generic assignment rule.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # GitHub tokens — ghp_ (classic PAT), gho_/ghu_/ghs_/ghr_, github_pat_ (fine-grained)
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    # AWS access key id
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # Slack tokens / webhooks
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("slack_webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/+]+")),
    # Anthropic / OpenAI style keys
    ("llm_api_key", re.compile(r"\b(?:sk-ant-|sk-)[A-Za-z0-9_-]{20,}\b")),
    # PEM private key blocks (whole block, not just the header)
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"
            r".*?-----END (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    # Credentials embedded in a URL: scheme://user:password@host
    ("url_credentials", re.compile(r"(?<=://)[^\s:/@]+:[^\s:/@]+(?=@)")),
    # KEY=value / KEY: value where the key name looks secret-ish.
    # Deliberately requires a non-trivial value so `PASSWORD=` or
    # `token: ""` in a template file isn't reported as a finding.
    #
    # Horizontal whitespace only ([ \t], not \s) around the separator, and the
    # value class excludes newlines. With \s* the separator could span a line
    # break, so `PASSWORD=\nTOKEN: ...` matched with "TOKEN:" as the value —
    # redacting a key name on the next line and reporting a phantom finding for
    # a file that only contained empty placeholders.
    (
        "assigned_secret",
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:SECRET|PASSWORD|PASSWD|TOKEN|API_?KEY|PRIVATE_?KEY|CREDENTIALS)"
            r"[A-Z0-9_]*)[ \t]*[:=][ \t]*[\"']?([^\s\"'#,}]{6,})[\"']?"
        ),
    ),
]


def scrub(text: str) -> tuple[str, dict[str, int]]:
    """Return ``(scrubbed_text, {pattern_name: hit_count})``.

    The findings map is returned rather than logged so callers can surface
    "N secrets redacted" in ``ToolResult.metadata`` — silence would make it
    impossible to tell a clean file from a scrubbing bug.
    """
    if not text:
        return text, {}

    findings: dict[str, int] = {}
    out = text
    for label, pattern in _PATTERNS:
        if label == "assigned_secret":
            # Keep the key name visible — "DB_PASSWORD was set here" is useful
            # context for an RCA; the value is what must not leak.
            out, n = pattern.subn(lambda m: f"{m.group(1)}={_PLACEHOLDER}", out)
        else:
            out, n = pattern.subn(_PLACEHOLDER, out)
        if n:
            findings[label] = findings.get(label, 0) + n
    return out, findings
