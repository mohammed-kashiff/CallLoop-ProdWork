"""Log redaction must not keep header values or API keys."""

from __future__ import annotations

from backend.applog import redact_line, safe_exception_text


def test_safe_exception_text_strips_illegal_header_value():
    class LocalProtocolError(Exception):
        pass

    exc = LocalProtocolError("Illegal header value b'sk-ant-fakekeyvaluexxxx\\n'")
    text = safe_exception_text(exc)
    assert "sk-ant-" not in text
    assert "fakekeyvaluexxxx" not in text
    assert "illegal HTTP header value" in text
    assert "LocalProtocolError" in text


def test_redact_line_covers_header_blob_and_key_prefix():
    line = "claude attempt 1/4 exception: LocalProtocolError: Illegal header value b'sk-ant-zzzzzzzzzzzzzzzz'"
    out = redact_line(line)
    assert "sk-ant-" not in out
    assert "zzzzzzzzzzzzzzzz" not in out
    assert "[REDACTED]" in out


def test_redact_line_covers_justcall_secret_fields():
    line = "justcall_api_secret=jc_sec_should_not_remain api_key=abc"
    out = redact_line(line)
    assert "jc_sec_should_not_remain" not in out
    assert "[REDACTED]" in out
