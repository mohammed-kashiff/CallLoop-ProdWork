"""IN-6/IN-11: Intercom REST client — conversation fetch, the search-based
polling backstop, and the attachment-fetch SSRF host allowlist. No live
Intercom API calls."""

from __future__ import annotations

import pytest

from backend import intercom_client


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None, content=b"{}"):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.content = content

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise intercom_client.httpx.HTTPStatusError(
                "error", request=None, response=self,
            )


def test_get_conversation_requires_an_id():
    with pytest.raises(ValueError):
        intercom_client.get_conversation("tok", "")


def test_get_conversation_returns_the_parsed_body(monkeypatch):
    monkeypatch.setattr(
        intercom_client.httpx, "get",
        lambda url, **k: _FakeResponse(200, {"id": "conv-1", "source": {}}),
    )
    result = intercom_client.get_conversation("tok", "conv-1")
    assert result == {"id": "conv-1", "source": {}}


def test_get_ticket_requires_an_id():
    with pytest.raises(ValueError):
        intercom_client.get_ticket("tok", "")


def test_get_ticket_returns_the_parsed_body(monkeypatch):
    monkeypatch.setattr(
        intercom_client.httpx, "get",
        lambda url, **k: _FakeResponse(200, {"id": "ticket-1", "ticket_attributes": {}}),
    )
    result = intercom_client.get_ticket("tok", "ticket-1")
    assert result == {"id": "ticket-1", "ticket_attributes": {}}


def test_search_closed_conversations_sends_the_right_query_shape(monkeypatch):
    captured = {}

    def fake_post(url, *, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse(200, {"conversations": [{"id": "c1"}, {"id": "c2"}]})

    monkeypatch.setattr(intercom_client.httpx, "post", fake_post)
    result = intercom_client.search_closed_conversations("tok", 1700000000)

    assert captured["url"] == f"{intercom_client.BASE_URL}/conversations/search"
    query = captured["json"]["query"]
    assert query["operator"] == "AND"
    fields = {f["field"]: f for f in query["value"]}
    assert fields["state"]["value"] == "closed"
    assert fields["updated_at"]["operator"] == ">"
    assert fields["updated_at"]["value"] == "1700000000"
    assert result == [{"id": "c1"}, {"id": "c2"}]


def test_search_closed_conversations_filters_out_non_dict_entries(monkeypatch):
    monkeypatch.setattr(
        intercom_client.httpx, "post",
        lambda *a, **k: _FakeResponse(200, {"conversations": [{"id": "c1"}, None, "garbage"]}),
    )
    result = intercom_client.search_closed_conversations("tok", 1700000000)
    assert result == [{"id": "c1"}]


def test_search_closed_conversations_handles_missing_conversations_key(monkeypatch):
    monkeypatch.setattr(intercom_client.httpx, "post", lambda *a, **k: _FakeResponse(200, {}))
    assert intercom_client.search_closed_conversations("tok", 1700000000) == []


def test_search_closed_tickets_sends_the_right_query_shape(monkeypatch):
    captured = {}

    def fake_post(url, *, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse(200, {"tickets": [{"id": "t1"}, {"id": "t2"}]})

    monkeypatch.setattr(intercom_client.httpx, "post", fake_post)
    result = intercom_client.search_closed_tickets("tok", 1700000000)

    assert captured["url"] == f"{intercom_client.BASE_URL}/tickets/search"
    query = captured["json"]["query"]
    assert query["operator"] == "AND"
    fields = {f["field"]: f for f in query["value"]}
    assert fields["open"]["value"] is False
    assert fields["updated_at"]["operator"] == ">"
    assert fields["updated_at"]["value"] == "1700000000"
    assert result == [{"id": "t1"}, {"id": "t2"}]


def test_search_closed_tickets_filters_out_non_dict_entries(monkeypatch):
    monkeypatch.setattr(
        intercom_client.httpx, "post",
        lambda *a, **k: _FakeResponse(200, {"tickets": [{"id": "t1"}, None, "garbage"]}),
    )
    result = intercom_client.search_closed_tickets("tok", 1700000000)
    assert result == [{"id": "t1"}]


def test_search_closed_tickets_handles_missing_tickets_key(monkeypatch):
    monkeypatch.setattr(intercom_client.httpx, "post", lambda *a, **k: _FakeResponse(200, {}))
    assert intercom_client.search_closed_tickets("tok", 1700000000) == []


# ── fetch_attachment host allowlist (IN-11) ─────────────────────────────────
# Confirmed against Intercom's own published CSP documentation, not guessed.

_ALLOWED_URLS = [
    "https://downloads.intercomcdn.com/i/o/123/image.png",
    "https://downloads.intercomcdn.eu/i/o/123/image.png",
    "https://downloads.au.intercomcdn.com/i/o/123/image.png",
    "https://uploads.intercomcdn.com/i/o/123/image.png",
    "https://uploads.intercomusercontent.com/i/o/123/image.png",
    "https://static.intercomassets.com/i/o/123/image.png",
    "https://static.intercomassets.eu/i/o/123/image.png",
    "https://intercom-attachments-1.com/i/o/123/image.png",
    "https://intercom-attachments-9.com/i/o/123/image.png",
    "https://sub.intercom-attachments-5.com/i/o/123/image.png",
    "https://au.intercom-attachments.com/i/o/123/image.png",
    "https://something.intercom-attachments.eu/i/o/123/image.png",
]

_DISALLOWED_URLS = [
    "https://evil.com/i/o/123/image.png",
    "https://intercom-attachments-0.com/i/o/123/image.png",  # 0 not in 1-9
    "https://intercom-attachments-10.com/i/o/123/image.png",  # two digits, not documented
    "https://evilintercom-attachments-1.com/i/o/123/image.png",  # no dot boundary
    "https://notintercomcdn.com/i/o/123/image.png",  # no dot boundary
    "https://intercomcdn.com.evil.com/i/o/123/image.png",  # suffix trick
    "http://downloads.intercomcdn.com/i/o/123/image.png",  # not https
]


@pytest.mark.parametrize("url", _ALLOWED_URLS)
def test_fetch_attachment_allows_the_documented_intercom_cdn_hosts(monkeypatch, url):
    captured = {}

    def _fake_get(u, **k):
        captured["url"] = u
        return _FakeResponse(200)

    monkeypatch.setattr(intercom_client.httpx, "get", _fake_get)
    intercom_client.fetch_attachment(url)
    assert captured["url"] == url


@pytest.mark.parametrize("url", _DISALLOWED_URLS)
def test_fetch_attachment_rejects_everything_else(monkeypatch, url):
    def _boom(*a, **k):
        raise AssertionError("must not fetch a disallowed host")

    monkeypatch.setattr(intercom_client.httpx, "get", _boom)
    with pytest.raises(intercom_client.DisallowedAttachmentHost):
        intercom_client.fetch_attachment(url)


def test_fetch_attachment_returns_the_response_body(monkeypatch):
    monkeypatch.setattr(
        intercom_client.httpx, "get",
        lambda u, **k: _FakeResponse(200, content=b"png-bytes-here"),
    )
    result = intercom_client.fetch_attachment("https://downloads.intercomcdn.com/x.png")
    assert result == b"png-bytes-here"


def test_fetch_attachment_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(
        intercom_client.httpx, "get",
        lambda u, **k: _FakeResponse(404),
    )
    with pytest.raises(intercom_client.httpx.HTTPStatusError):
        intercom_client.fetch_attachment("https://downloads.intercomcdn.com/x.png")
