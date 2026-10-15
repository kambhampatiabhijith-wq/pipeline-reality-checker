# Pipeline Reality Checker

A tool for B2B sales managers that shows which open deals are likely to slip, why, and what to do next.

Sales pipelines often look healthy while revenue falls short, because a CRM stage records what the rep last updated, not what the buyer is doing. This tool checks each deal against fixed rules (inactivity, time stuck in a stage, missed close dates) and uses an LLM only to read deal notes for signals the rules can't see, such as a budget freeze or a competitor in the picture.

## How it works

1. **Input:** add deals through a form or upload a CSV.
2. **Validation:** bad rows are rejected with a plain-English reason (for example, an activity date in the future).
3. **Logic layer** (`logic.py`): rules score each deal from 0 to 100 and assign a tier: Healthy, Watch or At Risk. Every point is explained on screen.
4. **AI layer** (`ai.py`): one batched call to Groq reads the notes. Every signal the AI reports must quote the notes, and the code checks each quote word for word against the original. It can add up to 30 points or remove 25, but it can never push a deal to At Risk on its own.
5. **Confidence:** each flag is marked High, Medium or Low depending on how well the rules and the notes agree.
6. **Storage** (`db.py`, SQLite): every run is saved. Managers record what happened to each deal (Won, Lost, Slipped, Still open) and can mark a flag "not at risk" with a reason.
7. **Admin dashboard:** flag accuracy against recorded outcomes, override rate, signals by reason, risk mix by run, and runs where the AI was unavailable.

If no API key is set or the AI call fails, the app still runs and shows rules-only results with a clear banner.

## Tech stack

- Python and Flask
- SQLite
- Groq API, model `openai/gpt-oss-120b` (free tier); OpenAI also supported
- Chart.js for the dashboard charts
- Hosted on Replit

## Running it

1. Add your Groq API key in Replit **Secrets** as `GROQ_API_KEY` (or an OpenAI key as `OPENAI_API_KEY`).
2. Run `python main.py`.
3. Upload `sample_deals.csv`, keep the review date at **2026-10-13**, and click **Analyse pipeline**.

Optional settings: `GROQ_MODEL` to change the model, `DB_PATH` to change where the database file is saved.

## Sample data

`sample_deals.csv` has seven deals, each built to test one behaviour. With the review date set to 2026-10-13:

| Deal | Tests | Expected result |
|---|---|---|
| D001 | Healthy baseline | Healthy, 0 |
| D002 | Inactivity plus large-deal rule | Watch, 35 |
| D003 | Two rules plus an unanswered-emails note | At Risk, 65 |
| D004 | Close date passed (strong rule) | At Risk, 50 |
| D005 | Risk only in the notes (budget on hold, competitor) | Watch, 30 |
| D006 | Quiet, but the notes explain why (false-positive check) | Healthy, 0 |
| D007 | Activity date in the future | Rejected by validation |

## Project files

| File | What it does |
|---|---|
| `main.py` | Web pages and routes |
| `logic.py` | Validation and rules-based scoring |
| `ai.py` | Prompt, LLM call, quote checks, fallback |
| `db.py` | Database tables, saving runs, dashboard numbers |
| `templates/`, `static/` | Pages and styles |
| `brief.pdf` | Product brief: problem, persona, JTBD, why AI, features, failure modes, trade-offs, cost, reflection |
