"""Benchmark the mapping pipeline against hand-labeled exports.

    python eval/run_eval.py --provider rules
    python eval/run_eval.py --provider jev            # needs TYPESAFE_API_KEY
    python eval/run_eval.py --provider rules --min-precision 0.95   # CI gate

For every decision (column mapping or enum value) it records whether the
pipeline auto-accepted it and whether the answer matches the label.

  precision = correct auto-accepted / auto-accepted   (can automation be trusted?)
  coverage  = auto-accepted / all decisions          (how much human work is saved?)
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "api"))

from app.config import get_settings  # noqa: E402
from app.decisions import get_provider  # noqa: E402
from app.mapping.pipeline import plan_mappings  # noqa: E402
from app.profiling import profile_file  # noqa: E402


def evaluate(provider_name: str, mapping_threshold: float, normalize_threshold: float) -> dict:
    labels = json.loads((ROOT / "labels.json").read_text())
    provider = get_provider(get_settings(), provider_name)
    rows = []
    start = time.perf_counter()

    for dataset, truth in labels.items():
        if dataset.startswith("_"):
            continue
        profile = profile_file((ROOT / "datasets" / dataset).read_bytes(), dataset).to_dict()
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

    return {"provider": provider.name, "elapsed_s": round(time.perf_counter() - start, 2), "rows": rows}


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
            "value": stats([r for r in rows if r["kind"] == "value"])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="rules", choices=["rules", "jev"])
    ap.add_argument("--mapping-threshold", type=float, default=0.80)
    ap.add_argument("--normalize-threshold", type=float, default=0.85)
    ap.add_argument("--min-precision", type=float, default=None, help="exit 1 if auto precision is lower")
    ap.add_argument("--out", default=str(ROOT / "results"))
    args = ap.parse_args()

    result = evaluate(args.provider, args.mapping_threshold, args.normalize_threshold)
    summary = summarize(result["rows"])

    print(f"Provider: {result['provider']}   ({result['elapsed_s']} s)\n")
    print(f"{'':10} {'decisions':>9} {'auto':>6} {'coverage':>9} {'precision':>10}")
    for name in ("mapping", "value", "all"):
        s = summary[name]
        print(f"{name:10} {s['decisions']:>9} {s['auto']:>6} {s['coverage']:>9.1%} {s['precision']:>10.1%}")

    wrong_auto = [r for r in result["rows"] if r["auto"] and not r["correct"]]
    if wrong_auto:
        print("\nAuto-accepted but WRONG (fix these first):")
        for r in wrong_auto:
            print(f"  [{r['dataset']}] {r['item']}: expected {r['expected']}, got {r['got']} "
                  f"({r['method']}, conf {r['confidence']:.2f})")

    pending = [r for r in result["rows"] if not r["auto"]]
    print(f"\nSent to human review: {len(pending)}")
    for r in pending:
        print(f"  [{r['dataset']}] {r['item']}  (expected {r['expected']})")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"eval_{result['provider']}.json"
    path.write_text(json.dumps({**result, "summary": summary}, indent=2))
    print(f"\nSaved {path}")

    if args.min_precision is not None and summary["all"]["precision"] < args.min_precision:
        print(f"FAIL: precision {summary['all']['precision']:.1%} < {args.min_precision:.1%}")
        sys.exit(1)


if __name__ == "__main__":
    main()
