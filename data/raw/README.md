# data/raw — provided inputs (not committed)

Files in this folder are **gitignored** (the locations dataset is ~1M rows). They are
*provided by the challenge*, not produced here — drop them in or download them.

## Expected files

| File | Source | Notes |
|------|--------|-------|
| `locations.csv` | Provided by Ready (not yet delivered — request it) | ~1,000,000 locations LEO providers committed to serve. Schema per issue #50: **`location_id, latitude, longitude`**. Confirm on arrival and update `docs/data-sources.md`. |

## How to populate

- **Manual:** download `locations.csv` from the issue and place it here as
  `data/raw/locations.csv`.
- **Scripted:** if you have a direct URL,
  `curl -L "<url>" -o data/raw/locations.csv`.

## First look

Run `notebooks/00_data_inspection.ipynb` (or the DuckDB one-liner inside it) to profile
row count, dtypes, null/duplicate coordinates, and lat/lon range — this feeds the
"data-quality issues" deliverable in `docs/data-sources.md`.
