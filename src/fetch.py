"""
fetch.py — acquisition stage for market-calibration.

Two stages, both resumable:

  1. Market metadata from the Gamma API, one JSON file per page.
  2. Daily price history from the CLOB API, one JSON file per market.

Resumability is the whole design: before every request the script checks
whether the output file already exists and skips it if so. A run that dies
at record 4,000 picks up at 4,000.

What the Night 0 diagnostic established, and why the parameters look like this:
  * prices-history returns an EMPTY LIST rather than an error when 'fidelity'
    is omitted, so fidelity is always passed explicitly.
  * fidelity is bucket width in minutes; 1440 gives one point per day.
  * startTs/endTs is rejected when the span exceeds roughly a week, so the
    whole-life arc has to come from interval=max.
  * Markets ending before ~2022 predate the CLOB order book and have no
    history at all. They are filtered out rather than retried.

Usage:
    python -m src.fetch                     # full run with defaults
    python -m src.fetch --max-pages 5       # short test run
    python -m src.fetch --force             # ignore existing files
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CLOB_URL = "https://clob.polymarket.com/prices-history"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_MARKETS_DIR = DATA_DIR / "raw" / "markets"
RAW_HISTORY_DIR = DATA_DIR / "raw" / "history"
LOG_DIR = DATA_DIR / "logs"

PAGE_SIZE = 100                  # markets per Gamma page
MAX_PAGES = 200                  # ceiling, so a bad loop cannot run forever
ARC_FIDELITY = 1440              # minutes per bucket; 1440 = daily

MIN_VOLUME = 20_000              # below this the prices are a handful of traders
MIN_DURATION_DAYS = 30           # excludes auto-generated sports micro-markets
EARLIEST_END_DATE = "2023-04-01T00:00:00Z"   # CLOB history starts here; earlier markets ran on an AMM

REQUEST_TIMEOUT = 25
SLEEP_BETWEEN = 0.6              # polite delay between calls
MAX_RETRIES = 4
BACKOFF_BASE = 1.7

log = logging.getLogger("fetch")


# --------------------------------------------------------------------------- #
# setup
# --------------------------------------------------------------------------- #

def setup_logging(verbose: bool = False) -> None:
    """Log to stdout and to a timestamped file, so a long run leaves a record."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logfile = LOG_DIR / f"fetch_{stamp}.log"

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(logfile, encoding="utf-8"),
        ],
    )
    log.info("logging to %s", logfile)


def ensure_dirs() -> None:
    for directory in (RAW_MARKETS_DIR, RAW_HISTORY_DIR, LOG_DIR):
        directory.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# parsing helpers
#
# Several Gamma fields arrive as JSON-encoded strings rather than lists, and
# not always the same ones. Everything goes through here.
# --------------------------------------------------------------------------- #

def parse_json_field(market: dict, key: str) -> list:
    raw = market.get(key)
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("could not decode %s on market %s", key, market.get("id"))
            return []
    return raw if isinstance(raw, list) else []


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
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


def duration_days(market: dict):
    start = parse_dt(market.get("startDate")) or parse_dt(market.get("createdAt"))
    end = parse_dt(market.get("endDate"))
    if start is None or end is None:
        return None
    return (end - start).total_seconds() / 86400


# --------------------------------------------------------------------------- #
# HTTP with retries
# --------------------------------------------------------------------------- #

def request_json(url: str, params: dict):
    """GET with exponential backoff and jitter.

    Returns the decoded payload, or None if the request could not be completed.
    A 4xx other than 429 is not retried — the request itself is wrong, and
    repeating it will not fix that.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            wait = BACKOFF_BASE ** attempt + random.uniform(0, 0.5)
            log.warning("attempt %d/%d failed (%s), retrying in %.1fs",
                        attempt, MAX_RETRIES, exc, wait)
            time.sleep(wait)
            continue

        if response.status_code == 200:
            try:
                return response.json()
            except ValueError:
                log.error("200 but body was not JSON: %s", response.text[:150])
                return None

        if response.status_code == 429 or response.status_code >= 500:
            wait = BACKOFF_BASE ** attempt + random.uniform(0, 0.5)
            log.warning("HTTP %d, retrying in %.1fs", response.status_code, wait)
            time.sleep(wait)
            continue

        log.error("HTTP %d (not retrying): %s",
                  response.status_code, response.text[:150])
        return None

    log.error("gave up after %d attempts: %s", MAX_RETRIES, params)
    return None


# --------------------------------------------------------------------------- #
# stage 1 — market metadata
# --------------------------------------------------------------------------- #

def fetch_market_pages(max_pages: int = MAX_PAGES, force: bool = False) -> int:
    """Page through closed markets, one file per page. Returns pages written."""
    log.info("stage 1: market metadata (page size %d, max %d pages)",
             PAGE_SIZE, max_pages)

    written = skipped = 0

    for page in range(max_pages):
        path = RAW_MARKETS_DIR / f"page_{page:04d}.json"

        # The resumability check. Everything else in this function is plumbing.
        if path.exists() and not force:
            skipped += 1
            log.debug("page %04d already on disk, skipping", page)
            continue

        payload = request_json(GAMMA_URL, {
            "closed": "true",
            "limit": PAGE_SIZE,
            "offset": page * PAGE_SIZE,
            "volume_num_min": MIN_VOLUME,
            "end_date_min": EARLIEST_END_DATE,
        })

        if payload is None:
            log.error("page %04d failed, stopping stage 1", page)
            break
        if not isinstance(payload, list) or not payload:
            log.info("page %04d empty — reached the end of the feed", page)
            break

        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        written += 1
        log.info("page %04d: %d markets -> %s", page, len(payload), path.name)
        time.sleep(SLEEP_BETWEEN)

    log.info("stage 1 done: %d pages written, %d already present", written, skipped)
    return written


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #

def load_all_markets() -> list:
    markets = []
    for path in sorted(RAW_MARKETS_DIR.glob("page_*.json")):
        try:
            markets.extend(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            log.error("could not read %s: %s", path.name, exc)
    return markets


def select_markets(markets: list) -> list:
    """Apply the filters, and report how many each one removed.

    The counts matter as much as the result — they are what the README's
    data-selection section is written from.
    """
    reasons = {
        "no token ids": 0,
        "no end date": 0,
        "ends before CLOB era": 0,
        f"volume < {MIN_VOLUME:,}": 0,
        f"shorter than {MIN_DURATION_DAYS}d": 0,
    }
    cutoff = parse_dt(EARLIEST_END_DATE)
    kept = []

    for market in markets:
        if not parse_json_field(market, "clobTokenIds"):
            reasons["no token ids"] += 1
            continue

        end = parse_dt(market.get("endDate"))
        if end is None:
            reasons["no end date"] += 1
            continue
        if cutoff and end < cutoff:
            reasons["ends before CLOB era"] += 1
            continue

        volume = parse_volume(market)
        if volume is None or volume < MIN_VOLUME:
            reasons[f"volume < {MIN_VOLUME:,}"] += 1
            continue

        days = duration_days(market)
        if days is not None and days < MIN_DURATION_DAYS:
            reasons[f"shorter than {MIN_DURATION_DAYS}d"] += 1
            continue

        kept.append(market)

    log.info("selection: %d in, %d kept", len(markets), len(kept))
    for reason, count in reasons.items():
        if count:
            log.info("  dropped %6d — %s", count, reason)
    return kept


# --------------------------------------------------------------------------- #
# stage 2 — price history
# --------------------------------------------------------------------------- #

def fetch_histories(markets: list, force: bool = False) -> tuple[int, int, int]:
    """One daily-fidelity arc per market. Returns (written, skipped, empty)."""
    log.info("stage 2: price history for %d markets", len(markets))

    written = skipped = empty = 0

    for index, market in enumerate(markets, start=1):
        market_id = str(market.get("id") or f"unknown_{index}")
        path = RAW_HISTORY_DIR / f"{market_id}.json"

        if path.exists() and not force:
            skipped += 1
            continue

        token_id = parse_json_field(market, "clobTokenIds")[0]

        payload = request_json(CLOB_URL, {
            "market": token_id,
            "interval": "max",
            "fidelity": ARC_FIDELITY,
        })

        points = (payload or {}).get("history") or []

        # An empty result is still written. Without it the market would be
        # retried on every future run, and "no history" is itself a finding.
        record = {
            "market_id": market_id,
            "token_id": token_id,
            "question": market.get("question"),
            "end_date": market.get("endDate"),
            "fidelity": ARC_FIDELITY,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "n_points": len(points),
            "history": points,
        }
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

        if points:
            written += 1
        else:
            empty += 1
            log.debug("no history for %s (%s)", market_id, market.get("endDate"))

        if index % 25 == 0:
            log.info("  %d/%d — %d with history, %d empty",
                     index, len(markets), written, empty)

        time.sleep(SLEEP_BETWEEN)

    log.info("stage 2 done: %d written, %d already present, %d empty",
             written, skipped, empty)
    return written, skipped, empty


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def parse_args():
    parser = argparse.ArgumentParser(description="Fetch Polymarket data.")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES,
                        help="ceiling on metadata pages (default %(default)s)")
    parser.add_argument("--limit-markets", type=int, default=None,
                        help="only fetch history for the first N markets")
    parser.add_argument("--force", action="store_true",
                        help="re-fetch even when the output file exists")
    parser.add_argument("--skip-history", action="store_true",
                        help="stage 1 only")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    ensure_dirs()
    setup_logging(args.verbose)

    started = time.time()

    fetch_market_pages(max_pages=args.max_pages, force=args.force)

    markets = load_all_markets()
    if not markets:
        log.error("no markets on disk — stage 1 produced nothing")
        return

    selected = select_markets(markets)
    if args.limit_markets:
        selected = selected[:args.limit_markets]
        log.info("limited to first %d markets", len(selected))

    if not args.skip_history:
        fetch_histories(selected, force=args.force)

    log.info("finished in %.1f minutes", (time.time() - started) / 60)


if __name__ == "__main__":
    main()