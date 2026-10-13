# Pipeline Reality Checker

A tool for B2B sales managers that shows which open deals are likely to slip, why, and what to do next.

Sales pipelines often look healthy while revenue falls short, because a CRM stage records what the rep last updated, not what the buyer is doing. This tool checks each deal against fixed rules (inactivity, time stuck in a stage, missed close dates) and uses an LLM only to read deal notes for signals the rules can't see, such as a budget freeze or a competitor in the picture.

## How it works

1. **Input:** add deals through a form or upload a CSV.
2. **Validation:** bad rows are rejected with a clear message.
3. **Logic layer:** rules score each deal from 0 to 100 and assign a tier: Healthy, Watch or At Risk.
4. **AI layer:** the LLM reads the notes, quotes its evidence, and writes one reason and one next action per deal. It can add up to 30 points or remove 25, but it can never push a deal to At Risk on its own.
5. **Confidence:** each flag is marked High, Medium or Low depending on how well the rules and the notes agree.
6. **Storage:** every analysis is saved, and the manager can record whether each deal was won, lost or slipped.
7. **Admin dashboard:** shows runs over time, flags by reason, and how often flagged deals actually slipped.

## Tech stack

- Python and Flask
- SQLite
- OpenAI API (LLM layer)
- Chart.js (dashboard charts)
- Hosted on Replit

## Running it

1. Add your OpenAI API key in Replit **Secrets** as `OPENAI_API_KEY`.
2. Click **Run**.
3. Upload `sample_deals.csv` to try it with seven test deals.

If no API key is set or the API call fails, the tool still runs and shows rules-only results.

## Project documents

- `brief.pdf`: product brief covering the problem, persona, JTBD, why AI, features, failure modes, trade-offs and cost.
