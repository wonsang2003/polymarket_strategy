"""Merge multiple climatology JSON files into one. Later files override
earlier ones for any (city, doy) collisions.

Run on EC2:
    venv/bin/python scripts/merge_climatology.py \\
        --inputs /tmp/climatology_era5.json /tmp/climatology_era5_missing.json \\
        --output /home/ubuntu/polymarket/data/weather/climatology.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--inputs", nargs="+", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    out_doy: dict[str, dict[str, dict]] = {}
    out_yr: dict[str, dict] = {}

    for path in args.inputs:
        p = Path(path)
        if not p.exists():
            print(f"[merge] missing {p}", file=sys.stderr)
            return 1
        with open(p) as f:
            data = json.load(f)
        for city, by_doy in (data.get("by_city_doy") or {}).items():
            out_doy.setdefault(city, {}).update(by_doy)
        for city, stats in (data.get("by_city_yearmean") or {}).items():
            out_yr[city] = stats
        n_cells = sum(len(v) for v in data.get("by_city_doy", {}).values())
        print(f"[merge] read {p.name}: {len(data.get('by_city_doy', {}))} cities, "
              f"{n_cells} cells")

    payload = {"by_city_doy": out_doy, "by_city_yearmean": out_yr}
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)

    n_total_cells = sum(len(v) for v in out_doy.values())
    print(f"[merge] wrote {out_path}")
    print(f"  cities: {len(out_doy)}")
    print(f"  total cells: {n_total_cells}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
