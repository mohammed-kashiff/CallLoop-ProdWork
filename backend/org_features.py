"""Per-org UI feature flags (AC-4).

A missing org+key row means the flag is on for trial-run UI switches. That
default is what keeps existing orgs unchanged on rollout. /api/me overlays DB
rows onto FEATURE_KEYS; extra keys in the table are returned too so new flags
do not need a migration.

Exception: use_selfhosted_transcription is off when unset, so deploy does not
silently move orgs off PyAI Hear. enable_bulk_call_clear is off when unset too
(AC-17) — org-wide hard-delete-adjacent "Clear cache" only runs for an org
once a platform admin turns it on there; it is unrelated to the per-call
delete a customer can always do from their own Audits list.
enable_call_rescoring is off when unset too — once a call has a completed
audit, its score is meant to stay fixed (Claude isn't perfectly
deterministic; re-running the same transcript could hand back a different
number). Off by default means neither ?refresh=true nor a retranscribe can
recompute an already-scored call's audit for an org until a platform admin
opts that org in.
enable_ticket_rescoring is the same rule for tickets (TA-11 / PRD §9).
**TA-33 (Launch Gating, 2026-09-16): deliberately kept off by default,
permanently — not a leftover launch-gating caution flag.** Same reasoning
as enable_call_rescoring: Claude isn't perfectly deterministic, so a
score should stay fixed once set rather than drift on a re-run, and
re-scoring costs a real Claude call per agent per ticket. An org opts in
per-org from Command Center when they specifically want that tradeoff.
show_ticket_audit_nav gates the "Ticket Audit" sidebar entry (TA-10).
**TA-32 (Launch Gating, 2026-09-16): on by default for new orgs** — the
Real Ticket QA Rubric, Roles, and Per-Agent Ticket Audit Rebuild epics
this flag was gating on are shipped. A platform admin can still turn it
off per org from Command Center if a specific team shouldn't see it yet.

**2026-09-18: show_growth_tools_nav split into three flags.** It used to
gate one bundled sidebar block (Feedbacks, Churn Risk, Integrations,
Training) under a name that described none of them precisely.
show_churn_feedback_nav is its replacement for Feedbacks + Churn Risk;
show_integrations_nav and show_training_nav are new, separate keys so
each flag's name actually says what it controls. The three orgs that
had an explicit (all `true`) row under the old key were migrated to
show_churn_feedback_nav directly in the DB — see the migration note in
this module's git history; no Alembic migration needed since
feature_key is a free-text column.

enable_justcall_integration / enable_intercom_integration gate the
JustCall and Intercom integrations independently per org: the connect
routes, the manual sync action, and the background poller all check
this before running (backend/api.py). On by default so no currently
connected org loses their integration on deploy. New orgs created via
signup (auth.ensure_membership) get an explicit
enable_justcall_integration=false row at creation time — JustCall
starts opted-out for new signups; Intercom does not get this special
case and stays on the coded default.

enable_ticket_auto_audit (IN-30..35, 2026-09-20): scores a ticket the
moment it finishes ingesting from Intercom, no manual click. Off by
default, permanently, same reasoning as enable_call_rescoring/
enable_ticket_rescoring being off by default — this changes real
behavior and spends real Claude money per closed ticket with no human
deciding when, so an org opts in deliberately. Checked by
ticket_score_api.auto_audit_ticket(), called from
intercom_ingest._ingest_intercom_object() only — the PDF upload path
is untouched, v1 is Intercom-only.

org_id is the JWT tenant only. Do not read it from the request body here.
"""

from __future__ import annotations

import logging
from typing import Literal, TypedDict

from fastapi import HTTPException

from . import applog
from . import audit_log
from . import db
from .org_ids import parse_org_id

log = logging.getLogger("callproof.org_features")

RiskTier = Literal["low", "medium", "danger"]


class FeatureDefinition(TypedDict):
    label: str
    description: str
    risk: RiskTier
    default_enabled: bool


# AC-36: single source of truth for every trial-run flag's metadata,
# including a risk tier — so Command Center can group flags into
# "Low Risk" / "Medium Risk" / "Danger Zone" sections and gate a
# confirmation prompt on danger-zone toggles *generically*, keyed off
# `risk`, instead of hardcoding which key names are dangerous in the
# frontend. Adding a flag here (with a risk tier) is the only change
# needed for it to show up correctly grouped — FEATURE_KEYS and
# DEFAULT_OFF_KEYS below are derived from this, not maintained separately.
#
# Risk assignment: "low" is a purely cosmetic UI/nav toggle with no
# behavior change; "medium" changes real behavior (which transcription
# engine runs, whether a score can be recomputed) but isn't destructive;
# "danger" can destroy data (enable_bulk_call_clear is hard-delete-adjacent
# — it soft-deletes every call and removes its recording, org-wide, at once).
FEATURE_DEFINITIONS: dict[str, FeatureDefinition] = {
    "show_usage_bar": {
        "label": "Usage bar",
        "description": "Shows the usage meter in the sidebar.",
        "risk": "low",
        "default_enabled": True,
    },
    "show_neighbourhood_nav": {
        "label": "Neighbourhood nav",
        "description": "Shows the Neighbourhood nav entry.",
        "risk": "low",
        "default_enabled": True,
    },
    "show_churn_feedback_nav": {
        "label": "Churn risk & feedback nav",
        "description": "Shows the Churn Risk and Feedbacks nav entries.",
        "risk": "low",
        "default_enabled": True,
    },
    "show_integrations_nav": {
        "label": "Integrations nav",
        "description": "Shows the Integrations nav entry.",
        "risk": "low",
        "default_enabled": True,
    },
    "show_training_nav": {
        "label": "Training nav",
        "description": "Shows the Training nav entry.",
        "risk": "low",
        "default_enabled": True,
    },
    "show_powered_by_pyai": {
        "label": "Powered by PyAI",
        "description": "Shows the \"Powered by PyAI\" badge.",
        "risk": "low",
        "default_enabled": True,
    },
    "show_billed_usage_panel": {
        "label": "Billed usage panel",
        "description": "Shows the billed-usage panel.",
        "risk": "low",
        "default_enabled": True,
    },
    "show_ticket_audit_nav": {
        "label": "Ticket Audit nav",
        "description": (
            "Shows the Ticket Audit page in this org's sidebar. On by "
            "default (TA-32, Launch Gating) — turn off per org if a "
            "specific team shouldn't see it yet."
        ),
        "risk": "low",
        "default_enabled": True,
    },
    "use_selfhosted_transcription": {
        "label": "Self-hosted transcription",
        "description": (
            "Use Whisper + pyannote on this host instead of PyAI Hear. "
            "Off until you turn it on for this org."
        ),
        "risk": "medium",
        "default_enabled": False,
    },
    "enable_call_rescoring": {
        "label": "Call rescoring",
        "description": (
            "Lets an already-audited call be re-scored (refresh, or after "
            "a retranscribe). Off by default so a call's score stays fixed "
            "once set. Off until you turn it on for this org."
        ),
        "risk": "medium",
        "default_enabled": False,
    },
    "enable_ticket_rescoring": {
        "label": "Ticket rescoring",
        "description": (
            "Lets an already-audited ticket be re-scored. Deliberately off "
            "by default, permanently (TA-33, Launch Gating) — a ticket's "
            "score stays fixed once set rather than drift on a re-run, and "
            "re-scoring spends a real Claude call per agent. Turn on per "
            "org if that tradeoff is wanted there."
        ),
        "risk": "medium",
        "default_enabled": False,
    },
    "enable_bulk_call_clear": {
        "label": "Bulk call clear",
        "description": (
            "Lets this org run Clear cache, which soft-deletes every call "
            "and removes their recordings at once. Off until you turn it "
            "on for this org."
        ),
        "risk": "danger",
        "default_enabled": False,
    },
    "enable_justcall_integration": {
        "label": "JustCall integration",
        "description": (
            "Lets this org connect JustCall, sync calls, and run the "
            "background JustCall poller. On by default for existing orgs; "
            "new orgs created via signup start with this off."
        ),
        "risk": "medium",
        "default_enabled": True,
    },
    "enable_intercom_integration": {
        "label": "Intercom integration",
        "description": (
            "Lets this org connect Intercom, and run the background "
            "Intercom poller. On by default."
        ),
        "risk": "medium",
        "default_enabled": True,
    },
    "enable_ticket_auto_audit": {
        "label": "Auto Audit (Intercom tickets)",
        "description": (
            "Scores a ticket automatically the moment it finishes "
            "ingesting from Intercom — no manual click. Off by default; "
            "spends a real Claude call per agent per closed ticket once "
            "turned on for this org."
        ),
        "risk": "medium",
        "default_enabled": False,
    },
}

# Trial-run dashboard switches. Insert other keys without a schema change —
# add them to FEATURE_DEFINITIONS above; these two stay derived from it.
FEATURE_KEYS = tuple(FEATURE_DEFINITIONS.keys())

# Missing key → on, except these. Unset must stay on PyAI, bulk clear and
# rescoring must stay off until a platform admin opts an org in.
DEFAULT_OFF_KEYS = frozenset(
    key for key, defn in FEATURE_DEFINITIONS.items() if not defn["default_enabled"]
)


def seed_new_org_defaults(conn, org_id: str) -> None:
    """Called once, from auth.ensure_membership's org-creation path, on the
    same connection/transaction that just inserted the row into `orgs`.

    Writes an explicit override row rather than changing a coded default,
    so this only affects orgs created from here on — every existing org
    keeps reading the JustCall default (on) via the normal missing-row
    rule in features_for_org(). Not routed through set_feature(): this is
    the org's starting state, not an admin decision, so it should not
    appear in org_features_history as a toggle."""
    oid = parse_org_id(org_id)
    if not oid:
        return
    conn.execute(
        """
        INSERT INTO org_features (org_id, feature_key, enabled, updated_at)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (org_id, feature_key) DO NOTHING
        """,
        (oid, "enable_justcall_integration", False),
    )


def feature_definitions() -> list[dict]:
    """AC-36: the full metadata list for Command Center's Flags tab —
    label/description/risk per key, so grouping and the danger-zone
    confirmation prompt can be driven generically instead of a hardcoded
    per-key frontend list. Order matches FEATURE_DEFINITIONS above."""
    return [
        {"key": key, **defn} for key, defn in FEATURE_DEFINITIONS.items()
    ]


def default_features() -> dict[str, bool]:
    return {key: key not in DEFAULT_OFF_KEYS for key in FEATURE_KEYS}


def features_for_org(org_id: str) -> dict[str, bool]:
    """Flags for this org. Missing trial-run keys are on; self-hosted ASR is off."""
    flags = default_features()
    oid = parse_org_id(org_id)
    if not oid:
        return flags
    try:
        with db.connection() as conn:
            db.apply_tenant_gucs(conn, org_id=oid)
            rows = conn.execute(
                """
                SELECT feature_key, enabled
                FROM org_features
                WHERE org_id = %s
                """,
                (oid,),
            ).fetchall()
    except Exception as e:  # noqa: BLE001
        log.debug("org_features lookup skipped: %s", e)
        return flags
    for row in rows or []:
        key = str(row.get("feature_key") or "").strip()
        if not key:
            continue
        flags[key] = bool(row.get("enabled"))
    return flags


def set_feature(
    org_id: str, feature_key: str, enabled: bool, *, changed_by: str,
) -> dict[str, bool]:
    """Upsert one flag and append a history row. Caller must already be a platform admin."""
    oid = parse_org_id(org_id)
    key = (feature_key or "").strip()
    actor = (changed_by or "").strip().lower()
    if not oid:
        raise HTTPException(status_code=400, detail="org_id is required.")
    if key not in FEATURE_KEYS:
        raise HTTPException(status_code=400, detail="Unknown feature_key.")
    if not actor or len(actor) > 254:
        raise HTTPException(status_code=400, detail="changed_by is required.")
    on = bool(enabled)
    was_on = features_for_org(oid).get(key)
    with db.connection() as conn:
        db.apply_tenant_gucs(conn, org_id=oid)
        conn.execute(
            """
            INSERT INTO org_features (org_id, feature_key, enabled, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (org_id, feature_key) DO UPDATE
            SET enabled = EXCLUDED.enabled, updated_at = now()
            """,
            (oid, key, on),
        )
        conn.execute(
            """
            INSERT INTO org_features_history (
                org_id, feature_key, enabled, changed_by
            )
            VALUES (%s, %s, %s, %s)
            """,
            (oid, key, on, actor),
        )
    applog.event(
        log, "org_feature_changed",
        org_id=oid,
        feature_key=key,
        enabled=on,
        changed_by=actor,
    )
    audit_log.record(
        oid, "org.feature_toggled",
        target_type="feature", target_id=key,
        before={"enabled": was_on}, after={"enabled": on},
        actor_email=actor,
    )
    return features_for_org(oid)


def feature_history(org_id: str, feature_key: str) -> list[dict]:
    """Chronological flag changes for this org+key. Empty if the org id is invalid."""
    oid = parse_org_id(org_id)
    key = (feature_key or "").strip()
    if not oid or not key:
        return []
    with db.connection() as conn:
        db.apply_tenant_gucs(conn, org_id=oid)
        rows = conn.execute(
            """
            SELECT org_id, feature_key, enabled, changed_by, changed_at
            FROM org_features_history
            WHERE org_id = %s AND feature_key = %s
            ORDER BY changed_at ASC, id ASC
            """,
            (oid, key),
        ).fetchall()
    out: list[dict] = []
    for row in rows or []:
        out.append(
            {
                "org_id": str(row.get("org_id") or ""),
                "feature_key": str(row.get("feature_key") or ""),
                "enabled": bool(row.get("enabled")),
                "changed_by": str(row.get("changed_by") or ""),
                "changed_at": row.get("changed_at"),
            }
        )
    return out


def feature_history_for_org(org_id: str) -> list[dict]:
    """Every flag change for this org, any key — the Account logs tab's
    'what feature was enabled/disabled and when' source. Newest first,
    unlike feature_history()'s per-key ascending order, since this reads
    as a log."""
    oid = parse_org_id(org_id)
    if not oid:
        return []
    with db.connection() as conn:
        db.apply_tenant_gucs(conn, org_id=oid)
        rows = conn.execute(
            """
            SELECT org_id, feature_key, enabled, changed_by, changed_at
            FROM org_features_history
            WHERE org_id = %s
            ORDER BY changed_at DESC, id DESC
            """,
            (oid,),
        ).fetchall()
    return [
        {
            "org_id": str(row.get("org_id") or ""),
            "feature_key": str(row.get("feature_key") or ""),
            "enabled": bool(row.get("enabled")),
            "changed_by": str(row.get("changed_by") or ""),
            "changed_at": row.get("changed_at"),
        }
        for row in rows or []
    ]
