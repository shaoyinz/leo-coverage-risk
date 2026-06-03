# Data sourcing & quality

> Status: complete. This is the **human-authored candidate catalog** the A1
> data-discovery agent starts from; at runtime the agent probes these sources with
> [`stac_search`](../src/leo_pipeline/tools/__init__.py) over the actual AOI, ranks them,
> and writes the live, version-stamped picks to `data/interim/data_manifest.json` (the
> H1 review artifact). The candidate collection ids here mirror
> [`config.DISCOVERY.candidate_collections`](../src/leo_pipeline/config.py).

## Area of interest (from the provided locations)

`get_aoi_bbox` over `DATA_CHALLENGE_50.csv` returns AOI bbox
**`[-84.32, 33.84, -75.46, 36.59]`** (lon/lat, EPSG:4326) — the **Carolinas / north
Georgia**, not the full CONUS. Every candidate below was confirmed (via `stac_search`)
to cover **100 % of this AOI** unless noted.

## Dataset selection (linked to obstruction factors)

The methodology ([rationale](rationale.md)) needs a **fused surface** = bare-earth terrain
**+ the taller of canopy/buildings**. So we source four factors. Preference follows the
fallback hierarchy: *true lidar DSM → DEM + max(canopy height, building height) → coarse
cover proxy*. "Access" shows how the discovery agent reaches each one — a STAC collection
on Microsoft Planetary Computer (`pc`) or an off-catalog web source.

| Obstruction factor | Dataset | Models | Resolution | Coverage | Vintage | Licence | Access |
|---|---|---|---|---|---|---|---|
| **surface (preferred)** | **USGS 3DEP lidar DSM** (`3dep-lidar-dsm`) | first-return surface (terrain+canopy+structures in one) | **~1 m** | CONUS, where lidar flown | rolling (per-project) | public domain (US Gov) | `pc` STAC |
| **terrain (in-CONUS)** | **USGS 3DEP seamless** (`3dep-seamless`) | bare-earth DEM | **10 m** (1/3 arc-sec) | CONUS | ~2019 (per-tile) | public domain (US Gov) | `pc` STAC |
| **terrain (global fallback)** | **Copernicus DEM GLO-30** (`cop-dem-glo-30`) | terrain (edited DSM→DTM) | 30 m | global | 2021 | open, attribution (ESA) | `pc` STAC |
| **terrain (global fallback)** | **NASADEM** (`nasadem`) | terrain | 30 m | 60°N–56°S | 2000 (SRTM epoch) | public domain (NASA) | `pc` STAC |
| **canopy** | **Meta / WRI High-Res Canopy Height** | tree-canopy **height** | **~1 m** | near-global | 2009–2020 composite | CC-BY-4.0 | **web** (AWS Open Data) |
| **canopy (alt)** | **ETH Global Canopy Height 2020** | tree-canopy **height** | 10 m | global | 2020 | CC-BY-4.0 | **web** (Zenodo/GEE) |
| **buildings** | **MS Global ML Building Footprints** (`ms-buildings`) | structure footprints | vector (some heights) | global | 2022 | ODbL (attribution) | `pc` STAC |
| **buildings (alt)** | **Overture Maps buildings** | footprints **+ heights** | vector | global | rolling | CDLA-Permissive 2.0 | web (S3/Azure) |

**Why these and not others.**

- **Surface first, then synthesise.** A 1 m lidar DSM already fuses terrain+canopy+structures,
  so it is the single best input where it exists — but US 3DEP lidar is patchy, so off-lidar
  we synthesise a pseudo-DSM `= DEM + max(canopy height, building height)`. That is why we
  source the three component layers (terrain, canopy, buildings) in addition to the DSM.
- **Canopy is off-catalog and that's expected.** No Planetary Computer collection carries
  tree-canopy **height** (verified: 134 collections, none canopy-height), so the agent must
  source it from the open web — the reason A1 holds `WebSearch`/`WebFetch`. We deliberately
  list canopy *height*, not *cover*: 100 % cover by 3 m shrubs and by 30 m conifers give the
  same cover but wildly different horizons. Canopy **cover** layers (NLCD TCC / `esa-worldcover`)
  are kept only as a coarse cross-check / proxy, never as a height substitute.
- **Global fallbacks behind the best-in-CONUS pick.** 3DEP (10 m / 1 m) wins inside the AOI;
  Copernicus GLO-30 and NASADEM are the graceful-degradation fallbacks the architecture's
  failure hierarchy calls for when high-res coverage is missing.
- **Licensing is the H1 decision.** All picks above are open/public-domain or permissive
  (ODbL/CC-BY require attribution). The discovery agent flags any attribution/share-alike
  obligation in the manifest `notes` so a human signs off before bulk download.

## Quality issues in the provided locations CSV

From `profile_locations` over the full 4.67 M-row file (`notebooks/00_data_inspection.ipynb`):

| Check | Count | Handling |
|---|---|---|
| Total rows | 4,674,917 | — |
| Null / missing coordinates | **0** | (would be excluded from the work list) |
| Out-of-range lat/lon | **0** | (would be excluded) |
| Duplicate `location_id` values | **12** | benign; keyed work is per *coordinate* |
| Distinct coordinates | 4,516,123 | one obstruction-sampling job each |
| Rows sharing a coordinate | **158,794 (3.4 %)** | collapsed by `deduplicate_coordinates`; fanned back out via `location_coord_map.parquet` |

The data is clean (no null island, no off-CONUS strays — the AOI bbox confirms a tight
Carolinas/Georgia extent). The one material structural fact is that **3.4 % of rows are
coordinate duplicates**, which the ingestion step removes so obstruction sampling runs once
per unique coordinate rather than once per location.

## What public data cannot model

A remote, public-data assessment is **screening-grade**, not a substitute for an on-site
dish-pointing check. The gaps (detailed under [rationale → Known limitations](rationale.md#known-limitations-vs-on-site-assessment)):

- **Exact dish mount height** — usually unknown; we sweep a mast-height parameter and report
  "clear if raised to *X* m" rather than asserting one value.
- **Fine-scale / near-field obstructions** — chimneys, vents, railings, single branches, power
  lines: below raster resolution, yet they block a phased-array cone on site.
- **Seasonal foliage & currency** — layers are leaf-state- and epoch-specific and can be years
  stale; trees grow, get cleared, structures appear. We use the newest epoch and treat canopy
  as leaf-on (conservative).
- **Exact pointing & a moving constellation** — the app picks the optimal boresight from live
  ephemerides; our azimuth weighting is a static approximation (v2: TLE-derived dwell time).
- **Micro-siting freedom** — an installer can move a few metres or change roof face; our
  parcel-clipped suggestions approximate but cannot replicate this.

These limits are why the output is a **banded, three-state** (`clear`/`at-risk`/`undetermined`)
prioritised verify-on-site list, not a guarantee.
