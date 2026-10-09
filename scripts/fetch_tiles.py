"""Step 1 (on GitHub Actions): lidar ground returns -> bare-earth tiles.

Each 2 km tile of the window is read from the USGS 3DEP point-cloud archive on AWS
(Entwine Point Tiles: PDAL pulls only the octree nodes inside the tile), keeps the
ground returns (ASPRS class 2), and grids them with inverse-distance weighting:

  core tiles    the canyon, at 1 m from every ground return
  margin tiles  the ridges and valley around it, at 4 m from a coarser level of
                the octree (fewer points to move; the viewer only needs 4 m there)

Cells with no ground return nearby stay empty (NaN): lakes, mostly. Where the first
project leaves more than 2% of a tile empty, the next one fills it, shifted by the
median height difference where both have ground, so no step shows at the seam.

    python scripts/fetch_tiles.py --shard 0 --shards 8      # this runner's share of tiles
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

EPT = "https://s3-us-west-2.amazonaws.com/usgs-lidar-public/{name}/ept.json"
PAD = 25.0                      # metres of points read beyond the tile, so IDW is right at its edge
TO_LL = Transformer.from_crs(config.CRS, "EPSG:4326", always_xy=True)
TO_WM = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)


def grid(project: str, x0: float, y0: float, res: float, part: Path) -> tuple[np.ndarray, int]:
    """Ground returns from one project gridded on the tile; NaN where there are none."""
    import pdal
    x1, y1 = x0 + config.TILE, y0 + config.TILE
    lons, lats = TO_LL.transform([x0 - PAD, x1 + PAD, x0 - PAD, x1 + PAD], [y0 - PAD, y0 - PAD, y1 + PAD, y1 + PAD])
    mx, my = TO_WM.transform(lons, lats)
    reader = {"type": "readers.ept", "filename": EPT.format(name=project), "threads": 6,
              "bounds": f"([{min(mx)},{max(mx)}],[{min(my)},{max(my)}])"}
    if res > 1.0:
        reader["resolution"] = res / 2           # a coarser octree level is plenty for a 4 m grid
    pipe = [reader,
            {"type": "filters.range", "limits": "Classification[2:2]"},
            {"type": "filters.reprojection", "out_srs": config.CRS},
            {"type": "filters.crop", "bounds": f"([{x0 - PAD},{x1 + PAD}],[{y0 - PAD},{y1 + PAD}])"},
            {"type": "writers.gdal", "filename": str(part), "resolution": res,
             "output_type": "idw,count", "radius": res * 2.5, "window_size": 3,
             "nodata": -9999, "data_type": "float32",
             "bounds": f"([{x0},{x1 - res / 2}],[{y0},{y1 - res / 2}])"}]
    n = int(pdal.Pipeline(json.dumps({"pipeline": pipe})).execute())
    if n == 0 or not part.exists():
        return np.full((int(config.TILE / res),) * 2, np.nan, np.float32), 0
    with rasterio.open(part) as s:
        z, c = s.read(1), s.read(2)
    part.unlink()
    return np.where((z == -9999) | (c <= 0), np.nan, z).astype(np.float32), n


def tile(x0: int, y0: int) -> dict:
    res = config.CORE_RES if config.in_core(x0, y0) else config.MARGIN_RES
    out = config.tile_path(x0, y0)
    z, used, n_total, shift = None, [], 0, {}
    for proj in config.PROJECTS:
        zi, n = grid(proj, x0, y0, res, out.with_suffix(f".{len(used)}.tif"))
        if n == 0:
            continue
        if z is None:
            z = zi
        else:
            both = np.isfinite(z) & np.isfinite(zi)
            dz = float(np.median(z[both] - zi[both])) if both.sum() > 500 else 0.0
            hole = ~np.isfinite(z) & np.isfinite(zi)
            z[hole] = zi[hole] + dz
            shift[proj] = round(dz, 3)
        used.append(proj)
        n_total += n
        if np.isfinite(z).mean() > 0.98:
            break
    if z is None:
        z = np.full((int(config.TILE / res),) * 2, np.nan, np.float32)
    prof = dict(driver="GTiff", width=z.shape[1], height=z.shape[0], count=1, dtype="float32",
                crs=config.CRS, transform=from_origin(x0, y0 + config.TILE, res, res), nodata=np.nan,
                compress="deflate", predictor=3, tiled=True, blockxsize=256, blockysize=256)
    with rasterio.open(out, "w", **prof) as d:
        d.write(z, 1)
        d.update_tags(projects=",".join(used), ground=str(n_total), shift=json.dumps(shift))
    return {"res": res, "projects": used, "ground": n_total, "cover": float(np.isfinite(z).mean()), "shift": shift}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    a = ap.parse_args()
    config.TILES.mkdir(parents=True, exist_ok=True)
    mine = [t for k, t in enumerate(config.tiles()) if k % a.shards == a.shard]
    print(f"shard {a.shard + 1} of {a.shards}: {len(mine)} tiles", flush=True)
    for k, (_, _, x0, y0) in enumerate(mine):
        if config.tile_path(x0, y0).exists():
            continue
        t0 = time.time()
        for attempt in range(3):                       # the archive occasionally drops a connection
            try:
                info = tile(x0, y0)
                break
            except Exception as e:
                print(f"  tile {x0},{y0}: {e} (attempt {attempt + 1})", flush=True)
                time.sleep(10)
        else:
            raise SystemExit(f"tile {x0},{y0} failed three times")
        print(f"  [{k + 1}/{len(mine)}] {x0},{y0} at {info['res']:g} m: {info['ground']:,} ground returns, "
              f"{info['cover']:.1%} covered, {'+'.join(info['projects']) or 'no lidar'} "
              f"{info['shift'] or ''} ({time.time() - t0:.0f} s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
