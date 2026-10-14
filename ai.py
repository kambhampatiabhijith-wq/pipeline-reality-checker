"""AI layer: reads deal notes and adds evidence the rules can't see.

Runs AFTER the logic layer. The AI can:
  - add note signals (+15 each, max +30), each backed by an exact quote
  - lower the score (-25) when the notes explain a quiet period with a dated next step
  - write a one-sentence reason and one specific next action
It can never push a deal to At Risk on its own: that always takes a rule.
Every quote is checked against the original notes before it is used.
"""

import json
import os
import re
import urllib.error
import urllib.request

from logic import TIER_ORDER, tier_for

# Groq's API uses the same format as OpenAI's, so either provider works.
# Set GROQ_API_KEY (free tier) or OPENAI_API_KEY in Replit Secrets.
PROVIDERS = {
    "Groq": {
        "key_env": "GROQ_API_KEY",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "model": os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
    },
    "OpenAI": {
        "key_env": "OPENAI_API_KEY",
        "url": "https://api.openai.com/v1/chat/completions",
        "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
    },
}
TEMPERATURE = 0.2
TIMEOUT_SECONDS = 30

NOTE_SIGNALS = {
    "no_decision_maker": "No decision-maker involved",
    "budget_on_hold": "Budget on hold",
    "competitor": "Competitor in the picture",
    "buyer_delay": "Buyer delaying or not responding",
}
NOTE_SIGNAL_POINTS = 15
NOTE_SIGNAL_CAP = 30
EXPLAINED_SILENCE_POINTS = -25
INACTIVITY_CODES = {"inactive", "very_inactive", "large_and_quiet"}

NOTE_ACTIONS = {
    "no_decision_maker": "Ask your contact to set up a meeting with the person who signs off.",
    "budget_on_hold": "Ask the buyer when budget reopens and agree a date to revisit.",
    "competitor": "Find out what the competing offer includes and send a comparison that answers it.",
    "buyer_delay": "Ask the buyer directly whether this deal is still a priority this quarter.",
}
EXPLAINED_ACTION = "Wait for the dated next step in the notes, then confirm it happened."

VAGUE_ACTION = re.compile(
    r"^(follow[- ]?up|check in|touch base|reach out|keep monitoring|monitor)\b.{0,30}$", re.I
)

SYSTEM_PROMPT = """You review B2B sales deal notes for a sales manager. A rules engine has already scored each deal using dates and stage limits. Your only job is to read the free-text notes for evidence the rules cannot see.

For each deal, return:
- note_signals: risks the notes state directly. Allowed types: no_decision_maker, budget_on_hold, competitor, buyer_delay. Each needs "quote": words copied exactly from the notes (3 to 20 words). Only report a risk the notes actually state. Never infer one.
- explained_silence: if the notes explain why the deal is quiet AND give a specific upcoming date or scheduled event, return {"quote": exact words from the notes}. Otherwise null.
- reason: one sentence for the manager on the deal's status, using only the rule signals given and the quotes you returned. Do not add facts that are not in the input.
- next_action: one specific step the deal owner can take this week, naming who to contact or what to send. Never just "follow up".

If the notes are empty or neutral, return an empty note_signals list and explained_silence null.
Reply with JSON only, in this shape:
{"deals":[{"deal_id":"D001","note_signals":[{"type":"competitor","quote":"..."}],"explained_silence":null,"reason":"...","next_action":"..."}]}"""


class AIError(Exception):
    """Raised when the LLM call fails or returns something unusable."""


# ---------- Prompt construction ----------

def build_payload(results):
    """Only deals with notes or rule hits are sent. Account names are left out."""
    items = []
    for r in results:
        deal = r["deal"]
        notes = (deal.get("notes") or "").strip()
        if not notes and not r["signals"]:
            continue
        items.append({
            "deal_id": deal["deal_id"],
            "stage": deal["stage"],
            "deal_size_inr": int(deal["deal_size_inr"]),
            "days_since_last_activity": r["days_inactive"],
            "days_in_stage": r["days_in_stage"],
            "expected_close_date": deal["expected_close_date"].isoformat(),
            "rule_signals": [f"{s['label']}: {s['detail']}" for s in r["signals"]],
            "notes": notes,
        })
    return items


# ---------- LLM API call ----------

def _error_message(raw):
    try:
        return json.loads(raw)["error"]["message"][:200]
    except (ValueError, KeyError, TypeError):
        return raw[:200]


def pick_provider():
    """Return (name, settings, api_key) for the first provider with a key set."""
    for name, settings in PROVIDERS.items():
        key = os.environ.get(settings["key_env"], "").strip()
        if key:
            return name, settings, key
    return None, None, ""


def call_llm(items, review_date, api_key, provider):
    name = provider["name"]
    body = {
        "model": provider["model"],
        "temperature": TEMPERATURE,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(
                {"review_date": review_date.isoformat(), "deals": items}
            )},
        ],
    }
    request = urllib.request.Request(
        provider["url"],
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "pipeline-reality-checker/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data = json.load(response)
    except urllib.error.HTTPError as e:
        raise AIError(f"{name} returned error {e.code}: "
                      f"{_error_message(e.read().decode('utf-8', 'ignore'))}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise AIError(f"couldn't reach {name} ({e})")

    try:
        parsed = json.loads(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError):
        raise AIError("the reply wasn't valid JSON")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("deals"), list):
        raise AIError("the reply didn't contain a list of deals")
    return parsed


# ---------- Output parsing and evidence checks ----------

def _normalise(text):
    text = re.sub(r"[\"'“”‘’]", "", text or "")
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_in_notes(quote, notes):
    """True only if the quote really appears in the deal's notes."""
    q = _normalise(quote).rstrip(".")
    return len(q) >= 3 and q in _normalise(notes)


def _is_specific(action):
    action = (action or "").strip()
    return len(action) >= 25 and not VAGUE_ACTION.match(action)


def apply_to_result(r, entry):
    """Merge one deal's AI output into its rules result, keeping only verified evidence."""
    notes = r["deal"].get("notes") or ""
    rule_codes = {s["code"] for s in r["signals"]}
    r.update(note_signals=[], explained=None, ai_checked=entry is not None,
             ai_reason=None, dropped=[], capped=False)

    if entry is not None:
        total = 0
        seen = set()
        for s in entry.get("note_signals") or []:
            if not isinstance(s, dict):
                continue
            kind, quote = s.get("type"), (s.get("quote") or "").strip()
            if kind not in NOTE_SIGNALS or kind in seen:
                continue
            if not quote_in_notes(quote, notes):
                r["dropped"].append(f"{NOTE_SIGNALS[kind]}: quote not found in the notes")
                continue
            seen.add(kind)
            points = min(NOTE_SIGNAL_POINTS, NOTE_SIGNAL_CAP - total)
            total += points
            r["note_signals"].append(
                {"type": kind, "label": NOTE_SIGNALS[kind], "quote": quote, "points": points}
            )

        explained = entry.get("explained_silence")
        if isinstance(explained, dict) and rule_codes & INACTIVITY_CODES:
            quote = (explained.get("quote") or "").strip()
            if quote_in_notes(quote, notes):
                r["explained"] = {"quote": quote, "points": EXPLAINED_SILENCE_POINTS}
            else:
                r["dropped"].append("Explained silence: quote not found in the notes")

    # ---- Score and tier ----
    score = r["rule_score"] + sum(s["points"] for s in r["note_signals"])
    if r["explained"]:
        score += r["explained"]["points"]
    score = max(0, min(100, score))
    if r["rule_score"] < 50 and score >= 50:
        score = 49          # note evidence alone can't make a deal At Risk
        r["capped"] = True
    r["score"] = score
    r["tier"] = tier_for(score)

    # ---- Reason ----
    has_evidence = bool(r["signals"] or r["note_signals"] or r["explained"])
    ai_reason = (entry or {}).get("reason")
    if isinstance(ai_reason, str) and ai_reason.strip() and has_evidence:
        r["ai_reason"] = ai_reason.strip()
    elif r["signals"]:
        r["ai_reason"] = None
    # A reason with no evidence behind it is dropped, never shown.

    # ---- Next action ----
    ai_action = (entry or {}).get("next_action")
    if isinstance(ai_action, str) and _is_specific(ai_action):
        r["next_action"] = ai_action.strip()
        r["action_source"] = "AI"
    else:
        if r["explained"]:
            r["next_action"] = EXPLAINED_ACTION
        elif not r["signals"] and r["note_signals"]:
            r["next_action"] = NOTE_ACTIONS[r["note_signals"][0]["type"]]
        r["action_source"] = "Rules"
        if entry is not None and ai_action:
            r["dropped"].append("Next action was too vague, replaced with a rule-based one")

    # ---- Confidence ----
    if not r["ai_checked"]:
        if notes.strip():
            r["confidence"], r["confidence_reason"] = (
                "Low", "The notes weren't analysed, so this is based on rules only.")
        else:
            r["confidence"], r["confidence_reason"] = (
                "Medium", "No notes to check, so this is based on rules only.")
    elif r["explained"]:
        r["confidence"], r["confidence_reason"] = (
            "Low", "The notes explain the quiet period, so the rules flag was lowered. "
                   "Check that the date in the notes still holds.")
    elif r["signals"] and r["note_signals"]:
        r["confidence"], r["confidence_reason"] = ("High", "Rules and notes point the same way.")
    elif r["note_signals"]:
        r["confidence"], r["confidence_reason"] = (
            "Medium", "Based on evidence in the notes; no rule fired.")
    elif r["signals"]:
        r["confidence"], r["confidence_reason"] = (
            "Medium", "Based on rules; the notes add nothing either way.")
    else:
        r["confidence"], r["confidence_reason"] = ("High", "Rules and notes both show no risk.")


# ---------- Main entry point ----------

def apply_ai(results, review_date):
    """Run the AI step over scored results. Always returns usable results,
    falling back to rules-only if the AI is unavailable."""
    name, settings, api_key = pick_provider()
    provider = dict(settings or {}, name=name)

    items = build_payload(results)
    status = {"used": False, "provider": name, "model": provider.get("model"),
              "deals_sent": len(items), "dropped": 0, "message": ""}

    responses = {}
    if not items:
        status["message"] = "No deals had notes or rule flags, so the AI wasn't needed."
    elif not api_key:
        status["message"] = ("AI analysis unavailable: no GROQ_API_KEY is set in Secrets. "
                             "Showing rules-only results.")
    else:
        error = None
        for _attempt in range(2):          # one retry
            try:
                parsed = call_llm(items, review_date, api_key, provider)
                responses = {str(d.get("deal_id")): d for d in parsed["deals"]
                             if isinstance(d, dict)}
                status["used"] = True
                break
            except AIError as e:
                error = e
        if not status["used"]:
            status["message"] = (f"AI analysis unavailable: {error}. "
                                 "Showing rules-only results.")

    sent_ids = {item["deal_id"] for item in items}
    for r in results:
        deal_id = r["deal"]["deal_id"]
        entry = responses.get(deal_id) if deal_id in sent_ids else None
        apply_to_result(r, entry)
        status["dropped"] += len(r["dropped"])

    results.sort(key=lambda r: (TIER_ORDER[r["tier"]], -r["score"], -r["deal"]["deal_size_inr"]))
    return status
