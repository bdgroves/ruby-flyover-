"""Step 2 (on GitHub Actions): tiles -> everything the flyover needs, in data/.

  dtm.tif          bare earth over the whole window at GEOM_RES (4 m), true scale, holes
                   filled: lakes flat at their shoreline, small gaps from their neighbours
  texture_k.jpg    the USDA NAIP aerial photo at 2 m, lightly shaded by the lidar so the
                   moraines, talus and glacial polish read (shading worked out at 1 m in
                   the canyon), in north-to-south strips
  centerline.json  the canyon floor from the mouth to Lamoille Lake, found in the DTM
                   (the cheapest way up, where height is expensive), for the flight
  place.json       bounds, heights, NAIP year, lidar sources: all the renderer needs
  quicklook.png    the texture at 16 m with the centreline, to check the build by eye

    python scripts/assemble.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import from_origin
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

Image.MAX_IMAGE_PIXELS = None

# Places the flight is built around (Wikipedia; the mouth is approximate, at the
# Powerhouse picnic area, and only needs to be near the canyon floor).
MOUTH = (40.692, -115.466)
LAMOILLE_LAKE = (40.5925, -115.3939)
LIBERTY_LAKE = (40.5800, -115.3950)


def to_xy(lat, lon):
    from rasterio.warp import transform
    xs, ys = transform("EPSG:4326", config.CRS, [lon], [lat])
    return xs[0], ys[0]


def mosaic(box, res, which):
    """Tiles of one kind (core or margin) onto a grid over box, NaN where none."""
    l, b, r, t = box
    w, h = int((r - l) / res), int((t - b) / res)
    z = np.full((h, w), np.nan, np.float32)
    meta = {}
    for _, _, x0, y0 in config.tiles(box):
        if config.in_core(x0, y0) != which:
            continue
        p = config.tile_path(x0, y0)
        if not p.exists():
            raise SystemExit(f"missing tile {p.name}")
        with rasterio.open(p) as s:
            zi, tags, tres = s.read(1), s.tags(), s.res[0]
        if tres != res:                                  # a finer tile on a coarser grid: block mean
            k = int(round(res / tres))
            with np.errstate(invalid="ignore"):
                zi = np.nanmean(zi.reshape(zi.shape[0] // k, k, zi.shape[1] // k, k), axis=(1, 3))
        c0, r0 = int((x0 - l) / res), int((t - (y0 + config.TILE)) / res)
        z[r0:r0 + zi.shape[0], c0:c0 + zi.shape[1]] = zi
        meta[p.name] = tags
    return z, meta


def fill(z: np.ndarray, res: float, lake_m2: float = 1500.0) -> np.ndarray:
    """Big enclosed holes are lakes: flat at the shoreline (the low 10th percentile of the ring
    around them). Everything else takes the nearest ground."""
    bad = ~np.isfinite(z)
    if not bad.any():
        return z
    lab, n = ndimage.label(bad)
    sizes = ndimage.sum(bad, lab, index=np.arange(1, n + 1)) * res * res
    edge = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])))
    out = z.copy()
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        if sl is None or i in edge or sizes[i - 1] < lake_m2:
            continue
        rs = slice(max(sl[0].start - 4, 0), sl[0].stop + 4)
        cs = slice(max(sl[1].start - 4, 0), sl[1].stop + 4)
        hole = lab[rs, cs] == i
        ring = ndimage.binary_dilation(hole, iterations=3) & ~hole & np.isfinite(z[rs, cs])
        if ring.any():
            sub = out[rs, cs]
            sub[hole] = np.percentile(z[rs, cs][ring], 10)
    del lab
    rest = ~np.isfinite(out)
    if rest.any():
        idx = ndimage.distance_transform_edt(rest, return_distances=False, return_indices=True)
        out = out[tuple(idx)]
    return out.astype(np.float32)


def shading(z: np.ndarray, res: float) -> np.ndarray:
    """A gentle, sun-independent shade factor around 1: soft light from all round, plus the
    local relief (ground above or below its 10 m surroundings) for the fine texture. The
    renderer adds the real sun on top, so nothing here leans one way."""
    gy, gx = np.gradient(z, res)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    el = np.radians(45)
    mdh = np.zeros_like(z)
    for az in np.radians([0, 60, 120, 180, 240, 300]):
        mdh += np.sin(el) * np.cos(slope) + np.cos(el) * np.sin(slope) * np.cos(az - aspect)
    mdh /= 6.0                                   # ~0.7 on flat ground, lower on steep slopes
    del gx, gy, slope, aspect
    lrm = z - ndimage.gaussian_filter(z, 10.0 / res)
    detail = np.tanh(lrm / 1.2)
    return ((0.80 + 0.30 * mdh) * (1.0 + 0.20 * detail)).astype(np.float32)


def shading_strips(z: np.ndarray, res: float, rows: int = 2000, pad: int = 64) -> np.ndarray:
    """shading() a strip at a time with overlap, so a 1 m grid doesn't need many copies at once."""
    out = np.empty_like(z)
    for a in range(0, z.shape[0], rows):
        lo, hi = max(a - pad, 0), min(a + rows + pad, z.shape[0])
        s = shading(z[lo:hi], res)
        out[a:min(a + rows, z.shape[0])] = s[a - lo:a - lo + min(rows, z.shape[0] - a)]
    return out


def block(a: np.ndarray, k: int) -> np.ndarray:
    h, w = a.shape[0] // k, a.shape[1] // k
    return a[:h * k, :w * k].reshape(h, k, w, k).mean(axis=(1, 3))


def naip(box, res):
    """NAIP RGB on a res grid over box from Microsoft's Planetary Computer, newest first, read
    from the first overview (about 1.2 m) since the texture is 2 m."""
    import planetary_computer
    import pystac_client
    from rasterio.warp import Resampling, reproject, transform_bounds
    l, b, r, t = box
    w, h = int((r - l) / res), int((t - b) / res)
    T = from_origin(l, t, res, res)
    cat = pystac_client.Client.open("https://planetarycomputer.microsoft.com/api/stac/v1",
                                    modifier=planetary_computer.sign_inplace)
    bbox = transform_bounds(config.CRS, "EPSG:4326", l, b, r, t, densify_pts=21)
    items = sorted(cat.search(collections=["naip"], bbox=bbox).items(), key=lambda it: it.datetime, reverse=True)
    out = np.zeros((3, h, w), np.uint8)
    have = np.zeros((h, w), bool)
    years = []
    for it in items:
        band = np.zeros((3, h, w), np.uint8)
        try:
            src = rasterio.open(it.assets["image"].href, overview_level=0)
        except Exception:
            src = rasterio.open(it.assets["image"].href)
        with src:
            for k in range(3):
                reproject(rasterio.band(src, k + 1), band[k], dst_transform=T, dst_crs=config.CRS,
                          resampling=Resampling.average, dst_nodata=0)
        new = (band.max(axis=0) > 0) & ~have
        if new.any():
            out[:, new] = band[:, new]
            have |= new
            years.append(it.datetime.year)
        if have.mean() > 0.999:
            break
    print(f"= NAIP {sorted(set(years), reverse=True)}: {have.mean():.1%} of the window", flush=True)
    return np.moveaxis(out, 0, -1), (max(years) if years else None)


def centerline(dem: np.ndarray, res: float):
    """The canyon floor, mouth to Lamoille Lake: the cheapest path when every metre of height
    costs a lot, on a 12 m grid. Smoothed and resampled every 50 m."""
    from skimage.graph import route_through_array
    k = max(1, int(round(12 / res)))
    z = block(dem, k)
    cres = res * k

    def cell(lat, lon, search_m):
        x, y = to_xy(lat, lon)
        c, r = int((x - config.LEFT) / cres), int((config.TOP - y) / cres)
        s = int(search_m / cres)
        win = z[r - s:r + s + 1, c - s:c + s + 1]
        dr, dc = np.unravel_index(np.argmin(win), win.shape)
        return r - s + dr, c - s + dc

    a, b = cell(*MOUTH, 600), cell(*LAMOILLE_LAKE, 120)
    zmin = z[a]
    cost = 1.0 + ((z - zmin).clip(0) / 60.0) ** 2
    path, _ = route_through_array(cost, a, b, fully_connected=True, geometric=True)
    p = np.array(path, float)
    xy = np.column_stack([config.LEFT + (p[:, 1] + 0.5) * cres, config.TOP - (p[:, 0] + 0.5) * cres])
    seg = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    s = np.concatenate([[0], np.cumsum(seg)])
    xy = np.column_stack([ndimage.gaussian_filter1d(xy[:, j], 8, mode="nearest") for j in range(2)])
    seg = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    s = np.concatenate([[0], np.cumsum(seg)])
    ss = np.arange(0, s[-1], 50.0)
    xy = np.column_stack([np.interp(ss, s, xy[:, j]) for j in range(2)])
    from rasterio.warp import transform
    lon, lat = transform(config.CRS, "EPSG:4326", list(xy[:, 0]), list(xy[:, 1]))
    rows = ((config.TOP - xy[:, 1]) / res).astype(int)
    cols = ((xy[:, 0] - config.LEFT) / res).astype(int)
    zz = dem[rows.clip(0, dem.shape[0] - 1), cols.clip(0, dem.shape[1] - 1)]
    print(f"= canyon floor: {ss[-1] / 1000:.1f} km from the mouth ({zz[0]:.0f} m) "
          f"to Lamoille Lake ({zz[-1]:.0f} m)", flush=True)
    return [{"s": round(float(a_), 1), "x": round(float(x), 1), "y": round(float(y), 1),
             "lat": round(la, 6), "lon": round(lo, 6), "z": round(float(zz_), 1)}
            for a_, (x, y), la, lo, zz_ in zip(ss, xy, lat, lon, zz)]


def main() -> int:
    D = config.DATA
    res_g, res_t = config.GEOM_RES, config.TEX_RES
    # margin first, at 4 m over the whole window; then the core at 1 m
    zm, meta_m = mosaic(config.WINDOW, config.MARGIN_RES, which=False)
    zc, meta_c = mosaic(config.CORE, config.CORE_RES, which=True)
    cov_core = float(np.isfinite(zc).mean())
    print(f"= core {zc.shape[1]} x {zc.shape[0]} at {config.CORE_RES:g} m: {cov_core:.1%} with ground returns", flush=True)
    zc = fill(zc, config.CORE_RES)
    # the core, averaged to 4 m, into the window
    k = int(config.MARGIN_RES / config.CORE_RES)
    cl, cb, cr, ct = config.CORE
    r0, c0 = int((config.TOP - ct) / config.MARGIN_RES), int((cl - config.LEFT) / config.MARGIN_RES)
    core4 = block(zc, k)
    zm[r0:r0 + core4.shape[0], c0:c0 + core4.shape[1]] = core4
    print(f"= margin at {config.MARGIN_RES:g} m: {np.isfinite(zm).mean():.1%} with ground returns", flush=True)
    zm = fill(zm, config.MARGIN_RES)

    # geometry
    if res_g != config.MARGIN_RES:
        from rasterio.warp import Resampling, reproject
        w, h = int((config.RIGHT - config.LEFT) / res_g), int((config.TOP - config.BOTTOM) / res_g)
        g = np.zeros((h, w), np.float32)
        reproject(zm, g, src_transform=from_origin(config.LEFT, config.TOP, 4, 4), src_crs=config.CRS,
                  dst_transform=from_origin(config.LEFT, config.TOP, res_g, res_g), dst_crs=config.CRS,
                  resampling=Resampling.bilinear)
    else:
        g = zm
    prof = dict(driver="GTiff", width=g.shape[1], height=g.shape[0], count=1, dtype="float32", crs=config.CRS,
                transform=from_origin(config.LEFT, config.TOP, res_g, res_g), compress="deflate", predictor=3,
                tiled=True, blockxsize=256, blockysize=256)
    with rasterio.open(D / "dtm.tif", "w", **prof) as d:
        d.write(g, 1)
    print(f"= dtm.tif {g.shape[1]} x {g.shape[0]} at {res_g:g} m, {g.min():.0f}-{g.max():.0f} m, "
          f"{(D / 'dtm.tif').stat().st_size / 1e6:.0f} MB", flush=True)

    # shading at 2 m: margin from 4 m (doubled), the core from 1 m (averaged in pairs)
    sh = np.repeat(np.repeat(shading(zm, config.MARGIN_RES), 2, axis=0), 2, axis=1)
    shc = block(shading_strips(zc, config.CORE_RES), 2)
    r0, c0 = int((config.TOP - ct) / res_t), int((cl - config.LEFT) / res_t)
    sh[r0:r0 + shc.shape[0], c0:c0 + shc.shape[1]] = shc
    del zc, shc
    rgb, year = naip(config.WINDOW, res_t)
    tex = np.clip(rgb.astype(np.float32) / 255.0 * sh[..., None], 0, 1)
    tex = (tex * 255 + 0.5).astype(np.uint8)
    del rgb, sh
    for k_, (p, l, t, r, b) in enumerate(config.image_tiles()):
        a_, b_ = int((config.TOP - t) / res_t), int((config.TOP - b) / res_t)
        Image.fromarray(tex[a_:b_]).save(p, quality=92, subsampling=0)
        print(f"= {p.name}: {tex.shape[1]} x {b_ - a_}, {p.stat().st_size / 1e6:.0f} MB", flush=True)

    line = centerline(zm, config.MARGIN_RES)
    (D / "centerline.json").write_text(json.dumps(line))

    q = Image.fromarray(tex[::8, ::8])
    from PIL import ImageDraw
    d = ImageDraw.Draw(q)
    pts = [((p["x"] - config.LEFT) / 16, (config.TOP - p["y"]) / 16) for p in line]
    d.line(pts, fill=(255, 220, 0), width=2)
    q.save(D / "quicklook.png")

    used = sorted({p for m in list(meta_m.values()) + list(meta_c.values()) for p in m.get("projects", "").split(",") if p})
    ground = sum(int(m.get("ground", 0)) for m in list(meta_m.values()) + list(meta_c.values()))
    shifts = {}
    for m in list(meta_m.values()) + list(meta_c.values()):
        for proj, v in json.loads(m.get("shift", "{}")).items():
            shifts.setdefault(proj, []).append(v)
    info = {"name": "Lamoille Canyon", "crs": config.CRS, "bounds": list(config.WINDOW), "core": list(config.CORE),
            "geom_res_m": res_g, "tex_res_m": res_t, "elev_m": [float(g.min()), float(g.max())],
            "naip_year": year, "lidar": used, "ground_returns": ground, "core_coverage": round(cov_core, 4),
            "seam_shift_m": {p: round(float(np.median(v)), 3) for p, v in shifts.items()}}
    (D / "place.json").write_text(json.dumps(info, indent=1))
    print(f"= lidar {' + '.join(used)}: {ground:,} ground returns; seam shifts {info['seam_shift_m']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
