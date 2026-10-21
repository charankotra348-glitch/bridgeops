# BridgeOps

**Turn a messy customer data export into a validated, repeatable import into HubSpot, with humans approving only the uncertain decisions.**

BridgeOps profiles an uploaded CSV/JSON export, maps its columns to HubSpot contact properties, normalizes values (e.g. `"Active-ish"` → `customer`), and gates every decision on confidence: confident decisions run automatically, uncertain ones go to a human review queue.

> Status: **Week 3 of 5**: the full pipeline from upload to HubSpot import works through the API: rules-first mapping, value normalization, duplicate detection, human review, dry run, idempotent batched import with retries and rate limiting, and rollback. An eval harness benchmarks rules vs an LLM (Gemini) vs Jev. The web UI lands in Week 4.

## How it works

```
upload ─▶ Profile ─▶ Map ─▶ Normalize ─▶ Dedupe ─▶ Validate ─▶ Dry run ─▶ Execute ─▶ (Rollback)
                       │        │           │          │                       │
                       ▼        ▼           ▼          ▼                       ▼
          Decision layer: rules first,              live HubSpot        batched upsert by email,
          then a decision model (Jev or Gemini)     property defs       rate-limited, retried
                       │
                       ▼  confidence below threshold
                Human review queue  ──▶  every change written to the audit log
```

- **Rules first.** Deterministic rules (synonyms, abbreviations, value patterns, type checks) handle what they are sure of. A Y/N column named `email_opt_in` is *not* mapped to `email`, because its values don't look like emails.
- **Models only for judgment calls.** Whatever the rules leave open goes to a `DecisionProvider`. The pipeline never calls a model directly, so Jev, an LLM (Gemini), or "abstain and ask a human" is a config switch (`DECISION_PROVIDER=jev|gemini|rules`).
- **Duplicates the same way.** Same email (any case) or same phone + last name is a certain duplicate. Rows that only share a name, or a last name and company, become a yes/no "same person?" question for the decision model. Detection only flags; nothing is merged without review.
- **Confidence gating.** Each decision is auto-accepted only above its threshold: 0.80 for mappings, 0.90 for values, 0.85 for duplicates. Values were raised from 0.85 after the Gemini sweep (below).
- **Measured, not guessed.** Every model request records its latency and the token counts the provider reported. Missing counts stay missing, and cost is always labeled an estimate.
- **Fail safe.** If the model times out, errors, or answers outside the offered options, the decision abstains and goes to review. It is never auto-accepted.
- **Nothing imports until review is done.** Execution refuses (409, with the list of blockers) while any mapping, value or duplicate group is pending. A dry run works at any time and shows, per row, whether it would be created, updated, skipped as unchanged, merged into a duplicate, or rejected as invalid.
- **Validated against the real account.** Before import, every record is checked against HubSpot's live contact property definitions: the property must exist and be writable, enum values must be real options in that account, and every record needs a valid email (the import upserts by email).
- **Safe to run twice.** Writes are batch upserts keyed by email, so a re-run never creates duplicate contacts. Rows already imported with the same values are skipped, rows whose values already match HubSpot are not written at all, and failed rows are retried.
- **One bad record doesn't sink a batch.** HubSpot rejects a whole batch of up to 100 for one invalid value; the client splits the batch until the bad record is isolated, so the rest import and each failure is pinned to its own row.
- **Within rate limits by design.** A client-side sliding window keeps requests under the private-app limit (100 per 10 seconds on Free/Starter; default 90), and 429/5xx responses are retried with backoff.
- **Reversible.** Before writing, the current values of every property about to change are snapshotted. Rollback archives the contacts the job created and restores the ones it updated, except contacts someone has changed since the import, which are left alone and reported as conflicts. HubSpot only moves lifecycle stage forward through the API, so restoring an earlier stage clears it first.

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
curl -X POST localhost:8000/jobs/<job_id>/dry-run                              # -> what an import would do
curl -X POST localhost:8000/jobs/<job_id>/execute                              # -> import (needs HUBSPOT_TOKEN)
curl -X POST localhost:8000/jobs/<job_id>/rollback                             # -> undo it
```

### Checking against a real HubSpot account

Create a free HubSpot developer **test** account and a private app in it with the scopes `crm.objects.contacts.read`, `crm.objects.contacts.write` and `crm.schemas.contacts.read`. Put its token in `.env` as `HUBSPOT_TOKEN`, then:

```bash
python scripts/live_import_check.py
```

It runs the whole flow on throwaway contacts (`@bridgeops-check.example`): dry run, import, re-run (must write nothing), rollback. It confirms each step in HubSpot and cleans up after itself. Never point it at a production portal.

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
| `POST` | `/jobs/{id}/dry-run` | Per-row preview of the import; never writes. Works without a HubSpot token (built-in definitions) |
| `POST` | `/jobs/{id}/execute` | Import into HubSpot. 409 with blockers while review is unfinished; safe to re-run (audited) |
| `POST` | `/jobs/{id}/rollback` | Archive created contacts, restore updated ones, skip ones changed since (audited) |
| `GET` | `/jobs/{id}/records` | What happened to each row (`?status=failed`), with the payload sent and the values it replaced |
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

### Results (77 decisions, October 2026)

Coverage / precision of auto-accepted decisions:

| Provider | Column mapping (47) | Value normalization (20) | Duplicate pairs (10) | **All (77)** | Wrong auto-accepts | Model requests | Cost per run |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Rules only | 72.3% / 100% | 45.0% / 100% | 30.0% / 100% | **59.7% / 100%** | 0 | 0 | — |
| Rules + Gemini 3.5 Flash-Lite | 100% / 100% | 70.0% / 100% | 80.0% / 100% | **89.6% / 100%** | 0 | 9 (855 ms mean) | ~$0.005 (est.) |
| Rules + Jev | — | — | — | — | — | — | waiting on API key |

Gemini runs measured at thresholds 0.80 / 0.85 / 0.85. The sweep gives identical coverage and precision with values at 0.90, which is now the default. Gemini used 8,702 input and 1,063 output tokens across 31 questions.

What the numbers do and don't show:

- **The LLM raises coverage by 30 points without a wrong auto-accept** on this set. The rules alone never guess, so their 100% precision comes from sending 40% of decisions to a human.
- **The margin is thin.** Gemini's two wrong answers ("Working - Contacted" → opportunity, "Hot" → lead) were both rated 0.80 confidence. They went to review only because the value threshold was above 0.80, which is why it is now 0.90.
- **Self-reported confidence is coarse.** Most of Gemini's uncertain answers are exactly 0.80. A provider with calibrated probabilities (the case for Jev) should separate right from wrong answers more cleanly. The Jev run tests that.
- **The duplicate sweep's 0.70 was not adopted.** It comes from 10 pairs, and a different-people pair sits exactly at 0.70.
- **Small sample.** 0 errors in 69 auto-accepts bounds true precision at roughly ≥96% (95% confidence, rule of three). Lifecycle labels such as "Hot" or "hot prospect" are judgment calls. The held-out benchmark in Week 5 is the real test.

CI fails the build if rules-only auto-accept precision falls below 95%.

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
    hubspot.py           HubSpot client: batches, rate limiter, retries, bad-record isolation
    execution/
      plan.py            reviewed decisions -> validated HubSpot records (pure, no network)
      run.py             dry run, idempotent import, rollback with conflict detection
  tests/                 54 tests, incl. an in-memory fake HubSpot (tests/fake_hubspot.py)
scripts/
  live_import_check.py   end-to-end check against a real HubSpot test account
eval/                    labeled datasets, benchmark runner, threshold sweep, comparison report
```

## Roadmap

- [x] **Week 1**: upload, profiling, rules baseline, review API, audit log, eval harness, CI
- [ ] **Week 2**: LLM baseline provider ✅, duplicate detection ✅, threshold sweep ✅, Alembic ✅, Jev run live and benchmarked against Gemini (waiting on API key)
- [x] **Week 3**: transforms, validation against live HubSpot property definitions, idempotent batched execution, retries, rate limiting, dry run, rollback
- [ ] **Week 4**: React UI (mapping editor, review queue, job progress), auth, observability
- [ ] **Week 5**: final benchmark on held-out data, deployment, demo

## Known limits

- **Imports run inside the request.** That suits exports of a few thousand rows (a few dozen batch requests). Larger files need a background worker, and Redis is already in the stack for it. Import speed against a real account is still to be measured.
- **Contacts only.** Companies, deals and associations are out of scope.
- **Rollback restores, it doesn't time-travel.** It reverts only the properties this job wrote, and leaves alone any contact that changed after the import.
- **Matching HubSpot's normalization.** "Unchanged" and conflict checks compare values ignoring case and surrounding spaces. If HubSpot reformats a value in another way (a phone number, say), that record is reported as updated rather than unchanged.
