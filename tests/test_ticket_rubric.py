"""TA-7/TA-13: the "Ticket QA" rubric — real content (PRD §10), still not
the final design (PRD §4/§11). Every LLM-judged dimension carries
scaffold=True so nothing downstream mistakes it for finished. TA-13 adds
the real rubrics-table storage (ensure_ticket_rubric/fetch_active_ticket_
rubric) and the new deterministic Response Timeliness dimension."""

from __future__ import annotations

import ast
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.paths import ROOT
from backend.ticket_rubric import (
    SCAFFOLD_TICKET_RUBRIC,
    evaluate_response_timeliness,
    get_scaffold_rubric,
)

FORBIDDEN_MODULES = ("rules_v8", "qa_v8")


def test_shares_no_code_with_the_call_rubric_dispatch():
    src = (ROOT / "backend" / "ticket_rubric.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in FORBIDDEN_MODULES
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not any(name in mod.split(".") for name in FORBIDDEN_MODULES)
    # no rules_v8-style deterministic "method" dispatch on any dimension
    for dim in SCAFFOLD_TICKET_RUBRIC:
        assert "method" not in dim


def test_every_dimension_is_labeled_as_scaffold():
    for dim in SCAFFOLD_TICKET_RUBRIC:
        assert dim["scaffold"] is True, dim["id"]


def test_every_dimension_has_the_fields_ticket_scoring_reads():
    for dim in SCAFFOLD_TICKET_RUBRIC:
        assert isinstance(dim["id"], str) and dim["id"]
        assert isinstance(dim["name"], str) and dim["name"]
        assert isinstance(dim["weight"], (int, float)) and dim["weight"] > 0
        assert isinstance(dim["question"], str) and dim["question"].strip()


def test_dimension_ids_are_unique():
    ids = [d["id"] for d in SCAFFOLD_TICKET_RUBRIC]
    assert len(ids) == len(set(ids))


def test_weights_sum_to_one_hundred():
    assert sum(d["weight"] for d in SCAFFOLD_TICKET_RUBRIC) == 100


def test_covers_the_six_prd_named_criteria():
    ids = {d["id"] for d in SCAFFOLD_TICKET_RUBRIC}
    assert ids == {
        "tone", "ownership", "diagnostic_reasoning", "investigation_rigor",
        "resolution_effectiveness", "escalation_quality",
    }


def test_get_scaffold_rubric_returns_an_independent_copy():
    a = get_scaffold_rubric()
    a[0]["weight"] = 999
    b = get_scaffold_rubric()
    assert b[0]["weight"] != 999
    assert b == SCAFFOLD_TICKET_RUBRIC


# ---------- exercises TA-6 end to end with this real rubric shape ----------


TURNS = [
    {"seq": 0, "speaker": "customer", "agent_user_id": None,
     "text": "Checkout keeps failing with a 504 error."},
    {"seq": 1, "speaker": "agent", "agent_user_id": None,
     "text": "I can see the 504 in the logs — restarting the payment worker now."},
    {"seq": 2, "speaker": "customer", "agent_user_id": None,
     "text": "That fixed it, thank you!"},
]


def test_scaffold_rubric_runs_end_to_end_through_ticket_scoring():
    from backend import ticket_scoring

    def _dispatch(prompt: str) -> str:
        if "diagnose" in prompt.lower():
            return json.dumps({
                "verdict": "pass", "reasoning": "correctly named the 504",
                "evidence_quote": "I can see the 504 in the logs", "evidence_seq": 1,
            })
        if "resolved" in prompt.lower():
            return json.dumps({
                "verdict": "pass", "reasoning": "customer confirmed the fix",
                "evidence_quote": "That fixed it, thank you!", "evidence_seq": 2,
            })
        return json.dumps({
            "verdict": "not_applicable", "reasoning": "n/a for this thread",
            "evidence_quote": "", "evidence_seq": None,
        })

    result = ticket_scoring.score_ticket(
        TURNS, get_scaffold_rubric(), call_claude_fn=_dispatch,
    )
    assert 0 <= result["score"] <= 100
    by_id = {f["id"]: f for f in result["findings"]}
    assert set(by_id) == {d["id"] for d in SCAFFOLD_TICKET_RUBRIC}
    assert by_id["diagnostic_reasoning"]["verdict"] == "pass"
    assert by_id["diagnostic_reasoning"]["evidence_verified"] is True
    assert by_id["resolution_effectiveness"]["verdict"] == "pass"
    # escalation/ownership/tone/investigation weren't matched by the fake
    # dispatcher's keywords, so they legitimately came back not_applicable
    # — not_applicable dimensions are excluded from the score, not scored
    # as failures (see ticket_scoring._numeric_score / _SKIP_SCORE).
    assert by_id["escalation_quality"]["verdict"] == "not_applicable"


# ---------- TA-13: evaluate_response_timeliness (deterministic, no Claude) ----------


def _dt(hour: int, minute: int = 0, day: int = 1) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=timezone.utc)


def test_timeliness_error_when_no_turn_has_a_timestamp():
    turns = [
        {"seq": 0, "speaker": "customer", "text": "hi", "sent_at": None},
        {"seq": 1, "speaker": "agent", "text": "hello", "sent_at": None},
    ]
    out = evaluate_response_timeliness(turns)
    assert out["verdict"] == "error"
    assert out["deterministic"] is True


def test_timeliness_not_applicable_when_no_one_ever_waited():
    turns = [
        {"seq": 0, "speaker": "agent", "text": "hi", "sent_at": _dt(9)},
        {"seq": 1, "speaker": "agent", "text": "still here", "sent_at": _dt(10)},
    ]
    out = evaluate_response_timeliness(turns)
    assert out["verdict"] == "not_applicable"


def test_timeliness_pass_for_a_quick_reply():
    turns = [
        {"seq": 0, "speaker": "customer", "text": "help", "sent_at": _dt(9, 0)},
        {"seq": 1, "speaker": "agent", "text": "on it", "sent_at": _dt(10, 0)},
    ]
    out = evaluate_response_timeliness(turns)
    assert out["verdict"] == "pass"
    assert out["evidence_seq"] == 1
    assert out["evidence_verified"] is True


def test_timeliness_partial_for_a_same_day_slow_reply():
    turns = [
        {"seq": 0, "speaker": "customer", "text": "help", "sent_at": _dt(9, 0)},
        {"seq": 1, "speaker": "agent", "text": "sorry, on it now", "sent_at": _dt(20, 0)},
    ]
    out = evaluate_response_timeliness(turns)
    assert out["verdict"] == "partial"


def test_timeliness_fail_for_a_multi_day_gap():
    turns = [
        {"seq": 0, "speaker": "customer", "text": "help", "sent_at": _dt(9, 0, day=1)},
        {"seq": 1, "speaker": "agent", "text": "sorry for the delay", "sent_at": _dt(9, 0, day=3)},
    ]
    out = evaluate_response_timeliness(turns)
    assert out["verdict"] == "fail"


def test_timeliness_a_bot_reply_does_not_count_as_the_agent_responding():
    turns = [
        {"seq": 0, "speaker": "customer", "text": "help", "sent_at": _dt(9, 0)},
        {"seq": 1, "speaker": "bot", "text": "a bot reply", "sent_at": _dt(9, 5)},
        {"seq": 2, "speaker": "agent", "text": "real reply", "sent_at": _dt(20, 0)},
    ]
    out = evaluate_response_timeliness(turns)
    assert out["verdict"] == "partial"  # measured from the customer to the AGENT reply
    assert out["evidence_seq"] == 2


def test_timeliness_uses_the_worst_gap_across_multiple_waits():
    turns = [
        {"seq": 0, "speaker": "customer", "text": "first", "sent_at": _dt(9, 0)},
        {"seq": 1, "speaker": "agent", "text": "quick reply", "sent_at": _dt(9, 30)},
        {"seq": 2, "speaker": "customer", "text": "second issue", "sent_at": _dt(10, 0)},
        {"seq": 3, "speaker": "agent", "text": "slow reply", "sent_at": _dt(9, 0, day=3)},
    ]
    out = evaluate_response_timeliness(turns)
    assert out["verdict"] == "fail"
    assert out["evidence_seq"] == 3


def test_timeliness_accepts_iso_strings_the_same_as_datetimes():
    """get_ticket()'s API response JSON-serializes sent_at to an ISO
    string via _iso() — this function must handle both."""
    turns = [
        {"seq": 0, "speaker": "customer", "text": "help", "sent_at": _dt(9, 0).isoformat()},
        {"seq": 1, "speaker": "agent", "text": "on it", "sent_at": _dt(10, 0).isoformat()},
    ]
    out = evaluate_response_timeliness(turns)
    assert out["verdict"] == "pass"


def test_timeliness_not_folded_into_the_weighted_score():
    """A deterministic finding sitting alongside the LLM ones must never
    be summed into score_ticket()'s own weighted score — it has no
    'weight' key, so ticket_scoring._numeric_score() already skips it
    (falls back to 0 and is excluded); this just documents that on purpose."""
    timeliness = evaluate_response_timeliness([
        {"seq": 0, "speaker": "customer", "text": "help", "sent_at": _dt(9, 0)},
        {"seq": 1, "speaker": "agent", "text": "on it", "sent_at": _dt(10, 0)},
    ])
    assert "weight" not in timeliness


# ---------- TA-13: the "Ticket QA" rubric as a real rubrics-table row ----------


def test_ensure_ticket_rubric_creates_a_row_when_none_exists_live():
    from dotenv import load_dotenv

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE
    from backend import ticket_rubric

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg
    from psycopg.rows import dict_row

    admin = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    org_id = str(uuid.uuid4())
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "ta13-rubric-live-test"))
        admin.commit()

        assert ticket_rubric.fetch_active_ticket_rubric(org_id) is None

        created = ticket_rubric.ensure_ticket_rubric(org_id)
        assert created["name"] == ticket_rubric.TICKET_QA_RUBRIC_NAME
        assert len(created["dimensions"]) == 6

        # Idempotent: calling again returns the same row, doesn't duplicate.
        again = ticket_rubric.ensure_ticket_rubric(org_id)
        assert again["id"] == created["id"]

        rows = admin.execute(
            "SELECT COUNT(*) AS n FROM rubrics WHERE org_id = %s", (org_id,),
        ).fetchone()
        assert int(rows["n"]) == 1
    finally:
        admin.execute("DELETE FROM rubrics WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
        admin.commit()
        admin.close()


def test_ticket_rubric_never_shows_up_as_a_call_rubrics_active_row_live():
    """The exact cross-engine hazard TA-13 has to avoid: an org running
    both engines must never have audit_store.fetch_active_rubric() (the
    CALL engine) hand back the Ticket QA rubric, or vice versa, even
    though both rows share the same org-wide is_active column."""
    from dotenv import load_dotenv

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE
    from backend import audit_store, ticket_rubric

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg
    from psycopg.rows import dict_row

    admin = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    org_id = str(uuid.uuid4())
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "ta13-cross-engine-test"))
        admin.commit()

        ticket_rubric.ensure_ticket_rubric(org_id)

        # The call engine's own active-rubric lookup must fall back to the
        # legacy default — never the Ticket QA row that's the only active
        # rubrics row for this org.
        rubric_id, version, definition = audit_store.fetch_active_rubric(admin, org_id=org_id)
        assert rubric_id == audit_store.DEFAULT_RUBRIC_ID
        assert definition.get("name") != ticket_rubric.TICKET_QA_RUBRIC_NAME
    finally:
        admin.execute("DELETE FROM rubrics WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
        admin.commit()
        admin.close()
