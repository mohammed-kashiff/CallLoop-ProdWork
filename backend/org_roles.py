"""AC-56/AC-58: the account owner promotes/demotes their own team members
to/from Manager. Owner-only — a Manager cannot create another Manager, and
this never touches the owner's own row (org-ownership transfer is
explicitly out of scope for the Roles epic).

org_members has no RLS (see 0005_rls.py's own docstring: "org_members is
not RLS'd: first-user claim reads it before the org GUC exists" — it's a
blanket GRANT to callproof_app instead). Every query here does its own
tenant scoping in the WHERE clause, with org_id always the caller's own
verified org_id (auth.org_id_from_request), never client input — the same
discipline every other org_members query in this codebase already follows
(ensure_membership, api.py's name update, ticket_agent_aliases.list_org_agents).
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from . import applog
from . import audit_log
from . import db
from .org_ids import org_scope, parse_org_id

log = logging.getLogger("callproof.org_roles")

_ASSIGNABLE_ROLES = frozenset({"manager", "member"})


def list_team(org_id: str | None) -> dict:
    """Every member of this org, for the owner's own "Your team" view —
    same query shape as ticket_agent_aliases.list_org_agents (names are
    nullable, captured once at signup)."""
    oid = parse_org_id(org_id)
    if not oid:
        raise HTTPException(status_code=400, detail="org_id is required.")
    with org_scope(oid):
        with db.connection() as conn:
            rows = conn.execute(
                """
                SELECT user_id, first_name, last_name, role
                FROM org_members
                WHERE org_id = %s
                ORDER BY role, first_name, last_name
                """,
                (oid,),
            ).fetchall()
    return {
        "org_id": oid,
        "members": [
            {
                "user_id": str(r["user_id"]),
                "first_name": r["first_name"],
                "last_name": r["last_name"],
                "role": r["role"],
            }
            for r in rows or []
        ],
    }


def set_member_role(
    org_id: str | None, target_user_id: str | None, new_role: str, *, changed_by: str,
) -> dict:
    """Promote a member to Manager, or demote a Manager back to member.

    Refuses to touch the owner's own row (role == "owner" is never a valid
    target here — ownership transfer is a separate, explicitly out-of-scope
    concern per AC-56) and refuses to ever set role to anything but
    "manager"/"member" — this endpoint can never mint a second owner.
    """
    oid = parse_org_id(org_id)
    if not oid:
        raise HTTPException(status_code=400, detail="org_id is required.")
    target = parse_org_id(target_user_id)
    if not target:
        raise HTTPException(status_code=400, detail="target_user_id is required.")
    role = (new_role or "").strip().lower()
    if role not in _ASSIGNABLE_ROLES:
        raise HTTPException(status_code=400, detail="role must be 'manager' or 'member'.")
    actor = (changed_by or "").strip().lower()
    if not actor or len(actor) > 254:
        raise HTTPException(status_code=400, detail="changed_by is required.")

    with org_scope(oid):
        with db.connection() as conn:
            row = conn.execute(
                """
                SELECT role FROM org_members
                WHERE org_id = %s AND user_id = %s
                FOR UPDATE
                """,
                (oid, target),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="No such member in this org.")
            current_role = str(row["role"])
            if current_role == "owner":
                raise HTTPException(
                    status_code=400, detail="The account owner's role can't be changed here.",
                )
            if current_role == role:
                return {
                    "org_id": oid, "user_id": target, "role": role, "changed": False,
                }
            conn.execute(
                """
                UPDATE org_members SET role = %s
                WHERE org_id = %s AND user_id = %s
                """,
                (role, oid, target),
            )
    applog.event(
        log, "org_member_role_changed",
        org_id=oid, target_user_id=target, previous_role=current_role,
        new_role=role, changed_by=actor,
    )
    audit_log.record(
        oid, "member.role_changed",
        target_type="org_member", target_id=target,
        before={"role": current_role}, after={"role": role},
        actor_email=actor,
    )
    return {"org_id": oid, "user_id": target, "role": role, "changed": True}
