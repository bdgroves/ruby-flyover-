# Ruby Mountains flyover

A forge3d flight up **Lamoille Canyon** in the Ruby Mountains of northeastern Nevada: in at the canyon mouth, up the glacial trough past the hanging side canyons, over Roads End and Lamoille Lake, and up to the crest at **Liberty Lake**, where the Ruby Crest Trail climbs out of the canyon. It's flown on USGS 3DEP lidar at true scale, under a blue sky, with low late-afternoon light coming up the canyon from the west.

This is the shorter piece (about 90 seconds). A longer flight along the whole crest can come later.

## The lidar

The USGS 3DEP point-cloud index shows two surveys over the window (115.52–115.33° W, 40.555–40.715° N):

| Project | Covers the window | Role |
|---|---|---|
| `NV_EastCentral_5_D21` (2021) | 95% | Main source. Every point on the planned flight, from the canyon mouth to Liberty Lake, is inside it. |
| `USGS_LPC_NV_UpperHumboldt_2016_LAS_2018` | 60% | Fills the 5% the 2021 survey misses, a strip of valley floor in the north-west corner. |

The **Lidar coverage probe** workflow (`.github/workflows/probe.yml`, run by hand from the Actions tab) checks this against the real point cloud. It counts ground returns at checkpoints along the flight, which tells us how fine a grid the bare earth will support, and writes the results to `probe/coverage.json`.

## How it's split (the same pattern as the Columbia and Dakar flyovers)

- **Data is built on GitHub Actions.** It needs PDAL, and the point cloud on AWS can't be reached from a cloud chat session. The bare-earth DTM and the NAIP photo go to a `flyover-data` side branch.
- **The render runs on the laptop's GPU** with `f3d`, the PowerShell function that borrows the trusted humphreys-orbit environment (forge3d 1.39). Smart App Control blocks a fresh `pixi install`, so this repo's own pixi environment is Linux-only and is used by Actions.

## Status

- [x] Repo, plan and coverage check from the 3DEP index
- [ ] Ground-return density along the flight (probe workflow)
- [ ] Data build: DTM from both surveys, seam-matched, plus NAIP texture → `flyover-data`
- [ ] Flight path, stills, render
