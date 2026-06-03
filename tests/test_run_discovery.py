"""Deterministic tests for the run_discovery CLI plumbing (no API, no network).

Covers argument parsing and the ``prepare`` step that resolves the AOI and output path
and applies the process-level overrides — everything up to (but not including) the live
agent call.
"""

from __future__ import annotations

import dataclasses

import pytest

import leo_pipeline.run_discovery as rd
import leo_pipeline.tools as tools
from leo_pipeline.config import PATHS


@pytest.fixture
def isolate(monkeypatch):
    """Capture the mutable module globals so each test's overrides are undone after."""
    monkeypatch.setattr(tools, "AOI_OVERRIDE", None)
    monkeypatch.setattr(tools, "DISCOVERY", tools.DISCOVERY)
    yield


def parse(argv):
    return rd.build_parser().parse_args(argv)


def test_bbox_sets_process_override(isolate):
    info = rd.prepare(parse(["--bbox", "-84.32", "33.84", "-75.46", "36.59"]))
    assert tools.AOI_OVERRIDE == [-84.32, 33.84, -75.46, 36.59]
    assert "override" in info["aoi_source"]


def test_out_redirects_manifest_path(isolate, tmp_path):
    out = tmp_path / "my_manifest.json"
    info = rd.prepare(parse(["--bbox", "-84", "33", "-75", "36", "--out", str(out)]))
    assert tools.DISCOVERY.manifest_path == out.resolve()
    assert info["out_path"] == out.resolve()


@pytest.mark.parametrize(
    "bbox",
    [
        ["-75", "36", "-84", "33"],  # min_lon > max_lon and min_lat > max_lat
        ["-200", "33", "-75", "36"],  # longitude out of range
        ["-84", "-100", "-75", "36"],  # latitude out of range
    ],
)
def test_invalid_bbox_rejected(isolate, bbox):
    with pytest.raises(SystemExit):
        rd.prepare(parse(["--bbox", *bbox]))


def _patch_missing_csv(monkeypatch, tmp_path):
    monkeypatch.setattr(
        rd, "PATHS", dataclasses.replace(PATHS, locations_csv=tmp_path / "nope.csv")
    )


def test_missing_csv_without_fallback_errors(isolate, monkeypatch, tmp_path):
    _patch_missing_csv(monkeypatch, tmp_path)
    with pytest.raises(SystemExit):
        rd.prepare(parse([]))  # no --bbox, no CSV, no --allow-fallback


def test_missing_csv_with_fallback_ok(isolate, monkeypatch, tmp_path):
    _patch_missing_csv(monkeypatch, tmp_path)
    info = rd.prepare(parse(["--allow-fallback"]))
    assert tools.AOI_OVERRIDE is None
    assert "fallback" in info["aoi_source"]


def test_csv_present_uses_csv_not_override(isolate, monkeypatch, tmp_path):
    csv = tmp_path / "locations.csv"
    csv.write_text("location_id,latitude,longitude\n")
    monkeypatch.setattr(rd, "PATHS", dataclasses.replace(PATHS, locations_csv=csv))
    info = rd.prepare(parse([]))
    assert tools.AOI_OVERRIDE is None  # let get_aoi_bbox derive from the CSV
    assert "locations CSV" in info["aoi_source"]


def test_dry_run_main_does_not_call_api(isolate, capsys):
    # main() with --dry-run must return without touching require_api_key / the SDK.
    rd.main(["--bbox", "-84.32", "33.84", "-75.46", "36.59", "--dry-run"])
    out = capsys.readouterr().out
    assert "dry run" in out
    assert tools.AOI_OVERRIDE == [-84.32, 33.84, -75.46, 36.59]
