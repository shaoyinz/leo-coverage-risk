"""Deterministic tests for the run_qa CLI plumbing (no API, no network).

Covers argument parsing, the ``prepare`` step that resolves the findings + AOI, and the
``--compute`` deterministic path that writes a QA report — everything up to (but not
including) the live agent call.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

import leo_pipeline.run_qa as rq
from leo_pipeline.config import QA


def parse(argv):
    return rq.build_parser().parse_args(argv)


def _write_findings(dir_path, tile, n, *, tier="severe", pct=40.0):
    dir_path.mkdir(parents=True, exist_ok=True)
    rows = [
        {"location_id": f"loc{i}", "tile_id": tile, "obstruction_pct": pct,
         "risk_tier": tier, "confidence": "high", "spec_version": "spec-2026.06-theta25"}
        for i in range(n)
    ]
    (dir_path / f"findings_{tile}.json").write_text(json.dumps(rows))
    return rows


def test_prepare_resolves_findings_and_aoi(tmp_path):
    _write_findings(tmp_path, "T1", 40)
    info = rq.prepare(parse([
        "--findings-dir", str(tmp_path), "--aoi", "-84", "33", "-75", "36",
    ]))
    assert info["findings_dir"] == tmp_path.resolve()
    assert len(info["findings"]) == 40
    assert info["aoi"] == [-84.0, 33.0, -75.0, 36.0]
    assert info["check_input"] is True


def test_prepare_no_input_flag(tmp_path):
    info = rq.prepare(parse(["--findings-dir", str(tmp_path), "--no-input"]))
    assert info["check_input"] is False
    assert info["findings"] == []  # empty dir


def test_compute_writes_report_and_flags_saturation(tmp_path, monkeypatch):
    findings_dir = tmp_path / "analysis"
    _write_findings(findings_dir, "HOT", 40)
    reports_dir = tmp_path / "qa"
    # redirect the report output away from the real data/interim/qa
    monkeypatch.setattr(rq, "QA", dataclasses.replace(QA, reports_dir=reports_dir))

    info = rq.prepare(parse(["--findings-dir", str(findings_dir), "--no-input"]))
    report = rq.compute_report(info)

    assert report["output_qa"]["review_required"] is True
    rules = {a["rule"] for a in report["output_qa"]["anomalies"]}
    assert "saturated_region" in rules
    assert (reports_dir / "qa_report.json").exists()
    assert report["_path"].endswith("qa_report.json")


def test_compute_tile_scoped_report_name(tmp_path, monkeypatch):
    findings_dir = tmp_path / "analysis"
    _write_findings(findings_dir, "T9", 40, tier="clear", pct=0.0)
    reports_dir = tmp_path / "qa"
    monkeypatch.setattr(rq, "QA", dataclasses.replace(QA, reports_dir=reports_dir))

    info = rq.prepare(parse(["--findings-dir", str(findings_dir), "--tile", "T9", "--no-input"]))
    rq.compute_report(info)
    assert (reports_dir / "qa_report_T9.json").exists()


def test_compute_skips_output_when_no_findings(tmp_path, monkeypatch):
    reports_dir = tmp_path / "qa"
    monkeypatch.setattr(rq, "QA", dataclasses.replace(QA, reports_dir=reports_dir))
    info = rq.prepare(parse(["--findings-dir", str(tmp_path / "empty"), "--no-input"]))
    report = rq.compute_report(info)
    assert "skipped" in report["output_qa"]


def test_dry_run_main_does_not_call_api(tmp_path, capsys):
    rq.main(["--findings-dir", str(tmp_path), "--no-input", "--dry-run"])
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "QA spec" in out
