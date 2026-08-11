"""Stage 6 — Security: scrub secrets and personal data out of observation text.

Why this stage exists
---------------------
Nothing in this repo redacts the log lines that reach an LLM today. RA-007 pulls
Loki streams, ``agents/rca_agent`` formats them into its prompt, and
``agents/notification_assembler`` quotes evidence into a Slack war-room body *and*
into ``demo/audit/chatops.jsonl``. So one connection string, AWS key id or customer
email address sitting in a single log line currently travels all three routes
unmodified, and the audit log persists it indefinitely. This stage is the one choke
point where that stops — which makes it a genuine security improvement rather than a
formality.

Why redaction runs *after* correlation and ranking
--------------------------------------------------
Redaction is lossy in a way that matters for identity: two log lines differing only
by a customer's email collapse to the same text once scrubbed. Run this before
stage 3 and those two lines present as the *same* signature — which is exactly the
input the correlator reads as cross-source agreement. Redaction would then
manufacture agreement out of nothing and inflate a rank. Running at stage 6 means
every judgement was formed from the real text and only the *rendered* text is
scrubbed.

The price is that redaction can merge signatures that were distinct. A consumer must
therefore not infer identity from ``signature`` alone: ``observation_id`` remains the
identity, and this stage deliberately does not recompute it. Recomputing would also
orphan every ``RankedObservation.observation_id`` emitted back at stage 4, since
those were derived from the pre-redaction signature.

Two redaction implementations, deliberately
-------------------------------------------
``agents/knowledge_synthesizer/redaction.py`` redacts PII in prose before a KB
article is published. It is **not** reused here and must not be: ``aiops/`` may never
import ``agents/`` (``tests/test_layering.py`` fails CI on it). Folding the two into
one platform module is the right end state and is deliberately out of scope for this
change — it is a refactor of a shipped agent plus its published-article tests.
``aiops/tools/scm/_secrets.py`` carries the same note for the same reason and *is*
reused here, because it already owns every credential-shaped pattern (GitHub, AWS,
Slack, LLM keys, PEM blocks, URL credentials, ``KEY=value`` secrets), ordered
most-specific-first. Nothing in that set is reimplemented here.

Ordering is load-bearing
------------------------
A broad pattern that runs before a specific one destroys the specific one's match and
mislabels the finding. Three orderings are relied on, and each one prevents a
concrete bug:

* **``scrub()`` before anything added here.** Given
  ``postgres://svc:s3cr3t@db.example.com/orders``, the email pattern matches
  ``s3cr3t@db.example.com``. Let it run first and the *password* is reported as an
  email, ``url_credentials`` counts zero, and the username ``svc`` is left in the
  text — so a reviewer asking "did this incident expose credentials?" is told no.
* **``email`` before ``ipv4``.** ``1.2.3.4@example.com`` is an address whose local
  part is IPv4-shaped. Redact the IP first and the local part becomes
  ``[REDACTED_IP]``, which the email pattern can no longer match, leaving the domain
  exposed.
* **``card_number`` last.** A bare run of digits is the broadest shape here, so every
  digit run belonging to something more specific (an AWS key id, an IPv4 octet group,
  a Slack token) must already have been claimed by its own pattern.

Idempotency
-----------
``redact_text`` is safe to call on already-redacted text: nothing is substituted
twice and no count moves. That does not come for free. ``scrub``'s
``assigned_secret`` rule re-matches its own output — in ``DB_PASSWORD=[REDACTED_SECRET]``
the placeholder is a legal value for that rule — and although the substitution is a
no-op the *count* still increments. A context redacted twice would therefore report
phantom findings and claim ``redaction_applied=True`` for text that was already
clean. So placeholders already present in the input are held out of the scan
(``_mask_placeholders``) and restored afterwards.

Placeholders created *during* a pass need no such treatment: every one is bracketed
upper-case letters and underscores, carrying no ``@``, no dotted decimal and no
digit, so none of the patterns added here can match one. That invariant is what lets
this be a single pass.

What is deliberately **not** redacted
-------------------------------------
* ``ContextSection.raw`` — left byte-for-byte alone. An agent adapter reproduces
  legacy prompt strings from it exactly (RCA's ``f"pod {pod}: cpu={cores:.2f}
  cores"``, RA-007's stream-order-dependent log truncation), so scrubbing it would
  silently change what those agents emit, which is the one thing a migration of this
  size cannot afford. **A consumer must therefore never log, notify or prompt from
  ``raw`` directly** — read the scrubbed ``observations``, or scrub the slice of
  ``raw`` you are about to render.
* ``SourceProvenance.error`` — same reason: adapters reproduce decision-trace lines
  that embed provider error text verbatim. A residual exposure, recorded here rather
  than glossed over.
* ``Observation.metadata`` — adapters read typed routing values out of it, and this
  is what keeps paging working: an on-call engineer's address is scrubbed out of
  ``evidence`` prose while the routing target the notification assembler actually
  reads survives in ``metadata`` and ``raw``. Redacting the prose does not break the
  page.

Purity
------
No I/O, no clock, no environment reads, no registry calls — same inputs give
byte-identical outputs, which is what lets the eval harness compare two runs. The
one import side effect is that reaching ``aiops.tools.scm._secrets`` executes
``aiops/tools/scm/__init__.py``, registering the GitHub provider's capabilities. That
is a registration, not a call; nothing here touches the registry.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from aiops.context.models import Observation
from aiops.context.pack import ContextSection, SecurityMetadata
from aiops.tools.scm._secrets import scrub

logger = logging.getLogger(__name__)

__all__ = ["redact", "redact_text"]

_EMAIL = "[REDACTED_EMAIL]"
_IP = "[REDACTED_IP]"
_CARD = "[REDACTED_CARD]"
_WITHHELD = "[REDACTED_UNSCRUBBABLE]"

# Every placeholder this layer can emit, plus a literal NUL. NUL is in the set
# because it is the mask token below: holding a pre-existing NUL as an atom too keeps
# the mask stream aligned with the restore list even for input that already contained
# one, so restoration cannot shift labels onto the wrong spans.
_PLACEHOLDER_RE = re.compile(r"\[REDACTED_[A-Z_]+\]|\x00")
_MASK = "\x00"
_MASK_RE = re.compile(re.escape(_MASK))

_NON_DIGIT_RE = re.compile(r"\D")

_Replacement = str | Callable[[re.Match[str]], str]


def _luhn_ok(digits: str) -> bool:
    """Whether ``digits`` satisfies the Luhn check digit.

    This is the precision lever for the card pattern, and without it the pattern is
    unusable in telemetry. Loki and OTel text is full of 13–19 digit runs that are
    not card numbers — epoch milliseconds (13), microseconds (16), nanoseconds (19),
    Prometheus counters, all-numeric span ids. Luhn rejects roughly nine in ten of
    them while accepting every real PAN, because a PAN is check-digited by
    definition.
    """
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _redact_card(match: re.Match[str]) -> str:
    """Replace a card-shaped digit run, or return it untouched if it fails validation.

    Returning the original text is how a validating replacement says "this candidate
    is not a finding" — see ``_apply``, which is why the counting does not use
    ``re.subn``.
    """
    digits = _NON_DIGIT_RE.sub("", match.group(0))
    if len(set(digits)) == 1:
        # OpenTelemetry's "invalid span id" is sixteen zeros, and an all-zero run is
        # Luhn-valid (its checksum is zero). It appears in trace text whenever a span
        # has no parent, so without this guard the single most common id in the
        # traces section would be redacted as a credit card. No card is one repeated
        # digit, so rejecting the whole class costs nothing.
        return match.group(0)
    if not _luhn_ok(digits):
        return match.group(0)
    return _CARD


# Ordered; see "Ordering is load-bearing" in the module docstring before reordering.
# These are the shapes that turn up in telemetry text but not in source code, which
# is why _secrets.scrub() does not carry them: running prose rules over source would
# mangle every ``user@host`` in a comment.
_PATTERNS: tuple[tuple[str, re.Pattern[str], _Replacement], ...] = (
    # Local part is deliberately permissive but the domain must end in an alphabetic
    # TLD, so ``pod@node-1`` and ``svc@10.0.0.4`` are not treated as addresses — the
    # first is not PII and the second is handled by ``ipv4`` with the right label.
    (
        "email",
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        _EMAIL,
    ),
    # Octet-validated rather than ``\d{1,3}(\.\d{1,3}){3}``, which would also claim
    # out-of-range quads such as a chart version ``1.999.0.2``. A four-part semantic
    # version that *is* in range (``1.16.0.3``) remains indistinguishable from an
    # address and is redacted — accepted knowingly, because pod and node IPs are far
    # commoner in telemetry than four-part versions, and the version also survives
    # untouched in ``raw`` for any adapter that needs it.
    (
        "ipv4",
        re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
        _IP,
    ),
    # Two alternatives only — an unseparated 13–19 digit run, or the canonical
    # 4×4 grouping. A generic "digit, optional separator, repeat" form bridges across
    # a single space and would join the tail of one number to the head of the next
    # word ("count=1234567890123 4 retries"), inventing a 14-digit candidate that
    # exists nowhere in the text. The lookarounds keep the match from starting or
    # ending inside a longer run, so a 25-digit id yields no candidate at all.
    #
    # Known gap: an account number without a check digit is indistinguishable by
    # shape from an epoch timestamp, so it is not guessed at. Redacting every long
    # integer would strip the timestamps and counters RCA reasons about — the wrong
    # trade for the wrong pattern.
    (
        "card_number",
        re.compile(r"(?<![\d.\-])(?:\d{13,19}|\d{4}(?:[ -]\d{4}){3})(?!\d)"),
        _redact_card,
    ),
)


def _apply(pattern: re.Pattern[str], replacement: _Replacement, text: str) -> tuple[str, int]:
    """Substitute ``pattern``, counting only substitutions that changed something.

    ``re.subn``'s own count cannot be used: a validating replacement such as
    ``_redact_card`` signals "not actually a finding" by returning the matched text
    unchanged, and ``subn`` would report every rejected candidate as a redaction. The
    counts land in ``SecurityMetadata`` where a reviewer reads them as evidence that
    redaction caught something, so an inflated count is a false alarm about a
    security control.
    """
    hits = 0

    def substitute(match: re.Match[str]) -> str:
        nonlocal hits
        new = replacement(match) if callable(replacement) else replacement
        if new == match.group(0):
            return new
        hits += 1
        return new

    return pattern.sub(substitute, text), hits


def _mask_placeholders(text: str) -> tuple[str, list[str]]:
    """Lift already-present placeholders out of the text so patterns cannot see them.

    The reason is ``scrub``'s ``assigned_secret`` rule re-matching its own output —
    see "Idempotency" in the module docstring. ``\\x00`` is the mask because it is
    absent from every pattern's character class *except* the two broad value classes
    in ``_secrets``, and a lone one-character value fails their ``{6,}`` minimum. So a
    mask cannot be matched, cannot be consumed, and survives the scan in order.
    """
    held: list[str] = []

    def hold(match: re.Match[str]) -> str:
        held.append(match.group(0))
        return _MASK

    return _PLACEHOLDER_RE.sub(hold, text), held


def _restore_placeholders(text: str, held: list[str]) -> str:
    """Put the held placeholders back, in the order they were lifted."""
    if not held:
        return text
    pending = iter(held)
    # An exhausted iterator drops the mask rather than emitting a stray NUL. That can
    # only happen if a match spanned a mask and deleted it (only the PEM block
    # pattern's DOTALL body can), in which case the conservative outcome is a lost
    # label — never a restored value.
    return _MASK_RE.sub(lambda _match: next(pending, ""), text)


def redact_text(text: str) -> tuple[str, dict[str, int]]:
    """Scrub ``text``. Returns ``(scrubbed, {pattern_name: hit_count})``.

    Pure, deterministic and idempotent. Counts are sorted by pattern name so the
    serialised form of a context is byte-identical regardless of which pattern
    happened to hit first.
    """
    if not text:
        return text, {}

    masked, held = _mask_placeholders(text)
    scrubbed, found = scrub(masked)
    counts = dict(found)
    for label, pattern, replacement in _PATTERNS:
        scrubbed, hits = _apply(pattern, replacement, scrubbed)
        if hits:
            counts[label] = counts.get(label, 0) + hits
    return _restore_placeholders(scrubbed, held), dict(sorted(counts.items()))


def _accumulate(into: dict[str, int], counts: dict[str, int]) -> None:
    for label, hits in counts.items():
        into[label] = into.get(label, 0) + hits


def _redact_observation(observation: Observation) -> tuple[Observation, dict[str, int]]:
    """Scrub one observation's rendered text, leaving its identity intact.

    ``observation_id`` is *not* recomputed. It answers "is this the same finding?",
    and a redacted finding is the same finding — but more concretely, stage 4 already
    emitted ``RankedObservation`` entries keyed on the pre-redaction id, and
    rederiving ids here would orphan every one of them.
    """
    try:
        evidence, evidence_counts = redact_text(observation.evidence)
        signature, signature_counts = redact_text(observation.signature)
    except Exception:
        # Fail *closed*. Everywhere else in this package a failure degrades to less
        # evidence, but a redactor that degraded to *unscrubbed* evidence would turn
        # a bug into a leak, so text we could not scrub is withheld entirely.
        # Logged without exc_info and without the text: an exception repr can carry
        # the very string this stage exists to keep out of the logs.
        logger.warning(
            "context redaction failed for observation %s; text withheld",
            observation.observation_id,
        )
        withheld = observation.model_copy(update={"evidence": _WITHHELD, "signature": _WITHHELD})
        return withheld, {"redaction_failed": 1}

    counts: dict[str, int] = {}
    _accumulate(counts, evidence_counts)
    _accumulate(counts, signature_counts)
    if not counts and evidence == observation.evidence and signature == observation.signature:
        # Same object, not an equal copy. An untouched observation staying identical
        # lets a consumer — and a test — prove by identity that nothing was rewritten.
        return observation, counts
    updated = observation.model_copy(update={"evidence": evidence, "signature": signature})
    return updated, counts


def redact(
    sections: dict[str, ContextSection],
) -> tuple[dict[str, ContextSection], SecurityMetadata]:
    """Return scrubbed sections plus what was found.

    Section keys and their order are preserved, and a section whose text needed no
    change is returned as the same object.

    ``redaction_applied`` means "something was actually redacted", not "this stage
    ran" — a clean incident reports ``False`` with empty counts, which is what makes
    a non-zero count meaningful to a reviewer. ``denied_capabilities`` is left at its
    default: the builder learns that at request-validation time, before any
    collection happens, and merges it into the final ``SecurityMetadata``.

    Deliberately status-blind. A ``FAILED`` or ``UNAVAILABLE`` section normally holds
    no observations, so this is usually moot — but gating the scrub on
    ``status.usable`` would make "we could not trust this payload" imply "so we did
    not clean it", which is exactly backwards for a security control. Emptiness
    semantics govern whether a consumer *believes* a payload; they have no bearing on
    whether it is safe to render.
    """
    scrubbed: dict[str, ContextSection] = {}
    counts: dict[str, int] = {}

    for name, section in sections.items():
        observations: list[Observation] = []
        changed = False
        for observation in section.observations:
            updated, hits = _redact_observation(observation)
            changed = changed or updated is not observation
            observations.append(updated)
            _accumulate(counts, hits)
        # Only ``observations`` is rebuilt: ``raw`` and ``provenance`` travel
        # untouched by design (see the module docstring).
        scrubbed[name] = (
            section.model_copy(update={"observations": tuple(observations)}) if changed else section
        )

    return scrubbed, SecurityMetadata(
        redaction_applied=bool(counts),
        redaction_counts=dict(sorted(counts.items())),
    )
