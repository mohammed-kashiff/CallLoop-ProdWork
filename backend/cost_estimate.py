"""
Approximate CallProof spend estimates (not provider invoices).

Rates come from env (USD). Tune to match your contract; defaults are
ballpark only and labeled as estimates in the UI.
"""

from __future__ import annotations

import os
from typing import Any


def _fenv(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def rates() -> dict[str, float]:
    """Configurable USD rates for estimates."""
    return {
        # Hear / PyAI: prefer metered units when present; else audio minutes.
        "pyai_usd_per_unit": _fenv("COST_PYAI_USD_PER_UNIT", 0.01),
        "pyai_usd_per_minute": _fenv("COST_PYAI_USD_PER_MINUTE", 0.01),
        # Claude: flat per outbound hit (no token meter yet); per-audit for calls.
        "claude_usd_per_hit": _fenv("COST_CLAUDE_USD_PER_HIT", 0.02),
        "claude_usd_per_audit": _fenv("COST_CLAUDE_USD_PER_AUDIT", 0.06),
    }


def _round_usd(n: float) -> float:
    return round(max(0.0, float(n or 0)), 4)


def estimate_call_cost(
    audio_seconds: float | int | None,
    *,
    has_audit: bool = False,
) -> dict[str, Any]:
    """
    Per-call approximate cost from duration + whether a scorecard exists.
    """
    r = rates()
    secs = float(audio_seconds or 0)
    minutes = secs / 60.0 if secs > 0 else 0.0
    pyai = _round_usd(minutes * r["pyai_usd_per_minute"])
    claude = _round_usd(r["claude_usd_per_audit"] if has_audit else 0.0)
    total = _round_usd(pyai + claude)
    return {
        "estimate": True,
        "currency": "USD",
        "pyai_usd": pyai,
        "claude_usd": claude,
        "total_usd": total,
        "audio_minutes": round(minutes, 3),
        "has_audit": bool(has_audit),
        "rates": {
            "pyai_usd_per_minute": r["pyai_usd_per_minute"],
            "claude_usd_per_audit": r["claude_usd_per_audit"],
        },
    }


def today_from_usage(usage: dict[str, Any] | None) -> dict[str, Any]:
    """
    Today's approximate spend from local api_usage aggregates (UTC window).
    PyAI: units × $/unit when units > 0, else actions × minute-proxy is weak —
    fall back to actions × (pyai_usd_per_minute) as a coarse stand-in only when
    no units were recorded.
    Claude: hits × $/hit.
    """
    r = rates()
    usage = usage or {}
    by = usage.get("by_provider") or {}
    pyai_u = by.get("pyai") or {}
    claude_u = by.get("anthropic") or {}

    units = float(pyai_u.get("units") or 0)
    actions = int(pyai_u.get("actions") or 0)
    claude_hits = int(claude_u.get("hits") or 0)

    if units > 0:
        pyai = _round_usd(units * r["pyai_usd_per_unit"])
        pyai_basis = "units"
    else:
        # Coarse fallback when PyAI did not return x-pyai-units.
        pyai = _round_usd(actions * r["pyai_usd_per_minute"])
        pyai_basis = "actions_as_minutes"

    claude = _round_usd(claude_hits * r["claude_usd_per_hit"])
    total = _round_usd(pyai + claude)

    return {
        "estimate": True,
        "currency": "USD",
        "window": usage.get("window") or "utc_today",
        "since": usage.get("since"),
        "pyai_usd": pyai,
        "claude_usd": claude,
        "total_usd": total,
        "pyai_basis": pyai_basis,
        "pyai_units": units,
        "pyai_actions": actions,
        "claude_hits": claude_hits,
        "rates": {
            "pyai_usd_per_unit": r["pyai_usd_per_unit"],
            "pyai_usd_per_minute": r["pyai_usd_per_minute"],
            "claude_usd_per_hit": r["claude_usd_per_hit"],
        },
        "label": f"Today ~${total:.2f}",
    }


def estimate_usage_cost(summary: dict[str, Any] | None) -> dict[str, float]:
    """All-time (or windowed) USD estimate from usage_summary() aggregates."""
    r = rates()
    usage = summary or {}
    by = usage.get("by_provider") or {}
    pyai_u = by.get("pyai") or {}
    claude_u = by.get("anthropic") or {}

    units = float(pyai_u.get("units") or 0)
    actions = int(pyai_u.get("actions") or 0)
    claude_hits = int(claude_u.get("hits") or 0)

    if units > 0:
        pyai = _round_usd(units * r["pyai_usd_per_unit"])
    else:
        pyai = _round_usd(actions * r["pyai_usd_per_minute"])
    claude = _round_usd(claude_hits * r["claude_usd_per_hit"])
    return {
        "pyai_usd": pyai,
        "claude_usd": claude,
        "total_usd": _round_usd(pyai + claude),
    }
