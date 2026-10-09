"""What gets named on screen (the renderer calls these TOWNS).

  name: (lat, lon, label)

Coordinates are from each place's Wikipedia infobox, checked against the lidar
when the flight map is drawn: Lamoille Lake 40.5925, -115.3939; Liberty Lake
40.5800, -115.3950; Liberty Peak 40.586518, -115.400077; Ruby Dome
40.621681, -115.475405. The canyon's name sits on its floor, a third of the way
up from the mouth (from centerline.json, found in the lidar).
"""
from __future__ import annotations

import json

import config

TOWNS = {
    "lamoille_lake": (40.5925, -115.3939, "Lamoille Lake"),
    "liberty_lake": (40.5800, -115.3950, "Liberty Lake"),
    "liberty_peak": (40.586518, -115.400077, "Liberty Peak"),
    "ruby_dome": (40.621681, -115.475405, "Ruby Dome"),
}
_line = config.DATA / "centerline.json"
if _line.exists():
    _c = json.loads(_line.read_text())
    _p = _c[len(_c) // 3]
    TOWNS["canyon"] = (_p["lat"], _p["lon"], "Lamoille Canyon")

LEGS = []
ROUTE_RGB = (242, 179, 94)
