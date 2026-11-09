# ties

A local-first personal relationship graph. This is the **Phase 0 baseline** from
[`docs/personal-graph-plan.md`](../docs/personal-graph-plan.md).

Everything lives in one SQLite file on your machine, which can be encrypted with
SQLCipher. The app runs in a browser tab on `127.0.0.1` and makes no network calls.

## What works today

- **People, organizations and places**, entered by hand. The name field checks for
  possible duplicates as you type (same email, nickname variants such as Bob/Robert,
  same initial and family name, similar spelling). Saving a likely duplicate asks
  first.
- **Facts.** Each fact records how you know it (`observed`, `told`, `heard`, `public`,
  `inferred`), a confidence, and the dates it was true. Facts are never overwritten:
  a correction supersedes the old one, and the full history stays visible.
- **Relationships** between any two entities, readable from both sides. For
  example, "Works at" on a person shows as "Employs" on the organization, and
  "Parent of" shows as "Child of".
- **Interactions.** You can log a past interaction or plan a future one, with
  participants, place, notes, tags, who reached out, and follow-ups ("I owe" or
  "they owe"). A follow-up attaches to the participant named in its text.
- **Contact tracking:**
  - days since contact;
  - how often to be in touch, set by closeness tier (inner 14, close 30, active 60,
    extended 120, wider 365 days) or a custom number of days;
  - next due date, with a note when your actual rhythm differs from the setting;
  - relationship strength (based on interactions, your closeness rating, number of
    contexts, and who usually reaches out).
- **Today** lists:
  - who to reach out to, ranked, with the reason for each;
  - follow-ups due;
  - the next 14 days, including birthdays;
  - planned events that have passed, so you can confirm whether they happened;
  - the last 7 days.

  A planned meeting counts as "covered", so that person isn't also listed as due.
- **Network** list with filters and sorting, a **Timeline**, a **JSON export**, and
  a file **backup**.

## Run it

```bash
cd ties
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[encryption]'      # drop [encryption] if you don't want SQLCipher

ties init --encrypt                 # creates ~/.ties/personal.vault and asks for a passphrase
ties serve --open                   # http://127.0.0.1:8765
```

To try it with sample data first:

```bash
ties --vault /tmp/demo.vault demo
ties --vault /tmp/demo.vault serve --open
```

Other commands:

| Command | What it does |
|---|---|
| `ties backup <folder-or-file>` | Copies the vault file. It stays encrypted. |
| `ties export --out data.json` | Exports everything as JSON. This file is **not** encrypted. |
| `--vault PATH` or `$TIES_VAULT` | Uses a vault other than `~/.ties/personal.vault` |
| `$TIES_PASSPHRASE` | Supplies the passphrase without a prompt (for scripts) |

## Layout

```
src/ties/
  migrations/001_baseline.sql   schema (follows Appendix A of the plan)
  registry.py                   entity types, predicates, tiers and weights
  db.py                         vault file, encryption, migrations, backup
  repo.py                       entities, facts, relationships, events, follow-ups, search
  scoring.py                    days since, cadence, due, strength, Today
  web.py                        FastAPI routes (localhost only)
  templates/, static/           server-rendered pages and a small amount of plain JS
  cli.py, demo.py
tests/                          pytest suite
```

## Tests

```bash
pip install -e '.[dev,encryption]'
pytest
```

## Not built yet

These come in later phases (see section 13 of the plan):

- importing Markdown notes and the other importers;
- the post-meeting capture flow;
- inferences, and a review inbox for confirming them;
- local AI extraction and search;
- the graph view;
- strategic features and the research vault.
