"""Benchmark the mapping pipeline against hand-labeled exports.

    python eval/run_eval.py --provider rules
    python eval/run_eval.py --provider gemini         # needs GEMINI_API_KEY
    python eval/run_eval.py --provider jev            # needs TYPESAFE_API_KEY
    python eval/run_eval.py --provider gemini --sweep # also sweep auto-accept thresholds
    python eval/run_eval.py --provider rules --min-precision 0.95   # CI gate
    python eval/run_eval.py --provider gemini --set heldout          # held-out benchmark

For every decision (column mapping, enum value, or duplicate pair) it records whether the
pipeline auto-accepted it and whether the answer matches the label.

  precision = correct auto-accepted / auto-accepted   (can automation be trusted?)
  coverage  = auto-accepted / all decisions          (how much human work is saved?)

It also records every model request the provider actually sent: count, latency,
and the token counts the provider reported. Cost is an estimate from
eval/prices.json and is left empty when tokens were not reported.

The sweep re-runs the pipeline at each threshold pair. Providers cache answers by
input, so the sweep sends no extra requests. Thresholds picked on this set are
tuned on the same data they are scored on; the held-out benchmark checks them.

--set heldout scores eval/heldout/ instead: exports in naming styles the tuning set
doesn't have, labeled before any run. It uses the configured thresholds as they are and
refuses --sweep, since tuning on held-out data would make it one more tuning set.
"""

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "api"))

from app.config import get_settings  # noqa: E402
from app.decisions import get_provider  # noqa: E402
from app.decisions.factory import PROVIDERS  # noqa: E402
from app.dedupe import find_duplicates  # noqa: E402
from app.mapping.pipeline import plan_mappings  # noqa: E402
from app.profiling import profile_file, read_rows  # noqa: E402

SWEEP = [round(0.50 + 0.05 * i, 2) for i in range(10)]  # 0.50 .. 0.95


SETS = {"tuning": ROOT, "heldout": ROOT / "heldout"}


def load_profiles(labels: dict, base: Path = ROOT) -> dict[str, dict]:
    return {name: profile_file((base / "datasets" / name).read_bytes(), name).to_dict()
            for name in labels if not name.startswith("_")}


def score(labels: dict, profiles: dict, provider, mapping_threshold: float,
          normalize_threshold: float) -> list[dict]:
    rows = []
    for dataset, profile in profiles.items():
        truth = labels[dataset]
        plan = plan_mappings(profile, provider, mapping_threshold, normalize_threshold)

        for col in plan.columns:
            expected = truth["columns"].get(col.column)
            if expected is None:
                continue
            got = col.target or "none"
            rows.append({"dataset": dataset, "kind": "mapping", "item": col.column, "expected": expected,
                         "got": got if col.method != "none" else None, "method": col.method,
                         "confidence": col.confidence, "auto": col.status == "auto",
                         "correct": got == expected and col.method != "none"})

            for value, vd in col.value_map.items():
                exp_value = truth.get("values", {}).get(col.column, {}).get(value)
                if exp_value is None:
                    continue
                rows.append({"dataset": dataset, "kind": "value", "item": f"{col.column}={value}",
                             "expected": exp_value, "got": vd.target, "method": vd.method,
                             "confidence": vd.confidence, "auto": vd.status == "auto",
                             "correct": vd.target == exp_value})
    return rows


def load_dedupe(base: Path = ROOT) -> tuple[dict, dict[str, list[dict]]]:
    labels = json.loads((base / "dedupe_labels.json").read_text())
    data = {name: read_rows((base / "datasets" / "dedupe" / name).read_bytes(), name)[1]
            for name in labels if not name.startswith("_")}
    return labels, data


def score_dedupe(labels: dict, data: dict, provider, threshold: float) -> list[dict]:
    """One row per pair the detector flagged, plus one per labeled duplicate it missed.

    A missed duplicate counts as an auto-accepted wrong decision: nothing flags it,
    so both rows would be imported as separate contacts.
    """
    rows = []
    for dataset, records in data.items():
        truth = labels[dataset]
        same = {(min(a, b), max(a, b)) for group in truth["same_person"]
                for i, a in enumerate(group) for b in group[i + 1:]}
        res = find_duplicates(records, truth["columns"], provider, threshold)
        flagged = set()
        for g in res.groups:
            for i, a in enumerate(g.rows):
                for b in g.rows[i + 1:]:
                    pair = (a, b)
                    flagged.add(pair)
                    expected = pair in same
                    rows.append({"dataset": dataset, "kind": "duplicate", "item": f"rows {a}+{b}",
                                 "expected": "same" if expected else "different",
                                 "got": None if g.same_person is None else ("same" if g.same_person
                                                                            else "different"),
                                 "method": g.method, "confidence": g.confidence, "auto": g.status == "auto",
                                 "correct": g.same_person is not None and g.same_person == expected})
        for a, b in sorted(same - flagged):
            rows.append({"dataset": dataset, "kind": "duplicate", "item": f"rows {a}+{b}", "expected": "same",
                         "got": "different", "method": "missed", "confidence": 0.0, "auto": True,
                         "correct": False})
    return rows


def summarize(rows: list[dict]) -> dict:
    def stats(subset):
        auto = [r for r in subset if r["auto"]]
        correct_auto = [r for r in auto if r["correct"]]
        return {
            "decisions": len(subset),
            "auto": len(auto),
            "coverage": round(len(auto) / len(subset), 3) if subset else 0.0,
            "precision": round(len(correct_auto) / len(auto), 3) if auto else 1.0,
            "answered_correctly": sum(r["correct"] for r in subset),  # incl. pending, if a model answered
        }

    return {"all": stats(rows), "mapping": stats([r for r in rows if r["kind"] == "mapping"]),
            "value": stats([r for r in rows if r["kind"] == "value"]),
            "duplicate": stats([r for r in rows if r["kind"] == "duplicate"])}


def usage_summary(usage: list, prices: dict) -> dict:
    calls = [asdict(u) for u in usage]
    reported = [c for c in calls if c["input_tokens"] is not None and c["output_tokens"] is not None]
    total_ms = sum(c["latency_ms"] for c in calls)
    out = {
        "requests": len(calls),
        "failed_requests": sum(c["error"] is not None for c in calls),
        "questions_sent": sum(c["questions"] for c in calls),
        "total_request_ms": round(total_ms, 1),
        "mean_request_ms": round(total_ms / len(calls), 1) if calls else None,
        "input_tokens": sum(c["input_tokens"] for c in reported) if reported else None,
        "output_tokens": sum(c["output_tokens"] for c in reported) if reported else None,
        "requests_without_token_counts": len(calls) - len(reported),
        "estimated_cost_usd": None,
        "price_table_version": None,
    }
    priced = [c for c in reported if c["model"] in prices["models"]]
    if calls and len(priced) == len(calls):
        usd = sum(c["input_tokens"] * prices["models"][c["model"]]["input_per_million"]
                  + c["output_tokens"] * prices["models"][c["model"]]["output_per_million"]
                  for c in priced) / 1_000_000
        out["estimated_cost_usd"] = round(usd, 6)
        out["price_table_version"] = prices["version"]
    return out


def sweep_duplicates(dlabels, ddata, provider, min_precision: float) -> dict:
    grid = [{"duplicate_threshold": t, **summarize(score_dedupe(dlabels, ddata, provider, t))["duplicate"]}
            for t in SWEEP]
    ok = [g for g in grid if g["precision"] >= min_precision]
    best = max(ok, key=lambda g: (g["coverage"], g["duplicate_threshold"]), default=None)
    return {"min_precision": min_precision, "best": best, "grid": grid}


def sweep(labels, profiles, provider, min_precision: float) -> dict:
    grid = []
    for mt in SWEEP:
        for nt in SWEEP:
            s = summarize(score(labels, profiles, provider, mt, nt))["all"]
            grid.append({"mapping_threshold": mt, "normalize_threshold": nt, **s})
    ok = [g for g in grid if g["precision"] >= min_precision]
    # Most coverage at the required precision; ties go to the stricter thresholds.
    best = max(ok, key=lambda g: (g["coverage"], g["mapping_threshold"] + g["normalize_threshold"]),
               default=None)
    return {"min_precision": min_precision, "best": best, "grid": grid}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="rules", choices=PROVIDERS)
    ap.add_argument("--mapping-threshold", type=float, default=None)
    ap.add_argument("--normalize-threshold", type=float, default=None)
    ap.add_argument("--duplicate-threshold", type=float, default=None)
    ap.add_argument("--min-precision", type=float, default=None, help="exit 1 if auto precision is lower")
    ap.add_argument("--sweep", action="store_true", help="sweep both thresholds (no extra requests)")
    ap.add_argument("--sweep-precision", type=float, default=0.98,
                    help="precision the sweep must keep when picking thresholds")
    ap.add_argument("--set", dest="eval_set", default="tuning", choices=list(SETS),
                    help="tuning (default) or heldout")
    ap.add_argument("--out", default=str(ROOT / "results"))
    args = ap.parse_args()
    if args.eval_set == "heldout" and args.sweep:
        ap.error("--sweep is not allowed on the held-out set: thresholds are chosen on the tuning set only")
    base = SETS[args.eval_set]

    settings = get_settings()
    mt = args.mapping_threshold if args.mapping_threshold is not None else settings.mapping_threshold
    nt = args.normalize_threshold if args.normalize_threshold is not None else settings.normalize_threshold
    dt = args.duplicate_threshold if args.duplicate_threshold is not None else settings.duplicate_threshold

    labels = json.loads((base / "labels.json").read_text())
    prices = json.loads((ROOT / "prices.json").read_text())
    profiles = load_profiles(labels, base)
    dlabels, ddata = load_dedupe(base)
    provider = get_provider(settings, args.provider)

    start = time.perf_counter()
    rows = score(labels, profiles, provider, mt, nt) + score_dedupe(dlabels, ddata, provider, dt)
    elapsed = round(time.perf_counter() - start, 2)
    summary = summarize(rows)
    usage = usage_summary(provider.usage, prices)

    print(f"Set: {args.eval_set}   Provider: {provider.name}   thresholds: mapping {mt:.2f}, values {nt:.2f}, "
          f"duplicates {dt:.2f}   ({elapsed} s)\n")
    print(f"{'':10} {'decisions':>9} {'auto':>6} {'coverage':>9} {'precision':>10}")
    for name in ("mapping", "value", "duplicate", "all"):
        s = summary[name]
        print(f"{name:10} {s['decisions']:>9} {s['auto']:>6} {s['coverage']:>9.1%} {s['precision']:>10.1%}")

    if usage["requests"]:
        cost = (f"${usage['estimated_cost_usd']:.6f} (estimate, prices {usage['price_table_version']})"
                if usage["estimated_cost_usd"] is not None else "not available")
        tokens = (f"{usage['input_tokens']} in / {usage['output_tokens']} out"
                  if usage["input_tokens"] is not None else "not reported")
        print(f"\nModel requests: {usage['requests']} ({usage['failed_requests']} failed), "
              f"{usage['questions_sent']} questions, mean {usage['mean_request_ms']} ms per request")
        print(f"Tokens: {tokens}   Cost: {cost}")
        if usage["failed_requests"]:
            print("WARNING: some requests failed; their decisions abstained and went to review.")

    wrong_auto = [r for r in rows if r["auto"] and not r["correct"]]
    if wrong_auto:
        print("\nAuto-accepted but WRONG (fix these first):")
        for r in wrong_auto:
            print(f"  [{r['dataset']}] {r['item']}: expected {r['expected']}, got {r['got']} "
                  f"({r['method']}, conf {r['confidence']:.2f})")

    pending = [r for r in rows if not r["auto"]]
    print(f"\nSent to human review: {len(pending)}")
    for r in pending:
        modeled = r["method"] not in ("none", "rule")
        got = f", model said {r['got']} @ {r['confidence']:.2f}" if modeled else ""
        print(f"  [{r['dataset']}] {r['item']}  (expected {r['expected']}{got})")

    result = {"set": args.eval_set, "provider": provider.name, "model": getattr(provider, "model", None), "elapsed_s": elapsed,
              "thresholds": {"mapping": mt, "normalize": nt, "duplicate": dt}, "summary": summary,
              "usage": usage, "calls": [asdict(u) for u in provider.usage], "rows": rows}

    if args.sweep:
        result["sweep"] = sweep(labels, profiles, provider, args.sweep_precision)
        result["sweep_duplicates"] = sweep_duplicates(dlabels, ddata, provider, args.sweep_precision)
        dbest = result["sweep_duplicates"]["best"]
        if dbest:
            print(f"\nSweep (duplicates): best threshold {dbest['duplicate_threshold']:.2f} -> coverage "
                  f"{dbest['coverage']:.1%}, precision {dbest['precision']:.1%}")
        else:
            print(f"\nSweep (duplicates): no threshold reaches precision {args.sweep_precision:.0%}")
        best = result["sweep"]["best"]
        if best:
            print(f"\nSweep: best thresholds at precision >= {args.sweep_precision:.0%}: "
                  f"mapping {best['mapping_threshold']:.2f}, values {best['normalize_threshold']:.2f} "
                  f"-> coverage {best['coverage']:.1%}, precision {best['precision']:.1%} "
                  f"(tuned on this set; confirm on held-out data)")
        else:
            print(f"\nSweep: no threshold pair reaches precision {args.sweep_precision:.0%}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    prefix = "eval" if args.eval_set == "tuning" else args.eval_set
    path = out / f"{prefix}_{provider.name}.json"
    path.write_text(json.dumps(result, indent=2))
    print(f"\nSaved {path}")

    if args.min_precision is not None and summary["all"]["precision"] < args.min_precision:
        print(f"FAIL: precision {summary['all']['precision']:.1%} < {args.min_precision:.1%}")
        sys.exit(1)


if __name__ == "__main__":
    main()
