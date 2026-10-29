"""Side-by-side comparison of every saved eval run (rules vs gemini vs jev).

    python eval/compare.py                      # prints markdown
    python eval/compare.py --write eval/results/comparison.md
    python eval/compare.py --set heldout        # the held-out benchmark

Reads eval/results/eval_<provider>.json (or heldout_<provider>.json), written by run_eval.py.
"""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ORDER = ["rules", "gemini", "jev"]


def pct(x: float) -> str:
    return f"{x:.1%}"


def build(results_dir: Path, prefix: str = "eval") -> str:
    runs = {}
    for path in results_dir.glob(f"{prefix}_*.json"):
        data = json.loads(path.read_text())
        runs[data["provider"]] = data
    if not runs:
        return "No eval results yet. Run eval/run_eval.py first.\n"

    names = sorted(runs, key=lambda n: ORDER.index(n) if n in ORDER else len(ORDER))
    lines = [
        "| Provider | Thresholds (map / value / dup) | Coverage | Precision | Mapping cov / prec | "
        "Value cov / prec | Duplicate cov / prec | Wrong auto-accepts | Requests | Mean request | "
        "Tokens in / out | Est. cost |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name in names:
        r = runs[name]
        s, u, t = r["summary"], r.get("usage", {}), r.get("thresholds", {})
        wrong = sum(1 for row in r["rows"] if row["auto"] and not row["correct"])
        tokens = (f"{u['input_tokens']} / {u['output_tokens']}"
                  if u.get("input_tokens") is not None else "—")
        cost = f"${u['estimated_cost_usd']:.4f}" if u.get("estimated_cost_usd") is not None else "—"
        mean = f"{u['mean_request_ms']:.0f} ms" if u.get("mean_request_ms") else "—"
        label = name + (f" ({r['model']})" if r.get("model") else "")
        dup = s.get("duplicate") or {"coverage": 0.0, "precision": 1.0}
        lines.append(
            f"| {label} | {t.get('mapping', 0):.2f} / {t.get('normalize', 0):.2f} / "
            f"{t.get('duplicate', 0):.2f} | "
            f"{pct(s['all']['coverage'])} | {pct(s['all']['precision'])} | "
            f"{pct(s['mapping']['coverage'])} / {pct(s['mapping']['precision'])} | "
            f"{pct(s['value']['coverage'])} / {pct(s['value']['precision'])} | "
            f"{pct(dup['coverage'])} / {pct(dup['precision'])} | {wrong} | "
            f"{u.get('requests', 0)} | {mean} | {tokens} | {cost} |"
        )

    notes = ["", f"Decisions scored per run: {runs[names[0]]['summary']['all']['decisions']}. "
             "Costs are estimates from eval/prices.json; '—' means not reported or not applicable."]
    for name in names:
        best = (runs[name].get("sweep") or {}).get("best")
        if best:
            notes.append(f"- {name}: sweep best at precision >= {runs[name]['sweep']['min_precision']:.0%} "
                         f"is {best['mapping_threshold']:.2f} / {best['normalize_threshold']:.2f} "
                         f"(coverage {pct(best['coverage'])}), tuned on this set.")
    return "\n".join(lines + notes) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=str(ROOT / "results"))
    ap.add_argument("--write", default=None)
    ap.add_argument("--set", dest="eval_set", default="tuning", choices=["tuning", "heldout"])
    args = ap.parse_args()
    report = build(Path(args.results), "eval" if args.eval_set == "tuning" else "heldout")
    print(report)
    if args.write:
        Path(args.write).write_text(report)


if __name__ == "__main__":
    main()
