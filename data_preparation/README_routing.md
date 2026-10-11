# Flood-aware routing to care (Route to Care)

This page explains the "Route to Care" part of the project: what it does, how it is built, the
decisions behind it, and what to watch out for when you continue the work. It builds on the
prepared data in `data_preparation/README.md` and the travel-time solver in
`data_preparation/README_baseline.md`; read those first if you haven't.

## What it does

In the dashboard you click a point in South Sudan. The tool then draws the route from that point to
the nearest health facility that can still provide care, going around ground that is often flooded
in that month of the year. For comparison it also draws the route you would take if there were no
floods, and shows how much longer the flood-aware route is.

The idea is simple: a cell that is likely to be flooded gets an infinite travel cost, so the
least-cost route automatically avoids it. Permanent water (rivers, lakes) is always blocked.

## Quick start

From the repository root, with `raw_data/` in place (see the main data README for the sources):

```
pip install -r requirements.txt
python -m data_preparation.build_static            # ~30 s
python -m data_preparation.build_permanent_water   # ~15 s, needs build_static first
python -m data_preparation.build_facilities        # ~5 s
python -m data_preparation.build_flood_monthly     # ~6 min
python -m data_preparation.build_flood_likelihood  # ~1-2 min, needs build_flood_monthly first (new)
python -m pytest tests                              # 26 tests, a few seconds
```

Then start the dashboard (PowerShell):

```
$env:PYTHONPATH = "."; streamlit run dashboard.py
```

On macOS/Linux: `PYTHONPATH=. streamlit run dashboard.py`.

Always run the build scripts as modules (`python -m data_preparation.build_x`), from the repository
root. Running a file directly (`python data_preparation\build_x.py`, or the Run button in VS Code)
fails with `ModuleNotFoundError: No module named 'data_preparation'`, because Python then looks for
imports in the script's own folder instead of the repository root.

## How it works

```
flood/flood_YYYY_MM.tif (276 months)
        |  build_flood_likelihood.py
        v
flood_likelihood_250m.tif      12 bands: per calendar month, share of years each cell was flooded
        |
        |  + permanent_water_250m.tif, friction from static_250m.tif, facility sites
        v
routing.py                     blocked cells -> least-cost path from the clicked cell to the nearest open facility
        |
        v
dashboard.py                   "Route to Care" section: map, metrics, warnings
```

### 1. Flood likelihood (`build_flood_likelihood.py`)

For every cell and every calendar month (January to December):

```
likelihood = years the cell was flooded in that month / years with data for that cell
```

- A year counts as flooded when the cell was flagged on at least `flood.min_days_flooded` days
  (3) in that month, using band 1 ("all") of the monthly flood files, so recurring and unusual
  flooding both count.
- Months flagged in `flood_months.csv` for data gaps are left out entirely (April 2006, May 2006,
  November 2007), so those months use 22 years instead of 23.
- Cells without flood data (the value 255, north of 10°N) get NaN, which means "unknown", not
  "never flooded".
- The output `data/interim/flood_likelihood_250m.tif` has one band per calendar month and records
  the `min_days_flooded` it was built with. `load_flood_likelihood(month)` in `loading.py` refuses
  to read a file built with a different value than the config, so change the config and rebuild
  together.

The calculation is the pure function `likelihood_from_years`, which is tested on its own.

### 2. Blocking rule

A cell is impassable when

```
permanent water   OR   likelihood > routing.max_flood_likelihood
```

The threshold is `routing.max_flood_likelihood` in `config.yaml`, and the dashboard has a slider
for it. **The default is 0**, so a cell is blocked when it was flooded in that calendar month in any
year since 2003. Higher values block only cells that flood more often (for example 0.2 = more than 20%
of the years).

A facility counts as able to provide care only when its own cell is not above the threshold.
Facilities in flood-prone cells are skipped, and the route goes to the next nearest one.

### 3. The route (`routing.py`)

```python
from data_preparation.routing import blocked_mask, open_sites, route, rc_to_lonlat

likelihood = load_flood_likelihood(10)                       # October
blocked = blocked_mask(water, likelihood, threshold=0.0)
usable = open_sites(sites_rc, likelihood, threshold=0.0)
res = route(static["friction_walking"], (row, col), sites_rc[usable], blocked=blocked,
            no_data=np.isnan(likelihood))
res.path_rc, res.minutes, res.length_m, res.site, res.through_no_data
```

- `route` uses the same cost model as the baseline (`travel.py`): 8-connected moves, and each step
  costs the mean friction of the two cells times the step length. It reuses `cell_costs`,
  `source_costs` and `check_sources` from `travel.py`.
- The step cost is symmetric, so the route from the point to a facility equals the least-cost path
  from that facility to the point. Its minutes are exactly `travel_time(...).minutes` at the start
  cell, so routing results compare directly with the baseline (this is tested).
- It runs one Dijkstra search (scikit-image `MCP_Geometric`) from the clicked cell and stops at
  the first facility it reaches, which is the nearest one.
- The start cell and the facility cells are always passable, so someone standing in a flood-prone
  cell can still walk out of it.
- `res.site` indexes the sites you passed in (`sites_rc[usable]`), the same convention as
  `travel_time`'s `nearest`. Map it back with `np.flatnonzero(usable)[res.site]`.
- `site = -1` and `minutes = NaN` mean no open facility can be reached without crossing a
  blocked cell.
- `through_no_data` is True when the path crosses cells without flood data (north of 10°N).

Facilities that share a 250 m cell are one site, built with `build_sites` from `build_baseline.py`
(the same sites as the baseline).

### 4. Dashboard (`dashboard.py`)

The Route to Care section sits below the National Overview map and the Top 5 table.

| Piece | What it does |
|---|---|
| `get_sites()` | Facility sites, built once |
| `get_route_inputs(month, threshold)` | Blocked mask, open facilities and no-data mask; cached, up to 4 settings |
| `compute_route(lat, lon, month, threshold, mode)` | Flood-aware route plus the no-flood route; cached, up to 16 |
| `national_overlay(month, threshold)` | Every blocked cell as one image at 500 m; cached, up to 8 |
| `blocked_overlay(...)` | Blocked cells at 250 m within 10 km (40 cells) of the routes |
| `boundaries_geojson(level)` | County and state outlines, simplified to about 110 m, cached |
| `build_route_map(...)` | Assembles the folium map |
| `render_route_section(...)` | Caption, toggle, map, click handling, metrics and warnings |

On the map:

- **red line**: flood-aware route; **dashed grey line**: route if there were no floods
- **blue cells**: blocked cells (flood-prone or permanent water), drawn near the route by default
- **"Show all flood-prone cells (nationwide)" toggle**: draws every blocked cell in the country. It
  only changes what is drawn; the route always avoids every blocked cell nationwide.
- **county and state boundaries**: each can be switched on and off in the layer menu

Below the map: travel time, route length, extra time compared with the no-flood route, and the
destination facility. Warnings appear for clicks outside South Sudan, unreachable points, and routes
that cross the area without flood data.

Images are reprojected to Web Mercator (`mask_to_png`) before they are drawn. Leaflet stretches an
image linearly in Web Mercator, so drawing the Albers grid directly would shift cells by kilometres
towards the edges of the country.

#### Which sidebar controls affect which map

| Control | National Overview | Route to Care |
|---|---|---|
| Historical / Predicted Scenario Month | that exact month (e.g. Oct 2022) | only the calendar month (every October since 2003) |
| Min Days Flooded to Block Access | yes | no, always 3 (from the config) |
| Prioritization Criterion | yes (Top 5 table) | no |
| Show Only Inaccessible / Flooded Facilities | yes | no |
| Filter / Zoom to County | yes | no |
| Travel Mode (under "Route to Care") | no | yes |
| Block Cells Flooded in More Than (% of Years) | no | yes |

## Decisions and why

| Decision | Why |
|---|---|
| Likelihood per **calendar month**, not overall | Flooding is strongly seasonal; a March route should not be blocked by November floods. |
| **Hard block** (infinite cost) instead of a penalty | It was the team's original idea and is easy to explain. Its downside is listed under limitations. |
| Default threshold **0** ("flooded in any year") | The flood record is sparse (below 1% of cells in any month), so higher thresholds blocked almost nothing. At 20%, about 0.08% of cells were blocked in October. |
| **Not** NASA's "recurring" label | It was tested. It covers almost the same tiny area as the 20% threshold (0.14% of cells in October, against 1.67% for "any year"). NASA's recurring mask is fixed, so all flood growth after 2020 is labelled "unusual" (see `README.md`). |
| Flood-prone facilities are skipped | The team decided facilities "count if they can provide care"; there is no data on status or staffing, so the facility's own flood exposure is the proxy. |
| No-data cells (north of 10°N) are passable | Blocking them would cut off the whole north; the dashboard warns instead. |
| Calendar-month history, not the selected year | Routing is meant for anticipation ("what usually floods in October"), while the National Overview shows what happened in one specific month. At the default settings, in every scenario month the route map blocks a superset of what the overview shows as flooded. |

Numbers for October (cells inside South Sudan):

| Rule | Cells blocked | People living in them |
|---|---|---|
| likelihood > 20% | 0.08% | ~5k |
| NASA recurring, any year | 0.14% | ~8k |
| flooded in any year (**current default**) | 1.67% (171,623 cells) | ~305k |

## Files

New:

| File | Content |
|---|---|
| `data_preparation/build_flood_likelihood.py` | Builds `flood_likelihood_250m.tif` |
| `data_preparation/routing.py` | `route`, `blocked_mask`, `open_sites`, `rc_to_lonlat`, `RouteResult` |
| `tests/test_routing.py` | 11 tests: matches `travel_time`, wall with a gap, skipped facility, blocked or enclosed start, start on a facility, no-data flag, blocked mask, likelihood |
| `data_preparation/README_routing.md` | This page |

Changed:

| File | Change |
|---|---|
| `dashboard.py` | Route to Care section, route controls in the sidebar, Top 5 table moved above Route to Care, maps no longer reload when zooming |
| `data_preparation/loading.py` | `load_flood_likelihood(month)` |
| `config.yaml` | `paths.flood_likelihood`, and the `routing:` section (`max_flood_likelihood`, `default_mode`) |
| `requirements.txt` | Added `scikit-image` and `psutil` (already needed by `travel.py` and `build_baseline.py`), upgraded `folium` to 0.20.0 and `streamlit_folium` to 0.27.4 |

`requirements.txt` is saved as UTF-16. pip reads it fine, but some editors and `grep` show it as
binary; keep the encoding when you edit it.

## Known limitations

- **Points inside flood-prone areas can be unreachable.** With hard blocking, someone inside a
  flood-prone patch has no allowed way out. In a test of 60 points near flood-prone cells in
  October at the 20% threshold, 5 were unreachable and 21 had a detour. The default of 0 blocks
  more cells, so expect more unreachable points; raising the threshold relaxes this.
- **Each click takes about 5 seconds.** The search runs on the full national grid (4500 x 5308
  cells), twice (with and without floods). Results are cached, so repeating a click is instant.
- **Flood counts are a lower bound.** Clouds hide flooding in the satellite data, most of all in the
  wet season, which is also why December and January show more flooding than August.
- **The Min Days slider does not affect routing.** The likelihood file is built with the config
  value (3 days).
- **Routes may pass through the 50 km buffer** outside South Sudan (Sudan, Uganda, ...), because the
  grid covers it. Only facilities inside South Sudan are destinations.
- **No flood data north of 10°N** (about 6% of the country); see the main data README.
- **Travel cost is 1 km data** repeated into 250 m cells, so read results per area, not per cell.
- **Abyei Region** is counted as part of South Sudan and has no facilities (see the main data README).

## Ideas for next steps

- Make the Min Days slider apply to routing too (rebuild the likelihood for one month on the fly
  and cache it).
- Add a "selected month only" option that routes around the flooding of the chosen month, like the
  National Overview, as an alternative to the all-years history.
- Zoom the route map to the county chosen in "Filter / Zoom to County".
- Speed up clicks by searching a window around the point first and growing it only when needed.
- A soft penalty (higher cost instead of infinite) as an option, so nobody is ever unreachable.

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'data_preparation'` | Run from the repository root with `python -m ...`, and start the dashboard with `PYTHONPATH=.` |
| `...static_250m.tif: No such file or directory` | The build scripts haven't run in this folder. Each checkout (and each git worktree) has its own `data/interim/`; build there or copy the folder over. |
| `...map_friction_maps\...geotiff: No such file or directory` | `raw_data/map_friction_maps/` and `raw_data/JRC_surface_water_data/` are missing; get them from the team. |
| `flood likelihood was built with min_days_flooded=...` | You changed `flood.min_days_flooded`; rerun `build_flood_likelihood`. |
| Maps go grey or reload when zooming | An old `streamlit_folium` is installed; run `pip install -r requirements.txt`. |
| `No module named 'skimage'` | `pip install scikit-image` (it's in `requirements.txt`). |

## Housekeeping

- `data/processed/` and `.claude/` are in `.gitignore`. `build_baseline` writes to `data/processed/`,
  including `internal/sites.parquet` with facility locations, which must stay out of git. They were
  committed once by mistake and removed again, so older commits still contain them.
