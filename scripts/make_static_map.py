#!/usr/bin/env python
"""Render a static, offline coverage-risk map (PNG) for the README / reviewers.

The interactive ``outputs/coverage_map.html`` needs a static server + internet (CDN +
basemap tiles). This is its zero-dependency complement: a tier-coloured scatter of the
scored locations that renders inline on GitHub with a clear legend and the headline
percentages — the "static map output with clear legends" the challenge accepts for core.

Reads the same data the A5 reporting engine uses:
  * ``data/interim/analysis/*.json``   — per-tile findings (one row per unique coordinate)
  * ``data/interim/unique_coords.parquet`` — coord_id -> lat/lon (the dedup work list)
  * ``outputs/aggregates.json``        — headline tier counts/rates (for the caption)

All at-risk + severe points are drawn; ``clear`` points are downsampled (there are ~4.5M)
so the at-risk signal stays visible. Run from anywhere:

    ../../.venv/bin/python scripts/make_static_map.py
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = REPO / "data" / "interim" / "analysis"
COORDS_PARQUET = REPO / "data" / "interim" / "unique_coords.parquet"
AGGREGATES = REPO / "outputs" / "aggregates.json"
OUT_PNG = REPO / "outputs" / "coverage_map.png"

# Tier -> hex, mirroring config.Reporting.tier_colors (🟢 clear / 🟡 at-risk / 🔴 severe).
TIER_COLORS = {
    "clear": "#1a9850",
    "at_risk": "#fdae61",
    "severe": "#d73027",
    "undetermined": "#999999",
}
CLEAR_SAMPLE_CAP = 150_000  # plot at most this many 'clear' points (downsample the rest)


def _coord_lonlat_arrays() -> tuple[np.ndarray, np.ndarray]:
    """coord_id-indexed lon/lat lookup arrays (NaN where an id is absent)."""
    t = pq.read_table(COORDS_PARQUET, columns=["coord_id", "latitude", "longitude"])
    ids = t.column("coord_id").to_numpy()
    lat = t.column("latitude").to_numpy()
    lon = t.column("longitude").to_numpy()
    size = int(ids.max()) + 1
    lon_by_id = np.full(size, np.nan)
    lat_by_id = np.full(size, np.nan)
    lon_by_id[ids] = lon
    lat_by_id[ids] = lat
    return lon_by_id, lat_by_id


def _collect_points(lon_by_id: np.ndarray, lat_by_id: np.ndarray):
    """Walk every findings file -> per-tier (lon, lat) lists. clear is downsampled by stride."""
    pts: dict[str, list[tuple[float, float]]] = {t: [] for t in TIER_COLORS}
    clear_seen = 0
    # Stride keeps the clear layer near CLEAR_SAMPLE_CAP without a second pass.
    n_clear_total = 0
    files = sorted(glob.glob(str(ANALYSIS_DIR / "*.json")))
    if not files:
        raise SystemExit(f"no findings under {ANALYSIS_DIR} — run the pipeline first")
    # First pass count is overkill; estimate stride from aggregates if present.
    stride = 1
    if AGGREGATES.exists():
        summ = json.loads(AGGREGATES.read_text()).get("summary", {})
        n_clear_total = int(summ.get("tier_counts", {}).get("clear", 0))
        if n_clear_total > CLEAR_SAMPLE_CAP:
            stride = max(1, n_clear_total // CLEAR_SAMPLE_CAP)

    for fp in files:
        rows = json.loads(Path(fp).read_text())
        for r in rows:
            tier = r.get("risk_tier") or "undetermined"
            cid = str(r.get("location_id", ""))
            if cid.startswith("coord_"):
                cid = cid[len("coord_"):]
            try:
                cid_i = int(cid)
            except ValueError:
                continue
            if cid_i >= lon_by_id.size:
                continue
            lon, lat = lon_by_id[cid_i], lat_by_id[cid_i]
            if np.isnan(lon) or np.isnan(lat):
                continue
            if tier == "clear":
                if clear_seen % stride == 0:
                    pts["clear"].append((lon, lat))
                clear_seen += 1
            else:
                pts.setdefault(tier, []).append((lon, lat))
    return pts


def main() -> None:
    lon_by_id, lat_by_id = _coord_lonlat_arrays()
    pts = _collect_points(lon_by_id, lat_by_id)

    summary = {}
    if AGGREGATES.exists():
        summary = json.loads(AGGREGATES.read_text()).get("summary", {})
    counts = summary.get("tier_counts", {})
    n_total = summary.get("n_findings") or sum(counts.values()) or 1

    fig, ax = plt.subplots(figsize=(12, 9), dpi=130)
    ax.set_facecolor("#f7f7f7")
    # Draw clear first (background), then at_risk, then severe on top so risk is never hidden.
    draw_order = ["clear", "at_risk", "severe", "undetermined"]
    sizes = {"clear": 0.4, "at_risk": 3.0, "severe": 6.0, "undetermined": 1.0}
    for tier in draw_order:
        xy = pts.get(tier) or []
        if not xy:
            continue
        arr = np.asarray(xy)
        ax.scatter(
            arr[:, 0], arr[:, 1],
            s=sizes.get(tier, 1.0), c=TIER_COLORS[tier],
            linewidths=0, alpha=0.7 if tier == "clear" else 0.9, label=None,
        )

    ax.set_aspect("equal", adjustable="datalim")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.grid(True, color="#dddddd", linewidth=0.5)
    ax.set_title(
        "LEO (Starlink) coverage-obstruction risk — scored locations",
        fontsize=14, fontweight="bold",
    )

    # Legend with counts + percentages from the aggregates summary.
    def pct(t: str) -> str:
        n = counts.get(t, 0)
        return f"{t.replace('_', '-')}: {n:,} ({100*n/n_total:.1f}%)"

    handles = [
        plt.Line2D([0], [0], marker="o", color="w", markerfacecolor=TIER_COLORS[t],
                   markersize=9, label=pct(t))
        for t in ["clear", "at_risk", "severe"]
        if counts.get(t, 0) or pts.get(t)
    ]
    ax.legend(handles=handles, loc="lower left", fontsize=10, framealpha=0.95,
              title=f"{int(n_total):,} locations assessed")

    clear_drawn = len(pts.get("clear") or [])
    caption = (
        f"clear points downsampled for legibility ({clear_drawn:,} of {counts.get('clear', 0):,} drawn); "
        f"all at-risk + severe shown. Remote screen — verify on site, not a service guarantee."
    )
    fig.text(0.5, 0.02, caption, ha="center", fontsize=8, color="#555555")
    fig.subplots_adjust(bottom=0.09)

    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PNG, bbox_inches="tight")
    print(f"wrote {OUT_PNG}  ({OUT_PNG.stat().st_size/1024:.0f} KB)")
    print("tier draw counts:", {t: len(pts.get(t) or []) for t in draw_order})


if __name__ == "__main__":
    main()
