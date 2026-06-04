"""Deterministic tests for the run_report CLI plumbing (no API, no network).

Covers argument parsing, the ``prepare`` step that resolves findings + outputs dir, and the
``--compute`` deterministic path that writes all A5 artifacts (with ``--no-tiles`` so the
tippecanoe binary is not required) — everything up to (but not including) the live agent call.
"""

from __future__ import annotations

import json

import leo_pipeline.run_report as rr


def parse(argv):
    return rr.build_parser().parse_args(argv)


def _write_findings(dir_path, tile, n):
    dir_path.mkdir(parents=True, exist_ok=True)
    rows = [
        {"location_id": f"loc_{i}", "tile_id": tile,
         "obstruction_pct": (40.0 if i % 5 == 0 else 0.0),
         "risk_tier": ("severe" if i % 5 == 0 else "clear"),
         "confidence": "high", "spec_version": "spec-2026.06-theta25",
         "lon": -80.0 + 0.001 * i, "lat": 35.0 + 0.001 * i}
        for i in range(n)
    ]
    (dir_path / f"findings_{tile}.json").write_text(json.dumps(rows))
    return rows


def test_prepare_resolves_findings_and_outputs(tmp_path):
    fdir = tmp_path / "analysis"
    _write_findings(fdir, "T1", 10)
    out = tmp_path / "out"
    info = rr.prepare(parse(["--findings-dir", str(fdir), "--outputs-dir", str(out), "--no-tiles"]))
    assert info["findings_dir"] == fdir.resolve()
    assert len(info["findings"]) == 10
    assert info["out_dir"] == out.resolve()
    assert info["run_tiles"] is False


def test_compute_writes_all_artifacts(tmp_path):
    fdir = tmp_path / "analysis"
    _write_findings(fdir, "T1", 10)
    out = tmp_path / "out"
    info = rr.prepare(parse(["--findings-dir", str(fdir), "--outputs-dir", str(out), "--no-tiles"]))
    result = rr.compute_report(info)

    assert (out / "aggregates.json").exists()
    assert (out / "locations.geojson").exists()
    assert (out / "coverage_map.html").exists()
    assert (out / "decision_log.md").exists()
    assert result["map"]["n_features"] == 10
    kinds = {a["kind"] for a in result["artifacts"]}
    assert {"aggregates", "geojson", "map_html", "decision_log"} <= kinds


def test_dry_run_main_does_not_call_api(tmp_path, capsys):
    rr.main(["--findings-dir", str(tmp_path), "--no-tiles", "--dry-run"])
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "Report spec" in out
