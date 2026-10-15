"""Pipeline Reality Checker - web app entry point.

Stage 6: admin dashboard on top of the saved runs, outcomes and overrides.
Pipeline per run: validation + rules (logic.py) -> AI notes (ai.py) -> database (db.py).
"""

import csv
import io
import os
from datetime import datetime

from flask import Flask, abort, flash, redirect, render_template, request, url_for

import db
from ai import apply_ai
from logic import STAGE_LIMITS, analyse_pipeline, today

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "dev-only-secret")

db.init_db()

STAGES = list(STAGE_LIMITS)

FIELDS = [
    "deal_id",
    "account_name",
    "owner",
    "stage",
    "deal_size_inr",
    "last_activity_date",
    "stage_entered_date",
    "expected_close_date",
    "notes",
]


def clean_row(raw):
    """Keep only the expected fields and strip stray spaces."""
    return {field: (raw.get(field) or "").strip() for field in FIELDS}


@app.template_filter("inr")
def format_inr(value):
    """Format a number in Indian style, e.g. 15000000 -> ₹1,50,00,000."""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return value
    digits = str(abs(number))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups) + "," + tail
    sign = "-" if number < 0 else ""
    return f"{sign}₹{digits}"


# ---------- Deals for the next review ----------

@app.route("/")
def index():
    return render_template(
        "index.html", deals=db.get_current_deals(), stages=STAGES, today=today().isoformat()
    )


@app.post("/deals")
def add_deal():
    deal = clean_row(request.form)
    db.add_current_deal(deal)
    flash(f"Added deal {deal['deal_id'] or '(no ID)'}.", "ok")
    return redirect(url_for("index"))


@app.post("/upload")
def upload_csv():
    file = request.files.get("file")

    if not file or not file.filename:
        flash("Choose a CSV file first.", "error")
        return redirect(url_for("index"))

    if not file.filename.lower().endswith(".csv"):
        flash("That file isn't a CSV. Export your deals as .csv and try again.", "error")
        return redirect(url_for("index"))

    try:
        text = file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("Couldn't read the file. Save it as a UTF-8 CSV and try again.", "error")
        return redirect(url_for("index"))

    reader = csv.DictReader(io.StringIO(text))
    missing = [field for field in FIELDS if field not in (reader.fieldnames or [])]
    if missing:
        flash("The CSV is missing these columns: " + ", ".join(missing), "error")
        return redirect(url_for("index"))

    rows = [clean_row(row) for row in reader]
    if not rows:
        flash("The CSV has headers but no deals in it.", "error")
        return redirect(url_for("index"))

    # An upload replaces the current list, so each review starts clean.
    db.replace_current_deals(rows)
    flash(f"Loaded {len(rows)} deals from {file.filename}.", "ok")
    return redirect(url_for("index"))


@app.post("/clear")
def clear_deals():
    db.clear_current_deals()
    flash("Cleared the deals list. Past results are still in History.", "ok")
    return redirect(url_for("index"))


# ---------- Analysis ----------

@app.post("/analyse")
def analyse():
    deals = db.get_current_deals()
    if not deals:
        flash("Add or upload some deals first.", "error")
        return redirect(url_for("index"))

    raw_date = request.form.get("review_date") or today().isoformat()
    try:
        review_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError:
        flash("The review date isn't valid. Use the date picker.", "error")
        return redirect(url_for("index"))

    # 1) Logic layer: validate and score with rules.
    results, rejected = analyse_pipeline(deals, review_date)
    # 2) AI layer: read notes, verify quotes, adjust within limits.
    ai_status = apply_ai(results, review_date)
    # 3) Database: save the whole run.
    run_id = db.save_run(review_date, results, rejected, ai_status)

    return redirect(url_for("view_run", run_id=run_id))


@app.route("/results")
def results():
    run_id = db.latest_run_id()
    if run_id is None:
        flash("Run an analysis first.", "error")
        return redirect(url_for("index"))
    return redirect(url_for("view_run", run_id=run_id))


@app.route("/runs/<int:run_id>")
def view_run(run_id):
    run = db.load_run(run_id)
    if run is None:
        abort(404)
    return render_template(
        "results.html", run=run, outcomes=db.get_outcomes(), outcome_options=db.OUTCOMES
    )


@app.route("/history")
def history():
    return render_template("history.html", runs=db.list_runs())


@app.route("/dashboard")
def dashboard():
    stats = db.dashboard_stats()
    chart_runs = {
        "labels": [f"#{r['id']}" for r in stats["recent_runs"]],
        "at_risk": [r["at_risk"] for r in stats["recent_runs"]],
        "watch": [r["watch"] for r in stats["recent_runs"]],
        "healthy": [r["healthy"] for r in stats["recent_runs"]],
    }
    chart_reasons = {
        "labels": [f"{x['label']} ({x['source']})" for x in stats["reasons"]],
        "counts": [x["count"] for x in stats["reasons"]],
    }
    return render_template(
        "dashboard.html", stats=stats, chart_runs=chart_runs, chart_reasons=chart_reasons,
        limits=STAGE_LIMITS, outcome_options=db.OUTCOMES,
    )


# ---------- Outcomes and overrides ----------

@app.post("/outcome")
def record_outcome():
    deal_id = (request.form.get("deal_id") or "").strip()
    outcome = request.form.get("outcome")
    run_id = request.form.get("run_id", type=int)
    result_id = request.form.get("result_id", type=int)
    try:
        if not deal_id:
            raise ValueError("Missing deal ID.")
        db.set_outcome(deal_id, outcome)
        flash(f"Saved outcome for {deal_id}: {outcome}.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    target = url_for("view_run", run_id=run_id) if run_id else url_for("results")
    return redirect(f"{target}#result-{result_id}" if result_id else target)


@app.post("/override/<int:result_id>")
def override(result_id):
    run_id = db.run_id_for_result(result_id)
    if run_id is None:
        abort(404)
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("Add a short reason before marking a deal as not at risk.", "error")
    else:
        db.set_override(result_id, reason[:300])
        flash("Saved. The flag is marked as not at risk, with your reason.", "ok")
    return redirect(url_for("view_run", run_id=run_id) + f"#result-{result_id}")


@app.post("/override/<int:result_id>/undo")
def undo_override(result_id):
    run_id = db.run_id_for_result(result_id)
    if run_id is None:
        abort(404)
    db.clear_override(result_id)
    flash("Override removed.", "ok")
    return redirect(url_for("view_run", run_id=run_id) + f"#result-{result_id}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
