"""
load.py — transform stage for market-calibration.

Reads the raw JSON written by fetch.py and builds a SQLite database:

  markets   one row per market: question, dates, volume, resolved outcome
  prices    the full daily price arc, one row per (market, timestamp)
  horizons  the analysis table: price at 90 / 30 / 7 / 1 days before resolution

Why horizons is a table rather than a query: Night 0 showed that at 24 hours
before resolution almost every market is already at 0.99 or 0.01, so a single
horizon gives a calibration curve with two dense points and eight empty ones.
Measuring several horizons turns the question into "how early do markets know",
which is both more interesting and actually answerable with this data.

The load is idempotent. Every write is INSERT OR REPLACE keyed on the primary
key, so running it twice produces the same database rather than duplicate rows.

Usage:
    python src/load.py                  # build data/market_calibration.sqlite3
    python src/load.py --parquet        # also export the analysis table
    python src/load.py --limit 50       # short test run
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("load")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_MARKETS_DIR = DATA_DIR / "raw" / "markets"
RAW_HISTORY_DIR = DATA_DIR / "raw" / "history"
DB_PATH = DATA_DIR / "market_calibration.sqlite3"
PARQUET_PATH = DATA_DIR / "horizons.parquet"
LOG_DIR = DATA_DIR / "logs"

# Days before resolution at which to sample the price.
HORIZONS = (90, 30, 7, 1)

# The arc is daily, so the nearest point to a target can be up to ~12h away.
# Anything further than this means the market had no price near that horizon.
MAX_GAP_HOURS = 36


# --------------------------------------------------------------------------- #
# parsing — pure functions, no I/O, so tests/test_load.py can hit them directly
# --------------------------------------------------------------------------- #

def parse_json_field(market: dict, key: str) -> list:
    """Gamma returns several list fields as JSON-encoded strings instead."""
    raw = market.get(key)
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return raw if isinstance(raw, list) else []


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def parse_volume(market: dict):
    for key in ("volumeNum", "volume", "volumeClob"):
        value = market.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def parse_outcome(market: dict):
    """Resolved outcome as 1 (Yes won) or 0 (No won), or None if unclear.

    For a closed market outcomePrices is the settlement, normally ["1","0"].
    Anything that is not close to 0 or 1 means the market did not settle
    cleanly and is not usable for a calibration study.
    """
    prices = parse_json_field(market, "outcomePrices")
    if not prices:
        return None
    try:
        yes = float(prices[0])
    except (TypeError, ValueError):
        return None
    if yes > 0.98:
        return 1
    if yes < 0.02:
        return 0
    return None


def parse_event(market: dict):
    """Returns (event_id, event_slug).

    Several markets can belong to one event — the UK election produced separate
    Labour / Conservative / LibDem markets on the same question. Those are not
    independent observations, so the event id has to survive into the database
    for the analysis to be able to group or drop them.
    """
    events = market.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        return str(events[0].get("id") or ""), events[0].get("slug")
    return "", None


def duration_days(market: dict):
    start = parse_dt(market.get("startDate")) or parse_dt(market.get("createdAt"))
    end = parse_dt(market.get("endDate"))
    if start is None or end is None:
        return None
    return (end - start).total_seconds() / 86400


def price_at_horizon(points: list, end_ts: float, days: int):
    """Nearest price to (end - days). Returns (price, ts, gap_hours) or None.

    The gap is returned rather than hidden because daily fidelity means the
    nearest point can be half a day off the target, and a reader of the
    analysis should be able to see that.
    """
    if not points:
        return None
    target = end_ts - days * 86400
    best, best_gap = None, None
    for point in points:
        try:
            ts = float(point.get("t"))
            price = float(point.get("p"))
        except (TypeError, ValueError):
            continue
        gap = abs(ts - target)
        if best_gap is None or gap < best_gap:
            best, best_gap = (price, ts), gap
    if best is None:
        return None
    gap_hours = best_gap / 3600
    if gap_hours > MAX_GAP_HOURS:
        return None
    return best[0], best[1], gap_hours


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    id               TEXT PRIMARY KEY,
    question         TEXT    NOT NULL,
    slug             TEXT,
    event_id         TEXT,
    event_slug       TEXT,
    start_date       TEXT,
    end_date         TEXT    NOT NULL,
    duration_days    REAL,
    volume           REAL,
    outcome_yes_won  INTEGER NOT NULL CHECK (outcome_yes_won IN (0, 1)),
    n_price_points   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS prices (
    market_id   TEXT    NOT NULL,
    ts          INTEGER NOT NULL,
    price_yes   REAL    NOT NULL,
    PRIMARY KEY (market_id, ts),
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE TABLE IF NOT EXISTS horizons (
    market_id     TEXT    NOT NULL,
    horizon_days  INTEGER NOT NULL,
    price_yes     REAL    NOT NULL,
    ts            INTEGER NOT NULL,
    gap_hours     REAL    NOT NULL,
    PRIMARY KEY (market_id, horizon_days),
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE INDEX IF NOT EXISTS idx_prices_market   ON prices (market_id);
CREATE INDEX IF NOT EXISTS idx_markets_event   ON markets (event_id);
CREATE INDEX IF NOT EXISTS idx_markets_end     ON markets (end_date);
CREATE INDEX IF NOT EXISTS idx_horizons_days   ON horizons (horizon_days);
"""


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# --------------------------------------------------------------------------- #
# reading the raw files
# --------------------------------------------------------------------------- #

def load_raw_markets() -> dict:
    """market_id -> raw market dict, from the page files."""
    markets = {}
    for path in sorted(RAW_MARKETS_DIR.glob("page_*.json")):
        try:
            page = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.error("could not read %s: %s", path.name, exc)
            continue
        for market in page:
            market_id = str(market.get("id") or "")
            if market_id:
                markets[market_id] = market
    log.info("read %d unique markets from %d pages",
             len(markets), len(list(RAW_MARKETS_DIR.glob("page_*.json"))))
    return markets


def load_raw_history(market_id: str):
    """The saved history record for one market, or None."""
    path = RAW_HISTORY_DIR / f"{market_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.error("could not read history for %s: %s", market_id, exc)
        return None


# --------------------------------------------------------------------------- #
# the load
# --------------------------------------------------------------------------- #

def build_database(limit: int | None = None) -> dict:
    conn = connect()
    raw_markets = load_raw_markets()

    dropped = {
        "no history file": 0,
        "empty history": 0,
        "no clean 0/1 outcome": 0,
        "no end date": 0,
        "no horizon in range": 0,
    }
    counts = {"markets": 0, "prices": 0, "horizons": 0}

    items = list(raw_markets.items())
    if limit:
        items = items[:limit]

    for market_id, market in items:
        record = load_raw_history(market_id)
        if record is None:
            dropped["no history file"] += 1
            continue

        points = record.get("history") or []
        if not points:
            dropped["empty history"] += 1
            continue

        outcome = parse_outcome(market)
        if outcome is None:
            dropped["no clean 0/1 outcome"] += 1
            continue

        end = parse_dt(market.get("endDate"))
        if end is None:
            dropped["no end date"] += 1
            continue

        event_id, event_slug = parse_event(market)

        conn.execute(
            """INSERT OR REPLACE INTO markets
               (id, question, slug, event_id, event_slug, start_date, end_date,
                duration_days, volume, outcome_yes_won, n_price_points)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                market_id,
                market.get("question") or "",
                market.get("slug"),
                event_id,
                event_slug,
                market.get("startDate"),
                market.get("endDate"),
                duration_days(market),
                parse_volume(market),
                outcome,
                len(points),
            ),
        )
        counts["markets"] += 1

        price_rows = []
        for point in points:
            try:
                price_rows.append((market_id, int(float(point["t"])), float(point["p"])))
            except (TypeError, ValueError, KeyError):
                continue
        conn.executemany(
            "INSERT OR REPLACE INTO prices (market_id, ts, price_yes) VALUES (?, ?, ?)",
            price_rows,
        )
        counts["prices"] += len(price_rows)

        end_ts = end.timestamp()
        found_any = False
        for days in HORIZONS:
            result = price_at_horizon(points, end_ts, days)
            if result is None:
                continue
            price, ts, gap_hours = result
            conn.execute(
                """INSERT OR REPLACE INTO horizons
                   (market_id, horizon_days, price_yes, ts, gap_hours)
                   VALUES (?, ?, ?, ?, ?)""",
                (market_id, days, price, int(ts), round(gap_hours, 2)),
            )
            counts["horizons"] += 1
            found_any = True

        if not found_any:
            dropped["no horizon in range"] += 1

        if counts["markets"] % 200 == 0:
            conn.commit()
            log.info("  %d markets loaded", counts["markets"])

    conn.commit()

    log.info("loaded %d markets, %d price points, %d horizon rows",
             counts["markets"], counts["prices"], counts["horizons"])
    for reason, n in dropped.items():
        if n:
            log.info("  dropped %5d — %s", n, reason)

    report_coverage(conn)
    conn.close()
    return counts


def report_coverage(conn: sqlite3.Connection) -> None:
    """How many markets have a usable price at each horizon, and how the
    outcomes split. These numbers go straight into the README."""
    log.info("")
    log.info("coverage by horizon:")
    rows = conn.execute(
        """SELECT h.horizon_days,
                  COUNT(*)                        AS n,
                  ROUND(AVG(h.price_yes), 3)      AS mean_price,
                  ROUND(AVG(m.outcome_yes_won), 3) AS base_rate,
                  ROUND(AVG(h.gap_hours), 1)      AS mean_gap_h
           FROM horizons h
           JOIN markets m ON m.id = h.market_id
           GROUP BY h.horizon_days
           ORDER BY h.horizon_days DESC"""
    ).fetchall()
    log.info("  %-8s %6s %11s %10s %9s", "horizon", "n", "mean price", "base rate", "gap (h)")
    for days, n, mean_price, base_rate, gap in rows:
        log.info("  %-8s %6d %11s %10s %9s", f"{days}d", n, mean_price, base_rate, gap)

    n_events, n_markets = conn.execute(
        "SELECT COUNT(DISTINCT event_id), COUNT(*) FROM markets WHERE event_id != ''"
    ).fetchone()
    if n_events:
        log.info("")
        log.info("%d markets across %d distinct events "
                 "(markets sharing an event are not independent observations)",
                 n_markets, n_events)


def export_parquet(path: Path = PARQUET_PATH) -> None:
    """The analysis table as Parquet, for anyone who wants it without SQLite."""
    try:
        import pandas as pd
    except ImportError:
        log.error("pandas not installed — skipping Parquet export")
        return

    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """SELECT m.id            AS market_id,
                  m.question,
                  m.event_id,
                  m.end_date,
                  m.duration_days,
                  m.volume,
                  m.outcome_yes_won,
                  h.horizon_days,
                  h.price_yes,
                  h.gap_hours
           FROM horizons h
           JOIN markets m ON m.id = h.market_id
           ORDER BY m.end_date, h.horizon_days""",
        conn,
    )
    conn.close()

    try:
        df.to_parquet(path, index=False)
    except ImportError:
        log.error("pyarrow not installed — run: pip install pyarrow")
        return
    log.info("exported %d rows to %s", len(df), path.name)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def setup_logging(verbose: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / f"load_{stamp}.log", encoding="utf-8"),
        ],
    )


def main():
    parser = argparse.ArgumentParser(description="Load raw Polymarket JSON into SQLite.")
    parser.add_argument("--limit", type=int, default=None,
                        help="only load the first N markets")
    parser.add_argument("--parquet", action="store_true",
                        help="also export the analysis table as Parquet")
    parser.add_argument("--rebuild", action="store_true",
                        help="delete the database first")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.rebuild and DB_PATH.exists():
        DB_PATH.unlink()
        log.info("deleted existing database")

    if not RAW_MARKETS_DIR.exists():
        log.error("no raw data at %s — run fetch.py first", RAW_MARKETS_DIR)
        return

    build_database(limit=args.limit)

    if args.parquet:
        export_parquet()


if __name__ == "__main__":
    main()
