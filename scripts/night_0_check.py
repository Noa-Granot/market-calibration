"""
CLOB history diagnostic — v2.

What the first run established:
  * interval=max alone returns an EMPTY LIST, not an error, for old/long markets.
    That is what made history look aged out. It is not.
  * interval=max&fidelity=1440 returned 298 points on a market that ended
    596 days ago. History is available; only the resolution is limited.
  * startTs/endTs is rejected when the span is long, but a 7-day window at
    fidelity=60 returned 168 points — hourly, 24/day.
  * Undocumented fidelity floors: '1m' needs fidelity >= 10, '1w' needs >= 5.

So the fetch recipe is two calls per market:
  ARC    interval=max, fidelity=1440   -> daily prices over the whole life
  WINDOW startTs/endTs, fidelity=60    -> hourly prices in the last 7 days

Experiment B now re-tests recency WITH fidelity, to confirm the retention
theory is dead and the dataset can span years.
"""

import json
import time
from datetime import datetime, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com/markets"
TAGS = "https://gamma-api.polymarket.com/tags"
CLOB = "https://clob.polymarket.com/prices-history"
TIMEOUT = 25
NOW = datetime.now(timezone.utc)

ARC_FIDELITY = 1440          # daily
WINDOW_FIDELITY = 60         # hourly
WINDOW_DAYS = 7


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def parse_json_field(market, key):
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
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def days_ago(market):
    end = parse_dt(market.get("endDate"))
    return None if end is None else (NOW - end).total_seconds() / 86400


def fetch_markets(**params):
    params.setdefault("closed", "true")
    r = requests.get(GAMMA, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def raw_history(params):
    """Returns (points_list, note). Never raises."""
    try:
        r = requests.get(CLOB, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return [], f"request error: {exc}"
    if r.status_code != 200:
        return [], f"HTTP {r.status_code}: {r.text[:110]}"
    try:
        payload = r.json()
    except ValueError:
        return [], f"non-JSON: {r.text[:110]}"
    history = payload.get("history")
    if history is None:
        return [], f"no 'history' key; keys={list(payload)[:6]}"
    return history, "" if history else "empty list"


# --------------------------------------------------------------------------- #
# the confirmed fetch recipe — this is what moves into fetch.py
# --------------------------------------------------------------------------- #

def fetch_arc(token_id):
    """Daily prices across the market's whole life."""
    return raw_history({
        "market": token_id,
        "interval": "max",
        "fidelity": ARC_FIDELITY,
    })


def fetch_window(token_id, end_dt, days=WINDOW_DAYS):
    """Hourly prices in the final `days` before resolution.

    This is the call the calibration study depends on: hourly resolution means
    the price at exactly 24h before resolution can be read directly rather than
    approximated from a daily bar.
    """
    end_ts = int(end_dt.timestamp())
    return raw_history({
        "market": token_id,
        "startTs": end_ts - days * 86400,
        "endTs": end_ts,
        "fidelity": WINDOW_FIDELITY,
    })


def price_at_hours_before(window_points, end_dt, hours=24):
    """Nearest point to (end - hours). Returns (price, timestamp) or (None, None)."""
    if not window_points:
        return None, None
    target = end_dt.timestamp() - hours * 3600
    best = min(window_points, key=lambda p: abs(float(p.get("t", 0)) - target))
    try:
        return float(best.get("p")), int(best.get("t"))
    except (TypeError, ValueError):
        return None, None


# --------------------------------------------------------------------------- #
# EXPERIMENT A — parameter shapes (kept, for the record)
# --------------------------------------------------------------------------- #

def experiment_a():
    print("=" * 78)
    print("EXPERIMENT A — parameter shapes")
    print("=" * 78)

    markets = fetch_markets(
        limit=5, volume_num_min=1_000_000, end_date_min="2025-01-01T00:00:00Z"
    )
    target = next((m for m in markets if parse_json_field(m, "clobTokenIds")), None)
    if target is None:
        print("Could not find a test market.\n")
        return

    token = parse_json_field(target, "clobTokenIds")[0]
    end = parse_dt(target.get("endDate"))

    print(f"Market : {(target.get('question') or '')[:64]}")
    print(f"Ends   : {target.get('endDate')}  ({days_ago(target):.0f} days ago)\n")

    variants = [
        ("interval=max (no fidelity)", {"market": token, "interval": "max"}),
        ("interval=max fid=1440", {"market": token, "interval": "max", "fidelity": 1440}),
        ("interval=max fid=720", {"market": token, "interval": "max", "fidelity": 720}),
        ("interval=1m fid=10", {"market": token, "interval": "1m", "fidelity": 10}),
        ("interval=1w fid=5", {"market": token, "interval": "1w", "fidelity": 5}),
    ]
    if end:
        end_ts = int(end.timestamp())
        variants += [
            ("last 7d fid=60", {"market": token, "startTs": end_ts - 7 * 86400,
                                "endTs": end_ts, "fidelity": 60}),
            ("last 30d fid=60", {"market": token, "startTs": end_ts - 30 * 86400,
                                 "endTs": end_ts, "fidelity": 60}),
            ("last 30d fid=1440", {"market": token, "startTs": end_ts - 30 * 86400,
                                   "endTs": end_ts, "fidelity": 1440}),
        ]

    print(f"{'variant':<28} {'pts':>6}  note")
    print("-" * 78)
    for label, params in variants:
        points, note = raw_history(params)
        print(f"{label:<28} {len(points):>6}  {note}")
        time.sleep(0.4)
    print()


# --------------------------------------------------------------------------- #
# EXPERIMENT B — recency, this time WITH fidelity
# --------------------------------------------------------------------------- #

BUCKETS = [
    ("ended  <30d ago",     0,   30),
    ("ended 30-90d ago",    30,  90),
    ("ended 90-180d ago",   90,  180),
    ("ended 180-365d ago",  180, 365),
    ("ended 365-730d ago",  365, 730),
    ("ended  >730d ago",    730, 2000),   # Polymarket starts around 2020
]


def experiment_b(per_bucket=6):
    print("=" * 78)
    print("EXPERIMENT B — recency, with fidelity supplied")
    print("=" * 78)
    print(f"{'bucket':<22} {'arc':>7} {'window':>7}  {'med pts':>8}  sample dates")
    print("-" * 78)

    for label, lo, hi in BUCKETS:
        end_max = NOW.timestamp() - lo * 86400
        end_min = NOW.timestamp() - hi * 86400
        if end_min <= 0:                      # guard: no negative timestamps
            end_min = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()

        try:
            markets = fetch_markets(
                limit=40,
                volume_num_min=20_000,
                end_date_min=iso(end_min),
                end_date_max=iso(end_max),
            )
        except requests.RequestException as exc:
            print(f"{label:<22} request failed: {exc}")
            continue

        markets = [m for m in markets
                   if parse_json_field(m, "clobTokenIds")
                   and parse_dt(m.get("endDate"))][:per_bucket]
        if not markets:
            print(f"{label:<22} {'no markets returned':>30}")
            continue

        arc_hits = win_hits = 0
        counts, dates = [], []
        for m in markets:
            token = parse_json_field(m, "clobTokenIds")[0]
            end = parse_dt(m.get("endDate"))

            arc, _ = fetch_arc(token)
            if arc:
                arc_hits += 1
                counts.append(len(arc))
            time.sleep(0.35)

            window, _ = fetch_window(token, end)
            if window:
                win_hits += 1
            time.sleep(0.35)

            dates.append((m.get("endDate") or "?")[:10])

        counts.sort()
        median = counts[len(counts) // 2] if counts else 0
        print(f"{label:<22} {arc_hits:>3}/{len(markets):<3} {win_hits:>3}/{len(markets):<3}"
              f"  {median:>8}  {dates[:3]}")

    print("\nIf every bucket comes back green, the retention theory is dead and the\n"
          "dataset can span years. Any bucket that fails is a real cutoff.\n")


# --------------------------------------------------------------------------- #
# EXPERIMENT C — end to end on one market, exactly as the study will do it
# --------------------------------------------------------------------------- #

def experiment_c(n=6):
    print("=" * 78)
    print("EXPERIMENT C — one calibration row per market, end to end")
    print("=" * 78)
    print(f"{'p@24h':>6} {'resolved':>8} {'arc':>5} {'win':>5}  ends       question")
    print("-" * 78)

    try:
        markets = fetch_markets(
            limit=30, volume_num_min=100_000,
            end_date_min="2025-01-01T00:00:00Z",
        )
    except requests.RequestException as exc:
        print(f"request failed: {exc}")
        return

    rows = 0
    for m in markets:
        if rows >= n:
            break
        tokens = parse_json_field(m, "clobTokenIds")
        end = parse_dt(m.get("endDate"))
        if not tokens or not end:
            continue

        prices = parse_json_field(m, "outcomePrices")
        try:
            resolved = float(prices[0]) if prices else None
        except (TypeError, ValueError):
            resolved = None

        arc, _ = fetch_arc(tokens[0])
        time.sleep(0.35)
        window, _ = fetch_window(tokens[0], end)
        time.sleep(0.35)

        price, _ = price_at_hours_before(window, end, hours=24)
        if price is None and arc:
            price, _ = price_at_hours_before(arc, end, hours=24)

        price_text = f"{price:.3f}" if price is not None else "  -  "
        res_text = f"{resolved:.0f}" if resolved is not None else "-"
        print(f"{price_text:>6} {res_text:>8} {len(arc):>5} {len(window):>5}"
              f"  {(m.get('endDate') or '?')[:10]}  {(m.get('question') or '')[:34]}")
        rows += 1

    print("\nA usable row needs a p@24h strictly between 0 and 1 and a resolved of\n"
          "0 or 1. If that holds here, the pipeline has everything it needs.\n")


# --------------------------------------------------------------------------- #
# tags
# --------------------------------------------------------------------------- #

def list_tags(pages=6, per_page=100):
    print("=" * 78)
    print("TAG SLUGS (paginated)")
    print("=" * 78)
    slugs = set()
    for page in range(pages):
        try:
            r = requests.get(TAGS, params={"limit": per_page, "offset": page * per_page},
                             timeout=TIMEOUT)
            r.raise_for_status()
            batch = r.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"page {page}: failed ({exc})")
            break
        if not batch:
            break
        slugs.update(t.get("slug") for t in batch if isinstance(t, dict) and t.get("slug"))
        time.sleep(0.3)

    wanted = ("politic", "econom", "crypto", "geopolit", "election", "macro",
              "fed", "world", "business", "science", "tech", "ai")
    interesting = sorted(s for s in slugs if any(w in s for w in wanted))

    print(f"{len(slugs)} slugs total. Forecast-type candidates:\n")
    for i in range(0, len(interesting), 4):
        print("  " + "".join(f"{s:<24}" for s in interesting[i:i + 4]))
    print()


def main():
    experiment_a()
    experiment_b()
    experiment_c()
    list_tags()


if __name__ == "__main__":
    main()