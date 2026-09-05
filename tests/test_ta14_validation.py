"""TA-14 (PRD §12 step 3, §13 — the epic's primary success metric):
end-to-end validation on a real ticket, driving the exact code that's
deployed to production (backend.api.app, in-process against the real
Postgres/Storage/Claude services this session already uses for every
other live test) — not a synthetic fixture, not a hand-built turns list.

A real PDF goes in via the real upload route, a scorecard with verified
evidence comes out via the real score route, with no manual intervention
— no step in between touches the parsed turns, the rubric, or the
findings by hand.

Screenshot coverage: NOT met by this file. The one real ticket sample
available (this one) has zero embedded images — TA-2's own finding,
which is what created this epic's whole screenshot-recoverability
tension in the first place. The user explicitly declined a broader scan
of other real tickets in Downloads for this validation pass, so this
stays an open, explicitly-unmet requirement rather than something
papered over with a synthetic image (PRD §13: "a text-only validation
pass does not count as done" — this file's own text-only pass is
reported as partial, not as satisfying TA-14 on its own).
"""

from __future__ import annotations

import os
import uuid

import pytest

REAL_SAMPLE_PATH = os.path.expanduser(
    "~/Downloads/justcall_2026_08_19_215475545067923.pdf"
)


@pytest.mark.skipif(
    not os.path.isfile(REAL_SAMPLE_PATH),
    reason="real sample PDF only present on the dev machine that has it",
)
def test_real_ticket_end_to_end_text_only(monkeypatch):
    from dotenv import load_dotenv

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg
    from fastapi.testclient import TestClient
    from psycopg.rows import dict_row

    from backend.api import app
    from tests.conftest import authorize

    admin = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    exists = admin.execute(
        """
        SELECT to_regclass('public.tickets') AS t,
               to_regclass('public.ticket_audits') AS a
        """
    ).fetchone()
    if not exists or not exists["t"] or not exists["a"]:
        admin.close()
        pytest.skip("required migrations not applied")

    with open(REAL_SAMPLE_PATH, "rb") as f:
        pdf_bytes = f.read()

    org_id = str(uuid.uuid4())
    ticket_id = None
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "ta14-validation"))
        admin.commit()

        client = TestClient(app)
        authorize(client, monkeypatch, org_id=org_id)

        # 1. A real ticket PDF goes in via the real upload route. No
        # manual construction of turns — this is TA-4/TA-5's actual
        # ingestion path, exactly as deployed.
        upload_resp = client.post(
            "/api/tickets/upload",
            files={"file": ("real_ticket.pdf", pdf_bytes, "application/pdf")},
        )
        assert upload_resp.status_code == 200, upload_resp.text
        ticket_id = upload_resp.json()["ticket_id"]
        assert upload_resp.json()["status"] == "ready"

        get_resp = client.get(f"/api/tickets/{ticket_id}")
        assert get_resp.status_code == 200
        ticket = get_resp.json()
        assert ticket["status"] == "ready"
        messages = ticket["messages"]
        # This real ticket is a long real support thread — sanity-check
        # it parsed into a substantial, correctly-ordered turn sequence,
        # not a handful of fallback/error turns.
        assert len(messages) > 40
        assert [m["seq"] for m in messages] == list(range(len(messages)))
        assert any(m["speaker"] == "customer" for m in messages)
        assert any(m["speaker"] == "agent" for m in messages)
        assert any(m["speaker"] == "bot" for m in messages)
        # Explicit, honest accounting: this real sample has no embedded
        # screenshots — confirmed here, not assumed.
        assert ticket["assets"] == []
        assert not any(m["has_image"] for m in messages)

        messages_by_seq = {m["seq"]: m for m in messages}

        # 2. A scorecard with verified evidence comes out via the real
        # score route — real Claude calls against the org's real
        # "Ticket QA" rubric (TA-13), no manual intervention.
        score_resp = client.post(f"/api/tickets/{ticket_id}/score")
        assert score_resp.status_code == 200, score_resp.text
        scorecard = score_resp.json()
        assert scorecard["rubric_scaffold"] is True
        assert 0 <= scorecard["score"] <= 100
        # 6 LLM-judged dimensions (TA-13) + Response Timeliness (deterministic).
        assert len(scorecard["findings"]) == 7

        # Independently re-verify the primary success metric's own
        # definition of "verified evidence" for text, using the exact
        # normalization qa_engine.validate_evidence() itself guarantees
        # (case/punctuation-insensitive — unchanged across the whole
        # epic, confirmed by this ticket's own diff-check guardrail) —
        # not a raw substring check, which is a stricter bar than the
        # system actually promises and produces false negatives.
        from backend.qa_engine import _norm

        verified_text_findings = 0
        for finding in scorecard["findings"]:
            if finding.get("deterministic"):
                # Response Timeliness: verified by construction (real
                # sent_at math), not a string match — already covered by
                # test_ticket_rubric.py's own dedicated tests.
                continue
            if finding["verdict"] in ("not_applicable", "error"):
                continue
            if finding["evidence_verified"]:
                cited_turn = messages_by_seq.get(finding["evidence_seq"])
                assert cited_turn is not None, (
                    f"{finding['id']}: evidence_seq {finding['evidence_seq']} "
                    f"is not a real turn on this ticket"
                )
                assert _norm(finding["evidence_text"]) in _norm(cited_turn["text"]), (
                    f"{finding['id']}: evidence_verified=True but the quote "
                    f"does not actually appear in the turn it cites (seq "
                    f"{finding['evidence_seq']})"
                )
                verified_text_findings += 1

        # At least one real, independently-confirmed verbatim-quote
        # finding is the text half of TA-14's primary success metric.
        assert verified_text_findings >= 1

        # Re-fetching must show the persisted result, not a fresh score
        # (TA-11) — still no manual intervention required to see it again.
        again = client.get(f"/api/tickets/{ticket_id}")
        assert again.json()["audit"] is not None
        assert again.json()["audit"]["score"] == scorecard["score"]
    finally:
        admin.execute("DELETE FROM ticket_audits WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM ticket_message_assets WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM ticket_messages WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM tickets WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM rubrics WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM api_usage WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
        admin.commit()
        admin.close()
