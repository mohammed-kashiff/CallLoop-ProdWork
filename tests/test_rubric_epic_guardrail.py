"""CR-15: epic-closing guardrail + change-logging attribution.

Guardrail (PRD §19): a single test spanning the real CR-10 (weight
consumption) + CR-11/CR-12 (org-aware loading) chain together, not
per-ticket units in isolation — an org untouched by this epic must score
byte-identically to before it shipped, for every verdict combination.

Change logging (PRD §18): rubric_weights_saved now carries changed_by,
old_weights, and new_weights (org_id/rubric_id/version already did) —
same applog.event pattern as org_feature_changed/cache_cleared. rubrics
has no changed_by column and this epic adds no migration, so this
structured log is the audit trail rather than a new Activity-feed row.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from itertools import product

from backend.org_ids import DEFAULT_RUBRIC_ID
from backend.qa_v8 import list_dimensions, score_v8
from backend.rules_v8 import WEIGHTS as LEGACY_WEIGHTS
from backend.rules_v8 import aggregate_score as legacy_aggregate_score

VERDICTS = ("pass", "partial", "fail")
DIM_IDS = (
    "resolution_effectiveness",
    "ownership_next_steps",
    "active_listening",
    "tone_empathy_professionalism",
)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _NoRubricConn:
    """An org this epic has never touched: zero rows in rubrics for it."""

    def execute(self, sql, params=None):
        return _Result([])


def _results(dims, verdicts, *, hostile=False):
    out = []
    for dim in dims:
        res = {"verdict": verdicts[dim["id"]]}
        if hostile and dim["id"] == "tone_empathy_professionalism":
            res["hostile_override"] = True
        out.append((dim, res))
    return out


def test_untouched_org_scores_identically_across_the_real_cr10_cr11_chain():
    """The actual production functions chained together — fetch_active_rubric
    (CR-11/CR-12) -> list_dimensions -> score_v8 -> aggregate_score (CR-10) —
    for an org with no rubrics row at all, against every verdict combination."""
    from backend.audit_store import fetch_active_rubric, load_v8_definition

    rubric_id, version, definition = fetch_active_rubric(_NoRubricConn(), org_id="anything")
    assert rubric_id == DEFAULT_RUBRIC_ID
    assert definition == load_v8_definition()

    dims = list_dimensions(definition)
    rubric_weights = {d["id"]: d["weight"] for d in dims}
    assert rubric_weights == LEGACY_WEIGHTS

    for combo in product(VERDICTS, repeat=len(DIM_IDS)):
        verdicts = dict(zip(DIM_IDS, combo))
        for hostile in (False, True):
            legacy = legacy_aggregate_score(verdicts, tone_hostile_override=hostile)
            got_score, _tally, got_hostile = score_v8(
                _results(dims, verdicts, hostile=hostile),
            )
            assert got_hostile is hostile
            assert got_score == legacy


# ---------- change logging ----------


class _Result2:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _SaveConn:
    """Minimal fake matching insert_weighted_version's exact query shapes,
    starting from a legacy-default org (no active row yet)."""

    def __init__(self):
        self.rows = []

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        if "FROM RUBRICS" in norm and "FOR UPDATE" in norm:
            active = [r for r in self.rows if r["is_active"]]
            return _Result2(active[:1])
        if "MAX(VERSION)" in norm:
            return _Result2([{"v": 0}])
        if "UPDATE RUBRICS" in norm and "IS_ACTIVE = FALSE" in norm:
            for row in self.rows:
                row["is_active"] = False
            return _Result2([])
        if "INSERT INTO RUBRICS" in norm:
            import datetime as _dt

            from psycopg.types.json import Json as _Json

            row = {
                "id": params[0], "org_id": params[1], "name": params[2],
                "version": params[3],
                "definition": params[4].obj if isinstance(params[4], _Json) else params[4],
                "is_active": True,
                "updated_at": _dt.datetime(2026, 3, 1, tzinfo=_dt.timezone.utc),
            }
            self.rows.append(row)
            return _Result2([row])
        raise AssertionError(f"unexpected query: {norm}")


@contextmanager
def _fake_db(monkeypatch, conn):
    @contextmanager
    def _connection(*, bypass_rls=False):
        assert not bypass_rls
        yield conn

    monkeypatch.setattr("backend.admin_console.db.connection", _connection)
    yield conn


def test_saving_new_weights_logs_org_admin_old_and_new_weights(monkeypatch, caplog):
    from backend.admin_console import save_org_rubric

    conn = _SaveConn()
    new_weights = {
        "resolution_effectiveness": 50,
        "ownership_next_steps": 20,
        "active_listening": 15,
        "tone_empathy_professionalism": 15,
    }
    with _fake_db(monkeypatch, conn):
        with caplog.at_level(logging.INFO, logger="callproof.admin"):
            out = save_org_rubric(
                "00000000-0000-4000-8000-0000000000aa",
                new_weights,
                changed_by="Admin@Example.com",
            )
    lines = [
        r.getMessage() for r in caplog.records if "rubric_weights_saved" in r.getMessage()
    ]
    assert len(lines) == 1
    line = lines[0]
    assert "org_id=00000000-0000-4000-8000-0000000000aa" in line
    assert "changed_by=admin@example.com" in line
    assert f"rubric_id={out['rubric_id']}" in line
    assert f"version={out['version']}" in line
    assert "new_weights=" in line and "50" in line
    assert "old_weights=" in line and "40" in line  # legacy default before this save


def test_changed_by_is_required():
    from fastapi import HTTPException
    import pytest

    from backend.admin_console import save_org_rubric

    with pytest.raises(HTTPException) as exc:
        save_org_rubric(
            "00000000-0000-4000-8000-0000000000aa",
            {
                "resolution_effectiveness": 40, "ownership_next_steps": 20,
                "active_listening": 20, "tone_empathy_professionalism": 20,
            },
            changed_by="",
        )
    assert exc.value.status_code == 400
