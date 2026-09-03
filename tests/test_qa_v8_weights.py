"""CR-10: aggregate score uses the rubric's dimension weights, not WEIGHTS."""

from __future__ import annotations

import json
from itertools import product
from pathlib import Path

from backend.paths import RUBRIC_PATH
from backend.qa_v8 import list_dimensions, score_v8
from backend.rules_v8 import WEIGHTS, aggregate_score

VERDICTS = ("pass", "partial", "fail")
DIM_IDS = (
    "resolution_effectiveness",
    "ownership_next_steps",
    "active_listening",
    "tone_empathy_professionalism",
)


def _rubric_dims():
    data = json.loads(Path(RUBRIC_PATH).read_text(encoding="utf-8"))
    return list_dimensions(data)


def _results(dims, verdicts, *, hostile=False):
    out = []
    for dim in dims:
        res = {"verdict": verdicts[dim["id"]]}
        if hostile and dim["id"] == "tone_empathy_professionalism":
            res["hostile_override"] = True
        out.append((dim, res))
    return out


def test_score_v8_is_byte_identical_to_legacy_weights_from_rubric_json():
    dims = _rubric_dims()
    rubric_weights = {d["id"]: d["weight"] for d in dims}
    assert rubric_weights == WEIGHTS

    for combo in product(VERDICTS, repeat=len(DIM_IDS)):
        verdicts = dict(zip(DIM_IDS, combo))
        for hostile in (False, True):
            legacy = aggregate_score(verdicts, tone_hostile_override=hostile)
            got, _tally, got_hostile = score_v8(
                _results(dims, verdicts, hostile=hostile),
            )
            assert got_hostile is hostile
            assert json.dumps(got) == json.dumps(legacy)


def test_fixture_weight_change_moves_aggregate_and_constant_is_not_load_bearing(monkeypatch):
    dims = _rubric_dims()
    verdicts = {
        "resolution_effectiveness": "pass",
        "ownership_next_steps": "fail",
        "active_listening": "fail",
        "tone_empathy_professionalism": "fail",
    }
    baseline, _, _ = score_v8(_results(dims, verdicts))
    assert baseline == 40.0

    monkeypatch.setattr(
        "backend.qa_v8.rv8.WEIGHTS",
        {k: 0 for k in WEIGHTS},
    )
    monkeypatch.setattr(
        "backend.rules_v8.WEIGHTS",
        {k: 0 for k in WEIGHTS},
    )
    still, _, _ = score_v8(_results(dims, verdicts))
    assert still == baseline

    reweighted = []
    for dim in dims:
        row = dict(dim)
        if row["id"] == "resolution_effectiveness":
            row["weight"] = 10
        reweighted.append(row)
    moved, _, _ = score_v8(_results(reweighted, verdicts))
    assert moved == 10.0
    assert moved != baseline
