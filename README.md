# BridgeOps

**Turn a messy customer data export into a validated, repeatable import into HubSpot, with humans approving only the uncertain decisions.**

BridgeOps profiles an uploaded CSV/JSON export, maps its columns to HubSpot contact properties, normalizes values (e.g. `"Active-ish"` → `customer`), and gates every decision on confidence: confident decisions run automatically, uncertain ones go to a human review queue.

> Status: **Week 1 of 5**: upload, schema inference, rules-only mapping baseline, review API, eval harness. Decision model (Jev), execution against HubSpot, and the web UI land in Weeks 2–4.

## How it works

```
upload ─▶ Profile ─▶ Map ─▶ Normalize ─▶ Dedupe* ─▶ Validate* ─▶ Execute*
                       │        │           │
                       ▼        ▼           ▼
                Decision layer: rules first, then a decision model (Jev)
                       │
                       ▼  confidence below threshold
                Human review queue  ──▶  every change written to the audit log
                                                          (* = Weeks 2–3)
```

- **Rules first.** Deterministic rules (synonyms, abbreviations, value patterns, type checks) handle what they are sure of. A Y/N column named `email_opt_in` is *not* mapped to `email`, because its values don't look like emails.
- **Models only for judgment calls.** Whatever the rules leave open goes to a `DecisionProvider`. The pipeline never calls a model directly, so Jev, an LLM, or "abstain and ask a human" is a config switch (`DECISION_PROVIDER`).
- **Confidence gating.** Each decision is auto-accepted only above its threshold (0.80 for mappings, 0.85 for values to start; tuned on the eval set).
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

Try it:

```bash
curl -F file=@eval/datasets/abbreviated_legacy.csv localhost:8000/sources      # -> source id + profile
curl -X POST localhost:8000/jobs -H 'content-type: application/json' \
     -d '{"source_id": "<id>"}'                                                # -> mappings + gating
curl localhost:8000/jobs/<job_id>/review                                       # -> what needs a human
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/sources` | Upload CSV/JSON; returns inferred schema (types, nulls, samples, value counts) |
| `GET` | `/sources/{id}` | Stored profile |
| `POST` | `/jobs` | Run mapping + normalization on a source (`provider`: `rules` or `jev`) |
| `GET` | `/jobs/{id}` | Job with every mapping, method, confidence and status |
| `GET` | `/jobs/{id}/review` | Human review queue |
| `PATCH` | `/mappings/{id}` | Approve/reject/correct a mapping or its value map (audited) |
| `GET` | `/healthz` | Liveness |

## Evaluation

Hand-labeled exports in `eval/datasets/` cover four naming styles: Salesforce-style, spreadsheet, abbreviated legacy, and nested JSON. `eval/labels.json` holds the ground truth.

```bash
python eval/run_eval.py --provider rules
python eval/run_eval.py --provider jev        # once TYPESAFE_API_KEY is set
```

- **Precision** = correct auto-accepted / auto-accepted (can the automation be trusted?)
- **Coverage** = auto-accepted / all decisions (how much human work is saved?)

Current results (rules-only baseline, 67 decisions):

| | Decisions | Auto-accepted | Coverage | Precision |
| --- | --- | --- | --- | --- |
| Column mapping | 47 | 34 | 72.3% | 100% |
| Value normalization | 20 | 9 | 45.0% | 100% |
| **All** | **67** | **43** | **64.2%** | **100%** |

The rules never guess, so precision is perfect and the remaining 36% goes to a human. The goal for the decision layer is to **raise coverage without dropping precision**. CI fails the build if auto-accept precision falls below 95%.

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
    decisions/
      base.py            Question / Decision / DecisionProvider interface
      jev.py             Jev provider (batched, cached, fail-soft)
      abstain.py         rules-only baseline provider
    models.py            sources, jobs, mappings, decisions, audit_log
  tests/                 22 tests (profiling, rules, gating, Jev client, API)
eval/                    labeled datasets + benchmark runner
```

## Roadmap

- [x] **Week 1**: upload, profiling, rules baseline, review API, audit log, eval harness, CI
- [ ] **Week 2**: Jev provider live, LLM baseline provider, duplicate detection, threshold tuning, Alembic
- [ ] **Week 3**: transforms, validation against live HubSpot property definitions, idempotent batched execution, retries, rate limiting, dry run, rollback
- [ ] **Week 4**: React UI (mapping editor, review queue, job progress), auth, observability
- [ ] **Week 5**: final benchmark on held-out data, deployment, demo
