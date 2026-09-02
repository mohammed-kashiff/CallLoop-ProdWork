"""CL-41: Agent vs Customer when outbound cues cancel each other."""

from __future__ import annotations

from backend import qa_engine as qa


def _turn(speaker, text, *, channel=None, seq=0, start=0.0):
    return {
        "seq": seq,
        "speaker": speaker,
        "channel": channel,
        "start": start,
        "end": start + 1,
        "text": text,
    }


KATRINA = [
    _turn("speaker_1", "Hello.", seq=0, start=2.1),
    _turn(
        "speaker_2",
        "Hey Katrina, I'm just calling from Investolift. How are you doing?",
        seq=1,
        start=2.5,
    ),
    _turn("speaker_1", "I'm good. I'm good. Thanks for asking.", seq=2, start=6.3),
]

# Same opener without "just" — both cue phrases are contiguous substrings.
KATRINA_NO_JUST = [
    _turn("speaker_1", "Hello.", seq=0, start=2.1),
    _turn(
        "speaker_2",
        "Hey Katrina, I'm calling from Investolift. How are you doing?",
        seq=1,
        start=2.5,
    ),
]

TIE_NO_CUES = [
    _turn("speaker_1", "Hello.", seq=0),
    _turn("speaker_2", "Hi there, is this a good time?", seq=1),
]


def test_outbound_calling_from_is_agent_not_a_customer_cue():
    assert qa._cue_score("Hey Katrina, I'm just calling from Investolift.") == 2
    assert qa._cue_score("Hey Katrina, I'm calling from Investolift.") == 2
    assert qa._cue_score("I'm calling about my order") < 0


def test_greeting_cues_win_without_claude(monkeypatch):
    monkeypatch.setattr(
        qa,
        "call_claude",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("claude must not run")),
    )
    segs = [
        _turn("speaker_1", "Thank you for calling Investor Lift, how can I help?"),
        _turn("speaker_2", "Hi, I have a question about my account."),
    ]
    assert qa.classify_roles(segs) == "speaker_1"


def test_katrina_opener_is_agent_without_claude(monkeypatch):
    monkeypatch.setattr(
        qa,
        "call_claude",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("claude must not run")),
    )
    assert qa.classify_roles(KATRINA) == "speaker_2"
    assert qa.classify_roles(KATRINA_NO_JUST) == "speaker_2"


def test_channel_tiebreak_skips_claude(monkeypatch):
    monkeypatch.setattr(
        qa,
        "call_claude",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("claude must not run")),
    )
    segs = [
        _turn("speaker_1", "Hello.", channel=0),
        _turn("speaker_2", "Hello.", channel=1),
    ]
    assert qa.classify_roles(segs) == "speaker_1"


def test_true_tie_uses_claude_not_first_speaker(monkeypatch):
    monkeypatch.setattr(qa, "ANTHROPIC_API_KEY", "test-not-a-real-key")

    def fake_claude(prompt, **_k):
        assert "speaker_2" in prompt
        return '{"agent_speaker": "speaker_2"}'

    monkeypatch.setattr(qa, "call_claude", fake_claude)
    assert qa.classify_roles(TIE_NO_CUES) == "speaker_2"


def test_claude_failure_falls_back_to_first_speaker(monkeypatch):
    monkeypatch.setattr(qa, "ANTHROPIC_API_KEY", "test-not-a-real-key")
    monkeypatch.setattr(
        qa, "call_claude", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("down")),
    )
    assert qa.classify_roles(TIE_NO_CUES) == "speaker_1"


def test_missing_anthropic_key_does_not_call_claude(monkeypatch):
    monkeypatch.setattr(qa, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(
        qa,
        "call_claude",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("claude must not run")),
    )
    assert qa.classify_roles(TIE_NO_CUES) == "speaker_1"


def test_claude_rejects_unknown_speaker_id(monkeypatch):
    monkeypatch.setattr(qa, "ANTHROPIC_API_KEY", "test-not-a-real-key")
    monkeypatch.setattr(qa, "call_claude", lambda *_a, **_k: '{"agent_speaker": "agent"}')
    assert qa.classify_roles(TIE_NO_CUES) == "speaker_1"
