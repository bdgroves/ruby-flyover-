"""Fly up Lamoille Canyon to Liberty Lake with forge3d, and write the MP4.

    f3d scripts\render.py --stills "5,30,60,95,125" --size 960x540 --lite   # check the look
    f3d scripts\render.py --preview                                        # every 3rd frame at 640x360
    do { f3d scripts\render.py --frames-dir out\frames --lite } until ($LASTEXITCODE -eq 0)
    f3d scripts\render.py --encode out\frames                              # the whole flight, 1080p

Terrain: USGS 3DEP bare-earth lidar, true scale (1 m in the canyon, gridded to 4 m
for the viewer). Texture: the USDA NAIP aerial photo with the 1 m lidar shading
baked in. Late-afternoon August sun from the west-north-west, a painted blue sky,
and a thin pale haze along the skyline so the far ridges sit back.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image

import forge3d as f3d

import config
import flight

# A clear high-desert sky: deep blue overhead, paling toward the skyline.
ZENITH = np.array([46, 104, 178]) / 255.0
HORIZON = np.array([196, 214, 230]) / 255.0
WARM = np.array([0.11, 0.045, -0.05])       # late-afternoon highlights...
COOL = np.array([-0.015, 0.0, 0.02])       # ...and only a breath of blue in the shade
CONTRAST = 1.20
LEVELS = (0.05, 0.97)                      # black and white points: the raw frames are flat and pale
SKYLINE_HAZE = 0.20                      # pale haze on the ground just under the skyline
SATURATE = 1.30                          # late-summer sage and granite are pale in the photo
# Terrain shadows come from sun_visibility (traced through the heightfield), not a shadow
# map: over a 100 km scene the shadow map's texels are tens of metres and leave blocky
# grey patches on flat ground like the lakes.
PBR = {"enabled": True, "exposure": 0.9, "msaa": 8, "shadow_technique": "none",
       "height_ao": {"enabled": True, "strength": 0.7, "max_distance": 150.0},
       "sun_visibility": {"enabled": True, "mode": "soft", "max_distance": 6000.0}}
LITE = {"msaa": 4}
PRESERVE_COLORS = False     # forge3d's colour-preserving overlay mode crushes shadows to black
# forge3d's physical sky with aerial perspective (distant walls fade into the air), and a
# thin layer of valley haze that catches the low sun as light shafts.
SKY = {"enabled": True, "turbidity": 3.2, "ground_albedo": 0.25, "sun_intensity": 0.55,
       "aerial_perspective": True, "sky_exposure": 0.65}
HAZE = {"enabled": True, "mode": "height", "density": 0.004, "height_falloff": 0.12, "scattering": 0.55,
        "absorption": 0.15, "light_shafts": True, "shaft_intensity": 0.30, "steps": 32, "half_res": False}
# A gentle sun and a strong sky light. forge3d's sun adds a specular sheen that doesn't depend
# on the ground's colour, so at full strength it turns dark forest and deep lakes pale grey
# wherever the ground faces the light; the sky light only scales the imagery's own colours.
SUN_INTENSITY = 0.7
TERRAIN = {"zscale": 1.0, "ambient": 0.38}     # true scale; less sky fill so the shade has depth
LABEL_RGB = (253, 250, 244)
LABEL_HALO = (28, 26, 22)
LABEL_MAX_KM = 9.0                      # places further than this aren't named
LABEL_FADE_KM = 1.5
TITLE = ("LAMOILLE CANYON", "Ruby Mountains, Nevada \u00b7 up the glacial trough to Liberty Lake",
         "Bare-earth lidar, true scale \u00b7 USGS 3DEP \u00b7 aerial photo USDA NAIP %s")
TITLE_S = 5.0


class _Tee:
    """Wraps the viewer's stdout so everything it prints also lands in out/viewer.log."""

    def __init__(self, stream, log):
        self._s, self._log = stream, log

    def _keep(self, line):
        if line:
            self._log.write(line)
            self._log.flush()
        return line

    def readline(self, *a):
        return self._keep(self._s.readline(*a))

    def read(self, *a):
        return self._keep(self._s.read(*a))

    def __iter__(self):
        for line in self._s:
            yield self._keep(line)

    def __getattr__(self, name):
        return getattr(self._s, name)


def open_viewer(log_path, **kw):
    """open_viewer_async, with the viewer's own messages saved to a log file."""
    import forge3d.viewer as fv
    log = open(log_path, "w", encoding="utf-8", errors="replace")
    real_popen = fv.subprocess.Popen

    def popen(*a, **k):
        proc = real_popen(*a, **k)
        if proc.stdout is not None:
            proc.stdout = _Tee(proc.stdout, log)
        return proc

    fv.subprocess.Popen = popen
    try:
        return f3d.open_viewer_async(timeout=180.0, **kw)
    finally:
        fv.subprocess.Popen = real_popen


def sky_mask(img: np.ndarray) -> np.ndarray:
    """The viewer's background: a smooth, near-white, grey gradient joined to the top edge."""
    from scipy import ndimage
    lo, hi = img.min(axis=2), img.max(axis=2)
    lum = img.mean(axis=2).astype(np.float32)
    smooth = np.abs(lum - ndimage.uniform_filter(lum, 3)) < 1.5
    cand = (lo >= 228) & (hi - lo <= 10) & smooth
    lab, n = ndimage.label(cand)
    top = np.unique(lab[0][lab[0] > 0])
    sky = np.isin(lab, top)
    # close pinholes left by the smoothness test
    return ndimage.binary_closing(sky, iterations=2) | sky


def distance(eye, aim, fov: float, w: int, h: int, ground: float) -> np.ndarray:
    """Distance in metres from the eye to the ground seen at each pixel, taking the ground
    as flat at height `ground` (the desert mostly is, at this scale); inf above its horizon."""
    import math
    fwd = (aim - eye) / np.linalg.norm(aim - eye)
    right = np.cross(fwd, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    up = np.cross(right, fwd)
    t = math.tan(math.radians(fov) / 2)
    xs = ((np.arange(w) + 0.5) / w * 2 - 1) * t * w / h
    ys = (1 - (np.arange(h) + 0.5) / h * 2) * t
    dz = fwd[2] + ys[:, None] * up[2] + xs[None, :] * right[2]          # ray's height change per unit
    length = np.linalg.norm(fwd[None, None, :] + ys[:, None, None] * up + xs[None, :, None] * right, axis=-1)
    with np.errstate(divide="ignore"):
        s = np.where(dz < -1e-6, (ground - eye[2]) / dz, np.inf)
    return s * length


def finish(png: Path, eye=None, aim=None, fov: float = 50.0, ground: float = 0.0) -> np.ndarray:
    """Evening look: a painted sky behind the terrain, warm highlights and cool shadows on the
    land, and haze thickening toward the skyline (distant ridges sit near it)."""
    img = np.asarray(Image.open(png).convert("RGB")).astype(np.int16)
    from scipy import ndimage
    # one pixel wider: the viewer's antialiased skyline is a blend with its white background,
    # which would otherwise show as a pale fringe against a blue sky
    sky = ndimage.binary_dilation(sky_mask(img), iterations=1)
    h = img.shape[0]
    # skyline: the typical height where the sky ends, never so high it squashes the gradient
    first_land = np.argmin(sky, axis=0)
    first_land[sky.all(axis=0)] = h
    horizon = int(np.clip(np.median(first_land), 0.30 * h, h))
    x = img.astype(np.float64) / 255.0

    lum = x @ np.array([0.2126, 0.7152, 0.0722])
    hi = np.clip((lum - 0.2) / 0.6, 0, 1)[..., None]
    hi = hi * hi * (3 - 2 * hi)
    x = x * (1 + hi * WARM) * (1 + (1 - hi) * COOL)
    x = np.clip((x - LEVELS[0]) / (LEVELS[1] - LEVELS[0]), 0, 1)
    x = np.clip((x - 0.5) * CONTRAST + 0.5, 0, 1)
    gray = (x @ np.array([0.2126, 0.7152, 0.0722]))[..., None]
    x = np.clip(gray + (x - gray) * SATURATE, 0, 1)
    # haze: strongest on the ground right under the skyline (the far ridges), gone a quarter
    # of the frame below it. Mountains break the flat-ground distance model, so it's by screen.
    below = np.arange(h)[:, None] - first_land[None, :]
    d = np.clip(below / (0.25 * h), 0, 1)[..., None]
    f = SKYLINE_HAZE * (1 - d) ** 2
    x = x * (1 - f) + HORIZON * f

    t = np.clip(np.arange(h) / max(horizon, 1), 0.0, 1.0)[:, None] ** 1.5
    paint = ZENITH * (1 - t) + HORIZON * t
    x[sky] = np.broadcast_to(paint[:, None, :], x.shape)[sky]
    # soften the skyline edge by a pixel
    a = sky.astype(np.float64)
    from scipy.ndimage import gaussian_filter
    a = gaussian_filter(a, 0.8)[..., None]
    sky_img = np.broadcast_to(paint[:, None, :], x.shape)
    x = x * (1 - a) + sky_img * a
    return np.clip(x * 255.0 + 0.5, 0, 255).astype(np.uint8)


def font(size: int):
    from PIL import ImageFont
    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


class Towns:
    """Names the places on each frame, when they're in sight (not behind a ridge)."""

    def __init__(self, dem: np.ndarray, w: int, h: int):
        import route
        names = list(route.TOWNS)
        x, y = flight.to_utm([route.TOWNS[n][0] for n in names], [route.TOWNS[n][1] for n in names])
        z = flight.sample(dem, x, y)
        self.pts = np.column_stack([x, y, z])
        self.dem = dem
        self.text = [route.TOWNS[n][2] for n in names]
        self.w, self.h = w, h
        self.font = font(max(12, round(h / 38)))
        self.small = font(max(10, round(h / 60)))
        self.big = font(max(20, round(h / 11)))

    def in_sight(self, eye, pt) -> float:
        """1 when the line from the eye to the place clears the ground by 20 m or more,
        0 when the ground stands 10 m or more above it, smooth in between (no flicker)."""
        s = np.linspace(0.03, 0.95, 120)[:, None]
        line = eye[None] + (pt + [0, 0, 15] - eye)[None] * s
        gap = line[:, 2] - flight.sample(self.dem, line[:, 0], line[:, 1])
        return float(np.clip((gap.min() + 10) / 30, 0, 1))

    def draw(self, rgb: np.ndarray, eye, aim, fov, t: float) -> np.ndarray:
        from PIL import ImageDraw
        img = Image.fromarray(rgb)
        d = ImageDraw.Draw(img)
        px, py, dist = flight.project(self.pts, eye, aim, fov, self.w, self.h)
        stroke = max(2, self.h // 360)
        for x, y, km, text, pt in sorted(zip(px, py, dist / 1000, self.text, self.pts), key=lambda v: -v[2]):
            if not np.isfinite(x) or km > LABEL_MAX_KM or not (0 <= x < self.w and 0 <= y < self.h):
                continue
            # names come in as the title goes out, fade with distance, and fade out behind ridges
            fade = float(np.clip((LABEL_MAX_KM - km) / LABEL_FADE_KM, 0, 1) * np.clip((t - TITLE_S + 1.5) / 1.5, 0, 1)
                         * self.in_sight(eye, pt))
            if fade <= 0.05:
                continue
            col = tuple(int(c * fade + l * (1 - fade)) for c, l in zip(LABEL_RGB, rgb[int(y), int(x)]))
            lift = self.h / 40
            d.line([(x, y - 4), (x, y - lift)], fill=col, width=max(1, stroke // 2))
            d.text((x, y - lift - 2), text, font=self.font, fill=col, anchor="md",
                   stroke_width=stroke, stroke_fill=LABEL_HALO)
        if t < TITLE_S:
            a = float(np.clip(min(t / 0.8, (TITLE_S - t) / 1.2), 0, 1))
            if a > 0.02:
                over = Image.new("RGBA", img.size, (0, 0, 0, 0))
                od = ImageDraw.Draw(over)
                x0, y0 = self.w * 0.06, self.h * 0.70
                od.text((x0, y0), TITLE[0], font=self.big, fill=LABEL_RGB + (int(255 * a),),
                        stroke_width=stroke, stroke_fill=LABEL_HALO + (int(200 * a),))
                od.text((x0 + self.h * 0.006, y0 + self.h * 0.115), TITLE[1], font=self.font,
                        fill=LABEL_RGB + (int(255 * a),), stroke_width=stroke, stroke_fill=LABEL_HALO + (int(200 * a),))
                od.text((x0 + self.h * 0.006, y0 + self.h * 0.165), TITLE[2] % (config.INFO.get("naip_year") or ""), font=self.small,
                        fill=LABEL_RGB + (int(230 * a),), stroke_width=max(1, stroke - 1),
                        stroke_fill=LABEL_HALO + (int(200 * a),))
                img = Image.alpha_composite(img.convert("RGBA"), over).convert("RGB")
        return np.asarray(img)


def uv(left, top, right, bottom):
    W, H = config.RIGHT - config.LEFT, config.TOP - config.BOTTOM
    return ((left - config.LEFT) / W, (config.TOP - top) / H, (right - config.LEFT) / W, (config.TOP - bottom) / H)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preview", action="store_true", help="640x360, every 3rd frame, quick")
    ap.add_argument("--stills", type=str, default=None, help="comma-separated seconds, e.g. 0,9,20,41,64")
    ap.add_argument("--size", type=str, default="1920x1080")
    ap.add_argument("--fps", type=int, default=flight.FPS)
    ap.add_argument("--lite", action="store_true", help="lighter antialiasing")
    ap.add_argument("--forge-sky", action="store_true", help="forge3d's physical sky instead of the painted one")
    ap.add_argument("--haze", action="store_true",
                    help="forge3d's volumetric haze (needs --forge-sky: it tints the background, so the sky can't be painted)")
    ap.add_argument("--no-labels", action="store_true", help="no town names or title on the frames")
    ap.add_argument("--out", type=str, default=None)
    ap.add_argument("--frames-dir", type=str, default=None,
                    help="write numbered JPEG frames here instead of a video (for rendering in parallel pieces)")
    ap.add_argument("--chunk", type=int, default=0, help="with --frames-dir: which piece of the flight, from 0")
    ap.add_argument("--chunks", type=int, default=1, help="with --frames-dir: how many pieces")
    ap.add_argument("--encode", type=str, default=None,
                    help="no rendering: join the numbered frames in this folder into the video")
    args = ap.parse_args()

    if args.encode:
        return encode(Path(args.encode), Path(args.out) if args.out else config.VIDEO, args.fps)

    config.dtm()
    for p in (config.DEM_X, *(config.graded(t[0]) for t in config.image_tiles())):
        if not p.exists():
            print(f"Missing {p}. Unpack the flyover-data branch into data\\ (see README.md)")
            return 1
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None and not (args.stills or args.frames_dir):
        print("ffmpeg must be on PATH")
        return 1

    with rasterio.open(config.DEM_X) as src:
        dem = src.read(1)
    min_h = float(dem.min())
    path = flight.plan(dem, args.fps)
    flight.report(path)

    w, h = (640, 360) if args.preview else map(int, args.size.lower().split("x"))
    if args.stills:
        frames = [min(int(round(float(s) * args.fps)), len(path.t) - 1) for s in args.stills.split(",")]
    elif args.preview:
        frames = list(range(0, len(path.t), 3))
    else:
        frames = list(range(len(path.t)))
    frames_dir = Path(args.frames_dir) if args.frames_dir else None
    if frames_dir:
        n_all = len(frames)
        a, b = n_all * args.chunk // args.chunks, n_all * (args.chunk + 1) // args.chunks
        frames_dir.mkdir(parents=True, exist_ok=True)
        frames = [i for i in frames[a:b] if not (frames_dir / f"frame_{i:05d}.jpg").exists()]
        print(f"chunk {args.chunk + 1} of {args.chunks}: frames {a}-{b - 1}, {len(frames)} still to render")
        if not frames:
            return 0
    out_fps = args.fps / 3 if args.preview else args.fps
    config.OUT.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else (config.OUT / "preview.mp4" if args.preview else config.VIDEO)
    stills_dir = config.OUT / "stills"
    if args.stills:
        stills_dir.mkdir(parents=True, exist_ok=True)

    encoder = None
    partial = out.with_suffix(".part.mp4")
    if not (args.stills or frames_dir):
        encoder = subprocess.Popen(
            [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
             "-framerate", f"{out_fps:g}", "-i", "-", "-c:v", "libx264", "-preset", "slow", "-crf", "17",
             "-pix_fmt", "yuv420p", "-vf", "scale=out_color_matrix=bt709", "-colorspace", "bt709",
             "-color_primaries", "bt709", "-color_trc", "bt709", "-movflags", "+faststart", "-f", "mp4",
             str(partial)], stdin=subprocess.PIPE)

    sun = f3d.sun_position_utc(*config.SUN_LATLON, *config.SUN_UTC)
    print(f"Sun: azimuth {sun.azimuth:.0f} deg, {sun.elevation:.0f} deg up")
    pbr = dict(PBR, **LITE) if (args.lite or args.preview) else dict(PBR)
    if args.forge_sky:
        pbr["sky"] = SKY
    if args.haze:
        pbr["volumetrics"] = HAZE
    towns = None if args.no_labels else Towns(dem, w, h)
    ground = float(np.median(dem[dem > 0])) if (dem > 0).any() else 0.0   # typical (stretched) land height
    snapshot = config.OUT / "_frame.png"
    start = time.perf_counter()
    viewer = open_viewer(config.OUT / "viewer.log", width=w, height=h, terrain_path=str(config.DEM_X),
                         fov_deg=flight.FOV, title=config.INFO["name"])
    try:
        for k, (p, l, t, r, b) in enumerate(config.image_tiles()):
            viewer.load_overlay(f"naip_{k}", config.graded(p), extent=uv(l, t, r, b), z_order=k,
                                preserve_colors=PRESERVE_COLORS)
        viewer.send_ipc({"cmd": "set_terrain_sun", "azimuth_deg": float(sun.azimuth),
                         "elevation_deg": float(sun.elevation), "intensity": SUN_INTENSITY})
        viewer.send_ipc({"cmd": "set_terrain_pbr", **pbr})
        viewer.send_ipc({"cmd": "set_terrain", **TERRAIN})
        viewer.send_ipc(flight.camera(path.eye[frames[0]], path.aim[frames[0]], min_h, path.fov[frames[0]]))
        time.sleep(2.0)
        for n, i in enumerate(frames):
            viewer.send_ipc(flight.camera(path.eye[i], path.aim[i], min_h, float(path.fov[i])))
            viewer.snapshot(snapshot, w, h)
            rgb = np.asarray(Image.open(snapshot).convert("RGB")) if args.forge_sky else finish(
                snapshot, path.eye[i], path.aim[i], float(path.fov[i]), ground)
            if towns is not None:
                rgb = towns.draw(rgb, path.eye[i], path.aim[i], float(path.fov[i]), float(path.t[i]))
            if args.stills:
                Image.fromarray(rgb).save(stills_dir / f"still_{path.t[i]:05.1f}s.png")
            elif frames_dir:
                tmp = frames_dir / f"frame_{i:05d}.part.jpg"
                Image.fromarray(rgb).save(tmp, quality=95)
                tmp.replace(frames_dir / f"frame_{i:05d}.jpg")
            else:
                encoder.stdin.write(rgb.tobytes())
            if n % 30 == 0 or args.stills or frames_dir:
                el = time.perf_counter() - start
                print(f"  frame {n + 1}/{len(frames)}  ({path.t[i]:.1f} s)  "
                      f"~{el / (n + 1) * (len(frames) - n - 1) / 60:.0f} min left", end="\n" if frames_dir else "\r")
        print()
    finally:
        viewer.close()
        snapshot.unlink(missing_ok=True)
        if encoder is not None:
            encoder.stdin.close()
    if encoder is not None:
        if encoder.wait() != 0:
            print("ffmpeg failed")
            return 1
        partial.replace(out)
        print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB) in {(time.perf_counter() - start) / 60:.0f} min")
    elif frames_dir:
        print(f"wrote {len(frames)} frames to {frames_dir}")
    else:
        print(f"wrote {len(frames)} stills to {stills_dir}")
    return 0


def encode(frames_dir: Path, out: Path, fps: int) -> int:
    """Join frame_00000.jpg, frame_00001.jpg, ... into the MP4."""
    have = sorted(frames_dir.glob("frame_*.jpg"))
    nums = [int(f.stem.split("_")[1]) for f in have]
    if not nums or nums != list(range(len(nums))):
        missing = sorted(set(range(max(nums, default=-1) + 1)) - set(nums))
        print(f"{len(nums)} frames, with gaps: {missing[:20]}{' ...' if len(missing) > 20 else ''}")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [shutil.which("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps),
           "-i", str(frames_dir / "frame_%05d.jpg"), "-c:v", "libx264", "-preset", "slow", "-crf", "17",
           "-pix_fmt", "yuv420p", "-vf", "scale=out_color_matrix=bt709", "-colorspace", "bt709",
           "-color_primaries", "bt709", "-color_trc", "bt709", "-movflags", "+faststart", str(out)]
    if subprocess.run(cmd).returncode != 0:
        print("ffmpeg failed")
        return 1
    print(f"wrote {out} from {len(nums)} frames ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (TimeoutError, f3d.viewer.ViewerError) as err:
        log = config.OUT / "viewer.log"
        print(f"\nThe viewer stopped answering ({err}).")
        if log.exists():
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            print("\n".join("  " + s for s in lines[-25:]))
        print("Try again with --lite, and send the log above if it still stalls.")
        sys.exit(1)
