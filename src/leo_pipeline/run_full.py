"""Run the whole pipeline over the **full** tile work list with no LLM in the loop.

The agentic orchestrator (``orchestrator --run``) drives A1–A5 through the Claude Agent SDK
and needs ``ANTHROPIC_API_KEY``; ``scripts/run_e2e_offline.py`` runs the deterministic engines
but on a *single* tile. This driver is the missing middle: the same deterministic A2 surface
build + A3 obstruction scoring, but fanned out across **every tile** in ``tiles.parquet`` on a
process pool — the no-API path for a full-dataset run.

Why fold A2 **and** A3 into one per-tile worker: tiles are independent (one cached surface and
one ``findings_<tile>.json`` each), and A3's ``--compute`` path scores point-by-point in a
single process, which over ~4.5M coordinates would take many hours single-threaded. Scoring
each tile inside its own worker parallelises that heavy step across cores.

It does **not** run A4/A5 — those are already whole-run CLIs; run them after this finishes:

    python -m leo_pipeline.run_full --workers 8      # A2 + A3 over all tiles (this script)
    python -m leo_pipeline.run_qa --compute          # A4 input audit + output-anomaly scan
    python -m leo_pipeline.run_report --compute       # A5 aggregates + PMTiles map + decision log

Resumable + idempotent: a tile whose ``findings_<tile_id>.json`` already exists is skipped, and
``fetch_aligned_surface`` is content-addressed so a re-fetch is a cache no-op. Re-running after
a partial/interrupted run only processes the tiles still missing findings (and the ones logged
in ``full_run_failures.json``).

Network is still required (A2 reads public COGs over HTTPS); no ``ANTHROPIC_API_KEY`` is.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import leo_pipeline.tools as tools
from leo_pipeline.config import ANALYSIS, PATHS
from leo_pipeline.tiling import TILES_FILE

FAILURES_FILE = "full_run_failures.json"


def _findings_path(tile_id: str) -> Path:
    return ANALYSIS.findings_dir / f"findings_{tile_id}.json"


def load_tiles() -> list[dict]:
    """Read the full tile work list (tile_id + EPSG:4326 bbox) from tiles.parquet."""
    import pandas as pd

    tiles_path = PATHS.data_interim / TILES_FILE
    if not tiles_path.exists():
        raise SystemExit(
            f"No tile work list at {tiles_path}. Run `python -m leo_pipeline.tiling` first."
        )
    df = pd.read_parquet(tiles_path)
    return [
        {
            "tile_id": r.tile_id,
            "bbox": [float(r.min_lon), float(r.min_lat), float(r.max_lon), float(r.max_lat)],
            "n_coords": int(r.n_coords),
        }
        for r in df.itertuples()
    ]


# --- per-tile worker (runs in a child process) ---------------------------------------


def process_tile(task: dict) -> dict:
    """Build one tile's aligned surface (A2) then score its locations (A3), deterministically.

    Returns a small status record (no big arrays cross the process boundary). Writes the
    surface into the A2 cache and ``findings_<tile_id>.json`` into ANALYSIS.findings_dir —
    exactly the artifacts the stock CLIs produce, so A4/A5 consume them unchanged.
    """
    from leo_pipeline import offline, run_analysis

    tile_id = task["tile_id"]
    bbox = task["bbox"]

    # Apply the cache-dir override (if any) inside the child — module state is per-process.
    if task.get("cache_dir"):
        tools.INGESTION = dataclasses.replace(tools.INGESTION, cache_dir=Path(task["cache_dir"]))

    t0 = time.monotonic()
    if task.get("dsm_uri"):
        # Re-score mode: a cached surface is already on disk (passed in by the parent), so
        # skip the STAC search + COG fetch entirely — pure, network-free re-scoring against
        # the existing DSM (e.g. after a spec/threshold change).
        surf = {"dsm_uri": task["dsm_uri"], "confidence": task.get("confidence"),
                "surface_mode": task.get("surface_mode")}
    else:
        # Use the surface payload fetch_aligned_surface returns directly — NOT load_surfaces(),
        # which scans the whole cache dir and picks an arbitrary entry per tile when more than
        # one exists (e.g. a stale single-granule surface alongside the fresh mosaic), so it
        # can score against the wrong (partial) surface.
        surf = offline.build_surface_sync(tile_id, bbox, verbose=False)
        if not surf.get("dsm_uri"):
            raise RuntimeError(f"surface build for {tile_id} returned no dsm_uri")

    info = {
        "work": [
            {
                "tile_id": tile_id,
                "dsm_uri": surf["dsm_uri"],
                "confidence": surf.get("confidence"),
                "points": run_analysis.points_for_tile(tile_id),
            }
        ],
        "sky_spec": task.get("sky_spec", {}),
        "dish_height_m": task.get("dish_height_m", ANALYSIS.mount_heights_m[0]),
    }
    findings = run_analysis.compute_findings(info)

    tier_counts: dict[str, int] = {}
    for f in findings:
        tier_counts[f["risk_tier"]] = tier_counts.get(f["risk_tier"], 0) + 1
    return {
        "tile_id": tile_id,
        "status": "ok",
        "surface_mode": surf.get("surface_mode"),
        "confidence": surf.get("confidence"),
        "n_findings": len(findings),
        "tier_counts": tier_counts,
        "secs": round(time.monotonic() - t0, 1),
    }


# --- driver --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="leo-full",
        description="Run A2 surface build + A3 obstruction scoring over the full tile work "
        "list, in parallel, with no LLM in the loop (no-API full-dataset run).",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 4,
        help="Parallel worker processes (default: CPU count = %(default)s).",
    )
    p.add_argument(
        "--limit",
        type=int,
        help="Process at most N (not-yet-done) tiles — for a smoke test (default: all).",
    )
    p.add_argument(
        "--mount-height",
        type=float,
        help="Dish mount height (m) above the surface (default 0.0, roof-only).",
    )
    p.add_argument(
        "--theta",
        type=float,
        help="Override the minimum reception angle θ in degrees (default from the spec, 25).",
    )
    p.add_argument("--cache-dir", help="Override the A2 surface cache directory.")
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-process tiles even if a findings file already exists (default: skip them).",
    )
    p.add_argument(
        "--rescore",
        action="store_true",
        help="Re-score every tile from its CACHED surface (no STAC/COG fetch, no network) — "
        "use after a spec/threshold change. Implies --force.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan (tiles total / already done / to process) and exit.",
    )
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    cache_dir = (
        str(Path(args.cache_dir).expanduser().resolve())
        if args.cache_dir
        else str(tools.INGESTION.cache_dir)
    )
    ANALYSIS.findings_dir.mkdir(parents=True, exist_ok=True)

    tiles = load_tiles()
    total = len(tiles)

    # Re-score mode: read the cached surfaces once in the parent and score against them with
    # no network. Only tiles that actually have a cached surface can be re-scored.
    surfaces: dict[str, dict] = {}
    if args.rescore:
        import leo_pipeline.run_analysis as ra

        surfaces = ra.load_surfaces(Path(cache_dir))
        tiles = [t for t in tiles if t["tile_id"] in surfaces]

    if args.force or args.rescore:
        todo = tiles
    else:
        todo = [t for t in tiles if not _findings_path(t["tile_id"]).exists()]
    done_already = total - len(todo)
    if args.limit:
        todo = todo[: args.limit]

    sky_spec = {"min_elev_deg": args.theta} if args.theta is not None else {}
    dish_h = ANALYSIS.mount_heights_m[0] if args.mount_height is None else args.mount_height

    print(f"Tiles total      : {total}")
    print(f"Already have findings: {done_already}")
    print(f"To process now   : {len(todo)}")
    print(f"Workers          : {args.workers}")
    print(f"Cache dir        : {cache_dir}")
    print(f"Findings dir     : {ANALYSIS.findings_dir}")
    print(f"Spec             : {ANALYSIS.spec_version}"
          + (f" (theta={args.theta})" if sky_spec else "")
          + f", dish_height_m={dish_h}")
    if args.dry_run or not todo:
        if not todo:
            print("Nothing to do — every tile already has findings.")
        else:
            print("(dry run — not processing)")
        return

    for t in todo:
        t["cache_dir"] = cache_dir
        t["sky_spec"] = sky_spec
        t["dish_height_m"] = dish_h
        if args.rescore:
            s = surfaces[t["tile_id"]]
            t["dsm_uri"] = s["dsm_uri"]
            t["confidence"] = s.get("confidence")
            t["surface_mode"] = s.get("surface_mode")

    t_start = time.monotonic()
    ok = 0
    failed: list[dict] = []
    agg_tiers: dict[str, int] = {}
    n_findings = 0

    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process_tile, t): t for t in todo}
        for i, fut in enumerate(as_completed(futures), start=1):
            t = futures[fut]
            try:
                res = fut.result()
                ok += 1
                n_findings += res["n_findings"]
                for tier, c in res["tier_counts"].items():
                    agg_tiers[tier] = agg_tiers.get(tier, 0) + c
            except Exception as exc:  # noqa: BLE001 — record and continue
                failed.append({"tile_id": t["tile_id"], "error": f"{type(exc).__name__}: {exc}"})
            if i % 25 == 0 or i == len(todo):
                elapsed = time.monotonic() - t_start
                rate = i / elapsed if elapsed else 0
                eta = (len(todo) - i) / rate if rate else 0
                print(
                    f"  [{i}/{len(todo)}] ok={ok} failed={len(failed)} "
                    f"findings={n_findings} | {rate:.1f} tiles/s, ETA {eta/60:.0f} min",
                    flush=True,
                )

    # Persist the failure list so a re-run (which also skips completed tiles) mops them up.
    fail_path = PATHS.data_interim / FAILURES_FILE
    if failed:
        fail_path.write_text(json.dumps(failed, indent=2))
    elif fail_path.exists():
        fail_path.unlink()  # clean slate when a re-run clears every previous failure

    elapsed = time.monotonic() - t_start
    print(f"\nDone in {elapsed/60:.1f} min. ok={ok} failed={len(failed)} "
          f"(of {len(todo)} processed; {done_already} already had findings).")
    print(f"Findings written : {n_findings} locations across {ok} tiles -> {ANALYSIS.findings_dir}")
    if agg_tiers:
        for tier in ("clear", "at_risk", "severe", "undetermined"):
            if tier in agg_tiers:
                print(f"  {tier:13} {agg_tiers[tier]}")
    if failed:
        print(f"\n{len(failed)} tiles FAILED — logged to {fail_path}. Re-run to retry them:")
        for f in failed[:10]:
            print(f"   - {f['tile_id']}: {f['error']}")
        if len(failed) > 10:
            print(f"   ... and {len(failed) - 10} more (see {fail_path}).")
    print("\nNext: python -m leo_pipeline.run_qa --compute && python -m leo_pipeline.run_report --compute")


if __name__ == "__main__":
    main()
