#!/usr/bin/env python
"""Fetch NC county boundaries -> ``outputs/nc_counties.geojson`` for the interactive map.

The coverage map's county *filter* works off FIPS attributes baked onto each point, but to
actually *see* county lines (the reviewer ask) the MapLibre viewer needs boundary polygons.
This pulls the Census cartographic boundary file (already generalised for web maps), keeps the
AOI state's counties, slims it to ``GEOID`` + ``NAME``, lightly simplifies, and writes a small
WGS84 GeoJSON next to the other outputs. ``report.build_map`` wires the overlay in automatically
when this file is present (and silently skips it when it is not — e.g. offline).

    ../../.venv/bin/python scripts/fetch_county_boundaries.py            # NC (state 37)
    ../../.venv/bin/python scripts/fetch_county_boundaries.py --state 45 # another state
"""
from __future__ import annotations

import argparse
import tempfile
import urllib.request
from pathlib import Path

import geopandas as gpd

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "outputs" / "nc_counties.geojson"
# Census 2023 cartographic boundary, 1:500k — generalised, small, public, no key.
SOURCE_URL = "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_county_500k.zip"
SIMPLIFY_TOLERANCE_DEG = 0.002  # ~200 m; trims vertices without visibly moving the line


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", default="37", help="2-digit state FIPS to keep (default 37 = NC)")
    ap.add_argument("--out", default=str(OUT), help="output GeoJSON path")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as td:
        zip_path = Path(td) / "counties.zip"
        print(f"downloading {SOURCE_URL}")
        urllib.request.urlretrieve(SOURCE_URL, zip_path)
        gdf = gpd.read_file(f"zip://{zip_path}")

    gdf = gdf[gdf["STATEFP"] == args.state].copy()
    if gdf.empty:
        raise SystemExit(f"no counties for state FIPS {args.state!r} in the source file")

    gdf = gdf.to_crs(epsg=4326)
    gdf["geometry"] = gdf.geometry.simplify(SIMPLIFY_TOLERANCE_DEG, preserve_topology=True)
    gdf = gdf[["GEOID", "NAME", "geometry"]]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Overwrite cleanly (to_file appends/errors on some drivers if the file exists).
    out_path.write_text(gdf.to_json())
    print(f"wrote {out_path}  ({len(gdf)} counties, {out_path.stat().st_size/1024:.0f} KB)")


if __name__ == "__main__":
    main()
