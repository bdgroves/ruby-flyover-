# Ruby Mountains flyover: Lamoille Canyon

A forge3d flight up **Lamoille Canyon** in the Ruby Mountains of northeastern Nevada. It opens high over the Lamoille valley with the whole canyon laid out ahead, drops in at the mouth, and runs low and fast up the glacial trough past the hanging valleys. It climbs the headwall to Lamoille Lake, crosses Liberty Pass with Liberty Lake below, and arcs round the lake to a last look back across it from the south. That's the way the Ruby Crest Trail climbs out of the canyon.

It's flown on USGS 3DEP bare-earth lidar at true scale, with the USDA NAIP aerial photo on top, under a blue sky in late-afternoon August light. It runs about 2½ minutes. A longer flight along the whole crest can come later.

## The lidar

| Project | Covers the window | Role |
|---|---|---|
| `NV_EastCentral_5_D21` (2021) | 95% | Main source. Every point on the flight is inside it, with 3.8–6.9 ground returns per m². |
| `USGS_LPC_NV_UpperHumboldt_2016_LAS_2018` | 60% | Fills the 5% the 2021 survey misses, a strip of valley floor near the canyon mouth (3.1 ground returns per m²). |

These figures come from the **Lidar coverage probe** workflow, which counts ground returns in 200 m boxes along the flight; see `probe/coverage.json`.

## How it's made

- **Data on GitHub Actions** (`.github/workflows/data.yml`, run by hand). It needs PDAL, and the point cloud on AWS can't be reached from a cloud chat session.
  - Eight runners read the lidar in 2 km tiles. The canyon itself (14 × 18 km) is gridded at **1 m** from every ground return; the ridges and valley around it at 4 m.
  - The assembly step fills the lakes flat at their shoreline.
  - It builds the terrain the viewer loads at 2.5 m, with the canyon averaged straight from the 1 m lidar, published as `dtm_0.tif` … `dtm_3.tif` and joined into `dtm.tif` on first use. It shades the NAIP photo with the 1 m lidar, gently and from all round so it never fights the real sun, and writes that as a 2 m texture in four strips.
  - It finds the canyon floor in the lidar (`centerline.json`) for the flight to follow.
  - Everything goes to the **`flyover-data`** branch, with `quicklook.png` to check it by eye.
  - To redo only the assembly, run it again with the earlier run's ID as **reuse_run**. The tiles are kept for 30 days.
- **Render on the laptop's GPU** with `f3d`, the PowerShell function that borrows the trusted humphreys-orbit environment (forge3d 1.39). Smart App Control blocks a fresh `pixi install`, so this repo's pixi environment is Linux-only, for Actions.

## Render it on GitHub (no laptop needed)

The **Render** workflow (`.github/workflows/render.yml`) runs forge3d on GitHub's CPUs with Mesa's software Vulkan:

- **mode `stills`** renders a few frames at the times listed and pushes them to the [`stills` branch](../../tree/stills), to look at right on GitHub.
- **mode `flyover`** splits the whole flight across 40 parallel jobs. It joins the frames and pushes a 1080p and a 720p MP4 plus a poster frame to the [`video` branch](../../tree/video), each under GitHub's 100 MB limit. The full-quality master is kept as the `flyover` artifact.
- If some pieces fail, run it again with that run's ID as **reuse_run**, and only the missing frames are rendered.

**What sets the sharpness.** forge3d's viewer draws the terrain as a 2048 × 2048 vertex mesh, whatever the DEM size. Over this 16 × 22 km window that's a vertex every 8–11 m, and close walls show the facets. It also builds the photo texture at the terrain grid's size. Its TIFF reader refuses terrain over about 256 MB, which is why the grid is 2.5 m (6400 × 8800) rather than 2 m.

## Render it on the laptop (PowerShell)

```powershell
cd C:\Users\brook\Projects
git clone https://github.com/bdgroves/ruby-flyover-.git ruby-flyover   # first time only
cd ruby-flyover
git pull
git fetch origin flyover-data
git archive -o fly.tar origin/flyover-data
mkdir data -Force; tar -xf fly.tar -C data; Remove-Item fly.tar

f3d scripts\flight.py                                                 # the flight on a map -> out\flight_map.png
f3d scripts\render.py --stills "5,30,60,95,115,130,148" --size 960x540 --lite   # check the look -> out\stills\
f3d scripts\render.py --preview                                       # quick 640x360 version

# the full flight as numbered frames, restarting itself after a stall, then the video
do { f3d scripts\render.py --frames-dir out\frames --lite } until ($LASTEXITCODE -eq 0)
f3d scripts\render.py --encode out\frames                             # -> out\ruby_lamoille_flyover.mp4
```

- Quote the `--stills` list: through `f3d`, PowerShell would otherwise split it into separate values.
- Don't minimize the viewer window while it renders: a minimized window stops drawing and the render stalls. Run one render at a time.
- `--forge-sky` swaps the painted blue sky for forge3d's physical sky with aerial perspective.
- To change the flight, edit `KEYS` in `scripts/flight.py`. Points are placed along the canyon floor (`("c", fraction, metres up)`) or on the map. To change the names on screen, edit `scripts/route.py`.

## Settings worth knowing

- **Sun:** August 1, 2026 at 6:15 pm PDT (Elko County keeps Pacific time), just north of west (azimuth 279°) and about 18° up, some 20° off the line of the lower canyon. It lights the north-east wall and leaves the south-west wall in shade, and it's behind the camera going up the canyon.
- **The flight** follows the canyon floor at 170–320 m above it, at up to about 240 m/s, then slows over Lamoille Lake and Liberty Pass and arcs round Liberty Lake (never turning faster than 10° a second) to a last look from the south, across the lake to the pass. The heading, pitch and distance to the aim are interpolated between keyframes rather than the aim point itself, so turns stay even. Every key view after the opening has a clear line of sight. The opening deliberately looks at the range front, since the canyon only opens up once you're in it.
- **forge3d's orbit camera** caps its radius quietly. `flight.camera()` keeps the eye and view direction and pulls the target along the line of sight to within 600 m, so the view is the one asked for (the same fix as the Dakar flyover).
