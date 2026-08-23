"""
Deterministic rule registry for the CallProof QA engine (v2).

Each rule takes:
  - segments: list of dicts with keys seq, speaker, channel, start, end, text
  - agent_speaker: the speaker label we treat as the agent (e.g. "speaker_1")
  - **kwargs: some rules take additional config (call_type, disclosure_config, etc.)
and returns a dict:
  { "verdict": "pass" | "partial" | "fail",
    "reasoning": str,
    "evidence_seq": int | None,
    "evidence_text": str | None }

Rules are keyword/heuristic + timestamp based: fast, free, and 100% reproducible.
Nuanced judgment (sarcasm, paraphrase quality, resolution appropriateness) is
handled by the LLM criteria instead -- see rubric_v3_final.json.

Every function here maps to a "check" value in rubric_v3_final.json.
Thresholds below match rules_v2_parameters.md; anything marked
NEEDS CALIBRATION is a starting value, not a validated one.
"""

import re

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

NEGATION_MARKERS = re.compile(r"\b(cant|cannot|wont|couldnt|didnt|dont|isnt|arent|wasnt)\b")
NEGATION_LOOKAHEAD_CHARS = 20  # how far past a match to scan for a negation marker


def find_term(text, terms):
    """Negation-aware phrase match. Returns the matched term, or None.

    Discards a match if a negation marker appears shortly after it, so
    "I can't help with that" no longer matches "i can".
    """
    t = (text or "").lower()
    for term in terms:
        idx = t.find(term)
        if idx == -1:
            continue
        window = t[idx: idx + len(term) + NEGATION_LOOKAHEAD_CHARS]
        if NEGATION_MARKERS.search(window):
            continue
        return term
    return None


def _agent_segments(segments, agent_speaker):
    return [s for s in segments if s.get("speaker") == agent_speaker]


def _customer_segments(segments, agent_speaker):
    return [s for s in segments if s.get("speaker") != agent_speaker]


def _result(verdict, reasoning, seg):
    return {
        "verdict": verdict,
        "reasoning": reasoning,
        "evidence_seq": seg["seq"] if seg else None,
        "evidence_text": seg["text"] if seg else None,
    }


# ---------------------------------------------------------------------------
# opening_purpose
# ---------------------------------------------------------------------------

GREETING_TERMS = ["hello", "hi ", "good morning", "good afternoon", "good evening",
                  "thank you for calling", "thanks for calling"]

# Narrowed from v1: requires agent-initiative framing, not the agent merely
# echoing what the customer already said (dropped "calling about",
# "regarding your", "about your", "following up").
PURPOSE_TERMS = ["how can i help", "how may i help", "what can i do for you",
                 "how can i assist", "what brings you in"]

OPENING_WINDOW = 2  # narrowed from 3 -- rewards prompt framing, not eventual framing


def has_opening_purpose(segments, agent_speaker):
    opening = _agent_segments(segments, agent_speaker)[:OPENING_WINDOW]
    greeting_seg = purpose_seg = None
    for s in opening:
        if greeting_seg is None and find_term(s["text"], GREETING_TERMS):
            greeting_seg = s
        if purpose_seg is None and find_term(s["text"], PURPOSE_TERMS):
            purpose_seg = s
    if greeting_seg and purpose_seg:
        return _result("pass", "Agent greeted and established the purpose of the call up front.", purpose_seg)
    if greeting_seg:
        return _result("partial", "Agent greeted but did not clearly state the call's purpose up front.", greeting_seg)
    if purpose_seg:
        return _result("partial", "Agent stated a purpose but without a clear greeting.", purpose_seg)
    return _result("fail", "No clear greeting or purpose statement in the agent's opening turns.", None)


# ---------------------------------------------------------------------------
# future_commitment (supporting evidence for resolution/closure, not
# independently weighted in rubric_v3_final.json -- kept as a reusable check)
# ---------------------------------------------------------------------------

COMMITMENT_TERMS = ["i'll send", "i'll email", "i'll get back to you", "i'll follow up",
                    "we'll follow up", "you'll receive", "i'll have", "i'll make sure you",
                    "send you", "send it over", "follow up with you", "i'll escalate",
                    "i'll update"]

TIME_REFERENCE_TERMS = ["today", "tomorrow", "by friday", "by monday", "by end of day",
                        "within 24 hours", "within an hour", "this week", "shortly"]


def has_future_commitment(segments, agent_speaker):
    for s in _agent_segments(segments, agent_speaker):
        commitment_term = find_term(s["text"], COMMITMENT_TERMS)
        if not commitment_term:
            continue
        time_term = find_term(s["text"], TIME_REFERENCE_TERMS)
        if time_term:
            return _result("pass", f"Agent stated a specific commitment with a timeframe "
                                    f"(matched '{commitment_term.strip()}' + '{time_term.strip()}').", s)
        return _result("partial", f"Agent stated a commitment (matched '{commitment_term.strip()}') "
                                   f"but without a specific timeframe.", s)
    return _result("fail", "Agent never stated a concrete follow-up or next step.", None)


# ---------------------------------------------------------------------------
# closure_recap (deterministic support terms; the recap judgment itself is LLM)
# ---------------------------------------------------------------------------

CLOSING_TERMS = ["thank you", "thanks", "have a good", "have a great", "have a nice",
                 "take care", "bye", "goodbye", "you're welcome", "appreciate"]

UNRESOLVED_CHECK_TERMS = ["anything else", "is there anything", "any other questions",
                          "did i answer everything", "does that resolve"]

CLOSING_WINDOW = 3  # agent's last N turns count as the close


def has_professional_close(segments, agent_speaker):
    agent = _agent_segments(segments, agent_speaker)
    closing = agent[-CLOSING_WINDOW:] if agent else []
    closing_seg = unresolved_seg = None
    for s in closing:
        if closing_seg is None and find_term(s["text"], CLOSING_TERMS):
            closing_seg = s
        if unresolved_seg is None and find_term(s["text"], UNRESOLVED_CHECK_TERMS):
            unresolved_seg = s
    if closing_seg and unresolved_seg:
        return _result("pass", "Agent closed courteously and checked for remaining concerns.", unresolved_seg)
    if closing_seg:
        return _result("partial", "Agent closed courteously but did not check for remaining concerns.", closing_seg)
    return _result("fail", "Agent did not close the call with a courteous sign-off.", None)


# ---------------------------------------------------------------------------
# active_listening_no_interrupt
# ---------------------------------------------------------------------------

OVERLAP_MIN_DURATION_SEC = 1.5  # NEEDS CALIBRATION
BACKCHANNEL_MAX_WORDS = 3       # NEEDS CALIBRATION
BACKCHANNEL_TERMS = ["mhm", "uh huh", "right", "yeah", "i see", "got it", "okay"]
INTERRUPTION_SCOPE_CUSTOMER_TURNS = 5  # only the problem-statement phase


def _is_real_interruption(agent_seg, prior_customer_seg):
    overlap = prior_customer_seg["end"] - agent_seg["start"]
    if overlap < OVERLAP_MIN_DURATION_SEC:
        return False
    word_count = len(agent_seg["text"].split())
    if word_count <= BACKCHANNEL_MAX_WORDS and find_term(agent_seg["text"], BACKCHANNEL_TERMS):
        return False
    return True


def has_no_interruptions(segments, agent_speaker):
    customer_turns_seen = 0
    overlaps = []
    prior_customer_seg = None
    for s in segments:
        if s.get("speaker") != agent_speaker:
            customer_turns_seen += 1
            prior_customer_seg = s
            if customer_turns_seen > INTERRUPTION_SCOPE_CUSTOMER_TURNS:
                break
            continue
        if prior_customer_seg and customer_turns_seen <= INTERRUPTION_SCOPE_CUSTOMER_TURNS:
            if _is_real_interruption(s, prior_customer_seg):
                overlaps.append(s)

    if not overlaps:
        return _result("pass", "No interruptions during the customer's problem-statement phase.", None)
    if len(overlaps) == 1:
        return _result("partial", "One interruption during the customer's problem-statement phase.", overlaps[0])
    return _result("fail", f"{len(overlaps)} interruptions during the customer's problem-statement phase.", overlaps[0])


# ---------------------------------------------------------------------------
# dead_air_hold_handling
# ---------------------------------------------------------------------------

GAP_THRESHOLD_SEC = 20  # NEEDS CALIBRATION
HOLD_WARNING_TERMS = ["one moment", "please hold", "bear with me", "give me a second",
                      "give me a moment", "let me check", "let me look into that",
                      "hold on", "just a sec", "one second"]


def has_clean_hold_handling(segments, agent_speaker):
    ordered = sorted(segments, key=lambda s: s["start"])
    any_gap = False
    all_warned = True
    first_unwarned = None

    for i in range(1, len(ordered)):
        prev_seg, cur_seg = ordered[i - 1], ordered[i]
        gap = cur_seg["start"] - prev_seg["end"]
        if gap <= GAP_THRESHOLD_SEC:
            continue
        any_gap = True
        # only the immediately preceding agent turn counts as a valid warning
        preceding_agent_turn = prev_seg if prev_seg.get("speaker") == agent_speaker else None
        warned = bool(preceding_agent_turn and find_term(preceding_agent_turn["text"], HOLD_WARNING_TERMS))
        if not warned:
            all_warned = False
            if first_unwarned is None:
                first_unwarned = preceding_agent_turn or prev_seg

    if not any_gap:
        return _result("pass", "No unexplained gaps in the call.", None)
    if all_warned:
        return _result("partial", "Gap(s) present but all were preceded by a hold warning.", None)
    return _result("fail", "At least one unexplained gap with no preceding hold warning.", first_unwarned)


# ---------------------------------------------------------------------------
# closure_ownership
# ---------------------------------------------------------------------------

OWNERSHIP_SPECIFIC_TERMS = ["i will personally", "i'll personally", "ticket #",
                           "ticket number", "case #", "case number",
                           "reference number", "confirmation number",
                           "escalated to", "assigned to"]

# Team names are org-specific -- load from config in production rather than
# hardcoding. Placeholder list kept here for standalone testing.
OWNERSHIP_TEAM_NAMES = ["billing", "engineering", "technical support", "tech support",
                        "retention", "escalations", "escalation", "account management"]

OWNERSHIP_TEAM_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in OWNERSHIP_TEAM_NAMES) + r")\s+team\b"
)

OWNERSHIP_VAGUE_TERMS = ["someone will", "we will look into it", "you'll hear back",
                        "someone from our team", "we'll be in touch"]


def has_stated_ownership(segments, agent_speaker):
    closing = _agent_segments(segments, agent_speaker)[-CLOSING_WINDOW:]
    vague_seg = None
    for s in closing:
        specific_term = find_term(s["text"], OWNERSHIP_SPECIFIC_TERMS)
        if specific_term:
            return _result("pass", f"Agent named a specific owner (matched '{specific_term.strip()}').", s)
        team_match = OWNERSHIP_TEAM_PATTERN.search((s["text"] or "").lower())
        if team_match:
            return _result("pass", f"Agent named a specific team ('{team_match.group(0)}').", s)
        if vague_seg is None and find_term(s["text"], OWNERSHIP_VAGUE_TERMS):
            vague_seg = s
    if vague_seg:
        return _result("partial", "Agent implied ownership vaguely without naming a specific owner.", vague_seg)
    return _result("fail", "No ownership of next steps stated in the closing turns.", None)


# ---------------------------------------------------------------------------
# compliance_disclosures (GATE)
# ---------------------------------------------------------------------------
# NEEDS SIGN-OFF: exact phrase wording must come from legal/compliance before
# this ships. disclosure_config below is a structural placeholder.
#
# Expected shape of disclosure_config, loaded from disclosure_requirements.json:
#   { "call_type_name": [["phrase option 1", "phrase option 2"], [...]] }
# Each inner list is a group of interchangeable phrasings -- at least one
# phrase in each group must be found for that requirement to be satisfied.

def has_required_disclosures(segments, agent_speaker, call_type=None, disclosure_config=None):
    disclosure_config = disclosure_config or {}
    required_groups = disclosure_config.get(call_type, [])
    if not required_groups:
        return _result("pass", f"No disclosures configured for call_type={call_type}.", None)

    agent = _agent_segments(segments, agent_speaker)
    missing = []
    for group in required_groups:
        found = any(find_term(s["text"], group) for s in agent)
        if not found:
            missing.append(group[0])

    if missing:
        return _result("fail", f"Missing required disclosure(s): {', '.join(missing)}.", None)
    return _result("pass", "All required disclosures present.", None)


# ---------------------------------------------------------------------------
# no_hostile_language (GATE, deterministic layer only -- LLM sarcasm pass
# lives in the LLM pipeline, not here)
# ---------------------------------------------------------------------------
# NEEDS TEAM INPUT: maintain PROFANITY_TERMS as a loadable, actively
# maintained config rather than a hardcoded list.

PROFANITY_TERMS = []  # load from config

HOSTILE_PHRASES = ["that's not my problem", "calm down", "i already told you",
                   "you need to listen", "that's not my fault",
                   "i can't help you if", "there's nothing i can do",
                   "read the policy"]


def has_hostile_language(segments, agent_speaker, profanity_terms=None):
    profanity_terms = profanity_terms if profanity_terms is not None else PROFANITY_TERMS
    for s in _agent_segments(segments, agent_speaker):
        term = find_term(s["text"], profanity_terms) or find_term(s["text"], HOSTILE_PHRASES)
        if term:
            return _result("fail", f"Hostile/inappropriate language detected (matched '{term.strip()}').", s)
    return _result("pass", "No hostile language detected (deterministic layer). "
                            "Sarcasm/condescension is caught separately by the LLM layer.", None)


# ---------------------------------------------------------------------------
# Registry -- keys match the "check" field in rubric_v3_final.json
# ---------------------------------------------------------------------------

REGISTRY = {
    "has_opening_purpose": has_opening_purpose,
    "has_future_commitment": has_future_commitment,
    "has_professional_close": has_professional_close,
    "has_no_interruptions": has_no_interruptions,
    "has_clean_hold_handling": has_clean_hold_handling,
    "has_stated_ownership": has_stated_ownership,
    "has_required_disclosures": has_required_disclosures,
    "has_hostile_language": has_hostile_language,
}
