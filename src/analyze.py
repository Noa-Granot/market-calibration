"""
analyze.py — analysis stage for market-calibration.

Produces the calibration figures from the SQLite database built by load.py.

Two things the data forced:

  1. Error bars are not optional. The 90-day sample has 407 markets in the
     bottom decile and 18 in the 70-80% decile. A point estimate from 18
     observations looks the same on a chart as one from 407, and it is not
     the same claim. Every rate here carries a Wilson score interval.

  2. Markets are clustered by event. 1,295 markets span only 601 events —
     a single election produced separate Labour / Conservative / LibDem
     markets on the same underlying question. Those are not independent
     observations, so every figure can be produced either on all markets or
     on one market per event, and both are reported.

Usage:
    python src/analyze.py                 # all charts to data/figures/
    python src/analyze.py --dedupe-events # one market per event
"""

from __future__ import annotations

import argparse
import math
import sqlite3
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "market_calibration.sqlite3"
FIGURES_DIR = PROJECT_ROOT / "data" / "figures"

HORIZONS = (90, 30, 7, 1)
N_BUCKETS = 10


# --------------------------------------------------------------------------- #
# statistics
# --------------------------------------------------------------------------- #

def wilson_interval(successes: int, n: int, z: float = 1.96):
    """Wilson score interval for a binomial proportion.

    Chosen over the normal approximation because several buckets have fewer
    than 30 observations and rates near 0 or 1, where the normal interval
    gives bounds outside [0, 1].
    """
    if n == 0:
        return 0.0, 0.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)


# --------------------------------------------------------------------------- #
# data access
# --------------------------------------------------------------------------- #

def load_horizons(dedupe_events: bool = False) -> pd.DataFrame:
    """The analysis table: one row per (market, horizon).

    With dedupe_events, keeps the highest-volume market from each event, so
    that one election does not contribute several correlated observations.
    """
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """SELECT m.id            AS market_id,
                  m.question,
                  m.event_id,
                  m.end_date,
                  m.volume,
                  m.outcome_yes_won,
                  h.horizon_days,
                  h.price_yes
           FROM horizons h
           JOIN markets m ON m.id = h.market_id""",
        conn,
    )
    conn.close()

    if dedupe_events:
        before = df["market_id"].nunique()
        keep = (
            df[df["event_id"] != ""]
            .sort_values("volume", ascending=False)
            .drop_duplicates("event_id")["market_id"]
        )
        unclustered = df[df["event_id"] == ""]["market_id"]
        df = df[df["market_id"].isin(set(keep) | set(unclustered))]
        print(f"deduped by event: {before} markets -> {df['market_id'].nunique()}")

    return df


def calibration_table(df: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Bucket prices into deciles and compare predicted against actual."""
    sub = df[df["horizon_days"] == horizon].copy()
    if sub.empty:
        return pd.DataFrame()

    # clip so that a price of exactly 1.0 lands in the top bucket, not an 11th
    sub["bucket"] = (sub["price_yes"] * N_BUCKETS).astype(int).clip(0, N_BUCKETS - 1)

    rows = []
    for bucket, group in sub.groupby("bucket"):
        n = len(group)
        successes = int(group["outcome_yes_won"].sum())
        lo, hi = wilson_interval(successes, n)
        rows.append({
            "bucket": bucket,
            "bucket_label": f"{bucket * 10}-{(bucket + 1) * 10}%",
            "n": n,
            "predicted": group["price_yes"].mean(),
            "actual": successes / n,
            "ci_lo": lo,
            "ci_hi": hi,
        })
    return pd.DataFrame(rows).sort_values("bucket").reset_index(drop=True)


def brier_score(df: pd.DataFrame, horizon: int):
    """Mean squared error of the price as a probability forecast.

    Lower is better. Useful alongside calibration because a market can be
    well calibrated on average while being uninformative — a forecast of the
    base rate for everything is perfectly calibrated and useless.
    """
    sub = df[df["horizon_days"] == horizon]
    if sub.empty:
        return None, None
    brier = ((sub["price_yes"] - sub["outcome_yes_won"]) ** 2).mean()
    base_rate = sub["outcome_yes_won"].mean()
    baseline = ((base_rate - sub["outcome_yes_won"]) ** 2).mean()
    return brier, baseline


# --------------------------------------------------------------------------- #
# figures
# --------------------------------------------------------------------------- #

def plot_calibration_grid(df: pd.DataFrame, path: Path) -> None:
    """One panel per horizon: predicted vs actual, with Wilson intervals."""
    fig, axes = plt.subplots(2, 2, figsize=(11, 9), sharex=True, sharey=True)

    for ax, horizon in zip(axes.flat, HORIZONS):
        table = calibration_table(df, horizon)
        if table.empty:
            ax.set_title(f"{horizon} days — no data")
            continue

        ax.plot([0, 1], [0, 1], color="#999", linestyle="--", linewidth=1,
                label="perfect calibration", zorder=1)

        yerr = [
            (table["actual"] - table["ci_lo"]).clip(lower=0),
            (table["ci_hi"] - table["actual"]).clip(lower=0),
        ]
        ax.errorbar(table["predicted"], table["actual"], yerr=yerr,
                    fmt="o-", color="#2b6cb0", ecolor="#a0aec0",
                    capsize=3, markersize=5, linewidth=1.5, zorder=3)

        # marker area proportional to sample size, so thin buckets look thin
        ax.scatter(table["predicted"], table["actual"],
                   s=table["n"] / table["n"].max() * 180 + 15,
                   color="#2b6cb0", alpha=0.35, zorder=2)

        n_markets = df[df["horizon_days"] == horizon]["market_id"].nunique()
        ax.set_title(f"{horizon} days before resolution  (n={n_markets})")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.grid(alpha=0.25)

    for ax in axes[1]:
        ax.set_xlabel("market price (predicted probability)")
    for ax in axes[:, 0]:
        ax.set_ylabel("observed frequency of Yes")

    axes[0, 0].legend(loc="upper left", fontsize=9)
    fig.suptitle("Are prediction market prices calibrated, and how early?",
                 fontsize=13)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"wrote {path.name}")


def plot_bias_by_horizon(df: pd.DataFrame, path: Path) -> None:
    """Mean price minus base rate, per horizon. Positive means Yes is overpriced."""
    rows = []
    for horizon in HORIZONS:
        sub = df[df["horizon_days"] == horizon]
        if sub.empty:
            continue
        rows.append({
            "horizon": horizon,
            "mean_price": sub["price_yes"].mean(),
            "base_rate": sub["outcome_yes_won"].mean(),
            "n": len(sub),
        })
    table = pd.DataFrame(rows)
    table["bias"] = table["mean_price"] - table["base_rate"]

    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.axhline(0, color="#999", linestyle="--", linewidth=1)
    ax.plot(table["horizon"], table["bias"], "o-", color="#c05621", linewidth=2)
    for _, row in table.iterrows():
        ax.annotate(f"n={row['n']:.0f}",
                    (row["horizon"], row["bias"]),
                    textcoords="offset points", xytext=(0, 9),
                    ha="center", fontsize=8, color="#666")
    ax.set_xscale("log")
    ax.set_xticks(list(HORIZONS))
    ax.set_xticklabels([f"{h}d" for h in HORIZONS])
    ax.invert_xaxis()
    ax.set_xlabel("days before resolution")
    ax.set_ylabel("mean price − base rate")
    ax.set_title("Does the pricing bias shrink as resolution approaches?")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"wrote {path.name}")


# --------------------------------------------------------------------------- #
# text summary
# --------------------------------------------------------------------------- #

def print_summary(df: pd.DataFrame) -> None:
    print()
    print(f"{'horizon':<9}{'n':>6}{'Brier':>9}{'baseline':>10}{'skill':>8}")
    print("-" * 42)
    for horizon in HORIZONS:
        brier, baseline = brier_score(df, horizon)
        if brier is None:
            continue
        n = len(df[df["horizon_days"] == horizon])
        skill = 1 - brier / baseline if baseline else float("nan")
        print(f"{str(horizon) + 'd':<9}{n:>6}{brier:>9.4f}{baseline:>10.4f}{skill:>8.3f}")
    print()
    print("skill = 1 - Brier/baseline. 0 means no better than always forecasting")
    print("the base rate; 1 means perfect. Negative means worse than the base rate.")
    print()

    for horizon in HORIZONS:
        table = calibration_table(df, horizon)
        if table.empty:
            continue
        print(f"--- {horizon} days before resolution ---")
        print(table[["bucket_label", "n", "predicted", "actual", "ci_lo", "ci_hi"]]
              .to_string(index=False,
                         formatters={
                             "predicted": "{:.3f}".format,
                             "actual": "{:.3f}".format,
                             "ci_lo": "{:.3f}".format,
                             "ci_hi": "{:.3f}".format,
                         }))
        print()


def main():
    parser = argparse.ArgumentParser(description="Analyse market calibration.")
    parser.add_argument("--dedupe-events", action="store_true",
                        help="keep one market per event (highest volume)")
    parser.add_argument("--no-figures", action="store_true",
                        help="print the tables only")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"no database at {DB_PATH} — run load.py first")
        return

    df = load_horizons(dedupe_events=args.dedupe_events)
    print(f"{len(df)} horizon rows across {df['market_id'].nunique()} markets")

    print_summary(df)

    if not args.no_figures:
        FIGURES_DIR.mkdir(parents=True, exist_ok=True)
        suffix = "_dedup" if args.dedupe_events else ""
        plot_calibration_grid(df, FIGURES_DIR / f"calibration{suffix}.png")
        plot_bias_by_horizon(df, FIGURES_DIR / f"bias_by_horizon{suffix}.png")


if __name__ == "__main__":
    main()