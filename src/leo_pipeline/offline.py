"""Deterministic, no-API surface build for A2 — the one piece that has no ``--compute`` CLI.

A3/A4/A5 each ship a ``--compute`` path that runs their geo engine with no LLM in the loop,
but A2's CLI (``run_ingestion``) only builds surfaces through the ingestion *agent*. This
module fills that gap: it turns a tile bbox into a cached, aligned DSM by calling the exact
same deterministic tools the A2 agent calls (``stac_item_read`` to resolve a COG href, then
``fetch_aligned_surface`` to read/reproject/fuse it) — only the agent's *orchestration* is
replaced by the fixed ``SURFACE_INPUTS`` fallback below.

Both the single-tile offline demo (``scripts/run_e2e_offline.py``) and the parallel full-run
driver (``leo_pipeline.run_full``) build on this, so the surface-build logic lives in one
importable place. Network is still required — the surface is a windowed read of a public COG
over HTTPS — but no ``ANTHROPIC_API_KEY`` is.
"""

from __future__ import annotations

import asyncio
import json

import pandas as pd

import leo_pipeline.tools as tools
from leo_pipeline.config import INGESTION, PATHS
from leo_pipeline.tiling import TILES_FILE

# A small, real work-list tile in central/eastern NC (~321 unique coords). Used as the default
# demo tile: big enough for the county/state rollup + QA anomaly rules to do something visible,
# small enough that the 595x595 @10 m surface reads in seconds.
DEFAULT_TILE = "32617_158_806"

# The two surface inputs A2 composites into a gap-filled mosaic: a lidar DSM (carries
# trees+buildings → real obstruction, but project-based so often patchy) laid over a globally
# complete DEM that fills the holes. Each entry is the STAC collection to resolve and the
# manifest "factor" slot it fills. fetch_aligned_surface auto-picks `mosaic` when both are
# present (lidar where valid, DEM elsewhere), else degrades to whichever single source resolved.
SURFACE_INPUTS = [
    ("3dep-lidar-dsm", "surface"),  # lidar DSM (true surface; patchy)
    ("cop-dem-glo-30", "terrain"),  # DEM base (complete; fills the lidar holes)
]


async def _call(sdk_tool, args: dict) -> tuple[dict, dict]:
    """Invoke an in-process MCP tool's handler and parse its text envelope into a dict."""
    env = await sdk_tool.handler(args)
    text = env["content"][0]["text"] if env.get("content") else "{}"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = {"_raw": text}
    return env, payload


def tile_bbox(tile_id: str) -> list[float]:
    """Look the tile's EPSG:4326 bbox up in the deterministic tiling work list."""
    tiles_path = PATHS.data_interim / TILES_FILE
    if not tiles_path.exists():
        raise SystemExit(
            f"No tile work list at {tiles_path}. Run `python -m leo_pipeline.tiling` first."
        )
    df = pd.read_parquet(tiles_path)
    row = df[df["tile_id"] == tile_id]
    if row.empty:
        raise SystemExit(f"tile_id {tile_id!r} not found in {tiles_path}")
    r = row.iloc[0]
    return [float(r.min_lon), float(r.min_lat), float(r.max_lon), float(r.max_lat)]


async def build_surface(
    tile_id: str, bbox: list[float], *, verbose: bool = True, with_buildings: bool = True
) -> dict:
    """A2, deterministically: resolve real COGs for the tile and composite them into a cached,
    gap-filled mosaic DSM (band 1 elevation + band 2 per-pixel provenance).

    Resolves each input in ``SURFACE_INPUTS`` and hands every one that resolved to
    ``fetch_aligned_surface`` in a single manifest, letting it auto-pick the best mode
    (``mosaic`` when both lidar + DEM are present) — exactly the judgement the A2 ingestion
    agent makes. When ``with_buildings`` (default), an OpenBuildingMap ``buildings`` factor is
    added so building heights fuse into the modelled-fill (non-lidar) regions of the surface.
    ``fetch_aligned_surface`` is idempotent + content-addressed, so a re-run with an existing
    cache entry is a no-op.
    """
    # Resolve EVERY intersecting granule per source (not just the best one) so a tile that
    # straddles a 1° granule boundary gets full coverage once fetch_aligned_surface mosaics
    # them — otherwise the DEM base is full of holes and those points score 'undetermined'.
    manifest: dict[str, list[str] | str] = {}
    for collection, factor in SURFACE_INPUTS:
        hrefs = tools._search_item_hrefs(collection, bbox)
        if verbose:
            print(f"  [stac] {collection:16} granules={len(hrefs)} "
                  f"-> {'ok' if hrefs else 'NONE'}")
        if hrefs:
            manifest[factor] = hrefs
    if not manifest:
        raise RuntimeError("no surface inputs resolved — check network access to the COG hosts")
    # OpenBuildingMap is vector GeoParquet resolved per-tile inside fetch_aligned_surface (from
    # the bbox's quadkey), so the manifest carries the source marker, not a STAC href. A remote
    # OBM failure is swallowed downstream — buildings are opportunistic, never required.
    if with_buildings:
        manifest["buildings"] = INGESTION.building_source_url
        if verbose:
            print(f"  [obm ] buildings        -> {INGESTION.building_source_url}")

    env, surf = await _call(
        tools.fetch_aligned_surface,
        {"tile_id": tile_id, "bbox": bbox, "manifest": manifest},
    )
    if env.get("is_error"):
        raise RuntimeError(f"fetch_aligned_surface failed: {surf.get('_raw') or surf}")
    if verbose:
        print(f"  [fetch] mode={surf['surface_mode']} confidence={surf['confidence']} "
              f"coverage={surf['coverage_flag']} valid_frac={surf['valid_fraction']} "
              f"provenance={surf.get('provenance_fractions')}")
    return surf


def build_surface_sync(
    tile_id: str, bbox: list[float], *, verbose: bool = True, with_buildings: bool = True
) -> dict:
    """Blocking wrapper around :func:`build_surface` for non-async callers (the full-run
    driver's process-pool workers run one tile per ``asyncio.run``)."""
    return asyncio.run(
        build_surface(tile_id, bbox, verbose=verbose, with_buildings=with_buildings)
    )
