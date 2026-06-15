"""Seed the on-call DB: 5 placeholder engineers covering every CMDB team.

Run:
    uv run python -m scripts.seed_oncall

Idempotent: re-running is a no-op if the engineers' emails already exist
(shifts, categories, and expertise are refreshed on every run). Use
``--force`` to wipe and re-seed engineers too.

Coverage model: every team in ``aiops/tools/itsm/_demo_cmdb.py`` gets a
**day owner** (00..12 UTC) and an **evening owner** (12..24 UTC), all
seven days — see ``_TEAM_OWNERS`` below. Before this, only Payments Team
and Order Experience had team-specific shifts, so every other demo
service (product-catalog, ad, recommendation, email, …) fell through to
the global wildcard escalation and paged the same engineer every time.
Spreading day/evening ownership across all five engineers is what makes
dashboard failure injection assign uniformly instead of always paging
the wildcard manager.

Engineers (placeholder data; real values come from
``AIOPS_ONCALL_ROSTER_JSON`` in ``.env.shared``):

| Key (stable) | Name    | Primary team     | Day owner of (00..12 UTC)               | Evening owner of (12..24 UTC)            |
|--------------|---------|------------------|------------------------------------------|------------------------------------------|
| chinmay      | Chinmay | Payments Team    | Payments, Pricing, Finance Systems       | Catalog, Assets                          |
| riya         | Riya    | Payments Team    | Catalog, Communications, Web Experience  | Payments, Personalization                |
| arjun        | Arjun   | Order Experience | Order Exp., Personalization, Trust & Saf.| Ads, Fulfillment                         |
| meera        | Meera   | Order Experience | Ads, Assets                              | Order Exp., Communications, Web Exp.     |
| vikram       | Vikram  | Platform         | Platform On-Call (24x7) + global wildcard| Pricing, Finance Systems, Trust & Safety |

Shifts + expertise reference engineers by ``key`` (not email), so an
env override can rewrite name/email/slack_* fields without breaking the
join. See ``SECRETS.md`` for how to populate the real roster.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from sqlmodel import Session, delete, func, select

# Load .env before reading env vars — the seed runs standalone (no FastAPI
# lifespan) so AIOPS_ONCALL_ROSTER_JSON / AIOPS_SLACK_USER_MAP_JSON would
# otherwise be invisible. Same loader the demo server uses.
from aiops._dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from aiops.state import get_engine, init_db  # noqa: E402
from aiops.state.models import (  # noqa: E402
    EngineerExpertiseRow,
    EngineerRow,
    FailureCategoryRow,
    ShiftRow,
)

logger = logging.getLogger(__name__)


# Committed roster — placeholder names, ``@example.com`` emails, and
# ``UPLACEHOLDER…`` Slack IDs. Real workspace values are loaded from
# ``AIOPS_ONCALL_ROSTER_JSON`` (in ``.env.shared``, encrypted) and merged
# in by :func:`_resolve_engineers` below; the env override is keyed by
# the engineer's stable ``key`` (NOT email, so the email itself can be
# overridden too).
DEFAULT_ENGINEERS: list[dict] = [
    {
        "key": "chinmay",
        "name": "Chinmay",
        "email": "chinmay@example.com",
        "slack_handle": "@chinmay",
        "slack_user_id": "UPLACEHOLDER1",
        "primary_team": "Payments Team",
        "skills_csv": "payments,kafka,kubernetes",
        "timezone": "Asia/Kolkata",
    },
    {
        "key": "riya",
        "name": "Riya",
        "email": "riya@example.com",
        "slack_handle": "@riya",
        "slack_user_id": "UPLACEHOLDER2",
        "primary_team": "Payments Team",
        "skills_csv": "payments,kubernetes,databases",
        "timezone": "Asia/Kolkata",
    },
    {
        "key": "arjun",
        "name": "Arjun",
        "email": "arjun@example.com",
        "slack_handle": "@arjun",
        "slack_user_id": "UPLACEHOLDER3",
        "primary_team": "Order Experience",
        "skills_csv": "cart,checkout,frontend",
        "timezone": "Asia/Kolkata",
    },
    {
        "key": "meera",
        "name": "Meera",
        "email": "meera@example.com",
        "slack_handle": "@meera",
        "slack_user_id": "UPLACEHOLDER4",
        "primary_team": "Order Experience",
        "skills_csv": "cart,kubernetes,networking",
        "timezone": "Asia/Kolkata",
    },
    {
        "key": "vikram",
        "name": "Vikram",
        "email": "vikram@example.com",
        "slack_handle": "@vikram",
        "slack_user_id": "UPLACEHOLDER5",
        "primary_team": "Platform",
        "skills_csv": "kubernetes,infra,observability",
        "timezone": "Asia/Kolkata",
    },
]

# Fields the env override is allowed to rewrite. Team/skills/timezone
# are repo config and stay in code so reviewers see them in diffs.
_OVERRIDE_FIELDS = ("name", "email", "slack_handle", "slack_user_id")

# Env var carrying real engineer identities (lives in .env.shared,
# encrypted via git-crypt). Shape:
#   {"chinmay": {"name": "...", "email": "...",
#                "slack_handle": "@...", "slack_user_id": "U…"}, ...}
_ROSTER_ENV_VAR = "AIOPS_ONCALL_ROSTER_JSON"


def _resolve_engineers() -> list[dict]:
    """Merge the env-supplied real roster on top of ``DEFAULT_ENGINEERS``.

    Each engineer is keyed by its stable ``key`` field so an override can
    rewrite the name/email/slack fields without breaking SHIFTS or
    EXPERTISE joins (which both reference the key). Malformed env input
    logs a warning and falls back to placeholders — committing a real
    roster to git is the failure mode we *don't* want, so degrading
    silently to fake values is the safer default.
    """
    merged = [dict(e) for e in DEFAULT_ENGINEERS]
    raw = os.environ.get(_ROSTER_ENV_VAR, "").strip()
    if not raw:
        return merged
    try:
        overrides = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "seed_oncall: %s is not valid JSON (%s); using placeholders",
            _ROSTER_ENV_VAR,
            exc,
        )
        return merged
    if not isinstance(overrides, dict):
        logger.warning(
            "seed_oncall: %s must be a JSON object keyed by engineer 'key'; "
            "got %s; using placeholders",
            _ROSTER_ENV_VAR,
            type(overrides).__name__,
        )
        return merged

    applied = 0
    for engineer in merged:
        override = overrides.get(engineer["key"])
        if not isinstance(override, dict):
            continue
        for field in _OVERRIDE_FIELDS:
            value = override.get(field)
            if isinstance(value, str) and value.strip():
                engineer[field] = value.strip()
        applied += 1
    if applied:
        logger.info(
            "seed_oncall: applied %s overrides for %d engineer(s)",
            _ROSTER_ENV_VAR,
            applied,
        )
    return merged


# Team ownership: (team, day_owner_key, evening_owner_key). Every CMDB team
# (aiops/tools/itsm/_demo_cmdb.py) appears here so no demo service falls
# through to the wildcard escalation. Ownership is spread so each engineer
# fronts a similar slice of the dashboard's injectable scenarios (day shift):
#   chinmay → payment_* (2)             riya  → product-catalog, email, frontend (4)
#   arjun   → cart, checkout, reco (3)  meera → ad_* (3)
#   vikram  → unmapped services (Platform On-Call default) + wildcard
_TEAM_OWNERS: list[tuple[str, str, str]] = [
    ("Payments Team", "chinmay", "riya"),
    ("Order Experience", "arjun", "meera"),
    ("Catalog Team", "riya", "chinmay"),
    ("Personalization Team", "arjun", "riya"),
    ("Web Experience", "riya", "meera"),
    ("Ads Team", "meera", "arjun"),
    ("Pricing Team", "chinmay", "vikram"),
    ("Fulfillment Team", "arjun", "vikram"),
    ("Communications", "riya", "meera"),
    ("Assets Team", "meera", "chinmay"),
    ("Trust and Safety", "arjun", "vikram"),
    ("Finance Systems", "chinmay", "vikram"),
    ("Platform On-Call", "vikram", "vikram"),
]

# Shifts keyed by engineer ``key`` (not email) so env overrides can swap
# emails without breaking the join.
# Format: (engineer_key, team, day_of_week, start_hour_utc, end_hour_utc, role)
# Days are 0=Mon..6=Sun (matches datetime.weekday).
# Daily coverage, all seven days:
#   Day shift   = 00..12 UTC  (~ 05:30..17:30 IST)
#   Evening     = 12..24 UTC  (~ 17:30..05:30 IST)
#   Manager     = 24/7 (any day, any hour) — managed by the repository's
#                 special-case for role='manager_escalation' so the hours
#                 stored here are advisory only.
# Full-day, full-week coverage per team is deliberate: a weekend or
# late-night demo must page the team's owner, not silently degrade every
# assignment to the wildcard escalation engineer.
SHIFTS: list[tuple[str, str, int, int, int, str]] = [
    *[
        (owner, team, dow, start, end, "primary")
        for team, day_owner, eve_owner in _TEAM_OWNERS
        for owner, start, end in ((day_owner, 0, 12), (eve_owner, 12, 24))
        for dow in range(0, 7)
    ],
    # Vikram — GLOBAL manager_escalation, always-on. Tagged with the
    # special team key "*" so the repository falls back here for ANY
    # team that has no other coverage (a Sev-1 on an OTel-demo service
    # whose team isn't onboarded yet still pages someone). See
    # ``aiops/state/oncall_repository._GLOBAL_TEAM_KEY``.
    *[("vikram", "*", dow, 0, 24, "manager_escalation") for dow in range(0, 7)],
]


# Failure categories — sub-domains within each team. Alerts are matched
# against ``keywords_csv`` to pick a category, then expertise routing
# picks the best engineer for that category.
CATEGORIES: list[dict] = [
    # ── Payments Team sub-domains ────────────────────────────────────
    {
        "name": "payment-gateway",
        "display_name": "Payment Gateway",
        "description": "Third-party payment gateway integration (Stripe, PayPal, Razorpay) — 5xx errors, auth failures, charge timeouts.",
        "team": "Payments Team",
        "keywords_csv": "payment,gateway,api,5xx,charge,authorize,stripe,paypal,razorpay,checkout-api",
    },
    {
        "name": "payment-database",
        "display_name": "Payment Database",
        "description": "DB layer issues affecting payment service: connection pools, slow queries, locks, deadlocks.",
        "team": "Payments Team",
        "keywords_csv": "payment,database,db,sql,connection,pool,query,deadlock,timeout,postgres,replica",
    },
    {
        "name": "payment-kafka",
        "display_name": "Payment Kafka / Events",
        "description": "Event streaming / Kafka pipeline issues for payment events: lag, consumer down, partition rebalance.",
        "team": "Payments Team",
        "keywords_csv": "payment,kafka,queue,event,stream,topic,consumer,producer,lag,partition,backpressure",
    },
    # ── Order Experience sub-domains ─────────────────────────────────
    {
        "name": "cart-state",
        "display_name": "Cart State",
        "description": "Cart service state issues: empty carts, session loss, add-to-cart failures, valkey/redis backend.",
        "team": "Order Experience",
        "keywords_csv": "cart,session,state,redis,valkey,add-to-cart,empty-cart",
    },
    {
        "name": "checkout-flow",
        "display_name": "Checkout Flow",
        "description": "Checkout pipeline issues: order placement, confirmation, downstream calls to payment/shipping.",
        "team": "Order Experience",
        "keywords_csv": "checkout,order,placement,confirmation,delivery,shipping",
    },
    # ── Platform On-Call sub-domains ─────────────────────────────────
    # Team string matches the CMDB default (aiops/tools/itsm/_demo_cmdb.py:
    # CMDB_DEFAULT) — alerts on unmapped services (kafka, loadgen, …) are
    # assigned to "Platform On-Call", and category teams must equal the
    # verdict's assigned_team exactly for expertise routing to engage.
    {
        "name": "kubernetes-platform",
        "display_name": "Kubernetes Platform",
        "description": "K8s control-plane / cluster-wide issues: pod evictions, scheduler problems, etcd lag, kubelet failures.",
        "team": "Platform On-Call",
        "keywords_csv": "kubernetes,k8s,pod,deployment,kubelet,etcd,scheduler,eviction,node",
    },
    {
        "name": "observability",
        "display_name": "Observability Stack",
        "description": "Monitoring stack issues: Prometheus, Grafana, Jaeger, Loki, OTel collector.",
        "team": "Platform On-Call",
        "keywords_csv": "prometheus,grafana,jaeger,tempo,loki,trace,metric,otel,collector",
    },
    {
        "name": "networking",
        "display_name": "Networking & CDN",
        "description": "Network, CDN, DNS, ingress, proxy, TLS — anything between client and service.",
        "team": "Platform On-Call",
        "keywords_csv": "network,cdn,dns,ingress,proxy,tls,latency,timeout,connection",
    },
    {
        "name": "platform-infra",
        "display_name": "Platform Infrastructure",
        "description": "Shared infra services: Kafka pipelines, load generators, queues — anything owned by nobody else.",
        "team": "Platform On-Call",
        "keywords_csv": "kafka,queue,backpressure,lag,consumer,loadgen,flood,infra,saturation",
    },
    # ── One sub-domain per newly-covered team ────────────────────────
    # Keywords are matched against tokens (≥3 chars, lowercase) pulled from
    # affected_service + alert_summary + runbook by RA-005 — favour the
    # words the OTel-demo alert rules actually put in their summaries.
    {
        "name": "catalog-service",
        "display_name": "Product Catalog",
        "description": "Product catalog lookups: error spikes, slow product queries, stale inventory data.",
        "team": "Catalog Team",
        "keywords_csv": "product,catalog,sku,inventory,slow,latency,errors",
    },
    {
        "name": "personalization",
        "display_name": "Personalization & Recommendations",
        "description": "Recommendation service issues: cache failures, model staleness, empty recommendation lists.",
        "team": "Personalization Team",
        "keywords_csv": "recommendation,recommendations,cache,personalization,model",
    },
    {
        "name": "web-frontend",
        "display_name": "Web Frontend",
        "description": "Storefront web tier: page latency, asset loading, frontend proxy errors.",
        "team": "Web Experience",
        "keywords_csv": "frontend,web,browser,page,proxy,latency,render",
    },
    {
        "name": "ads-serving",
        "display_name": "Ads Serving",
        "description": "Ad service issues: creative serving errors, CPU saturation, GC pauses.",
        "team": "Ads Team",
        "keywords_csv": "ads,adservice,creative,cpu,garbage,collection,pauses",
    },
    {
        "name": "pricing-fx",
        "display_name": "Pricing & FX",
        "description": "Currency conversion and quote issues: stale FX rates, conversion errors, pricing drift.",
        "team": "Pricing Team",
        "keywords_csv": "currency,pricing,quote,exchange,conversion,rates",
    },
    {
        "name": "fulfillment-shipping",
        "display_name": "Fulfillment & Shipping",
        "description": "Shipping quote and tracking issues: carrier API failures, slow quote responses.",
        "team": "Fulfillment Team",
        "keywords_csv": "shipping,fulfillment,delivery,tracking,carrier,warehouse",
    },
    {
        "name": "email-delivery",
        "display_name": "Email Delivery",
        "description": "Transactional email issues: SMTP failures, memory leaks in the mailer, bounce storms.",
        "team": "Communications",
        "keywords_csv": "email,smtp,memory,rss,leak,bounce,mailer",
    },
    {
        "name": "asset-delivery",
        "display_name": "Asset Delivery",
        "description": "Image/static asset serving: slow image loads, missing assets, provider timeouts.",
        "team": "Assets Team",
        "keywords_csv": "image,images,asset,assets,slow,load,provider",
    },
    {
        "name": "trust-safety",
        "display_name": "Trust & Safety",
        "description": "Fraud detection pipeline issues: scoring failures, rule-engine errors, queue buildup.",
        "team": "Trust and Safety",
        "keywords_csv": "fraud,detection,abuse,risk,scoring",
    },
    {
        "name": "finance-systems",
        "display_name": "Finance Systems",
        "description": "Accounting/ledger issues: posting failures, reconciliation lag, billing errors.",
        "team": "Finance Systems",
        "keywords_csv": "accounting,ledger,finance,billing,reconciliation",
    },
]


# Per-(engineer, category) expertise keyed by engineer ``key``. Format:
#   (engineer_key, category_name, proficiency, incidents_resolved, feedback_score, manual_priority)
# proficiency: novice | intermediate | expert | principal
EXPERTISE: list[tuple[str, str, str, int, float, int]] = [
    # ── chinmay — payments gateway + kafka strong; k8s passable ──────
    ("chinmay", "payment-gateway", "expert", 15, 4.5, 0),
    ("chinmay", "payment-kafka", "expert", 20, 4.7, 0),
    ("chinmay", "kubernetes-platform", "intermediate", 5, 4.0, 0),
    # ── riya — payments database specialist ──────────────────────────
    ("riya", "payment-database", "expert", 12, 4.6, 0),
    ("riya", "payment-gateway", "intermediate", 4, 3.8, 0),
    # ── arjun — order experience expert ──────────────────────────────
    ("arjun", "cart-state", "expert", 18, 4.4, 0),
    ("arjun", "checkout-flow", "expert", 14, 4.5, 0),
    # ── meera — order + infra mix ────────────────────────────────────
    ("meera", "cart-state", "intermediate", 6, 3.9, 0),
    ("meera", "networking", "expert", 10, 4.6, 0),
    ("meera", "kubernetes-platform", "intermediate", 7, 4.1, 0),
    # ── vikram — platform principal, manager_escalation ──────────────
    ("vikram", "kubernetes-platform", "expert", 25, 4.8, 0),
    ("vikram", "observability", "expert", 18, 4.5, 0),
    ("vikram", "networking", "intermediate", 5, 4.0, 0),
    ("vikram", "platform-infra", "expert", 16, 4.6, 0),
    # ── newly-covered teams: day owner expert, evening owner backup ──
    ("riya", "catalog-service", "expert", 11, 4.4, 0),
    ("chinmay", "catalog-service", "intermediate", 4, 3.9, 0),
    ("arjun", "personalization", "expert", 9, 4.3, 0),
    ("riya", "personalization", "intermediate", 3, 3.8, 0),
    ("riya", "web-frontend", "expert", 12, 4.5, 0),
    ("meera", "web-frontend", "intermediate", 4, 3.9, 0),
    ("meera", "ads-serving", "expert", 8, 4.2, 0),
    ("arjun", "ads-serving", "intermediate", 3, 3.7, 0),
    ("chinmay", "pricing-fx", "expert", 7, 4.3, 0),
    ("vikram", "pricing-fx", "intermediate", 2, 3.8, 0),
    ("arjun", "fulfillment-shipping", "expert", 6, 4.1, 0),
    ("vikram", "fulfillment-shipping", "intermediate", 2, 3.7, 0),
    ("riya", "email-delivery", "expert", 10, 4.4, 0),
    ("meera", "email-delivery", "intermediate", 3, 3.8, 0),
    ("meera", "asset-delivery", "expert", 9, 4.3, 0),
    ("chinmay", "asset-delivery", "intermediate", 3, 3.8, 0),
    ("arjun", "trust-safety", "expert", 5, 4.0, 0),
    ("vikram", "trust-safety", "intermediate", 2, 3.7, 0),
    ("chinmay", "finance-systems", "expert", 6, 4.2, 0),
    ("vikram", "finance-systems", "intermediate", 2, 3.8, 0),
]


def _seed(session: Session, *, force: bool) -> int:
    """Idempotent seed; returns number of engineers in the DB after seeding."""
    if force:
        # Order matters: expertise FK→{engineers,categories}; shifts FK→engineers.
        session.exec(delete(EngineerExpertiseRow))  # type: ignore[arg-type]
        session.exec(delete(ShiftRow))  # type: ignore[arg-type]
        session.exec(delete(FailureCategoryRow))  # type: ignore[arg-type]
        session.exec(delete(EngineerRow))  # type: ignore[arg-type]
        session.commit()
        logger.info("seed_oncall: --force wiped existing rows")

    engineers = _resolve_engineers()
    key_to_id: dict[str, int] = {}
    for spec in engineers:
        # ``key`` is the SHIFTS/EXPERTISE join identifier; it isn't a
        # column on EngineerRow, so strip it before constructing the row.
        key = spec["key"]
        row_fields = {k: v for k, v in spec.items() if k != "key"}
        existing = session.exec(
            select(EngineerRow).where(EngineerRow.email == row_fields["email"])
        ).first()
        if existing is None:
            row = EngineerRow(**row_fields)
            session.add(row)
            session.commit()
            session.refresh(row)
            key_to_id[key] = row.id or 0
            logger.info(
                "seed_oncall: inserted engineer key=%r name=%r (id=%s)",
                key,
                row_fields["name"],
                row.id,
            )
        else:
            key_to_id[key] = existing.id or 0

    # Shifts: clear existing rows for these engineers then re-insert (cheap;
    # tiny row count). This lets you edit the SHIFTS table above and re-run.
    if key_to_id:
        session.exec(
            delete(ShiftRow).where(ShiftRow.engineer_id.in_(list(key_to_id.values())))  # type: ignore[arg-type]
        )
        session.commit()
    for engineer_key, team, dow, start, end, role in SHIFTS:
        eid = key_to_id.get(engineer_key)
        if eid is None:
            continue
        session.add(
            ShiftRow(
                engineer_id=eid,
                team=team,
                day_of_week=dow,
                start_hour_utc=start,
                end_hour_utc=end,
                role=role,
            )
        )
    session.commit()

    # ── Failure categories (insert-if-missing by name) ────────────────────
    name_to_cat_id: dict[str, int] = {}
    for cat in CATEGORIES:
        existing_cat = session.exec(
            select(FailureCategoryRow).where(FailureCategoryRow.name == cat["name"])
        ).first()
        if existing_cat is None:
            crow = FailureCategoryRow(**cat)
            session.add(crow)
            session.commit()
            session.refresh(crow)
            name_to_cat_id[cat["name"]] = crow.id or 0
            logger.info(
                "seed_oncall: inserted category %r (team=%s, id=%s)",
                cat["name"],
                cat["team"],
                crow.id,
            )
        else:
            # Refresh the descriptive fields + keywords in case the spec
            # in this file evolves; the id stays stable.
            existing_cat.display_name = cat["display_name"]
            existing_cat.description = cat["description"]
            existing_cat.team = cat["team"]
            existing_cat.keywords_csv = cat["keywords_csv"]
            session.add(existing_cat)
            session.commit()
            name_to_cat_id[cat["name"]] = existing_cat.id or 0

    # ── Expertise mappings: wipe-and-reinsert (composite-PK rows; cheap) ──
    # We always reset these so edits to EXPERTISE above take effect on
    # re-seed without needing --force.
    if key_to_id and name_to_cat_id:
        session.exec(
            delete(EngineerExpertiseRow).where(  # type: ignore[arg-type]
                EngineerExpertiseRow.engineer_id.in_(list(key_to_id.values()))
            )
        )
        session.commit()

    for engineer_key, cat_name, prof, incidents, score, manual in EXPERTISE:
        eid = key_to_id.get(engineer_key)
        cid = name_to_cat_id.get(cat_name)
        if eid is None or cid is None:
            logger.warning(
                "seed_oncall: skipping expertise row (%s, %s) — engineer or category missing",
                engineer_key,
                cat_name,
            )
            continue
        session.add(
            EngineerExpertiseRow(
                engineer_id=eid,
                category_id=cid,
                proficiency_level=prof,
                incidents_resolved=incidents,
                feedback_score=score,
                manual_priority=manual,
            )
        )
    session.commit()
    logger.info(
        "seed_oncall: inserted %d expertise rows across %d categories",
        len(EXPERTISE),
        len(name_to_cat_id),
    )

    count = session.exec(select(func.count()).select_from(EngineerRow)).one()
    return int(count or 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the on-call DB")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Wipe engineers + shifts + categories + expertise before seeding (default: insert-if-missing)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    init_db()
    with Session(get_engine()) as session:
        count = _seed(session, force=args.force)
    print(f"seed_oncall: done; engineers in DB = {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
