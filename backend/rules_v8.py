"""
Deterministic + orchestration layer for the Call QA minimal rubric (v8).

Implements rubric_minimal_v8.json:
  - Resolution Effectiveness   (40) -- pure LLM, no deterministic piece here
  - Ownership & Next Steps     (20) -- deterministic step 1, conditional LLM step 2
  - Active Listening           (20) -- pure deterministic
  - Tone, Empathy & Prof.      (20) -- deterministic hostile check, then LLM

Each check function returns:
  { "verdict": "pass" | "partial" | "fail",
    "reasoning": str,
    "evidence_seq": int | None,
    "evidence_text": str | None,
    "coaching_note": str | None,
    "needs_llm": bool,                 # True if caller must run an LLM step
    "llm_context": dict | None }       # what the LLM step needs, if needs_llm

The actual LLM calls are NOT made here -- this module prepares what the LLM
step needs and combines the result once the caller has it. Keeps this file
testable without network access, same separation as the original rules.py.
"""

import re

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

NEGATION_MARKERS = re.compile(r"\b(cant|cannot|wont|couldnt|didnt|dont|isnt|arent|wasnt)\b")
NEGATION_LOOKAHEAD_CHARS = 20


def _fold_speech(text):
    t = (text or "").lower().replace("\u2019", "'").replace("\u2018", "'").replace("`", "'")
    return re.sub(r"\s+", " ", t).strip()


_SHORT_WORD = re.compile(r"^[a-z']+$")


def _term_start(folded_text, term):
    """Index of term in folded text, or -1.

    Single words (sorry, miss, please) use word boundaries so 'miss' does not
    match 'dismiss' / 'missing'. Multi-word phrases stay substring matches.
    """
    if not term:
        return -1
    if " " not in term and _SHORT_WORD.fullmatch(term):
        m = re.search(r"\b" + re.escape(term) + r"\b", folded_text)
        return m.start() if m else -1
    return folded_text.find(term)


def find_term(text, terms):
    """Negation-aware match. Use for commitment/ownership-style phrases where
    negation genuinely changes the meaning ("I won't be able to personally
    handle this" should not match "i will personally")."""
    t = _fold_speech(text)
    for term in sorted((terms or []), key=len, reverse=True):
        idx = _term_start(t, term)
        if idx == -1:
            continue
        window = t[idx: idx + len(term) + NEGATION_LOOKAHEAD_CHARS]
        if NEGATION_MARKERS.search(window):
            continue
        return term
    return None


def find_term_plain(text, terms):
    """Word-boundary match, NO negation exemption. Use for hostile/profane
    language, where negation doesn't neutralize the behavior the way it
    neutralizes a commitment phrase.

    Word boundaries keep short terms like 'ass' from matching 'class' / 'pass'.
    Longer phrases ('piece of shit') still match as a whole.
    """
    t = _fold_speech(text)
    for term in sorted((terms or []), key=len, reverse=True):
        if not term:
            continue
        if re.search(r"\b" + re.escape(term) + r"\b", t):
            return term
    return None


def _normalize(s):
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


# ---------------------------------------------------------------------------
# LLM output validation -- "bad JSON gets caught, never shipped"
# ---------------------------------------------------------------------------

import json

REQUIRED_LLM_FIELDS = {
    "resolution_effectiveness": {"verdict", "reasoning", "evidence_seq", "evidence_text", "coaching_note"},
    "tone_step2": {"verdict", "reasoning", "evidence_seq", "evidence_text", "coaching_note"},
    "ownership_step2": {"classification", "reasoning", "coaching_note"},
}
ALLOWED_VERDICTS = {"pass", "partial", "fail"}
ALLOWED_CLASSIFICATIONS = {"transparent_honest", "dismissive"}


class LLMOutputError(Exception):
    """Raised when an LLM response fails schema validation. Caller must
    fail closed (retry, then flag for review) -- never catch this and
    silently ship the unvalidated output."""


def validate_llm_output(raw_output, dimension_key):
    """Parses + validates a single LLM response against its expected schema.
    raw_output: str (raw model text, expected JSON) or an already-parsed dict.
    Returns the validated dict, or raises LLMOutputError.
    This function does not call the model or retry -- see
    run_llm_step_with_validation for the bounded-retry wrapper."""
    if isinstance(raw_output, str):
        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError as e:
            raise LLMOutputError(f"Malformed JSON from LLM: {e}")
    else:
        parsed = raw_output

    required = REQUIRED_LLM_FIELDS.get(dimension_key)
    if required is None:
        raise LLMOutputError(f"Unknown dimension_key '{dimension_key}' -- no schema registered.")
    missing = required - parsed.keys()
    if missing:
        raise LLMOutputError(f"Missing required fields: {sorted(missing)}")

    if "verdict" in parsed and parsed["verdict"] not in ALLOWED_VERDICTS:
        raise LLMOutputError(f"Invalid verdict '{parsed['verdict']}' -- must be one of {sorted(ALLOWED_VERDICTS)}")
    if "classification" in parsed and parsed["classification"] not in ALLOWED_CLASSIFICATIONS:
        raise LLMOutputError(f"Invalid classification '{parsed['classification']}' -- must be one of {sorted(ALLOWED_CLASSIFICATIONS)}")
    if "evidence_seq" in parsed and parsed["evidence_seq"] is not None and not isinstance(parsed["evidence_seq"], int):
        raise LLMOutputError("evidence_seq must be an int or null")

    return parsed


def run_llm_step_with_validation(call_llm_fn, dimension_key, max_retries=1):
    """Bounded, aimed retry -- the harness principle applied to every LLM
    call in this pipeline, not just the ones with obvious stakes.

    call_llm_fn: zero-arg callable that invokes the LLM and returns raw
    text or a dict. Must accept a `retry_reason` kwarg so a retry attempt
    can include the previous failure in its prompt (that's the "aimed"
    part -- not a blind identical retry).

    Raises LLMOutputError if still invalid after max_retries -- caller
    must fail closed (flag the call for reprocessing / human review), never
    silently fall through with unvalidated data."""
    last_error = None
    for _ in range(max_retries + 1):
        raw = call_llm_fn(retry_reason=last_error)
        try:
            return validate_llm_output(raw, dimension_key)
        except LLMOutputError as e:
            last_error = str(e)
    raise LLMOutputError(f"Failed validation after {max_retries + 1} attempts: {last_error}")


# ---------------------------------------------------------------------------
# Coaching note evidence verification -- "no proof, no claim" applied to
# the coaching notes themselves, not just the verdicts
# ---------------------------------------------------------------------------

QUOTE_PATTERN = re.compile(r"['\u2018\u2019\"]([^'\u2018\u2019\"]{4,})['\u2018\u2019\"]")


def verify_coaching_note_evidence(coaching_note, evidence_text, full_transcript_text=None):
    """Extracts quoted phrases from a coaching_note and checks each one
    actually appears in the cited evidence (or the full transcript, as a
    fallback in case the note legitimately references a different line).
    Returns (is_valid, unverified_quotes)."""
    if not coaching_note:
        return True, []
    quotes = QUOTE_PATTERN.findall(coaching_note)
    if not quotes:
        return True, []  # nothing quoted to verify -- e.g. a purely behavioral note
    evidence_norm = _normalize(evidence_text)
    transcript_norm = _normalize(full_transcript_text) if full_transcript_text else ""
    unverified = [q for q in quotes
                  if _normalize(q) not in evidence_norm and not (transcript_norm and _normalize(q) in transcript_norm)]
    return (len(unverified) == 0), unverified


def safe_coaching_note(coaching_note, evidence_text, full_transcript_text=None, dimension_id=None):
    """Wraps a coaching_note with the evidence check. If verification fails,
    the note is WITHHELD -- never shipped with a hallucinated quote -- and a
    flag is returned for a separate LLM-QA queue (distinct from the agent
    manager-review queue -- this is a data-quality issue, not a performance
    issue, and the two should not be conflated in the same inbox).
    Returns (safe_note, flag_or_none)."""
    is_valid, unverified = verify_coaching_note_evidence(coaching_note, evidence_text, full_transcript_text)
    if is_valid:
        return coaching_note, None
    fallback = "Coaching note withheld pending review -- a quoted claim could not be verified against the transcript."
    flag = {
        "dimension_id": dimension_id,
        "reason": "unverified_quote_in_coaching_note",
        "original_note": coaching_note,
        "unverified_quotes": unverified,
    }
    return fallback, flag


# ---------------------------------------------------------------------------
# #1 -- Delivery routing: reinforcement goes to the agent directly,
# correction goes to the manager first
# ---------------------------------------------------------------------------

def coaching_delivery_channel(verdict):
    """pass -> straight to the agent's own dashboard (reinforcement).
    partial/fail -> manager queue first -- the manager decides how and when
    to raise it, rather than the agent finding a correction with no context.
    Mirrors the project's existing principle that analysis should open a
    constructive conversation, not put someone on the defensive."""
    if verdict == "pass":
        return "agent_dashboard"
    if verdict in ("partial", "fail"):
        return "manager_queue"
    return "manager_queue"  # unknown verdict -- fail closed toward the safer routing


# ---------------------------------------------------------------------------
# #5 -- Confidence field: route low-confidence LLM verdicts to a human
# spot-check queue instead of trusting every verdict equally
# ---------------------------------------------------------------------------

ALLOWED_CONFIDENCE = {"high", "medium", "low"}
SPOT_CHECK_CONFIDENCE_LEVELS = {"low"}  # NEEDS CALIBRATION -- could expand to include "medium"


def needs_spot_check(llm_result):
    """llm_result must include a 'confidence' field (add this to the LLM
    prompt schema alongside verdict/reasoning/coaching_note). Returns True
    if this verdict should be queued for human review rather than trusted
    outright. Cheap to add, meaningfully improves trust -- doesn't require
    changing anything about how verdicts are scored, only how much weight
    a downstream human process gives to reviewing them."""
    confidence = llm_result.get("confidence")
    if confidence not in ALLOWED_CONFIDENCE:
        return True  # missing/invalid confidence -- fail toward reviewing it
    return confidence in SPOT_CHECK_CONFIDENCE_LEVELS


# ---------------------------------------------------------------------------
# #2 -- Weekly rollup: digest instead of a per-call firehose
# ---------------------------------------------------------------------------

def weekly_coaching_digest(week_results, max_notes_per_dimension=1):
    """week_results: list of per-call dimension result dicts for ONE agent
    over one week, each shaped like {"dimension_id": str, "verdict": str,
    "coaching_note": str, "call_id": str, "call_date": str}.

    Groups by dimension, and for each dimension with 2+ non-pass instances,
    folds them into a single pattern statement instead of repeating near-
    identical notes. Dimensions with only 0-1 flagged instances keep their
    original note as-is (nothing to summarize).

    Needs real call history across a week to be meaningful -- this is
    exactly why it wasn't demoed live with sample data, but the function
    itself is real and ready for the team to wire up once call history is
    flowing."""
    from collections import defaultdict
    by_dimension = defaultdict(list)
    for r in week_results:
        if r["verdict"] in ("partial", "fail"):
            by_dimension[r["dimension_id"]].append(r)

    digest = []
    for dim_id, instances in by_dimension.items():
        if len(instances) <= 1:
            for r in instances:
                digest.append({"dimension_id": dim_id, "type": "single_instance",
                               "note": r["coaching_note"], "call_id": r["call_id"]})
            continue
        dates = [r["call_date"] for r in instances]
        pattern_note = (f"Flagged on {dim_id.replace('_', ' ')} {len(instances)} times this week "
                        f"({', '.join(dates)}) -- worth a specific conversation rather than "
                        f"treating these as isolated incidents.")
        digest.append({
            "dimension_id": dim_id, "type": "pattern",
            "note": pattern_note, "instance_count": len(instances),
            "call_ids": [r["call_id"] for r in instances],
            "representative_notes": [r["coaching_note"] for r in instances[:max_notes_per_dimension]],
        })
    return digest


# ---------------------------------------------------------------------------
# #4 -- Repeat-pattern awareness fed INTO note generation, not just detected
# after the fact on the dashboard
# ---------------------------------------------------------------------------

REPEAT_PATTERN_THRESHOLD = 3  # NEEDS CALIBRATION


def detect_repeat_pattern(recent_history, dimension_id, current_verdict):
    """recent_history: list of {"dimension_id": str, "verdict": str,
    "call_date": str} for this agent's recent calls (e.g. trailing 7 days),
    NOT including the current call.

    Returns a short context string to inject into the LLM prompt for the
    CURRENT call's coaching_note generation, so the note itself can say
    "this is the 3rd call this week..." instead of the dashboard being the
    only place that knows about the pattern. Returns None if no pattern
    (nothing to inject -- keeps the prompt clean on the common case)."""
    if current_verdict not in ("partial", "fail"):
        return None
    same_dim_recent_flags = [h for h in recent_history
                             if h["dimension_id"] == dimension_id and h["verdict"] in ("partial", "fail")]
    count = len(same_dim_recent_flags) + 1  # +1 for the current call
    if count < REPEAT_PATTERN_THRESHOLD:
        return None
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(count if count < 20 else count % 10, "th")
    return (f"NOTE FOR PROMPT CONTEXT: this is the {count}{suffix} call in the recent window flagged "
           f"on {dimension_id.replace('_', ' ')} for this agent. Reference the pattern directly "
           f"in the coaching_note rather than writing it as an isolated incident.")


def _agent_segments(segments, agent_speaker):
    return [s for s in segments if s.get("speaker") == agent_speaker]


def _result(verdict, reasoning, seg, coaching_note=None, needs_llm=False, llm_context=None):
    return {
        "verdict": verdict,
        "reasoning": reasoning,
        "evidence_seq": seg["seq"] if seg else None,
        "evidence_text": seg["text"] if seg else None,
        "coaching_note": coaching_note,
        "needs_llm": needs_llm,
        "llm_context": llm_context,
    }


# ---------------------------------------------------------------------------
# Ownership & Next Steps -- weight 20
# ---------------------------------------------------------------------------

OWNERSHIP_SPECIFIC_TERMS = [
    # Personal commitment (first person)
    "i'll take charge", "i will take charge", "i'm taking charge", "i am taking charge",
    "i'm taking over", "i am taking over", "i'll take over", "i will take over",
    "i'll take care of", "i will take care of", "i'll take this", "i will take this",
    "let me take this", "leave this with me", "i've got this", "i've got this one",
    "i'll own this", "i will own this", "i'm owning this",
    "i'll handle", "i will handle", "i'll see this through", "i will see this through",
    "i'll stay on this", "i will stay on this",
    "i will personally", "i'll personally",
    "i'll make sure", "i will make sure", "we'll make sure", "we will make sure",
    "i'll personally ensure", "make sure to update", "make sure to have",
    "i'm on the case", "i am on the case", "on the case", "on the ticket",
    "rest assured", "you can count on me",
    # Concrete follow-up the agent owns
    "i'll keep you updated", "i will keep you updated", "we'll keep you updated",
    "i'll update you", "i will update you", "we'll update you", "update you as soon",
    "i'll let you know", "i will let you know", "we'll let you know", "i'll let you",
    "i'll email you", "i will email you", "i'm emailing you", "i am emailing you",
    "i'll email", "i will email", "we'll email",
    "i'll send you", "i will send you", "we'll send you", "we'll send it",
    "i'll text you", "i will text you",
    "i'll call you back", "i will call you back", "can i call you back",
    "i'll call you", "i will call you", "i'll call back", "call you back",
    "i'll follow up", "i will follow up", "we'll follow up", "we will follow up",
    "i'll get back to you", "i will get back to you", "we'll get back to you",
    "we will get back", "i'll get back", "we'll get back",
    "i'll reach out", "i will reach out", "i'm reaching out", "i am reaching out",
    "reaching out to",
    "i'll check with", "i will check with", "i'll ask our", "i will ask our",
    "i'll ask the", "i will give this", "i will give",
    "expect an update", "you can expect",
    "i'll talk to you", "i will talk to you",
    # Named artifact / assignment
    "ticket #", "ticket number", "case #", "case number",
    "reference number", "confirmation number",
    "escalated to", "we've escalated", "we have escalated", "escalated your",
    "assigned to", "i'm assigning", "i am assigning",
    "i've created a ticket", "i've opened a ticket", "i opened a ticket",
    "i created a ticket",
]

OWNERSHIP_TEAM_NAMES = [
    "billing", "engineering", "engineers", "engineer",
    "technical support", "tech support", "support",
    "retention", "escalations", "escalation",
    "account management", "accounts", "account",
    "operations", "dispatch", "customer service", "customer success",
]
OWNERSHIP_TEAM_PATTERN = re.compile(
    r"\b(?:"
    + "|".join(re.escape(t) for t in OWNERSHIP_TEAM_NAMES)
    + r")\s+team\b|\bour (?:engineers?|engineering|billing|support|accounts?)\b"
)

OWNERSHIP_COMMIT_RE = re.compile(
    r"\b(?:"
    r"(?:i(?:'ll| will)|we(?:'ll| will)|let me)\s+"
    r"(?:just\s+|go ahead and\s+)?"
    r"(?:get back|let you know|call you back|call back|follow up|reach out|"
    r"make sure|take care of|handle (?:this|it)|ask (?:our|the)|"
    r"check with|send (?:it|you)|email|update you|chase|"
    r"escalate|sort (?:this|it)|resolve)"
    r"|i(?:'m| am) on the (?:case|ticket)"
    r"|on the case"
    r"|rest assured"
    r"|you can expect(?: an update)?"
    r"|expect an update"
    r"|i(?:'m| am) reaching out|reaching out to"
    r"|we(?:'ve| have) escalated|escalated (?:your|this|it)"
    r")\b"
)

OWNERSHIP_VAGUE_TERMS = [
    "someone will", "we will look into it", "you'll hear back",
    "someone from our team", "we'll be in touch", "they will get back",
    "we'll look into", "we'll see what we can do", "someone from",
]

CLOSING_WINDOW = 8  # vague-ownership scan only; specific commits search the whole call
PHRASE_JOIN_WINDOW = 4  # consecutive agent turns — Hear often splits one sentence


def check_ownership_step1(segments, agent_speaker):
    """Deterministic pass. Specific commitments anywhere in the agent's turns
    (including ASR-split adjacent turns). Vague phrases still use the close."""
    windows = _joined_agent_windows(segments, agent_speaker)
    for s, chunk in windows:
        specific_term = find_term(chunk, OWNERSHIP_SPECIFIC_TERMS)
        if specific_term:
            return _result(
                "pass",
                f"Pass because the agent made a concrete personal commitment "
                f"(matched '{specific_term.strip()}'). That names who owns the next step.",
                s,
            )
        team_match = OWNERSHIP_TEAM_PATTERN.search(_fold_speech(chunk))
        if team_match:
            return _result(
                "pass",
                f"Pass because the agent named a specific team that owns the follow-up "
                f"('{team_match.group(0)}').",
                s,
            )
        commit = OWNERSHIP_COMMIT_RE.search(_fold_speech(chunk))
        if commit:
            return _result(
                "pass",
                f"Pass because the agent committed to a next step "
                f"('{commit.group(0).strip()}').",
                s,
            )

    closing = _agent_segments(segments, agent_speaker)[-CLOSING_WINDOW:]
    vague_seg = vague_term = None
    for s, chunk in _joined_agent_windows(closing, agent_speaker):
        term = find_term(chunk, OWNERSHIP_VAGUE_TERMS)
        if term:
            vague_seg, vague_term = s, term
            break
    if vague_seg:
        return _result(
            None,
            f"Vague ownership phrase '{vague_term.strip()}' in the closing turns — "
            f"needs a judgment of honest vs dismissive.",
            vague_seg, needs_llm=True,
            llm_context={"matched_term": vague_term, "segment_seq": vague_seg["seq"]},
        )
    return _result(
        "fail",
        "Fail because the agent never made a personal commitment "
        "(for example I'll get back to you, I'll let you know, I'll email you, "
        "I'm on the case), no ticket/case number, and no named team.",
        None,
        coaching_note="No ownership of next steps was stated before the call ended -- "
                       "always name who owns the follow-up.",
    )


def _joined_agent_windows(segments, agent_speaker, window=None):
    """Pair each agent turn with the next few agent turns joined (Hear splits sentences)."""
    n = window if window is not None else PHRASE_JOIN_WINDOW
    agent = _agent_segments(segments, agent_speaker)
    out = []
    for i, s in enumerate(agent):
        chunk = " ".join((agent[j].get("text") or "") for j in range(i, min(i + n, len(agent))))
        out.append((s, chunk))
    return out


def combine_ownership_result(step1_result, llm_classification=None, full_transcript_text=None):
    """Call after check_ownership_step1 if needs_llm was True.
    llm_classification: 'transparent_honest' | 'dismissive'"""
    if not step1_result["needs_llm"]:
        return step1_result
    term = step1_result["llm_context"]["matched_term"]
    evidence_text = step1_result["evidence_text"]
    evidence_seg = {"seq": step1_result["evidence_seq"], "text": evidence_text}
    if llm_classification == "transparent_honest":
        note = ("Being upfront that this depends on another team, while still "
                "personally committing to follow up, is exactly the right way "
                "to handle something outside your control -- keep doing this.")
        safe_note, flag = safe_coaching_note(note, evidence_text, full_transcript_text, "ownership_next_steps")
        result = _result(
            "pass",
            f"Pass because the vague line ('{term.strip()}') was judged transparently honest: "
            f"the agent named a real constraint and still took personal accountability.",
            evidence_seg, coaching_note=safe_note,
        )
    else:
        note = (f"Vague ownership phrase used ('{term.strip()}') without real "
                f"accountability -- next time, even without a timeline, commit "
                f"personally: 'I don't have an exact timeline, but I'll personally "
                f"make sure you hear back.'")
        safe_note, flag = safe_coaching_note(note, evidence_text, full_transcript_text, "ownership_next_steps")
        result = _result(
            "partial",
            f"Partial because the closing used vague language ('{term.strip()}') without "
            f"personal accountability — half credit (10 of 20).",
            evidence_seg, coaching_note=safe_note,
        )
    result["llm_qa_flag"] = flag  # None unless the note failed evidence verification
    return result


# ---------------------------------------------------------------------------
# Active Listening -- weight 20, pure deterministic
# ---------------------------------------------------------------------------

OVERLAP_MIN_DURATION_SEC = 1.5     # NEEDS CALIBRATION
BACKCHANNEL_MAX_WORDS = 3          # NEEDS CALIBRATION
BACKCHANNEL_TERMS = ["mhm", "uh huh", "right", "yeah", "i see", "got it", "okay"]
INTERRUPTION_SCOPE_CUSTOMER_TURNS = 5


def _is_real_interruption(agent_seg, prior_customer_seg):
    overlap = prior_customer_seg["end"] - agent_seg["start"]
    if overlap < OVERLAP_MIN_DURATION_SEC:
        return False
    customer_duration = prior_customer_seg["end"] - prior_customer_seg["start"]
    if customer_duration > 0 and overlap > customer_duration:
        return False  # almost certainly a diarization error, not a real interruption
    word_count = len(agent_seg["text"].split())
    if word_count <= BACKCHANNEL_MAX_WORDS and find_term(agent_seg["text"], BACKCHANNEL_TERMS):
        return False
    return True


GAP_THRESHOLD_SEC = 20
HOLD_WARNING_TERMS = [
    "one moment", "please hold", "bear with me", "give me a second",
    "give me a moment", "just a moment", "just a second", "just a sec",
    "one second", "hold on", "please wait", "stay on the line",
    "don't hang up", "do not hang up", "i'll be right back", "i will be right back",
    "let me check", "let me look into", "let me look that up", "let me pull that up",
    "let me pull up", "let me see", "transferring you",
]

_VERDICT_RANK = {"pass": 0, "partial": 1, "fail": 2}


def _worse_verdict(*verdicts):
    return max(verdicts, key=lambda v: _VERDICT_RANK.get(v, 0))


def _collect_interruptions(segments, agent_speaker):
    ordered = sorted(segments, key=lambda s: s.get("start") or 0)
    customer_turns_seen = 0
    overlaps = []
    prior_customer_seg = None
    for s in ordered:
        if s.get("speaker") != agent_speaker:
            customer_turns_seen += 1
            prior_customer_seg = s
            if customer_turns_seen > INTERRUPTION_SCOPE_CUSTOMER_TURNS:
                break
            continue
        if prior_customer_seg and customer_turns_seen <= INTERRUPTION_SCOPE_CUSTOMER_TURNS:
            if _is_real_interruption(s, prior_customer_seg):
                overlaps.append(s)
    return overlaps


def _interruption_check(overlaps):
    if not overlaps:
        return {
            "id": "interruptions",
            "name": "Interruptions",
            "verdict": "pass",
            "matched_term": None,
            "evidence_seq": None,
            "evidence_text": None,
            "jump_at": None,
            "reasoning": (
                "No overlap of 1.5s or more during the first 5 customer turns "
                "(short backchannels like okay/mhm are ignored)."
            ),
        }
    seg = overlaps[0]
    jump_at = seg.get("start")
    if len(overlaps) == 1:
        return {
            "id": "interruptions",
            "name": "Interruptions",
            "verdict": "partial",
            "matched_term": None,
            "evidence_seq": seg.get("seq"),
            "evidence_text": seg.get("text"),
            "jump_at": jump_at,
            "reasoning": (
                f"Agent interrupted once at {seg['start']:.0f}s while the customer "
                f"was still speaking."
            ),
        }
    return {
        "id": "interruptions",
        "name": "Interruptions",
        "verdict": "fail",
        "matched_term": None,
        "evidence_seq": seg.get("seq"),
        "evidence_text": seg.get("text"),
        "jump_at": jump_at,
        "reasoning": (
            f"Agent interrupted {len(overlaps)} times during the customer's opening "
            f"turns (first at {seg['start']:.0f}s)."
        ),
    }


def _dead_air_check(segments, agent_speaker, resolution_passed=False):
    ordered = sorted(segments, key=lambda s: (s.get("start") or 0, s.get("seq") or 0))
    gaps = []
    for i in range(1, len(ordered)):
        prev_seg, cur_seg = ordered[i - 1], ordered[i]
        try:
            gap = float(cur_seg["start"]) - float(prev_seg["end"])
        except (TypeError, ValueError, KeyError):
            continue
        if gap <= GAP_THRESHOLD_SEC:
            continue
        preceding_agent = prev_seg if prev_seg.get("speaker") == agent_speaker else None
        term = find_term(preceding_agent["text"], HOLD_WARNING_TERMS) if preceding_agent else None
        gaps.append({
            "gap_sec": gap,
            "prev": prev_seg,
            "cur": cur_seg,
            "warned": bool(term),
            "term": term,
        })

    if not gaps:
        return {
            "id": "dead_air",
            "name": "Dead air",
            "verdict": "pass",
            "matched_term": None,
            "evidence_seq": None,
            "evidence_text": None,
            "jump_at": None,
            "reasoning": f"No silence of {GAP_THRESHOLD_SEC:.0f}s or more between turns.",
        }

    unwarned = [g for g in gaps if not g["warned"]]
    worst = unwarned[0] if unwarned else gaps[0]
    prev_seg = worst["prev"]
    jump_at = prev_seg.get("end")
    gap_n = worst["gap_sec"]
    if unwarned:
        extra = f" {len(gaps)} gaps over {GAP_THRESHOLD_SEC:.0f}s in total." if len(gaps) > 1 else ""
        return {
            "id": "dead_air",
            "name": "Dead air",
            "verdict": "fail",
            "matched_term": None,
            "evidence_seq": prev_seg.get("seq"),
            "evidence_text": prev_seg.get("text"),
            "jump_at": jump_at,
            "reasoning": (
                f"Unexplained {gap_n:.0f}s gap starting at {jump_at:.0f}s with no hold "
                f"warning in the turn before the silence.{extra}"
            ),
        }
    term = worst["term"]
    extra = f" {len(gaps)} warned gaps in total." if len(gaps) > 1 else ""
    if resolution_passed:
        return {
            "id": "dead_air",
            "name": "Dead air",
            "verdict": "pass",
            "matched_term": term,
            "evidence_seq": prev_seg.get("seq"),
            "evidence_text": prev_seg.get("text"),
            "jump_at": jump_at,
            "reasoning": (
                f"{gap_n:.0f}s gap at {jump_at:.0f}s was preceded by a hold warning "
                f"('{term}') and Resolution passed, so the silence counts as time spent "
                f"fixing the issue.{extra}"
            ),
        }
    return {
        "id": "dead_air",
        "name": "Dead air",
        "verdict": "partial",
        "matched_term": term,
        "evidence_seq": prev_seg.get("seq"),
        "evidence_text": prev_seg.get("text"),
        "jump_at": jump_at,
        "reasoning": (
            f"{gap_n:.0f}s gap at {jump_at:.0f}s was preceded by a hold warning "
            f"('{term}').{extra}"
        ),
    }


def score_listening_categories(segments, agent_speaker, resolution_passed=False):
    """Interruptions + dead air. Overall verdict is the worse of the two."""
    checks = [
        _interruption_check(_collect_interruptions(segments, agent_speaker)),
        _dead_air_check(segments, agent_speaker, resolution_passed=resolution_passed),
    ]
    interrupt, dead = checks
    verdict = _worse_verdict(interrupt["verdict"], dead["verdict"])

    notes = []
    if interrupt["verdict"] == "partial":
        notes.append(
            f"One interruption detected at {interrupt['jump_at']:.0f}s while the customer "
            f"was describing their issue -- let them finish before responding."
        )
    elif interrupt["verdict"] == "fail":
        notes.append(
            f"{interrupt['reasoning']} Give the customer the full problem statement "
            f"before jumping in."
        )
    if dead["verdict"] == "fail":
        notes.append(
            f"{dead['reasoning']} Tell the customer you need a moment before you go quiet."
        )

    if verdict == "pass":
        reasoning = (
            "Pass because the agent did not talk over the customer in the opening turns "
            f"and dead air passed: {dead['reasoning']}"
        )
        evidence_seq = dead.get("evidence_seq") if dead.get("matched_term") else None
        evidence_text = dead.get("evidence_text") if dead.get("matched_term") else None
    elif verdict == "partial":
        weak = interrupt if interrupt["verdict"] != "pass" else dead
        reasoning = (
            f"Partial because of {weak['name'].lower()}: {weak['reasoning']} "
            "Half credit (10 of 20)."
        )
        evidence_seq = weak.get("evidence_seq")
        evidence_text = weak.get("evidence_text")
    else:
        weak = interrupt if interrupt["verdict"] == "fail" else dead
        reasoning = f"Fail because of {weak['name'].lower()}: {weak['reasoning']} 0 of 20."
        evidence_seq = weak.get("evidence_seq")
        evidence_text = weak.get("evidence_text")

    return {
        "verdict": verdict,
        "reasoning": reasoning,
        "evidence_seq": evidence_seq,
        "evidence_text": evidence_text,
        "coaching_note": " ".join(notes) or None,
        "checks": checks,
    }


def has_active_listening(segments, agent_speaker, resolution_passed=False):
    scored = score_listening_categories(
        segments, agent_speaker, resolution_passed=resolution_passed,
    )
    seg = (
        {"seq": scored["evidence_seq"], "text": scored["evidence_text"]}
        if scored.get("evidence_seq") is not None else None
    )
    out = _result(scored["verdict"], scored["reasoning"], seg, coaching_note=scored.get("coaching_note"))
    out["checks"] = scored["checks"]
    return out


# ---------------------------------------------------------------------------
# Tone, Empathy & Professionalism -- weight 20, deterministic override + LLM
# ---------------------------------------------------------------------------

HOSTILE_PHRASES = [
    "that's not my problem", "not my problem", "calm down",
    "i already told you", "like i said", "you need to listen",
    "that's not my fault", "not my fault",
    "i can't help you if", "there's nothing i can do", "nothing i can do",
    "read the policy", "that's the policy",
    "you are being difficult", "you're being difficult",
    "you're not listening", "i don't have time for this",
    "that's not how this works", "you should have",
]

PROFANITY_TERMS = [
    "fuck", "fucking", "fuck you", "fuck off", "motherfucker",
    "shit", "shitty", "bullshit", "piece of shit",
    "screw you", "screw this", "piss off",
    "asshole", "dumbass", "jackass", "bitchass", "ass", "arse",
    "bitch", "cunt", "cock", "cocksucker", "dick", "tits", "boobs", "pussy",
    "damn it", "goddamn", "hell with you", "to hell with you",
    "shut up", "bastard", "idiot", "stupid", "retard", "faggot",
]

EMPATHY_PHRASES = [
    "i'm sorry to hear", "i am sorry to hear", "sorry to hear",
    "really sorry about", "so sorry about", "sorry about that", "sorry about",
    "i'm sorry for", "i am sorry for", "i'm sorry", "i am sorry",
    "so sorry", "really sorry", "terribly sorry",
    "i apologize", "i apologise", "apologies for", "apologize for",
    "apologising for", "apologizing for", "apologies",
    "i understand", "i do understand", "i completely understand",
    "i totally understand", "i fully understand", "totally understand",
    "i hear you", "i get it", "i know how that",
    "that must be frustrating", "that must be difficult", "that sounds frustrating",
    "that sounds difficult", "must be frustrating", "must be annoying",
    "i can imagine", "i'd be frustrated too", "i would be frustrated",
    "i know this is frustrating", "i know this is",
    "i appreciate your patience", "i appreciate you waiting", "i appreciate it",
    "i appreciate your", "appreciate your patience", "appreciate it",
    "i'm sorry for the wait", "sorry for the wait", "sorry for the delay",
    "sorry for the inconvenience", "i apologize for the inconvenience",
    "sorry you've had to deal with",
    "that doesn't sound right", "i can see why you'd be upset",
    "i can see why", "that doesn't sound",
    "no worries", "no problem", "not a problem", "not a problem at all",
    "that's frustrating", "i know it's frustrating",
    "appreciate",
    "i would do the same if i were in your position",
    "i totally get it", "totally get it",
    "sincere apologies", "grateful", "understand", "sorry",
]

EMPATHY_RE = re.compile(
    r"\b(?:"
    r"i(?:'m| am) sorry|so sorry|really sorry|terribly sorry|"
    r"sorry (?:to hear|about|for|that)|"
    r"apologi[sz](?:e|es|ing)|"
    r"no worries|no problem|not a problem|"
    r"i (?:completely |totally |fully |do )?understand|"
    r"i hear you|i get it|"
    r"that must be|i can imagine|"
    r"i appreciate(?: it| your)?"
    r")\b"
)

PROFESSIONAL_PHRASES = [
    "thank you for calling", "thanks for calling", "so much for calling",
    "thank you for contacting", "thanks for contacting", "for calling",
    "thank you for waiting", "thanks for waiting", "thank you for your patience",
    "thank you so much", "thanks so much", "thank you very much",
    "thanks very much", "thank you for the confirmation",
    "how can i help you", "how can i help", "how may i help you", "how may i help",
    "how can i assist", "how may i assist", "can i help you", "can i help",
    "for your security", "let me pull up", "let me look into",
    "i've pulled up", "is there anything else", "anything else i can help",
    "anything else", "i appreciate your time",
    "thank you for confirming", "thanks for confirming", "just to confirm",
    "happy to clarify", "let me confirm",
    "have a great day", "have a good day", "have a nice day",
    "you're welcome", "you are welcome", "most welcome",
    "please stay on the line", "thank you", "thanks for your",
    "to be perfectly candid", "allow me to explain", "let me know if",
    "please understand that", "reiterating", "please",
    "sir", "madam", "miss", "sire", "allow me some time",
]

PROFESSIONAL_RE = re.compile(
    r"\b(?:"
    r"thank you(?: so much| very much| for (?:calling|waiting|your patience|confirming|the confirmation))?"
    r"|thanks(?: so much| very much| for calling)?"
    r"|how (?:can|may) i (?:help|assist)"
    r"|can i help(?: you)?"
    r"|have a (?:great|good|nice) day"
    r"|you(?:'re| are) welcome|most welcome"
    r"|is there anything else|just to confirm"
    r"|for your security|so much for calling"
    r")\b"
)

WILLINGNESS_PHRASES = [
    "i'm happy to help", "i'd be happy to help", "happy to help",
    "i can help you with", "let me help", "i'm here to help", "i want to help",
    "i'll do my best", "i will do my best", "let me see what i can do",
    "i'll see what i can do", "i can take care of that", "we can take care of that",
    "i'd be glad to", "of course i can", "absolutely, i can", "absolutely i can",
    "i'll look into this", "i will look into this", "i'll look into", "i will look into",
    "let me look into this", "let me check this", "let me check on this",
    "i'll get the details for you", "i will get the details for you",
    "i'll get those details", "i will get those details",
    "checking this", "i'm checking this", "i am checking this",
    "leave it with me",
    "checking", "i'll check", "i will check", "let me check",
    "allow me some time to check", "let me know if this helped",
    "please let me know", "helps", "works",
]

CUSTOMER_HELPED_PHRASES = [
    "that helped", "this helped", "that really helped",
    "thank you for this", "thanks for this",
    "that's helpful", "that is helpful", "that was helpful", "this is helpful",
    "very helpful", "so helpful",
    "looks great", "looking great", "that's great", "that is great",
    "amazing",
    "thank you for your help", "thanks for your help",
    "thanks for helping", "thank you for helping",
    "appreciate your help", "appreciate the help",
    "you solved", "that solved", "that works",
    "thanks so much", "thank you so much",
]

GREETING_PHRASES = [
    "thank you for calling", "thanks for calling",
    "thank you for contacting", "thanks for contacting",
    "thank you for reaching out", "thanks for reaching out",
    "good morning", "good afternoon", "good evening", "good day",
    "hi there", "hey there", "hello",
    "hi,", "hi.", "hi!", "hi ",
    "hey,", "hey ",
    "my name is", "this is",
    "how are you today", "how are you doing",
    "welcome to",
]
GREETING_WINDOW_AGENT_TURNS = 3


def _first_phrase_hit(segments, agent_speaker, terms, plain=False, join_window=None):
    finder = find_term_plain if plain else find_term
    n = join_window if join_window is not None else PHRASE_JOIN_WINDOW
    for s, chunk in _joined_agent_windows(segments, agent_speaker, window=n):
        term = finder(chunk, terms)
        if term:
            return s, term
    return None, None


def _first_regex_hit(segments, agent_speaker, pattern, join_window=None):
    n = join_window if join_window is not None else PHRASE_JOIN_WINDOW
    for s, chunk in _joined_agent_windows(segments, agent_speaker, window=n):
        m = pattern.search(_fold_speech(chunk))
        if m:
            return s, m.group(0)
    return None, None


def _first_customer_phrase_hit(segments, agent_speaker, terms, plain=False):
    finder = find_term_plain if plain else find_term
    for s in segments:
        if s.get("speaker") == agent_speaker:
            continue
        term = finder(s["text"], terms)
        if term:
            return s, term
    return None, None


def _willingness_check(will_seg, will_term, cust_seg, cust_term, resolution_passed):
    fail_reason = (
        "No willingness-to-help phrases (for example I'm happy to help, let me check this), "
        "no customer confirmation that it helped, and Resolution did not pass."
    )
    if will_seg:
        return _tone_check(
            "willingness", "Willingness to help",
            will_seg, will_term, False,
            "Willingness-to-help phrase found ('{term}').",
            fail_reason,
        )
    if cust_seg:
        return {
            "id": "willingness",
            "name": "Willingness to help",
            "verdict": "pass",
            "matched_term": cust_term,
            "evidence_seq": cust_seg.get("seq"),
            "evidence_text": cust_seg.get("text"),
            "reasoning": (
                f"Customer confirmed the help ('{cust_term.strip()}')."
            ),
        }
    if resolution_passed:
        return {
            "id": "willingness",
            "name": "Willingness to help",
            "verdict": "pass",
            "matched_term": None,
            "evidence_seq": None,
            "evidence_text": None,
            "reasoning": (
                "Willingness credited because Resolution passed — the issue was actually handled."
            ),
        }
    return _tone_check(
        "willingness", "Willingness to help",
        None, None, False,
        "Willingness-to-help phrase found ('{term}').",
        fail_reason,
    )


def _tone_check(check_id, name, found_seg, found_term, fail_if_found, pass_reason, fail_reason):
    if fail_if_found:
        hit = found_seg is not None
        return {
            "id": check_id,
            "name": name,
            "verdict": "fail" if hit else "pass",
            "matched_term": found_term if hit else None,
            "evidence_seq": found_seg["seq"] if hit else None,
            "evidence_text": found_seg["text"] if hit else None,
            "reasoning": fail_reason.format(term=found_term.strip()) if hit else pass_reason,
        }
    hit = found_seg is not None
    return {
        "id": check_id,
        "name": name,
        "verdict": "pass" if hit else "fail",
        "matched_term": found_term if hit else None,
        "evidence_seq": found_seg["seq"] if hit else None,
        "evidence_text": found_seg["text"] if hit else None,
        "reasoning": (
            pass_reason.format(term=found_term.strip()) if hit else fail_reason
        ),
    }


def score_tone_categories(segments, agent_speaker, resolution_passed=False):
    """Phrase banks under Tone. Hostile/profanity force fail; positives set pass/partial."""
    opening = sorted(
        _agent_segments(segments, agent_speaker),
        key=lambda s: s.get("start") or 0,
    )[:GREETING_WINDOW_AGENT_TURNS]
    prof_seg, prof_term = _first_phrase_hit(segments, agent_speaker, PROFANITY_TERMS, plain=True)
    host_seg, host_term = _first_phrase_hit(segments, agent_speaker, HOSTILE_PHRASES, plain=True)
    greet_seg, greet_term = _first_phrase_hit(opening, agent_speaker, GREETING_PHRASES)
    emp_seg, emp_term = _first_phrase_hit(segments, agent_speaker, EMPATHY_PHRASES)
    if emp_seg is None:
        emp_seg, emp_term = _first_regex_hit(segments, agent_speaker, EMPATHY_RE)
    pro_seg, pro_term = _first_phrase_hit(segments, agent_speaker, PROFESSIONAL_PHRASES)
    if pro_seg is None:
        pro_seg, pro_term = _first_regex_hit(segments, agent_speaker, PROFESSIONAL_RE)
    will_seg, will_term = _first_phrase_hit(segments, agent_speaker, WILLINGNESS_PHRASES)
    helped_seg, helped_term = _first_customer_phrase_hit(
        segments, agent_speaker, CUSTOMER_HELPED_PHRASES,
    )
    positive_count = 4

    checks = [
        _tone_check(
            "profanity", "Profanity",
            prof_seg, prof_term, True,
            "No profanity in the agent's turns.",
            "Agent used profanity ('{term}').",
        ),
        _tone_check(
            "hostile_phrases", "Hostile phrases",
            host_seg, host_term, True,
            "No hostile phrases in the agent's turns.",
            "Agent used a hostile phrase ('{term}').",
        ),
        _tone_check(
            "greetings", "Greetings",
            greet_seg, greet_term, False,
            "Greeting found in the opening turns ('{term}').",
            "No greeting in the agent's first 3 turns (for example hello, good morning, thank you for calling).",
        ),
        _tone_check(
            "empathy", "Empathy",
            emp_seg, emp_term, False,
            "Empathy phrase found ('{term}').",
            "No empathy phrases (for example I'm sorry, no worries, I understand, I appreciate it).",
        ),
        _tone_check(
            "professional", "Professionalism",
            pro_seg, pro_term, False,
            "Professional phrase found ('{term}').",
            "No professional courtesy phrases (for example thank you, how can I help, have a great day).",
        ),
        _willingness_check(
            will_seg, will_term, helped_seg, helped_term, resolution_passed,
        ),
    ]

    hostile_override = checks[0]["verdict"] == "fail" or checks[1]["verdict"] == "fail"
    positive_hits = sum(1 for c in checks[2:] if c["verdict"] == "pass")
    if hostile_override:
        bad = checks[0] if checks[0]["verdict"] == "fail" else checks[1]
        verdict = "fail"
        reasoning = (
            f"Fail because {bad['reasoning']} This forces Tone to 0 of 20, flags manager review, "
            f"and caps the overall score at 60."
        )
        evidence_seq = bad.get("evidence_seq")
        evidence_text = bad.get("evidence_text")
    elif positive_hits >= 2:
        verdict = "pass"
        names = [c["name"] for c in checks[2:] if c["verdict"] == "pass"]
        reasoning = (
            f"Pass because the agent showed {', '.join(names).lower()} "
            f"({positive_hits} of {positive_count} positive checks) and no profanity or hostile phrases."
        )
        hit = next(c for c in checks[2:] if c["verdict"] == "pass")
        evidence_seq = hit.get("evidence_seq")
        evidence_text = hit.get("evidence_text")
    elif positive_hits == 1:
        verdict = "partial"
        hit = next(c for c in checks[2:] if c["verdict"] == "pass")
        missing = [c["name"] for c in checks[2:] if c["verdict"] == "fail"]
        reasoning = (
            f"Partial because only {hit['name'].lower()} was found ('{hit['matched_term']}'); "
            f"missing {', '.join(n.lower() for n in missing)}. Half credit (10 of 20)."
        )
        evidence_seq = hit.get("evidence_seq")
        evidence_text = hit.get("evidence_text")
    else:
        verdict = "fail"
        reasoning = (
            "Fail because there was no profanity or hostility, but also no greeting, empathy, "
            "professional courtesy, or willingness-to-help phrases in the agent's turns. "
            "0 of 20."
        )
        evidence_seq = None
        evidence_text = None

    return {
        "verdict": verdict,
        "reasoning": reasoning,
        "evidence_seq": evidence_seq,
        "evidence_text": evidence_text,
        "hostile_override": hostile_override,
        "checks": checks,
        "positive_hits": positive_hits,
    }


def check_hostile_step1(segments, agent_speaker):
    """Word-boundary match, deliberately not negation-aware -- see find_term_plain."""
    scored = score_tone_categories(segments, agent_speaker)
    if scored["hostile_override"]:
        return _result(
            "fail", scored["reasoning"],
            {"seq": scored["evidence_seq"], "text": scored["evidence_text"]}
            if scored.get("evidence_seq") is not None else None,
        )
    return _result(None, "No hostile language detected -- proceed to LLM tone judgment.", None, needs_llm=True)


def combine_tone_result(step1_result, llm_verdict=None, llm_reasoning=None,
                         llm_evidence_seq=None, llm_evidence_text=None, llm_coaching_note=None,
                         full_transcript_text=None):
    if not step1_result["needs_llm"]:
        # hostile hit -- no coaching_note, this routes to manager review instead
        return step1_result
    safe_note, flag = safe_coaching_note(llm_coaching_note, llm_evidence_text, full_transcript_text, "tone_empathy_professionalism")
    result = _result(
        llm_verdict, llm_reasoning,
        {"seq": llm_evidence_seq, "text": llm_evidence_text} if llm_evidence_seq else None,
        coaching_note=safe_note,
    )
    result["llm_qa_flag"] = flag
    return result


# ---------------------------------------------------------------------------
# Score bands
# ---------------------------------------------------------------------------

SCORE_BANDS = [
    (95, 100, "Star Performer"),
    (90, 94, "Excelling"),
    (80, 89, "Solid Performer"),
    (70, 79, "Developing"),
    (60, 69, "Needs Improvement"),
    (0, 59, "Needs Immediate Attention"),
]


def score_band(score):
    for low, high, label in SCORE_BANDS:
        if low <= score <= high:
            return label
    raise ValueError(f"score {score} out of expected 0-100 range")


# ---------------------------------------------------------------------------
# Manager review routing
# ---------------------------------------------------------------------------

LOW_SCORE_THRESHOLD = 60  # aligned to the "Needs Immediate Attention" band


def check_manager_review(dimension_results, final_score, hostile_matched, hostile_evidence=None):
    triggers = []
    if hostile_matched:
        triggers.append({"reason": "hostile_language_override", "severity": "high", "evidence": hostile_evidence})
    if final_score < LOW_SCORE_THRESHOLD:
        worst = sorted(dimension_results, key=lambda d: d["score"])[:2]
        triggers.append({
            "reason": "low_overall_score", "severity": "medium", "final_score": final_score,
            "evidence": [{"dimension_id": d["id"], "evidence_seq": d.get("evidence_seq"),
                         "evidence_text": d.get("evidence_text")} for d in worst],
        })
    return triggers


# ---------------------------------------------------------------------------
# Score aggregation
# ---------------------------------------------------------------------------

WEIGHTS = {
    "resolution_effectiveness": 40,
    "ownership_next_steps": 20,
    "active_listening": 20,
    "tone_empathy_professionalism": 20,
}
VERDICT_POINTS = {"pass": 1.0, "partial": 0.5, "fail": 0.0}


def aggregate_score(dimension_verdicts, tone_hostile_override=False, weights=None):
    """dimension_verdicts: {dimension_id: 'pass'|'partial'|'fail'}
    If tone_hostile_override is True, score is capped at 60 regardless of
    the weighted average -- same override behavior the old standalone gate
    provided, now living inside the tone dimension's own detection function.

    `weights` is {dimension_id: points}. Defaults to WEIGHTS so legacy callers
    keep working; the real scoring path must pass the rubric's own weights.
    """
    table = WEIGHTS if weights is None else weights
    weighted_sum = sum(table[dim_id] * VERDICT_POINTS[verdict] for dim_id, verdict in dimension_verdicts.items())
    score = round(weighted_sum, 1)
    if tone_hostile_override:
        score = min(score, 60)
    return score


REGISTRY = {
    "ownership_step1": check_ownership_step1,
    "ownership_combine": combine_ownership_result,
    "active_listening": has_active_listening,
    "tone_step1": check_hostile_step1,
    "tone_combine": combine_tone_result,
    "score_band": score_band,
    "manager_review": check_manager_review,
    "aggregate_score": aggregate_score,
    "validate_llm_output": validate_llm_output,
    "run_llm_step_with_validation": run_llm_step_with_validation,
    "verify_coaching_note_evidence": verify_coaching_note_evidence,
    "safe_coaching_note": safe_coaching_note,
    "coaching_delivery_channel": coaching_delivery_channel,
    "needs_spot_check": needs_spot_check,
    "weekly_coaching_digest": weekly_coaching_digest,
    "detect_repeat_pattern": detect_repeat_pattern,
}
