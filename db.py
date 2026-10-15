"""Database layer (SQLite).

Stores:
  - current_deals: the deals loaded for the next review (survive restarts)
  - runs: one row per analysis, with tier counts and AI status
  - deal_results: every scored deal in every run, with its evidence
  - rejected_deals: rows that failed validation, with the reasons
  - outcomes: what actually happened to each deal (Won / Lost / Slipped / Still open)
Manager overrides ("not at risk") are stored on deal_results.
"""

import json
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

DB_PATH = os.environ.get("DB_PATH", "pipeline.db")
LOCAL_TZ = ZoneInfo(os.environ.get("APP_TIMEZONE", "Asia/Kolkata"))
OUTCOMES = ["Won", "Lost", "Slipped", "Still open"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS current_deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    review_date TEXT NOT NULL,
    deals_scored INTEGER NOT NULL,
    rejected INTEGER NOT NULL,
    at_risk INTEGER NOT NULL,
    watch INTEGER NOT NULL,
    healthy INTEGER NOT NULL,
    ai_used INTEGER NOT NULL,
    ai_provider TEXT,
    ai_model TEXT,
    ai_deals_sent INTEGER,
    ai_dropped INTEGER,
    ai_message TEXT
);

CREATE TABLE IF NOT EXISTS deal_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    position INTEGER NOT NULL,
    deal_id TEXT NOT NULL,
    account_name TEXT,
    owner TEXT,
    stage TEXT,
    deal_size_inr REAL,
    last_activity_date TEXT,
    stage_entered_date TEXT,
    expected_close_date TEXT,
    notes TEXT,
    days_inactive INTEGER,
    days_in_stage INTEGER,
    rule_score INTEGER,
    score INTEGER,
    tier TEXT,
    confidence TEXT,
    confidence_reason TEXT,
    ai_reason TEXT,
    next_action TEXT,
    action_source TEXT,
    signals TEXT,
    note_signals TEXT,
    explained TEXT,
    dropped TEXT,
    capped INTEGER,
    override_reason TEXT,
    overridden_at TEXT
);

CREATE TABLE IF NOT EXISTS rejected_deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    deal_id TEXT,
    account_name TEXT,
    errors TEXT
);

CREATE TABLE IF NOT EXISTS outcomes (
    deal_id TEXT PRIMARY KEY,
    outcome TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_results_run ON deal_results(run_id);
CREATE INDEX IF NOT EXISTS idx_results_deal ON deal_results(deal_id);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)


def now_text():
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


# ---------- Current deals (the review being prepared) ----------

def get_current_deals():
    with connect() as conn:
        rows = conn.execute("SELECT data FROM current_deals ORDER BY id").fetchall()
    return [json.loads(row["data"]) for row in rows]


def add_current_deal(deal):
    with connect() as conn:
        conn.execute("INSERT INTO current_deals (data) VALUES (?)", (json.dumps(deal),))


def replace_current_deals(deals):
    with connect() as conn:
        conn.execute("DELETE FROM current_deals")
        conn.executemany("INSERT INTO current_deals (data) VALUES (?)",
                         [(json.dumps(d),) for d in deals])


def clear_current_deals():
    with connect() as conn:
        conn.execute("DELETE FROM current_deals")


# ---------- Saving and loading analysis runs ----------

def save_run(review_date, results, rejected, ai_status):
    """Save one full analysis and return its run id."""
    counts = {tier: sum(1 for r in results if r["tier"] == tier)
              for tier in ("At Risk", "Watch", "Healthy")}

    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO runs (created_at, review_date, deals_scored, rejected,
                   at_risk, watch, healthy, ai_used, ai_provider, ai_model,
                   ai_deals_sent, ai_dropped, ai_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now_text(), review_date.isoformat(), len(results), len(rejected),
             counts["At Risk"], counts["Watch"], counts["Healthy"],
             int(ai_status["used"]), ai_status.get("provider"), ai_status.get("model"),
             ai_status.get("deals_sent"), ai_status.get("dropped"), ai_status.get("message")),
        )
        run_id = cur.lastrowid

        for position, r in enumerate(results):
            d = r["deal"]
            conn.execute(
                """INSERT INTO deal_results (run_id, position, deal_id, account_name, owner,
                       stage, deal_size_inr, last_activity_date, stage_entered_date,
                       expected_close_date, notes, days_inactive, days_in_stage,
                       rule_score, score, tier, confidence, confidence_reason, ai_reason,
                       next_action, action_source, signals, note_signals, explained,
                       dropped, capped)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?)""",
                (run_id, position, d["deal_id"], d["account_name"], d["owner"], d["stage"],
                 d["deal_size_inr"], str(d["last_activity_date"]),
                 str(d["stage_entered_date"]), str(d["expected_close_date"]),
                 d.get("notes", ""), r["days_inactive"], r["days_in_stage"],
                 r["rule_score"], r["score"], r["tier"], r["confidence"],
                 r["confidence_reason"], r.get("ai_reason"), r["next_action"],
                 r.get("action_source", "Rules"), json.dumps(r["signals"]),
                 json.dumps(r.get("note_signals", [])), json.dumps(r.get("explained")),
                 json.dumps(r.get("dropped", [])), int(r.get("capped", False))),
            )

        conn.executemany(
            "INSERT INTO rejected_deals (run_id, deal_id, account_name, errors) VALUES (?, ?, ?, ?)",
            [(run_id, item["deal"].get("deal_id"), item["deal"].get("account_name"),
              json.dumps(item["errors"])) for item in rejected],
        )
    return run_id


def _result_from_row(row):
    return {
        "id": row["id"],
        "deal": {
            "deal_id": row["deal_id"],
            "account_name": row["account_name"],
            "owner": row["owner"],
            "stage": row["stage"],
            "deal_size_inr": row["deal_size_inr"],
            "last_activity_date": row["last_activity_date"],
            "stage_entered_date": row["stage_entered_date"],
            "expected_close_date": row["expected_close_date"],
            "notes": row["notes"],
        },
        "days_inactive": row["days_inactive"],
        "days_in_stage": row["days_in_stage"],
        "rule_score": row["rule_score"],
        "score": row["score"],
        "tier": row["tier"],
        "confidence": row["confidence"],
        "confidence_reason": row["confidence_reason"],
        "ai_reason": row["ai_reason"],
        "next_action": row["next_action"],
        "action_source": row["action_source"],
        "signals": json.loads(row["signals"] or "[]"),
        "note_signals": json.loads(row["note_signals"] or "[]"),
        "explained": json.loads(row["explained"] or "null"),
        "dropped": json.loads(row["dropped"] or "[]"),
        "capped": bool(row["capped"]),
        "override_reason": row["override_reason"],
        "overridden_at": row["overridden_at"],
    }


def load_run(run_id):
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            return None
        rows = conn.execute(
            "SELECT * FROM deal_results WHERE run_id = ? ORDER BY position", (run_id,)
        ).fetchall()
        rejected_rows = conn.execute(
            "SELECT * FROM rejected_deals WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()

    return {
        "id": run["id"],
        "created_at": run["created_at"],
        "review_date": run["review_date"],
        "results": [_result_from_row(row) for row in rows],
        "rejected": [
            {"deal": {"deal_id": r["deal_id"], "account_name": r["account_name"]},
             "errors": json.loads(r["errors"] or "[]")}
            for r in rejected_rows
        ],
        "counts": {"At Risk": run["at_risk"], "Watch": run["watch"], "Healthy": run["healthy"]},
        "ai": {
            "used": bool(run["ai_used"]),
            "provider": run["ai_provider"],
            "model": run["ai_model"],
            "deals_sent": run["ai_deals_sent"],
            "dropped": run["ai_dropped"],
            "message": run["ai_message"],
        },
    }


def latest_run_id():
    with connect() as conn:
        row = conn.execute("SELECT MAX(id) AS id FROM runs").fetchone()
    return row["id"]


def list_runs():
    with connect() as conn:
        return conn.execute("SELECT * FROM runs ORDER BY id DESC").fetchall()


def run_id_for_result(result_id):
    with connect() as conn:
        row = conn.execute("SELECT run_id FROM deal_results WHERE id = ?",
                           (result_id,)).fetchone()
    return row["run_id"] if row else None


# ---------- Outcomes and overrides ----------

def set_outcome(deal_id, outcome):
    if outcome not in OUTCOMES:
        raise ValueError(f"Unknown outcome: {outcome}")
    with connect() as conn:
        conn.execute(
            """INSERT INTO outcomes (deal_id, outcome, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(deal_id) DO UPDATE SET outcome = excluded.outcome,
                                                  updated_at = excluded.updated_at""",
            (deal_id, outcome, now_text()),
        )


def get_outcomes():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM outcomes").fetchall()
    return {row["deal_id"]: row["outcome"] for row in rows}


def set_override(result_id, reason):
    with connect() as conn:
        conn.execute(
            "UPDATE deal_results SET override_reason = ?, overridden_at = ? WHERE id = ?",
            (reason, now_text(), result_id),
        )


def clear_override(result_id):
    with connect() as conn:
        conn.execute(
            "UPDATE deal_results SET override_reason = NULL, overridden_at = NULL WHERE id = ?",
            (result_id,),
        )


# ---------- Admin dashboard ----------

def dashboard_stats():
    """Everything the admin dashboard shows, computed from the saved data."""
    with connect() as conn:
        runs = conn.execute("SELECT * FROM runs ORDER BY id").fetchall()
        all_results = conn.execute(
            "SELECT tier, signals, note_signals, explained, override_reason FROM deal_results"
        ).fetchall()
        # The most recent result for each deal is the one judged against its outcome.
        latest = conn.execute(
            """SELECT dr.deal_id, dr.tier, o.outcome
               FROM deal_results dr
               JOIN (SELECT deal_id, MAX(id) AS max_id FROM deal_results GROUP BY deal_id) last
                 ON dr.id = last.max_id
               LEFT JOIN outcomes o ON o.deal_id = dr.deal_id"""
        ).fetchall()
        overrides = conn.execute(
            """SELECT id, run_id, deal_id, account_name, tier, override_reason, overridden_at
               FROM deal_results WHERE override_reason IS NOT NULL
               ORDER BY overridden_at DESC LIMIT 10"""
        ).fetchall()

    tiers = ("At Risk", "Watch", "Healthy")

    # Outcome matrix: latest tier x recorded outcome.
    matrix = {t: {o: 0 for o in OUTCOMES} for t in tiers}
    for row in latest:
        if row["outcome"]:
            matrix[row["tier"]][row["outcome"]] += 1

    def share_bad(rows_for):
        closed = sum(rows_for[o] for o in ("Won", "Lost", "Slipped"))
        bad = rows_for["Lost"] + rows_for["Slipped"]
        return (round(100 * bad / closed) if closed else None), closed

    at_risk_accuracy, at_risk_closed = share_bad(matrix["At Risk"])
    healthy_bad, healthy_closed = share_bad(matrix["Healthy"])

    flagged = sum(1 for r in all_results if r["tier"] != "Healthy")
    overridden = sum(1 for r in all_results if r["override_reason"])
    override_rate = round(100 * overridden / flagged) if flagged else None

    # Flags by reason, across every saved result.
    reasons = {}
    for r in all_results:
        for s in json.loads(r["signals"] or "[]"):
            key = ("Rule", s["label"])
            reasons[key] = reasons.get(key, 0) + 1
        for s in json.loads(r["note_signals"] or "[]"):
            key = ("Notes", s["label"])
            reasons[key] = reasons.get(key, 0) + 1
        if json.loads(r["explained"] or "null"):
            key = ("Notes", "Quiet period explained (lowered)")
            reasons[key] = reasons.get(key, 0) + 1
    reason_rows = sorted(
        ({"source": k[0], "label": k[1], "count": v} for k, v in reasons.items()),
        key=lambda x: -x["count"],
    )

    ai_runs = sum(1 for r in runs if r["ai_used"])
    return {
        "total_runs": len(runs),
        "deals_tracked": len(latest),
        "outcomes_recorded": sum(1 for r in latest if r["outcome"]),
        "ai_runs": ai_runs,
        "ai_rate": round(100 * ai_runs / len(runs)) if runs else None,
        "ai_dropped": sum(r["ai_dropped"] or 0 for r in runs),
        "at_risk_accuracy": at_risk_accuracy,
        "at_risk_closed": at_risk_closed,
        "healthy_bad": healthy_bad,
        "healthy_closed": healthy_closed,
        "flagged": flagged,
        "overridden": overridden,
        "override_rate": override_rate,
        "matrix": matrix,
        "reasons": reason_rows,
        "overrides": overrides,
        "recent_runs": runs[-12:],
        "failed_runs": [r for r in reversed(runs) if not r["ai_used"]][:5],
    }
