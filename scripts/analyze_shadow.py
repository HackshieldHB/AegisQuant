import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from AegisQuantConfig import CONFIG


def main() -> int:
    summary_path = os.path.join(
        CONFIG["REPORTING"]["LOG_DIR"], "shadow_summary.json"
    )
    try:
        with open(summary_path, encoding="utf-8") as handle:
            summary = json.load(handle)
    except FileNotFoundError:
        print("No shadow summary yet. Let the engine collect forward outcomes first.")
        return 1

    print(
        f"Completed={summary['completed_samples']} "
        f"Pending={summary['pending_samples']} "
        f"Horizon={summary['horizon_bars']} bars"
    )
    print("threshold samples win_rate expectancy profit_factor brier")
    for row in summary["thresholds"]:
        profit_factor = row["profit_factor"]
        print(
            f"{row['threshold']:.2f} "
            f"{row['samples']:7d} "
            f"{row['win_rate']:8.2%} "
            f"{row['expectancy']:10.4%} "
            f"{profit_factor if profit_factor is not None else 0.0:13.3f} "
            f"{row['brier_score'] if row['brier_score'] is not None else 0.0:.4f}"
        )
    if summary["recommendation_ready"]:
        print(f"Recommended threshold: {summary['recommended_threshold']:.2f}")
        return 0
    print(
        "No threshold recommendation yet. More samples or positive net expectancy required."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
