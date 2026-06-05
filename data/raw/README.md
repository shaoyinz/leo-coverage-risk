# data/raw — provided inputs (not committed)

The challenge supplies the Locations CSV. It is **gitignored** (large, and not ours to
redistribute), so it is not in the repo — drop it here yourself before running the pipeline:

```
data/raw/locations.csv
```

## Expected columns

The ingest step reads the CSV with DuckDB `read_csv_auto`, so column order doesn't matter,
but these names must be present (see `src/leo_pipeline/ingest.py` and `src/leo_pipeline/config.py`):

| Column | Used for |
|--------|----------|
| `location_id` | Row identity; duplicates are flagged, never dropped. |
| `latitude` | Obstruction sampling + AOI (EPSG:4326, −90..90). |
| `longitude` | Obstruction sampling + AOI (EPSG:4326, −180..180). |
| `geoid` (census block) | County (5-digit FIPS) + state rollups in QA / reporting. |

## Once the file is in place

```bash
python -m leo_pipeline.ingest    # profile + de-duplicate to one work item per unique coord
python -m leo_pipeline.tiling    # tile the de-duplicated work list into UTM tiles
```

See the top-level `README.md` for the full run sequence (A1–A5) and `docs/` for methodology.
