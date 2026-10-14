"""Pipeline Reality Checker - web app entry point.

Stage 4: logic layer (validation + rules) followed by the AI note analysis.
Deals are held in memory for now; stage 5 moves them into a database.
"""

import csv
import io
import os
from datetime import datetime

from flask import Flask, flash, redirect, render_template, request, url_for

from ai import apply_ai
from logic import STAGE_LIMITS, analyse_pipeline, today

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "dev-only-secret")

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

# In-memory storage (replaced by the database in stage 5).
DEALS = []
LAST_RUN = {}


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


@app.route("/")
def index():
    return render_template(
        "index.html", deals=DEALS, stages=STAGES, today=today().isoformat()
    )


@app.post("/deals")
def add_deal():
    deal = clean_row(request.form)
    DEALS.append(deal)
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
    DEALS.clear()
    DEALS.extend(rows)
    flash(f"Loaded {len(rows)} deals from {file.filename}.", "ok")
    return redirect(url_for("index"))


@app.post("/clear")
def clear_deals():
    DEALS.clear()
    LAST_RUN.clear()
    flash("Cleared all deals.", "ok")
    return redirect(url_for("index"))


@app.post("/analyse")
def analyse():
    if not DEALS:
        flash("Add or upload some deals first.", "error")
        return redirect(url_for("index"))

    raw_date = request.form.get("review_date") or today().isoformat()
    try:
        review_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError:
        flash("The review date isn't valid. Use the date picker.", "error")
        return redirect(url_for("index"))

    # 1) Logic layer: validate and score with rules.
    results, rejected = analyse_pipeline(DEALS, review_date)
    # 2) AI layer: read notes, verify quotes, adjust within limits.
    ai_status = apply_ai(results, review_date)

    LAST_RUN.clear()
    LAST_RUN.update(
        review_date=review_date,
        results=results,
        rejected=rejected,
        ai=ai_status,
        counts={
            tier: sum(1 for r in results if r["tier"] == tier)
            for tier in ("At Risk", "Watch", "Healthy")
        },
    )
    return redirect(url_for("results"))


@app.route("/results")
def results():
    if not LAST_RUN:
        flash("Run an analysis first.", "error")
        return redirect(url_for("index"))
    return render_template("results.html", run=LAST_RUN)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
