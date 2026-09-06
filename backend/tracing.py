"""Best-effort Sentry spans. Never raises into the request.

Scoring, ingest, and PyAI/Claude calls must keep running if Sentry is
down, uninitialised, or a span callback throws — the same rule
qa_v8 already applies to on_dimension_event.
"""

from __future__ import annotations

import sys
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from . import applog

_SAFE_DETAIL_KEYS = frozenset({"method", "weight", "name", "verdict", "error"})

_open_spans: dict[tuple[int, str], Any] = {}
_lock = threading.Lock()


def _safe_data(detail: dict | None) -> dict[str, Any]:
    if not isinstance(detail, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in detail.items():
        if key not in _SAFE_DETAIL_KEYS or value is None:
            continue
        if key == "error":
            text = applog.redact_line(str(value))[:200]
            if text:
                out[key] = text
            continue
        if isinstance(value, (str, int, float, bool)):
            out[key] = value
    return out


def _set_span_data(span: Any, data: dict[str, Any]) -> None:
    if span is None:
        return
    setter = getattr(span, "set_data", None)
    if not callable(setter):
        return
    for key, value in data.items():
        try:
            setter(key, value)
        except Exception:  # noqa: BLE001
            pass


@contextmanager
def span(op: str, name: str, **data: Any) -> Iterator[Any]:
    """Open a child span of the current request transaction. No-op if Sentry
    is missing or the request was not sampled. Work inside the `with` always
    runs; tracing failures are swallowed."""
    cm = None
    entered = False
    current = None
    try:
        import sentry_sdk

        cm = sentry_sdk.start_span(op=op, name=name)
        current = cm.__enter__()
        entered = True
        _set_span_data(current, {k: v for k, v in data.items() if v is not None})
    except Exception:  # noqa: BLE001
        cm = None
        entered = False
        current = None
    try:
        yield current
    except BaseException:
        if entered and cm is not None:
            try:
                cm.__exit__(*sys.exc_info())
            except Exception:  # noqa: BLE001
                pass
        raise
    else:
        if entered and cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


def _span_key(dim: dict | None) -> tuple[int, str]:
    did = "unknown"
    if isinstance(dim, dict):
        did = str(dim.get("id") or "unknown")
    return (threading.get_ident(), did)


def dimension_event(dim: dict | None, status: str, detail: dict | None = None) -> None:
    """AC-24 hook companion: started opens a span, succeeded/failed closes it.

    Never raises. Does not attach reasoning, evidence, or transcript text.
    """
    try:
        key = _span_key(dim)
        if status == "started":
            _open_dimension_span(key, dim, detail)
            return
        if status in ("succeeded", "failed"):
            _finish_dimension_span(key, ok=(status == "succeeded"), detail=detail)
    except Exception:  # noqa: BLE001
        pass


def _open_dimension_span(
    key: tuple[int, str], dim: dict | None, detail: dict | None,
) -> None:
    did = key[1]
    try:
        import sentry_sdk

        cm = sentry_sdk.start_span(op="gen_ai.chat", name=f"criterion:{did}")
        span_obj = cm.__enter__()
        data = _safe_data(detail)
        if isinstance(dim, dict) and dim.get("method"):
            data.setdefault("method", dim.get("method"))
        _set_span_data(span_obj, data)
    except Exception:  # noqa: BLE001
        return
    with _lock:
        old = _open_spans.pop(key, None)
        _open_spans[key] = cm
    if old is not None:
        try:
            old.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


def _finish_dimension_span(
    key: tuple[int, str], *, ok: bool, detail: dict | None,
) -> None:
    with _lock:
        cm = _open_spans.pop(key, None)
    if cm is None:
        return
    try:
        span_obj = getattr(cm, "_span", None) or cm
        _set_span_data(span_obj, _safe_data(detail))
        setter = getattr(span_obj, "set_status", None)
        if callable(setter):
            setter("ok" if ok else "internal_error")
    except Exception:  # noqa: BLE001
        pass
    try:
        cm.__exit__(None, None, None)
    except Exception:  # noqa: BLE001
        pass
