# BridgeOps

**Turn a messy customer data export into a validated, repeatable import into HubSpot, with humans approving only the uncertain decisions.**

BridgeOps profiles an uploaded CSV/JSON export, maps its columns to HubSpot contact properties, normalizes values (e.g. `"Active-ish"` → `customer`), and gates every decision on confidence: confident decisions run automatically, uncertain ones go to a human review queue.

> Status: **Week 2 of 5**: upload, schema inference, rules-first mapping, value normalization, duplicate detection, review API, Alembic migrations, and an eval harness that benchmarks rules vs an LLM (Gemini) vs Jev. Execution against HubSpot and the web UI land in Weeks 3–4.

## How it works

```
upload ─▶ Profile ─▶ Map ─▶ Normalize ─▶ Dedupe ─▶ Validate* ─▶ Execute*
                       │        │           │
                       ▼        ▼           ▼
          Decision layer: rules first, then a decision model (Jev or Gemini)
                       │
                       ▼  confidence below threshold
                Human review queue  ──▶  every change written to the audit log
                                                          (* = Week 3)
```

- **Rules first.** Deterministic rules (synonyms, abbreviations, value patterns, type checks) handle what they are sure of. A Y/N column named `email_opt_in` is *not* mapped to `email`, because its values don't look like emails.
- **Models only for judgment calls.** Whatever the rules leave open goes to a `DecisionProvider`. The pipeline never calls a model directly, so Jev, an LLM (Gemini), or "abstain and ask a human" is a config switch (`DECISION_PROVIDER=jev|gemini|rules`).
- **Duplicates the same way.** Same email (any case) or same phone + last name is a certain duplicate. Rows that only share a name, or a last name and company, become a yes/no "same person?" question for the decision model. Detection only flags; nothing is merged without review.
- **Confidence gating.** Each decision is auto-accepted only above its threshold (0.80 for mappings, 0.85 for values and duplicates to start; tuned on the eval set).
- **Measured, not guessed.** Every model request records its latency and the token counts the provider reported. Missing counts stay missing, and cost is always labeled an estimate.
- **Fail safe.** If the model times out, errors, or answers outside the offered options, the decision abstains and goes to review. It is never auto-accepted.

## Quick start

```bash
cp .env.example .env
docker compose up --build          # API on http://localhost:8000, docs at /docs
```

Without Docker (SQLite):

```bash
cd api
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload
```

The API applies Alembic migrations on startup (`AUTO_MIGRATE=true`). To run them separately: `cd api && python -m app.migrate`. After changing a model: `alembic revision --autogenerate -m "..."`; a test fails if models and migrations drift apart.

Try it:

```bash
curl -F file=@eval/datasets/abbreviated_legacy.csv localhost:8000/sources      # -> source id + profile
curl -X POST localhost:8000/jobs -H 'content-type: application/json' \
     -d '{"source_id": "<id>"}'                                                # -> mappings + gating
curl localhost:8000/jobs/<job_id>/review                                       # -> what needs a human
curl localhost:8000/jobs/<job_id>/duplicates                                   # -> rows that look like one person
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/sources` | Upload CSV/JSON; returns inferred schema (types, nulls, samples, value counts) |
| `GET` | `/sources/{id}` | Stored profile |
| `POST` | `/jobs` | Run mapping, normalization and duplicate detection (`provider`: `rules`, `gemini` or `jev`) |
| `GET` | `/jobs/{id}` | Job with every mapping, method, confidence and status |
| `GET` | `/jobs/{id}/review` | Human review queue |
| `PATCH` | `/mappings/{id}` | Approve/reject/correct a mapping or its value map (audited) |
| `GET` | `/jobs/{id}/duplicates` | Duplicate groups with the rows side by side (`?pending_only=true`) |
| `PATCH` | `/duplicates/{id}` | Approve (same person, import `keep_row` only) or reject (different people) (audited) |
| `GET` | `/healthz` | Liveness |

## Evaluation

Hand-labeled exports in `eval/datasets/` cover four naming styles: Salesforce-style, spreadsheet, abbreviated legacy, and nested JSON. `eval/labels.json` holds the ground truth. `eval/datasets/dedupe/` is a contact list with planted duplicates (exact repeats, case-changed emails, reformatted phones, nicknames) and look-alikes that are different people; `eval/dedupe_labels.json` holds its ground truth.

```bash
python eval/run_eval.py --provider rules
python eval/run_eval.py --provider gemini --sweep   # needs GEMINI_API_KEY
python eval/run_eval.py --provider jev --sweep      # needs TYPESAFE_API_KEY
python eval/compare.py                              # side-by-side table of every saved run
```

- **Precision** = correct auto-accepted / auto-accepted (can the automation be trusted?)
- **Coverage** = auto-accepted / all decisions (how much human work is saved?)
- A labeled duplicate the detector never flags counts as an auto-accepted wrong decision, since both rows would be imported.
- Each run also reports model requests, mean request latency, reported tokens and an estimated cost (`eval/prices.json`).
- `--sweep` re-scores every threshold from 0.50 to 0.95 and reports the setting with the most coverage at ≥98% precision. Providers cache answers, so the sweep sends no extra requests. Thresholds picked this way are tuned on the data they are scored on; the held-out benchmark in Week 5 checks them.

Current results (rules-only baseline, 77 decisions):

| | Decisions | Auto-accepted | Coverage | Precision |
| --- | --- | --- | --- | --- |
| Column mapping | 47 | 34 | 72.3% | 100% |
| Value normalization | 20 | 9 | 45.0% | 100% |
| Duplicate pairs | 10 | 3 | 30.0% | 100% |
| **All** | **77** | **46** | **59.7%** | **100%** |

The rules never guess, so precision is perfect and the remaining 40% goes to a human. The goal for the decision layer is to **raise coverage without dropping precision**. Gemini and Jev results get added here once they are run with real keys. CI fails the build if auto-accept precision falls below 95%.

## Project layout

```
api/
  app/
    main.py              FastAPI routes
    profiling.py         schema inference (deterministic)
    mapping/
      target_schema.py   HubSpot contact properties, synonyms, enum options
      rules.py           rules-only mapper + value normalizer
      pipeline.py        rules → decision provider → confidence gating
    dedupe.py            duplicate detection: rules → decision provider → gating
    decisions/
      base.py            Question / Decision / DecisionProvider / CallUsage interface
      jev.py             Jev provider (batched, cached, fail-soft)
      gemini.py          Gemini LLM baseline (batched JSON, validated, cached, fail-soft)
      abstain.py         rules-only baseline provider
    models.py            sources, jobs, mappings, decisions, duplicate_groups, audit_log
    migrate.py           runs Alembic migrations
  migrations/            Alembic revisions
  tests/                 40 tests (profiling, rules, gating, providers, dedupe, migrations, API)
eval/                    labeled datasets, benchmark runner, threshold sweep, comparison report
```

## Roadmap

- [x] **Week 1**: upload, profiling, rules baseline, review API, audit log, eval harness, CI
- [ ] **Week 2**: LLM baseline provider ✅, duplicate detection ✅, threshold sweep ✅, Alembic ✅, Jev run live and benchmarked against Gemini (waiting on API key)
- [ ] **Week 3**: transforms, validation against live HubSpot property definitions, idempotent batched execution, retries, rate limiting, dry run, rollback
- [ ] **Week 4**: React UI (mapping editor, review queue, job progress), auth, observability
- [ ] **Week 5**: final benchmark on held-out data, deployment, demo
