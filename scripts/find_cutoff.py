# scripts/find_cutoff.py
import json, time, requests
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB = "https://clob.polymarket.com/prices-history"

def tokens(m):
    raw = m.get("clobTokenIds")
    if isinstance(raw, str):
        try: raw = json.loads(raw)
        except json.JSONDecodeError: return []
    return raw if isinstance(raw, list) else []

for year in (2022, 2023, 2024, 2025):
    for q, (lo, hi) in enumerate([("01-01","04-01"), ("04-01","07-01"),
                                  ("07-01","10-01"), ("10-01","12-31")], 1):
        r = requests.get(GAMMA, params={
            "closed": "true", "limit": 6, "volume_num_min": 20000,
            "end_date_min": f"{year}-{lo}T00:00:00Z",
            "end_date_max": f"{year}-{hi}T00:00:00Z",
        }, timeout=25)
        markets = [m for m in r.json() if tokens(m)]
        if not markets:
            print(f"{year} Q{q}: no markets"); continue
        hits = 0
        for m in markets:
            h = requests.get(CLOB, params={"market": tokens(m)[0],
                                           "interval": "max", "fidelity": 1440},
                             timeout=25)
            if h.status_code == 200 and h.json().get("history"):
                hits += 1
            time.sleep(0.5)
        print(f"{year} Q{q}: {hits}/{len(markets)} with history")
        time.sleep(0.5)