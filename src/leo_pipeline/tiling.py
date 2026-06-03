"""Tile the unique-coordinate work list for the A2 surface-ingestion stage.

Discovery (A1) decides *which* datasets to fetch; ingestion (A2) fetches them. But a
COG window read, reproject, and fuse is done per **tile**, not per coordinate — a tile is
the unit at which the windowed read amortises and at which the LLM batches its work, which
is what keeps LLM cost ``O(tiles)`` while compute stays ``O(locations)`` (architecture §6).

This module bins the ``unique_coords.parquet`` work list (from
:mod:`leo_pipeline.ingest`) onto a square metric grid and emits:

- ``tiles.parquet`` — one row per non-empty tile: ``tile_id, utm_epsg, min_lon, min_lat,
  max_lon, max_lat, n_coords``. The bbox is the tile's grid cell **plus a buffer** so an
  obstacle just outside a tile edge still enters that tile's surface (architecture §5,
  "tile-edge object").
- ``coord_tile_map.parquet`` — ``coord_id, tile_id``: which tile each work item belongs to,
  the join that lets a per-tile surface fan back out to its coordinates.

The grid is built in a single UTM zone (the AOI centroid's) so each cell is a real square
on the ground rather than a lat/lon trapezoid. The AOI may straddle two zones; the modest
edge distortion is acceptable for *grouping* work — the per-tile reprojection inside
``fetch_aligned_surface`` still resolves an ``auto-UTM`` CRS from each tile's own bbox.

Run standalone:

    python -m leo_pipeline.tiling                       # 5 km tiles, 300 m buffer
    python -m leo_pipeline.tiling --tile-size-m 2000    # finer grid
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path

from leo_pipeline.config import INGESTION, PATHS
from leo_pipeline.ingest import UNIQUE_COORDS_FILE

TILES_FILE = "tiles.parquet"
COORD_TILE_MAP_FILE = "coord_tile_map.parquet"


@dataclass(frozen=True)
class TilingResult:
    """Outcome of binning the unique-coordinate work list onto a tile grid."""

    utm_epsg: int
    tile_size_m: float
    buffer_m: float
    n_coords: int
    n_tiles: int
    tiles_path: Path
    coord_map_path: Path


def utm_epsg_for_lonlat(lon: float, lat: float) -> int:
    """EPSG code of the UTM zone containing ``(lon, lat)``.

    Northern hemisphere zones are ``326NN``, southern ``327NN``, where ``NN`` is the
    6°-wide zone number. Used both to build the tiling grid and as the ``auto-UTM`` target
    CRS in :func:`leo_pipeline.tools.fetch_aligned_surface`.
    """
    zone = int(math.floor((lon + 180.0) / 6.0)) % 60 + 1
    return (32600 if lat >= 0 else 32700) + zone


def assign_tiles(
    unique_coords_path: Path | None = None,
    *,
    tile_size_m: float = INGESTION.tile_size_m,
    buffer_m: float = INGESTION.tile_buffer_m,
    out_dir: Path | None = None,
) -> TilingResult:
    """Bin unique coordinates onto a UTM grid and write the tile list + coord→tile map.

    Tiles are keyed ``f"{utm_epsg}_{ix}_{iy}"`` where ``ix = floor(easting / tile_size_m)``
    and ``iy = floor(northing / tile_size_m)`` — a stable, reproducible id derived only
    from the grid, so re-running is idempotent and tile ids are comparable across runs.
    Each tile's lon/lat bbox is computed from its grid cell (buffered by ``buffer_m``) by
    inverse-transforming the cell corners, independent of which points landed inside it.
    """
    import numpy as np
    import pandas as pd
    from pyproj import Transformer

    src = unique_coords_path or (PATHS.data_interim / UNIQUE_COORDS_FILE)
    if not src.exists():
        raise FileNotFoundError(
            f"Work list not found at {src}. Run `python -m leo_pipeline.ingest` first to "
            "profile + de-duplicate the locations into unique_coords.parquet."
        )
    out = out_dir or PATHS.data_interim
    out.mkdir(parents=True, exist_ok=True)
    if tile_size_m <= 0:
        raise ValueError(f"tile_size_m must be > 0, got {tile_size_m}")
    if buffer_m < 0:
        raise ValueError(f"buffer_m must be >= 0, got {buffer_m}")

    df = pd.read_parquet(src, columns=["coord_id", "latitude", "longitude"])
    if df.empty:
        raise ValueError(f"work list {src} is empty")

    lon = df["longitude"].to_numpy()
    lat = df["latitude"].to_numpy()
    # One UTM zone for the whole grid: the AOI centroid's. Keeps the grid contiguous even
    # when the AOI straddles two zones (the per-tile fetch reprojection is still exact).
    utm_epsg = utm_epsg_for_lonlat(float(lon.mean()), float(lat.mean()))
    fwd = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
    inv = Transformer.from_crs(f"EPSG:{utm_epsg}", "EPSG:4326", always_xy=True)

    east, north = fwd.transform(lon, lat)
    ix = np.floor(east / tile_size_m).astype(np.int64)
    iy = np.floor(north / tile_size_m).astype(np.int64)

    tile_id = np.char.add(
        np.char.add(np.char.add(f"{utm_epsg}_", ix.astype(str)), "_"), iy.astype(str)
    )
    coord_map = pd.DataFrame({"coord_id": df["coord_id"].to_numpy(), "tile_id": tile_id})
    coord_map.to_parquet(out / COORD_TILE_MAP_FILE, index=False)

    # One row per non-empty tile, with its coordinate count.
    cells = pd.DataFrame({"ix": ix, "iy": iy})
    counts = cells.groupby(["ix", "iy"]).size().reset_index(name="n_coords")

    # Buffered UTM cell -> lon/lat bbox via the four corners (min/max over corners).
    cix = counts["ix"].to_numpy()
    ciy = counts["iy"].to_numpy()
    x0 = cix * tile_size_m - buffer_m
    y0 = ciy * tile_size_m - buffer_m
    x1 = (cix + 1) * tile_size_m + buffer_m
    y1 = (ciy + 1) * tile_size_m + buffer_m
    corner_x = np.concatenate([x0, x1, x0, x1])
    corner_y = np.concatenate([y0, y0, y1, y1])
    clon, clat = inv.transform(corner_x, corner_y)
    n = len(counts)
    clon = clon.reshape(4, n)
    clat = clat.reshape(4, n)

    counts["tile_id"] = (
        f"{utm_epsg}_"
        + counts["ix"].astype(str)
        + "_"
        + counts["iy"].astype(str)
    )
    counts["utm_epsg"] = utm_epsg
    counts["min_lon"] = clon.min(axis=0)
    counts["min_lat"] = clat.min(axis=0)
    counts["max_lon"] = clon.max(axis=0)
    counts["max_lat"] = clat.max(axis=0)

    tiles = counts[
        ["tile_id", "utm_epsg", "min_lon", "min_lat", "max_lon", "max_lat", "n_coords"]
    ].sort_values("tile_id", ignore_index=True)
    tiles.to_parquet(out / TILES_FILE, index=False)

    return TilingResult(
        utm_epsg=utm_epsg,
        tile_size_m=tile_size_m,
        buffer_m=buffer_m,
        n_coords=int(len(df)),
        n_tiles=int(len(tiles)),
        tiles_path=out / TILES_FILE,
        coord_map_path=out / COORD_TILE_MAP_FILE,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tile-size-m",
        type=float,
        default=INGESTION.tile_size_m,
        help=f"tile edge length in metres (default {INGESTION.tile_size_m:g})",
    )
    parser.add_argument(
        "--buffer-m",
        type=float,
        default=INGESTION.tile_buffer_m,
        help=f"edge overlap buffer in metres (default {INGESTION.tile_buffer_m:g})",
    )
    args = parser.parse_args()

    result = assign_tiles(tile_size_m=args.tile_size_m, buffer_m=args.buffer_m)

    print("Tiling")
    print(f"  utm_epsg        EPSG:{result.utm_epsg}")
    print(f"  tile_size_m     {result.tile_size_m:,.0f}")
    print(f"  buffer_m        {result.buffer_m:,.0f}")
    print(f"  unique coords   {result.n_coords:,}")
    print(f"  tiles           {result.n_tiles:,}")
    print(f"  mean coords/tile {result.n_coords / max(result.n_tiles, 1):,.1f}")
    print(f"  -> {result.tiles_path}")
    print(f"  -> {result.coord_map_path}")


if __name__ == "__main__":
    main()
