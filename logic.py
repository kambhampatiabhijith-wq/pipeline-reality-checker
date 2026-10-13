"""Logic layer: input validation and rules-based risk scoring.

Everything here runs BEFORE any AI call. The rules decide each deal's
score and tier; the AI (stage 4) can only explain, add quoted note
signals, or lower a tier when the notes explain a quiet period.
"""

import math
from datetime import date, datetime

# Default stage thresholds from the product brief (editable later).
STAGE_LIMITS = {
    "Discovery": {"inactive": 21, "in_stage": 30},
    "Proposal": {"inactive": 14, "in_stage": 30},
    "Negotiation": {"inactive": 10, "in_stage": 21},
    "Contract Review": {"inactive": 14, "in_stage": 30},
}

POINTS = {
    "close_passed": 50,      # strong signal
    "very_inactive": 50,     # strong signal: inactive > 2x stage limit
    "inactive": 25,
    "stage_stuck": 25,
    "large_and_quiet": 10,
}

LARGE_DEAL_SHARE = 0.20      # top 20% of deals by size
LARGE_DEAL_QUIET_DAYS = 7

REQUIRED_FIELDS = {
    "deal_id": "Deal ID",
    "account_name": "Account name",
    "owner": "Owner",
    "stage": "Stage",
    "deal_size_inr": "Deal size",
    "last_activity_date": "Last activity date",
    "stage_entered_date": "Stage entered date",
    "expected_close_date": "Expected close date",
}

NEXT_ACTIONS = {
    "close_passed": "Confirm a new close date with the buyer this week and update the forecast.",
    "very_inactive": "Call the main contact this week; if there's no reply, escalate to their manager.",
    "inactive": "Book a follow-up with the buyer within the next 3 days.",
    "stage_stuck": "Agree with the buyer on the one step needed to move to the next stage.",
    "large_and_quiet": "Check in with the buyer this week; this is one of the largest deals in the pipeline.",
}


# ---------- Tiers ----------

def tier_for(score):
    if score >= 50:
        return "At Risk"
    if score >= 25:
        return "Watch"
    return "Healthy"


# ---------- Validation ----------

def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def validate_deal(raw, review_date):
    """Return (clean_deal, errors). clean_deal is None if there are errors."""
    errors = []

    for field, label in REQUIRED_FIELDS.items():
        if not raw.get(field):
            errors.append(f"{label} is missing.")
    if errors:
        return None, errors

    stage = raw["stage"]
    if stage not in STAGE_LIMITS:
        errors.append(
            f"Stage '{stage}' isn't recognised. Use one of: {', '.join(STAGE_LIMITS)}."
        )

    try:
        size = float(raw["deal_size_inr"])
        if size <= 0:
            errors.append("Deal size must be more than 0.")
    except ValueError:
        size = None
        errors.append(f"Deal size '{raw['deal_size_inr']}' isn't a number.")

    dates = {}
    for field in ("last_activity_date", "stage_entered_date", "expected_close_date"):
        try:
            dates[field] = parse_date(raw[field])
        except ValueError:
            errors.append(
                f"{REQUIRED_FIELDS[field]} '{raw[field]}' isn't a valid date. Use YYYY-MM-DD."
            )

    last = dates.get("last_activity_date")
    entered = dates.get("stage_entered_date")

    if last and last > review_date:
        errors.append(
            f"Last activity date {last} is after the review date {review_date}. "
            "Activity can't be in the future."
        )
    if entered and entered > review_date:
        errors.append(f"Stage entered date {entered} is after the review date {review_date}.")
    if last and entered and entered > last:
        errors.append(
            f"Stage entered date {entered} is after the last activity date {last}. "
            "Moving stage counts as activity, so check both dates."
        )

    if errors:
        return None, errors

    clean = dict(raw)
    clean["deal_size_inr"] = size
    clean.update(dates)
    return clean, []


def validate_batch(raw_deals, review_date):
    """Validate every deal. Returns (valid_deals, rejected) where rejected
    is a list of {"deal": raw, "errors": [...]}."""
    valid, rejected = [], []
    seen_ids = set()

    for raw in raw_deals:
        deal, errors = validate_deal(raw, review_date)
        deal_id = raw.get("deal_id")
        if deal_id and deal_id in seen_ids:
            errors = errors + [f"Deal ID {deal_id} appears more than once."]
            deal = None
        if deal_id:
            seen_ids.add(deal_id)

        if deal is None:
            rejected.append({"deal": raw, "errors": errors})
        else:
            valid.append(deal)

    return valid, rejected


# ---------- Scoring ----------

def large_deal_threshold(deals):
    """Smallest deal size that still counts as top 20% of the pipeline."""
    if not deals:
        return math.inf
    sizes = sorted((d["deal_size_inr"] for d in deals), reverse=True)
    count = max(1, math.ceil(len(sizes) * LARGE_DEAL_SHARE))
    return sizes[count - 1]


def score_deal(deal, review_date, large_threshold):
    """Apply the rules to one validated deal and explain every point."""
    limits = STAGE_LIMITS[deal["stage"]]
    days_inactive = (review_date - deal["last_activity_date"]).days
    days_in_stage = (review_date - deal["stage_entered_date"]).days
    days_past_close = (review_date - deal["expected_close_date"]).days

    signals = []

    if days_past_close > 0:
        signals.append({
            "code": "close_passed",
            "label": "Close date has passed",
            "detail": f"Expected close was {deal['expected_close_date']}, "
                      f"{days_past_close} days before the review date.",
            "points": POINTS["close_passed"],
            "strong": True,
        })

    if days_inactive > 2 * limits["inactive"]:
        signals.append({
            "code": "very_inactive",
            "label": "Very long silence",
            "detail": f"No activity for {days_inactive} days, more than twice the "
                      f"{limits['inactive']}-day limit for {deal['stage']}.",
            "points": POINTS["very_inactive"],
            "strong": True,
        })
    elif days_inactive > limits["inactive"]:
        signals.append({
            "code": "inactive",
            "label": "Inactive",
            "detail": f"No activity for {days_inactive} days; the limit for "
                      f"{deal['stage']} is {limits['inactive']}.",
            "points": POINTS["inactive"],
            "strong": False,
        })

    if days_in_stage > limits["in_stage"]:
        signals.append({
            "code": "stage_stuck",
            "label": "Stuck in stage",
            "detail": f"In {deal['stage']} for {days_in_stage} days; the limit is "
                      f"{limits['in_stage']}.",
            "points": POINTS["stage_stuck"],
            "strong": False,
        })

    if deal["deal_size_inr"] >= large_threshold and days_inactive > LARGE_DEAL_QUIET_DAYS:
        signals.append({
            "code": "large_and_quiet",
            "label": "Large deal going quiet",
            "detail": f"One of the largest deals in this pipeline, with no activity "
                      f"for {days_inactive} days.",
            "points": POINTS["large_and_quiet"],
            "strong": False,
        })

    rule_score = min(100, sum(s["points"] for s in signals))

    if signals:
        top = max(signals, key=lambda s: s["points"])
        next_action = NEXT_ACTIONS[top["code"]]
    else:
        next_action = "No action needed this week."

    return {
        "deal": deal,
        "days_inactive": days_inactive,
        "days_in_stage": days_in_stage,
        "signals": signals,
        "rule_score": rule_score,
        "score": rule_score,
        "tier": tier_for(rule_score),
        "next_action": next_action,
        "confidence": "Medium",
        "confidence_reason": "Based on rules only; notes not yet analysed.",
    }


TIER_ORDER = {"At Risk": 0, "Watch": 1, "Healthy": 2}


def analyse_pipeline(raw_deals, review_date):
    """Full logic layer: validate, then score. Returns results and rejects."""
    valid, rejected = validate_batch(raw_deals, review_date)
    threshold = large_deal_threshold(valid)
    results = [score_deal(d, review_date, threshold) for d in valid]
    results.sort(key=lambda r: (TIER_ORDER[r["tier"]], -r["score"], -r["deal"]["deal_size_inr"]))
    return results, rejected


def today():
    return date.today()
