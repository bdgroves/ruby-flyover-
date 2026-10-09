"""The flight: where the camera is and what it looks at, every frame.

The path is a handful of keyframes (a time, an eye point and an aim point, each
as latitude, longitude and altitude in metres). The eye follows a smooth spline
through its points at a speed that changes smoothly between keyframes; the aim
glides between its points the same way. Nothing here needs the viewer, so

    pixi run path

draws the flight over the terrain (out/flight_map.png) and prints the speed and
the height above the ground along the way.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import ndimage
from scipy.interpolate import PchipInterpolator

import config
import route

# Each keyframe: (eye, aim, speed through this point m/s, lens fov deg, note).
# Times come from the speeds: each stretch takes its length / the average of its two speeds.
# An eye or aim point is given one of two ways:
#   ("c", f, up)          on the canyon floor, a fraction f of the way from the mouth (0) to
#                         Lamoille Lake (1), `up` metres above the floor there
#   ("ll", lat, lon, up)  a map point, `up` metres above the (smoothed) ground
# The floor is the centreline found in the lidar (data/centerline.json), so the camera
# follows the real canyon. Heights above the floor, not the local ground, keep the
# flight level past side canyons and talus; the clearance check below lifts it if
# anything comes within MIN_CLEARANCE.
LAMOILLE_LAKE = (40.5925, -115.3939)
LIBERTY_LAKE = (40.5800, -115.3950)
LIBERTY_PASS = (40.5860, -115.3948)   # approximate: the saddle between the two lakes

KEYS = [
    # 1. From high over the Lamoille valley, the whole canyon laid out to the south-east.
    (("ll", 40.712, -115.500, 650), ("c", 0.40, 0), 160, 50, "Lamoille Canyon from the valley"),
    # 2. In at the mouth, dropping into the glacial trough.
    (("c", 0.00, 320), ("c", 0.14, 60), 210, 50, "In at the mouth"),
    (("c", 0.16, 200), ("c", 0.32, 60), 240, 52, "The U-shaped trough"),
    # 3. Low and fast up the canyon, the sun behind us lighting the north-east wall.
    (("c", 0.38, 180), ("c", 0.55, 60), 250, 52, "Up the canyon"),
    (("c", 0.60, 170), ("c", 0.70, 200), 230, 52, "Past the hanging valleys"),
    # 4. Slowing past Roads End and up the headwall to Lamoille Lake.
    (("c", 0.80, 170), ("c", 1.00, 80), 160, 50, "Up the headwall"),
    (("c", 0.97, 130), ("ll", *LIBERTY_PASS, 120), 100, 50, "Lamoille Lake"),
    # 5. Over Liberty Pass, and Liberty Lake below.
    (("ll", *LIBERTY_PASS, 110), ("ll", *LIBERTY_LAKE, 10), 75, 50, "Over Liberty Pass"),
    # 6. Climbing over Liberty Lake, swing west along the crest into the low sun...
    (("ll", 40.5795, -115.3965, 260), ("ll", 40.5780, -115.4250, 0), 50, 50, "West along the crest"),
    # 7. ...and on round to look back down the upper canyon (the lower canyon is round the bend,
    #    behind the ridge), with the evening sun off to the left.
    (("ll", 40.5810, -115.3990, 420), ("c", 0.80, 0), 45, 50, "Back down Lamoille Canyon"),
]
HOLD_S = 4.0
FPS = 30
MIN_CLEARANCE = 70.0           # metres above the highest ground within LOOK_RADIUS of the eye
LOOK_RADIUS = 120.0
FOV = 50.0
ORBIT_MAX = 600.0             # metres; see camera(): same view, target pulled close along the line of sight


def to_utm(lat, lon):
    from rasterio.warp import transform
    xs, ys = transform("EPSG:4326", config.CRS, list(np.atleast_1d(lon)), list(np.atleast_1d(lat)))
    return np.array(xs), np.array(ys)


def centerline():
    import json
    pts = json.loads((config.DATA / "centerline.json").read_text())
    return (np.array([p["s"] for p in pts]), np.array([p["x"] for p in pts]),
            np.array([p["y"] for p in pts]), np.array([p["z"] for p in pts]))


_SMOOTH = {}


def smooth_ground(dem: np.ndarray) -> np.ndarray:
    """The ground blurred over about 300 m: what 'metres above the ground' means for a key."""
    key = id(dem)
    if key not in _SMOOTH:
        _SMOOTH.clear()
        _SMOOTH[key] = ndimage.gaussian_filter(dem.astype(np.float32), 300.0 / config.DEM_RES / 2)
    return _SMOOTH[key]


def resolve(spec, dem: np.ndarray) -> tuple[float, float, float]:
    """A key point as (easting, northing, altitude)."""
    if spec[0] == "c":
        s, x, y, z = centerline()
        at = spec[1] * s[-1]
        floor = ndimage.gaussian_filter1d(z, 4, mode="nearest")      # the floor, without its steps
        return float(np.interp(at, s, x)), float(np.interp(at, s, y)), float(np.interp(at, s, floor) + spec[2])
    _, lat, lon, up = spec
    x, y = to_utm([lat], [lon])
    g = float(sample(smooth_ground(dem), x, y)[0])
    return float(x[0]), float(y[0]), g + up


def catmull_rom(points: np.ndarray, samples: int = 400) -> np.ndarray:
    """Uniform Catmull-Rom through the points; `samples` per segment, keyframe k at index k*samples."""
    p = np.vstack([points[0], points, points[-1]])
    t = np.linspace(0.0, 1.0, samples, endpoint=False)[:, None]
    seg = [0.5 * (2 * p1 + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t ** 2
                  + (-p0 + 3 * p1 - 3 * p2 + p3) * t ** 3)
           for p0, p1, p2, p3 in zip(p[:-3], p[1:-2], p[2:-1], p[3:])]
    return np.vstack(seg + [points[-1:]])


@dataclass
class Flight:
    t: np.ndarray        # seconds, per frame
    eye: np.ndarray      # (n, 3) easting, northing, altitude
    aim: np.ndarray      # (n, 3)
    fov: np.ndarray      # lens, degrees
    clearance: np.ndarray
    speed: np.ndarray    # m/s along the path
    lifted: float        # most the clearance check had to raise the path, metres
    key_t: np.ndarray    # when the camera passes each keyframe, seconds


def ground_max(dem: np.ndarray) -> np.ndarray:
    k = max(1, int(round(LOOK_RADIUS / config.DEM_RES)))
    return ndimage.maximum_filter(dem, size=2 * k + 1)


def sample(grid: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    col = (x - config.LEFT) / config.DEM_RES - 0.5
    row = (config.TOP - y) / config.DEM_RES - 0.5
    return ndimage.map_coordinates(grid, [row, col], order=1, mode="nearest")


def key_times(s_k: np.ndarray) -> np.ndarray:
    v = np.array([k[2] for k in KEYS], dtype=float)
    seg = np.diff(s_k) / np.maximum((v[:-1] + v[1:]) / 2, 5.0)
    return np.concatenate([[0.0], np.cumsum(seg)])


def plan(dem: np.ndarray | None = None, fps: int = FPS) -> Flight:
    if dem is None:
        raise SystemExit("needs the DTM: unpack the flyover-data branch into data\\ (see README)")
    eye_k = np.array([resolve(k[0], dem) for k in KEYS])
    aim_k = np.array([resolve(k[1], dem) for k in KEYS])

    samples = 400
    dense = catmull_rom(eye_k, samples)
    arc = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(dense, axis=0), axis=1))])
    s_k = arc[np.arange(len(KEYS)) * samples]
    times = key_times(s_k)
    # the hold: one more key at the same place, HOLD_S later
    times = np.append(times, times[-1] + HOLD_S)
    s_k = np.append(s_k, s_k[-1])
    aim_k = np.vstack([aim_k, aim_k[-1]])
    fov_k = np.array([k[3] for k in KEYS] + [KEYS[-1][3]], dtype=float)
    s_of_t = PchipInterpolator(times, s_k)            # smooth, never runs backwards

    n = int(round(times[-1] * fps)) + 1
    t = np.arange(n) / fps
    s = s_of_t(t)
    eye = np.column_stack([np.interp(s, arc, dense[:, j]) for j in range(3)])
    # The view turns smoothly: heading, pitch and the distance to the aim are interpolated
    # between keyframes (heading the short way round), rather than the aim point itself,
    # which would whip the camera round whenever the aim swings past the eye.
    eye_at_k = np.vstack([eye_k, eye_k[-1]])
    rel = aim_k - eye_at_k
    head = np.unwrap(np.arctan2(rel[:, 0], rel[:, 1]))
    pitch = np.arctan2(rel[:, 2], np.hypot(rel[:, 0], rel[:, 1]))
    dist = np.log(np.linalg.norm(rel, axis=1))
    hd, pt, ds = (PchipInterpolator(times, v)(t) for v in (head, pitch, dist))
    r = np.exp(ds)
    aim = eye + np.column_stack([r * np.cos(pt) * np.sin(hd), r * np.cos(pt) * np.cos(hd), r * np.sin(pt)])
    fov = PchipInterpolator(times, fov_k)(t)
    speed = np.abs(s_of_t(t, 1))

    lifted = 0.0
    clear = np.full(n, np.nan)
    if dem is not None:
        top = sample(ground_max(dem), eye[:, 0], eye[:, 1])
        need = np.maximum(0.0, top + MIN_CLEARANCE - eye[:, 2])
        if need.max() > 0:
            # raise smoothly: spread each lift over a couple of seconds either side
            lift = ndimage.maximum_filter1d(need, size=2 * fps + 1)
            lift = ndimage.gaussian_filter1d(lift, fps * 0.8)
            lift = np.maximum(lift, need)
            eye[:, 2] += lift
            lifted = float(lift.max())
        clear = eye[:, 2] - sample(dem, eye[:, 0], eye[:, 1])
    return Flight(t, eye, aim, fov, clear, speed, lifted, times)


def camera(eye: np.ndarray, aim: np.ndarray, min_h: float, fov: float = FOV) -> dict:
    """The viewer's orbit camera (target, distance, angles) for an eye looking at an aim point.
    Viewer world: x = easting, y = height above the DEM minimum, z = -northing."""
    def world(p):
        return np.array([p[0], p[2] - min_h, -p[1]])
    e, a = world(eye), world(aim)
    off = e - a
    r = float(np.linalg.norm(off))
    if r > ORBIT_MAX:
        # The viewer quietly caps the orbit radius (about 50 km for this terrain), which
        # would pull a high camera down toward its target. The same view, from the same
        # eye, has its target anywhere along the line of sight: put it within the cap.
        a = e - off * (ORBIT_MAX / r)
        off = e - a
        r = ORBIT_MAX
    return {"cmd": "set_terrain_camera", "phi_deg": math.degrees(math.atan2(off[2], off[0])),
            "theta_deg": math.degrees(math.acos(np.clip(off[1] / r, -1, 1))), "radius": r,
            "fov_deg": fov, "target": [float(v) for v in a]}


def project(points: np.ndarray, eye: np.ndarray, aim: np.ndarray, fov: float, w: int, h: int):
    """Where map points (easting, northing, altitude) land on screen for this camera:
    pixel x, pixel y and distance in metres; x and y are NaN behind the camera.
    The same pinhole as the viewer's camera: vertical field of view, world up = height."""
    pts = np.atleast_2d(points).astype(float)
    fwd = aim - eye
    fwd = fwd / np.linalg.norm(fwd)
    up = np.array([0.0, 0.0, 1.0])
    right = np.cross(fwd, up)                      # map frame: x east, y north, z up
    right /= np.linalg.norm(right)
    cam_up = np.cross(right, fwd)
    d = pts - eye
    z = d @ fwd
    t = math.tan(math.radians(fov) / 2)
    with np.errstate(divide="ignore", invalid="ignore"):
        x = (d @ right) / z / (t * w / h)
        y = (d @ cam_up) / z / t
    px, py = (x + 1) * w / 2, (1 - y) * h / 2
    behind = z <= 0
    px[behind] = py[behind] = np.nan
    return px, py, np.linalg.norm(d, axis=1)


def report(f: Flight) -> None:
    fps = round(1 / (f.t[1] - f.t[0])) if len(f.t) > 1 else FPS
    print(f"{len(f.t)} frames, {f.t[-1]:.0f} s at {fps} fps")
    for t0, k in zip(f.key_t, KEYS):
        i = min(int(round(t0 * fps)), len(f.t) - 1)
        c = f"{f.clearance[i]:5.0f} m up" if np.isfinite(f.clearance[i]) else ""
        print(f"  {t0:5.1f} s  {k[4]:<22} {f.speed[i]:5.0f} m/s  lens {f.fov[i]:3.0f}  {c}")
    if np.isfinite(f.clearance).any():
        i = int(np.nanargmin(f.clearance))
        print(f"  lowest: {f.clearance[i]:.0f} m above the ground at {f.t[i]:.1f} s; "
              f"path raised by up to {f.lifted:.0f} m to clear the terrain")


def draw_map(f: Flight, dem: np.ndarray, path) -> None:
    """Hillshade with the flight drawn on: eye track coloured by time, aim lines every 2 s."""
    from PIL import Image, ImageDraw
    step = 3
    z = dem[::step, ::step].astype(np.float64)
    gy, gx = np.gradient(z, config.DEM_RES * step)
    az, el = np.radians(315), np.radians(40)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    shade = np.sin(el) * np.cos(slope) + np.cos(el) * np.sin(slope) * np.cos(az - aspect)
    zn = (z - z.min()) / (z.max() - z.min())
    base = np.clip(0.25 + 0.6 * shade, 0, 1)[..., None] * (0.55 + 0.45 * zn[..., None]) * np.array([235, 230, 220])
    img = Image.fromarray(base.astype(np.uint8)).convert("RGB")
    d = ImageDraw.Draw(img)

    def px(x, y):
        return ((x - config.LEFT) / (config.DEM_RES * step), (config.TOP - y) / (config.DEM_RES * step))

    n = len(f.t)
    fps = round(1 / (f.t[1] - f.t[0])) if n > 1 else FPS
    for i in range(0, n, max(1, fps * 2)):
        d.line([px(*f.eye[i, :2]), px(*f.aim[i, :2])], fill=(255, 255, 255), width=1)
    for i in range(n - 1):
        u = i / max(n - 1, 1)
        col = (int(255 * u), int(80 + 100 * (1 - u)), int(255 * (1 - u)))
        d.line([px(*f.eye[i, :2]), px(*f.eye[i + 1, :2])], fill=col, width=3)
    for t0, k in zip(f.key_t, KEYS):
        note = k[4]
        i = min(int(round(t0 * fps)), n - 1)
        x, y = px(*f.eye[i, :2])
        d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=(255, 255, 0))
        d.text((x + 6, y - 6), f"{t0:g}s {note}", fill=(255, 255, 0))
    names = list(route.TOWNS)
    tx, ty = to_utm([route.TOWNS[n][0] for n in names], [route.TOWNS[n][1] for n in names])
    for n, x0, y0 in zip(names, tx, ty):
        x, y = px(x0, y0)
        d.rectangle([x - 3, y - 3, x + 3, y + 3], fill=route.ROUTE_RGB)
        d.text((x + 6, y + 4), route.TOWNS[n][2], fill=route.ROUTE_RGB)
    img.save(path)
    print(f"wrote {path}")


def main() -> int:
    import rasterio
    dem = None
    config.dtm()
    if config.DEM_X.exists():
        with rasterio.open(config.DEM_X) as src:
            dem = src.read(1)
    f = plan(dem)
    report(f)
    if dem is not None:
        config.OUT.mkdir(parents=True, exist_ok=True)
        draw_map(f, dem, config.OUT / "flight_map.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
