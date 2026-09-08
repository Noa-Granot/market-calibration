import json
from pathlib import Path

root = Path(__file__).resolve().parent.parent
markets_dir = root / "data" / "raw" / "markets"

print(f"looking in: {markets_dir}")
print(f"exists: {markets_dir.exists()}")

files = sorted(markets_dir.glob("page_*.json"))
print(f"files found: {len(files)}")
if not files:
    raise SystemExit("no page files — check the path")

vols, dates, no_vol = [], [], 0

for f in files:
    for m in json.loads(f.read_text(encoding="utf-8")):
        v = m.get("volumeNum") or m.get("volume")
        if v:
            try:
                vols.append(float(v))
            except (TypeError, ValueError):
                no_vol += 1
        else:
            no_vol += 1
        dates.append((m.get("endDate") or "?")[:7])

print(f"markets: {len(dates)}, with volume: {len(vols)}, without: {no_vol}")

if vols:
    vols.sort()
    print(f"min={vols[0]:,.0f} median={vols[len(vols)//2]:,.0f} max={vols[-1]:,.0f}")
else:
    sample = json.loads(files[0].read_text(encoding="utf-8"))[0]
    print("no volume field found. keys on first market:")
    print(sorted(sample.keys()))

print("earliest end months:", sorted(set(dates))[:8])