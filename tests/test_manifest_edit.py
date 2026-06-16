"""Tests for the H1 manifest-edit CLI (leo_pipeline.manifest_edit)."""

from __future__ import annotations

import json

from leo_pipeline import manifest_edit as me


def _read(path):
    return json.loads(path.read_text())


def test_add_creates_manifest_with_building_access_params(tmp_path):
    path = tmp_path / "data_manifest.json"
    rc = me.main(
        [
            "--path", str(path), "add",
            "--factor", "buildings",
            "--dataset-id", "OpenBuildingMap (GFZ/GEM)",
            "--access", "web", "--selected",
            "--source-url", "https://www.openbuildingmap.org/",
            "--license", "ODbL-1.0",
            "--url-template", "https://data.source.coop/.../building.{quadkey}.parquet",
            "--quadkey-zoom", "6",
            "--vintage", "GHSL 2023A",
            "--bbox", "-84.32", "33.84", "-75.46", "36.59",
        ]
    )
    assert rc == 0
    manifest = _read(path)
    assert manifest["aoi_bbox"] == [-84.32, 33.84, -75.46, 36.59]
    (entry,) = manifest["entries"]
    assert entry["factor"] == "buildings" and entry["selected"] is True
    assert entry["access_params"]["url_template"].endswith("building.{quadkey}.parquet")
    assert entry["access_params"]["quadkey_zoom"] == 6  # coerced to int


def test_add_grounding_gate_deselects_ungrounded_web_pick(tmp_path):
    path = tmp_path / "m.json"
    me.main(
        [
            "--path", str(path), "add",
            "--factor", "canopy", "--dataset-id", "guess-canopy",
            "--access", "web", "--selected",  # no source_url / template -> ungrounded
        ]
    )
    (entry,) = _read(path)["entries"]
    assert entry["selected"] is False
    assert any("ungrounded" in n for n in _read(path)["notes"])


def test_add_replaces_same_factor_and_id(tmp_path):
    path = tmp_path / "m.json"
    common = ["--path", str(path), "add", "--factor", "terrain", "--dataset-id", "cop-dem-glo-30",
              "--access", "stac"]
    me.main(common + ["--gsd-m", "30"])
    me.main(common + ["--gsd-m", "10", "--rationale", "updated"])
    entries = _read(path)["entries"]
    assert len(entries) == 1  # replaced, not duplicated
    assert entries[0]["gsd_m"] == 10.0 and entries[0]["rationale"] == "updated"


def test_remove_by_id(tmp_path):
    path = tmp_path / "m.json"
    me.main(["--path", str(path), "add", "--factor", "buildings",
             "--dataset-id", "ms-buildings", "--access", "stac"])
    assert me.main(["--path", str(path), "remove", "--dataset-id", "ms-buildings"]) == 0
    assert _read(path)["entries"] == []
    # removing a non-existent id is a non-zero (no-op) exit
    assert me.main(["--path", str(path), "remove", "--dataset-id", "nope"]) == 1


def test_delete_removes_file(tmp_path):
    path = tmp_path / "m.json"
    me.main(["--path", str(path), "add", "--factor", "terrain",
             "--dataset-id", "x", "--access", "stac"])
    assert path.exists()
    assert me.main(["--path", str(path), "delete"]) == 0
    assert not path.exists()
    assert me.main(["--path", str(path), "delete"]) == 1  # nothing to delete


def test_list_on_missing_manifest(tmp_path, capsys):
    rc = me.main(["--path", str(tmp_path / "absent.json"), "list"])
    assert rc == 1
    assert "No manifest" in capsys.readouterr().out
