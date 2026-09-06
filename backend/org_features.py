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
Off by default: an already-audited ticket is not silently re-scored;
?refresh=true on POST /api/tickets/{id}/score is 403 until a platform
admin opts the org in.
show_ticket_audit_nav gates the "Ticket Audit" sidebar entry (TA-10) —
off by default since the whole engine is still scaffolding (PRD §11: it
must not be mistaken for production). A platform admin turns it on per
org from Command Center to let a specific team try it.

org_id is the JWT tenant only. Do not read it from the request body here.
"""

from __future__ import annotations

import logging
from typing import Literal, TypedDict

from fastapi import HTTPException

from . import applog
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
    "show_growth_tools_nav": {
        "label": "Growth tools nav",
        "description": "Shows the Growth tools nav entry.",
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
            "Shows the Ticket Audit page in this org's sidebar. Off by "
            "default — the ticket-scoring engine is still scaffolding, not "
            "the final rubric. Turn on per org to let a team try it."
        ),
        "risk": "low",
        "default_enabled": False,
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
            "Lets an already-audited ticket be re-scored. Off by default "
            "so a ticket's score stays fixed once set. Off until you turn "
            "it on for this org."
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
}

# Trial-run dashboard switches. Insert other keys without a schema change —
# add them to FEATURE_DEFINITIONS above; these two stay derived from it.
FEATURE_KEYS = tuple(FEATURE_DEFINITIONS.keys())

# Missing key → on, except these. Unset must stay on PyAI, bulk clear and
# rescoring must stay off until a platform admin opts an org in.
DEFAULT_OFF_KEYS = frozenset(
    key for key, defn in FEATURE_DEFINITIONS.items() if not defn["default_enabled"]
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
