"""Which USGS 3DEP lidar covers Lamoille Canyon? (runs on GitHub Actions)

1. Reads the index of every project in the USGS 3DEP point-cloud archive on AWS
   (Entwine Point Tiles) and keeps the ones whose footprint touches the window.
2. For each of those, counts ground returns (ASPRS class 2) in a 200 m box at
   each checkpoint along the planned flight, from the canyon mouth to Liberty Lake.

Prints the findings as lines starting with "= " (the workflow turns them into
annotations) and writes them to probe/coverage.json.

    python scripts/probe.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests
from pyproj import Transformer
from shapely.geometry import box, shape

INDEX = "https://raw.githubusercontent.com/hobu/usgs-lidar/master/boundaries/resources.geojson"
EPT = "https://s3-us-west-2.amazonaws.com/usgs-lidar-public/{name}/ept.json"

# The flyover window: Lamoille Canyon from its mouth to the crest above Liberty Lake,
# with room on every side (west, south, east, north, degrees).
WINDOW = (-115.52, 40.555, -115.33, 40.715)

# Checkpoints along the planned flight (lat, lon). Liberty Lake is from Wikipedia;
# the others are approximate and only need to land in the right part of the canyon.
CHECKPOINTS = {
    "Lamoille Canyon mouth": (40.690, -115.470),
    "Mid canyon (Thomas Canyon)": (40.655, -115.425),
    "Roads End trailhead": (40.612, -115.377),
    "Lamoille Lake": (40.598, -115.383),
    "Liberty Lake": (40.580, -115.395),
    "Crest south of Liberty": (40.565, -115.400),
}
BOX_M = 200.0
OUT = Path(__file__).resolve().parent.parent / "probe"


def say(msg: str) -> None:
    print(f"= {msg}", flush=True)


def projects():
    feats = requests.get(INDEX, timeout=120).json()["features"]
    win = box(*WINDOW)
    hits = []
    for f in feats:
        g = shape(f["geometry"])
        if g.intersects(win):
            p = f["properties"]
            hits.append({"name": p["name"], "points": p.get("count"),
                         "cover": round(g.intersection(win).area / win.area, 3)})
    hits.sort(key=lambda h: -h["cover"])
    return hits


def ground_count(name: str, lat: float, lon: float) -> int:
    import pdal
    to3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    x, y = to3857.transform(lon, lat)
    h = BOX_M / 2 / 0.7604           # Web Mercator stretches by 1/cos(lat), about 1.32 here
    pipe = [{"type": "readers.ept", "filename": EPT.format(name=name),
             "bounds": f"([{x - h},{x + h}],[{y - h},{y + h}])", "threads": 4},
            {"type": "filters.range", "limits": "Classification[2:2]"}]
    return int(pdal.Pipeline(json.dumps({"pipeline": pipe})).execute())


def main() -> int:
    OUT.mkdir(exist_ok=True)
    hits = projects()
    if not hits:
        say("no 3DEP point-cloud project touches the window: the flyover would use the 10 m DEM")
        (OUT / "coverage.json").write_text(json.dumps({"projects": []}, indent=1))
        return 0
    for h in hits:
        meta = requests.get(EPT.format(name=h["name"]), timeout=60).json()
        h["srs"] = meta.get("srs", {}).get("horizontal")
        say(f"project {h['name']}: covers {h['cover']:.0%} of the window, "
            f"{h['points'] or 0:,} points in all, EPSG:{h['srs']}")
    area = BOX_M * BOX_M
    table = {}
    for label, (lat, lon) in CHECKPOINTS.items():
        row = {}
        for h in hits:
            try:
                n = ground_count(h["name"], lat, lon)
            except Exception as e:           # a bad project shouldn't stop the probe
                print(f"  {h['name']} at {label}: {e}", file=sys.stderr)
                n = -1
            row[h["name"]] = n
        best = max(row, key=row.get)
        dens = row[best] / area
        table[label] = {"lat": lat, "lon": lon, "ground": row}
        say(f"{label}: best {best}, {row[best]:,} ground returns = {dens:.2f} per m2"
            + ("  (enough for 1 m)" if dens >= 1 else "  (thin: grid at 2 m or coarser)" if dens > 0 else "  (NO LIDAR)"))
    (OUT / "coverage.json").write_text(json.dumps({"window": WINDOW, "box_m": BOX_M,
                                                   "projects": hits, "checkpoints": table}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
