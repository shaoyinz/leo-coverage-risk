"""Ingestion & coordinate de-duplication for the locations dataset.

The obstruction analysis (``lookup_obstruction_layer``) is a pure function of a
coordinate: two locations at the same lat/lon get identical canopy / terrain /
structure samples and therefore identical risk. The provided dataset is
``location``-grained, not ``coordinate``-grained, so it contains many locations
that resolve to a coordinate another location already occupies.

Sampling every row would repeat that expensive raster/vector work for no new
information. This module collapses the input to **one work item per unique
coordinate**, then keeps a ``location_id -> coord_id`` map so per-coordinate
results can be fanned back out to every location at the end.

Everything runs through DuckDB so the ~4.7M-row CSV is streamed, never loaded
into RAM, and the outputs land in ``data/interim/`` as Parquet.

Run standalone:

    python -m leo_pipeline.ingest                # profile + dedup at 6 dp
    python -m leo_pipeline.ingest --precision 5  # snap to ~1m grid first
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb

from leo_pipeline.config import PATHS

# Default coordinate precision (decimal degrees of latitude/longitude).
# 6 dp ~= 0.11 m, the precision the source data is supplied at. Rounding to fewer
# places snaps near-coincident points to a common grid cell, which raises the
# de-duplication rate when the obstruction rasters are coarser than the points.
DEFAULT_PRECISION = 6

UNIQUE_COORDS_FILE = "unique_coords.parquet"
LOCATION_MAP_FILE = "location_coord_map.parquet"


@dataclass(frozen=True)
class ProfileResult:
    """Data-quality snapshot of the raw locations dataset."""

    total_rows: int
    null_coords: int
    out_of_range_coords: int
    duplicate_location_ids: int
    distinct_coords: int

    @property
    def usable_rows(self) -> int:
        return self.total_rows - self.null_coords - self.out_of_range_coords


@dataclass(frozen=True)
class DedupResult:
    """Outcome of collapsing locations to unique coordinates."""

    precision: int
    total_locations: int
    unique_coords: int
    reduction_pct: float
    unique_coords_path: Path
    location_map_path: Path

    @property
    def duplicate_locations(self) -> int:
        """Locations that share a coordinate with another (saved samples)."""
        return self.total_locations - self.unique_coords


def connect(csv_path: Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection with the locations CSV exposed as ``locations``.

    The CSV is registered as a view, so queries stream from disk rather than
    materialising 4.7M rows in memory.
    """
    path = csv_path or PATHS.locations_csv
    if not path.exists():
        raise FileNotFoundError(
            f"Locations CSV not found at {path}. See data/raw/README.md for how to "
            "drop in the challenge-provided file."
        )
    con = duckdb.connect()
    # CREATE VIEW can't take a bound parameter for the file path, so escape and
    # inline it (path comes from config, not user input, but quote-double anyway).
    escaped = str(path).replace("'", "''")
    con.execute(f"CREATE VIEW locations AS SELECT * FROM read_csv_auto('{escaped}')")
    return con


def profile_locations(con: duckdb.DuckDBPyConnection) -> ProfileResult:
    """Compute the data-quality metrics the ingestion agent reports."""
    total, nulls, oor, distinct = con.execute(
        """
        SELECT
            count(*),
            count(*) FILTER (WHERE latitude IS NULL OR longitude IS NULL),
            count(*) FILTER (
                WHERE latitude NOT BETWEEN -90 AND 90
                   OR longitude NOT BETWEEN -180 AND 180
            ),
            count(DISTINCT (latitude, longitude))
        FROM locations
        """
    ).fetchone()
    dup_ids = con.execute(
        "SELECT count(*) - count(DISTINCT location_id) FROM locations"
    ).fetchone()[0]
    return ProfileResult(
        total_rows=total,
        null_coords=nulls,
        out_of_range_coords=oor,
        duplicate_location_ids=dup_ids,
        distinct_coords=distinct,
    )


def aoi_bbox(con: duckdb.DuckDBPyConnection) -> dict:
    """Bounding box (lon/lat) of the usable locations — the AOI for data discovery.

    Computed over the same clean subset ``profile_locations`` /
    ``deduplicate_coordinates`` use (null and out-of-range coordinates excluded), so
    the area handed to the discovery agent matches the area that will actually be
    sampled. Returns a STAC-style ``[min_lon, min_lat, max_lon, max_lat]`` plus the
    total and distinct-coordinate counts.
    """
    min_lon, min_lat, max_lon, max_lat, n_total, n_distinct = con.execute(
        """
        SELECT
            min(longitude), min(latitude),
            max(longitude), max(latitude),
            count(*),
            count(DISTINCT (latitude, longitude))
        FROM locations
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
          AND latitude  BETWEEN -90  AND 90
          AND longitude BETWEEN -180 AND 180
        """
    ).fetchone()
    return {
        "bbox": [min_lon, min_lat, max_lon, max_lat],
        "crs": "EPSG:4326",
        "n_total": n_total,
        "n_distinct": n_distinct,
    }


def deduplicate_coordinates(
    con: duckdb.DuckDBPyConnection,
    *,
    precision: int = DEFAULT_PRECISION,
    out_dir: Path | None = None,
) -> DedupResult:
    """Collapse locations to unique coordinates and write the work list + map.

    Produces two Parquet files in ``out_dir`` (default ``data/interim``):

    - ``unique_coords.parquet`` — one row per unique coordinate:
      ``coord_id, latitude, longitude, n_locations``. This is the work list the
      geo-analysis agent samples obstruction layers against.
    - ``location_coord_map.parquet`` — ``location_id, coord_id``: the fan-out
      map used to broadcast a per-coordinate risk score back to every location.

    Coordinates are keyed on integer micro-degrees (``round(value * 10**precision)``)
    rather than floats so the group-by and the join are exact and reproducible,
    free of floating-point drift. Null and out-of-range coordinates are excluded
    from the work list (``profile_locations`` accounts for them separately).
    """
    out = out_dir or PATHS.data_interim
    out.mkdir(parents=True, exist_ok=True)
    if precision < 0:
        raise ValueError(f"precision must be >= 0, got {precision}")
    scale = 10**precision  # integer literal, safe to inline (CREATE can't bind params)

    # Clean, integer-keyed view of every usable location.
    con.execute(
        f"""
        CREATE OR REPLACE TEMP VIEW clean AS
        SELECT
            location_id,
            CAST(round(latitude  * {scale}) AS BIGINT) AS lat_key,
            CAST(round(longitude * {scale}) AS BIGINT) AS lon_key
        FROM locations
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
          AND latitude  BETWEEN -90  AND 90
          AND longitude BETWEEN -180 AND 180
        """
    )

    # One row per unique coordinate, with a stable contiguous coord_id.
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE unique_coords AS
        SELECT
            row_number() OVER (ORDER BY lat_key, lon_key) - 1 AS coord_id,
            lat_key,
            lon_key,
            lat_key / {scale}.0 AS latitude,
            lon_key / {scale}.0 AS longitude,
            n_locations
        FROM (
            SELECT lat_key, lon_key, count(*) AS n_locations
            FROM clean
            GROUP BY lat_key, lon_key
        )
        """
    )

    # location_id -> coord_id fan-out map.
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE location_coord_map AS
        SELECT c.location_id, u.coord_id
        FROM clean c
        JOIN unique_coords u USING (lat_key, lon_key)
        """
    )

    unique_path = out / UNIQUE_COORDS_FILE
    map_path = out / LOCATION_MAP_FILE
    unique_sql = str(unique_path).replace("'", "''")
    map_sql = str(map_path).replace("'", "''")
    con.execute(
        "COPY (SELECT coord_id, latitude, longitude, n_locations FROM unique_coords "
        f"ORDER BY coord_id) TO '{unique_sql}' (FORMAT PARQUET)"
    )
    con.execute(
        "COPY (SELECT location_id, coord_id FROM location_coord_map) "
        f"TO '{map_sql}' (FORMAT PARQUET)"
    )

    total = con.execute("SELECT count(*) FROM location_coord_map").fetchone()[0]
    unique = con.execute("SELECT count(*) FROM unique_coords").fetchone()[0]
    reduction = 100.0 * (1 - unique / total) if total else 0.0

    return DedupResult(
        precision=precision,
        total_locations=total,
        unique_coords=unique,
        reduction_pct=reduction,
        unique_coords_path=unique_path,
        location_map_path=map_path,
    )


def run(precision: int = DEFAULT_PRECISION) -> tuple[ProfileResult, DedupResult]:
    """Profile then de-duplicate; returns both results."""
    con = connect()
    try:
        profile = profile_locations(con)
        dedup = deduplicate_coordinates(con, precision=precision)
    finally:
        con.close()
    return profile, dedup


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--precision",
        type=int,
        default=DEFAULT_PRECISION,
        help=f"coordinate decimal places before dedup (default {DEFAULT_PRECISION})",
    )
    args = parser.parse_args()

    profile, dedup = run(precision=args.precision)

    print("Profile")
    for k, v in asdict(profile).items():
        print(f"  {k:24} {v:,}")
    print(f"  {'usable_rows':24} {profile.usable_rows:,}")

    print("\nDe-duplication")
    print(f"  precision (dp)           {dedup.precision}")
    print(f"  total_locations          {dedup.total_locations:,}")
    print(f"  unique_coords            {dedup.unique_coords:,}")
    print(f"  duplicate_locations      {dedup.duplicate_locations:,}")
    print(f"  reduction                {dedup.reduction_pct:.1f}% fewer samples")
    print(f"  -> {dedup.unique_coords_path}")
    print(f"  -> {dedup.location_map_path}")


if __name__ == "__main__":
    main()
