"""Shared settings for the Ruby Mountains flyover (Lamoille Canyon to Liberty Lake).

Everything is in UTM zone 11N (EPSG:26911, NAD83), in metres.

Two grids:
  the window  the whole block the viewer loads, ridges and valley around the canyon
  the core    the canyon itself, where the camera flies low: gridded from every lidar
              ground return at 1 m, so the shading on the texture carries the fine detail

The viewer gets the terrain at GEOM_RES (4 m by default) and one texture at TEX_RES
(2 m), split into strips. forge3d composites its overlays into one texture no bigger
than the GPU's 2D limit (16384 px on a side), so a finer texture would only be
shrunk again; the 1 m detail lives in the shading baked into it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "out"
TILES = DATA / "tiles"

CRS = "EPSG:26911"
TILE = 2000                                    # metres per lidar tile
WINDOW = (626000, 4488000, 642000, 4510000)    # left, bottom, right, top
CORE = (626000, 4490000, 640000, 4508000)      # gridded at 1 m
CORE_RES = 1.0
MARGIN_RES = 4.0
# 2.5 m: forge3d's TIFF reader refuses terrain over about 256 MB (8000 x 11000 float32
# at 2 m is 352 MB); at 2.5 m it's 6400 x 8800, 225 MB.
GEOM_RES = float(os.environ.get("RUBY_GEOM_RES", "2.5"))
DTM_STRIPS = 4
TEX_RES = 2.0
TEX_STRIPS = 4

# 3DEP point-cloud projects on AWS, best first (see probe/coverage.json): the 2021
# East Central Nevada survey covers the whole flight; the 2016 Upper Humboldt survey
# fills a strip of valley floor in the north-west corner.
PROJECTS = ["NV_EastCentral_5_D21", "USGS_LPC_NV_UpperHumboldt_2016_LAS_2018"]

LEFT, BOTTOM, RIGHT, TOP = WINDOW
DEM_X = DATA / "dtm.tif"                       # what the viewer loads (true scale)
EXAGGERATION = 1.0
VIDEO = OUT / "ruby_lamoille_flyover.mp4"

# Late afternoon in high summer: August 1, 2026, 6:15 pm PDT (Elko County keeps
# Pacific time). The sun is just north of west (azimuth 279, about 18 degrees up), some
# 20 degrees off the line of the lower canyon: it lights the north-east wall and leaves
# the south-west wall in shade, and it's behind the camera going up the canyon.
SUN_UTC = (2026, 8, 2, 1, 15, 0)
SUN_LATLON = (40.62, -115.42)

_info = DATA / "place.json"
INFO = json.loads(_info.read_text()) if _info.exists() else {"name": "Lamoille Canyon", "naip_year": None}
DEM_RES = float(INFO.get("geom_res_m", GEOM_RES))    # the grid the data was actually built at


def dtm():
    """Path of the terrain, joining the published strips (dtm_0.tif, ...) into dtm.tif once."""
    parts = sorted(DATA.glob("dtm_*.tif"), key=lambda p: int(p.stem.split("_")[1]))
    if DEM_X.exists() and (not parts or DEM_X.stat().st_mtime >= max(p.stat().st_mtime for p in parts)):
        return DEM_X
    if not parts:
        return DEM_X
    import numpy as np
    import rasterio
    arrays, prof = [], None
    for p in parts:
        with rasterio.open(p) as s:
            arrays.append(s.read(1))
            prof = prof or s.profile
    z = np.vstack(arrays)
    t = rasterio.open(parts[0]).transform
    prof.update(height=z.shape[0], width=z.shape[1], transform=t, compress="deflate", predictor=3)
    with rasterio.open(DEM_X, "w", **prof) as d:
        d.write(z, 1)
    print(f"joined {len(parts)} terrain strips into {DEM_X.name}: {z.shape[1]} x {z.shape[0]}")
    return DEM_X


def tiles(box=WINDOW):
    """(col, row, left, bottom) for every TILE x TILE tile in box, north-west first."""
    l, b, r, t = box
    out = []
    for j, y in enumerate(range(t - TILE, b - 1, -TILE)):
        for i, x in enumerate(range(l, r, TILE)):
            out.append((i, j, x, y))
    return out


def in_core(x, y) -> bool:
    l, b, r, t = CORE
    return l <= x < r and b <= y < t


def tile_path(x, y) -> Path:
    return TILES / f"t_{x}_{y}.tif"


def strip_bounds():
    """(left, top, right, bottom) of each texture strip, north first, on whole TEX_RES rows."""
    rows = int((TOP - BOTTOM) / TEX_RES)
    edges = [TOP - round(rows * k / TEX_STRIPS) * TEX_RES for k in range(TEX_STRIPS + 1)]
    return [(LEFT, edges[k], RIGHT, edges[k + 1]) for k in range(TEX_STRIPS)]


def image_tiles():
    """(path, left, top, right, bottom) for each texture strip, north first."""
    return [(DATA / f"texture_{k}.jpg", *b) for k, b in enumerate(strip_bounds())]


def graded(path: Path) -> Path:
    return path
