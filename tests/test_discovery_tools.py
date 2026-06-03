"""Deterministic unit tests for the A1 data-discovery tools.

No network and no API key: STAC catalog access is monkeypatched and DuckDB access is
stubbed, so these run fast and offline. The tools under test live in
``leo_pipeline.tools`` and back the DATA_DISCOVERY_AGENT allow-list.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import leo_pipeline.tools as tools
from leo_pipeline.config import DISCOVERY
from leo_pipeline.state import DataManifest, DatasetCandidate
from leo_pipeline.tools import _summarize_stac_item

from conftest import run_tool


# --- get_aoi_bbox --------------------------------------------------------------------


def test_get_aoi_bbox_fallback_when_csv_absent(monkeypatch):
    """No locations CSV -> CONUS fallback bbox, flagged as such, with zero counts."""

    def _raise():
        raise FileNotFoundError("no csv")

    monkeypatch.setattr(tools.ingest, "connect", _raise)

    envelope, payload = run_tool(tools.get_aoi_bbox, {})

    assert not envelope.get("is_error")
    assert payload["source"] == "default_conus_fallback"
    assert payload["bbox"] == list(DISCOVERY.default_aoi_bbox)
    assert payload["n_total"] == 0
    assert payload["crs"] == "EPSG:4326"


def test_get_aoi_bbox_override_counts_points_in_box(monkeypatch):
    """An AOI_OVERRIDE is tagged cli_override but still counts CSV points inside it."""
    closed = {"v": False}
    fake_con = SimpleNamespace(close=lambda: closed.__setitem__("v", True))
    monkeypatch.setattr(tools, "AOI_OVERRIDE", [-84.32, 33.84, -75.46, 36.59])
    monkeypatch.setattr(tools.ingest, "connect", lambda: fake_con)
    monkeypatch.setattr(
        tools.ingest,
        "count_in_bbox",
        lambda con, bbox: {"n_total": 1234, "n_distinct": 1200},
    )

    envelope, payload = run_tool(tools.get_aoi_bbox, {})

    assert not envelope.get("is_error")
    assert payload["source"] == "cli_override"
    assert payload["bbox"] == [-84.32, 33.84, -75.46, 36.59]
    assert payload["n_total"] == 1234
    assert payload["n_distinct"] == 1200
    assert closed["v"], "connection should be closed"


def test_get_aoi_bbox_override_zero_when_csv_absent(monkeypatch):
    """With no CSV, an override still resolves (zero counts) rather than erroring."""

    def _raise():
        raise FileNotFoundError("no csv")

    monkeypatch.setattr(tools, "AOI_OVERRIDE", [-84.32, 33.84, -75.46, 36.59])
    monkeypatch.setattr(tools.ingest, "connect", _raise)

    envelope, payload = run_tool(tools.get_aoi_bbox, {})

    assert not envelope.get("is_error")
    assert payload["source"] == "cli_override"
    assert payload["n_total"] == 0
    assert payload["n_distinct"] == 0


def test_get_aoi_bbox_success_tags_source_and_closes(monkeypatch):
    """When the CSV is present, the AOI is passed through and tagged locations_csv."""
    closed = {"v": False}
    fake_con = SimpleNamespace(close=lambda: closed.__setitem__("v", True))
    fixed = {
        "bbox": [-84.32, 33.84, -75.46, 36.59],
        "crs": "EPSG:4326",
        "n_total": 100,
        "n_distinct": 90,
    }
    monkeypatch.setattr(tools.ingest, "connect", lambda: fake_con)
    monkeypatch.setattr(tools.ingest, "aoi_bbox", lambda con: dict(fixed))

    envelope, payload = run_tool(tools.get_aoi_bbox, {})

    assert not envelope.get("is_error")
    assert payload["source"] == "locations_csv"
    assert payload["bbox"] == fixed["bbox"]
    assert payload["n_distinct"] == 90
    assert closed["v"], "connection should be closed"


# --- _summarize_stac_item ------------------------------------------------------------


def test_summarize_item_prefers_data_asset_and_maps_metadata(make_stac_item):
    item = make_stac_item(
        item_id="abc",
        properties={"proj:epsg": 4326, "gsd": 1.0, "datetime": "2023-01-01T00:00:00Z"},
        asset_hrefs={"dem": "http://x/dem.tif", "data": "http://x/data.tif"},
    )
    out = _summarize_stac_item(item)
    assert out["id"] == "abc"
    assert out["asset_key"] == "data"  # "data" beats "dem" in the priority list
    assert out["asset_href"] == "http://x/data.tif"
    assert out["crs"] == "EPSG:4326"
    assert out["gsd_m"] == 1.0
    assert out["datetime"] == "2023-01-01T00:00:00Z"


def test_summarize_item_datetime_falls_back_and_handles_unknown_asset(make_stac_item):
    item = make_stac_item(
        properties={"start_datetime": "2020-06-01T00:00:00Z"},  # no "datetime"
        asset_hrefs={"thumbnail": "http://x/thumb.png"},  # none of the preferred keys
    )
    out = _summarize_stac_item(item)
    assert out["datetime"] == "2020-06-01T00:00:00Z"
    assert out["asset_key"] == "thumbnail"  # first asset when no preferred key present
    assert out["crs"] is None  # no proj:epsg


# --- stac_search ---------------------------------------------------------------------


def test_stac_search_rejects_bad_bbox():
    envelope, payload = run_tool(
        tools.stac_search, {"collections": ["x"], "bbox": [1, 2, 3]}
    )
    assert envelope.get("is_error")
    assert "bbox must be" in payload


def test_stac_search_requires_collections():
    envelope, payload = run_tool(
        tools.stac_search, {"collections": [], "bbox": [-80, 34, -79, 35]}
    )
    assert envelope.get("is_error")
    assert "at least one collection" in payload


class _FakeCollection:
    def __init__(self, extent_bbox):
        spatial = SimpleNamespace(bboxes=[extent_bbox]) if extent_bbox else None
        self.extent = SimpleNamespace(spatial=spatial)


class _FakeSearch:
    def __init__(self, items):
        self._items = items

    def items(self):
        return list(self._items)


def _fake_client_factory(*, collection=None, items=(), get_coll_error=None):
    """Build a fake pystac_client.Client class whose .open returns a fake client."""
    captured = {}

    class _FakeClient:
        @staticmethod
        def open(url):
            captured["url"] = url
            return _FakeClient()

        def get_collection(self, coll_id):
            if get_coll_error:
                raise RuntimeError(get_coll_error)
            return collection

        def search(self, **kwargs):
            captured["search_kwargs"] = kwargs
            return _FakeSearch(items)

    return _FakeClient, captured


def test_stac_search_success_computes_coverage_and_items(monkeypatch, make_stac_item):
    # Collection extent fully contains the AOI -> 100% coverage (real shapely math).
    coll = _FakeCollection([-90.0, 30.0, -70.0, 40.0])
    items = [
        make_stac_item("i0", {"proj:epsg": 4326, "gsd": 1.0}, {"data": "http://a"}),
        make_stac_item("i1", {"proj:epsg": 5070, "gsd": 10.0}, {"dem": "http://b"}),
    ]
    fake_client, captured = _fake_client_factory(collection=coll, items=items)
    monkeypatch.setattr("pystac_client.Client", fake_client)

    envelope, payload = run_tool(
        tools.stac_search,
        {
            "catalog": "planetary_computer",
            "collections": ["3dep-lidar-dsm"],
            "bbox": [-80.0, 34.0, -79.0, 35.0],
        },
    )

    assert not envelope.get("is_error")
    # catalog label resolved to its configured root URL
    assert payload["catalog"] == DISCOVERY.stac_catalogs["planetary_computer"]
    assert captured["url"] == DISCOVERY.stac_catalogs["planetary_computer"]
    (entry,) = payload["collections"]
    assert entry["collection"] == "3dep-lidar-dsm"
    assert entry["error"] is None
    assert entry["aoi_coverage_pct"] == 100.0
    assert entry["n_items_sampled"] == 2
    assert entry["items"][0]["asset_href"] == "http://a"
    assert entry["items"][1]["crs"] == "EPSG:5070"


def test_stac_search_collection_lookup_failure_still_samples_items(
    monkeypatch, make_stac_item
):
    items = [make_stac_item("only", {"gsd": 30.0}, {"data": "http://c"})]
    fake_client, _ = _fake_client_factory(
        items=items, get_coll_error="collection 404"
    )
    monkeypatch.setattr("pystac_client.Client", fake_client)

    envelope, payload = run_tool(
        tools.stac_search,
        {"collections": ["nasadem"], "bbox": [-80.0, 34.0, -79.0, 35.0]},
    )

    (entry,) = payload["collections"]
    assert entry["aoi_coverage_pct"] is None
    assert entry["error"] and "collection lookup failed" in entry["error"]
    assert entry["n_items_sampled"] == 1  # search path independent of collection lookup


# --- write_data_manifest -------------------------------------------------------------


def _entry(factor, dataset_id, *, access="stac", selected=True, **extra):
    return {
        "factor": factor,
        "dataset_id": dataset_id,
        "access": access,
        "selected": selected,
        **extra,
    }


def test_write_manifest_happy_path_round_trips(redirect_manifest):
    aoi = {"bbox": [-84.32, 33.84, -75.46, 36.59], "crs": "EPSG:4326"}
    entries = [
        _entry("terrain", "cop-dem-glo-30", rationale="global 30m fallback"),
        _entry("surface", "3dep-lidar-dsm", gsd_m=1.0),
        _entry("buildings", "ms-buildings", license="ODbL"),
        _entry(
            "canopy",
            "meta-canopy-1m",
            access="web",
            source_url="https://registry.opendata.aws/dataforgood-fb-forests/",
        ),
        # a rejected alternative (selected=False) should be persisted but not counted
        _entry("terrain", "nasadem", selected=False, rationale="coarser alt"),
        # an unknown key must be filtered out (not a DatasetCandidate field), no crash
        _entry("buildings", "osm-buildings", selected=False, bogus_key="drop me"),
    ]

    envelope, payload = run_tool(
        tools.write_data_manifest,
        {"aoi": aoi, "entries": entries, "notes": ["verify canopy licence"]},
    )

    assert not envelope.get("is_error")
    assert payload["factors_covered"] == ["buildings", "canopy", "surface", "terrain"]
    assert payload["factors_missing"] == []
    assert payload["n_entries"] == 6

    # File written to the redirected path and faithfully reloadable into the dataclasses.
    assert redirect_manifest.exists()
    import json

    raw = json.loads(redirect_manifest.read_text())
    manifest = DataManifest(
        aoi_bbox=raw["aoi_bbox"],
        generated_at=raw["generated_at"],
        entries=[DatasetCandidate(**e) for e in raw["entries"]],
        notes=raw["notes"],
    )
    assert manifest.aoi_bbox == aoi["bbox"]
    assert len(manifest.entries) == 6
    assert manifest.notes == ["verify canopy licence"]
    assert all(not hasattr(c, "bogus_key") for c in manifest.entries)


def test_write_manifest_reports_missing_factors(redirect_manifest):
    aoi = {"bbox": [-84.32, 33.84, -75.46, 36.59]}
    entries = [_entry("terrain", "cop-dem-glo-30"), _entry("surface", "3dep-lidar-dsm")]

    _, payload = run_tool(tools.write_data_manifest, {"aoi": aoi, "entries": entries})

    assert payload["factors_covered"] == ["surface", "terrain"]
    assert sorted(payload["factors_missing"]) == ["buildings", "canopy"]


@pytest.mark.parametrize(
    "entries, needle",
    [
        ([], "non-empty list"),
        ("not a list", "non-empty list"),
        (["not-a-dict"], "each entry must be an object"),
        ([{"factor": "terrain", "dataset_id": "x"}], "missing required keys"),
    ],
)
def test_write_manifest_validation_errors(redirect_manifest, entries, needle):
    envelope, payload = run_tool(
        tools.write_data_manifest, {"aoi": {"bbox": []}, "entries": entries}
    )
    assert envelope.get("is_error")
    assert needle in payload
    assert not redirect_manifest.exists(), "no file should be written on a bad request"


# --- opendata_registry_search --------------------------------------------------------


def test_registry_search_finds_meta_canopy_offline(monkeypatch):
    """Keyword search returns the grounded Meta canopy record from the curated snapshot,
    deterministically and without any network (live verify stubbed to 'offline')."""
    monkeypatch.setattr(tools, "_fetch_registry_record", lambda rid, timeout=8.0: None)

    envelope, payload = run_tool(
        tools.opendata_registry_search,
        {"keywords": ["canopy", "height"], "factor": "canopy"},
    )

    assert not envelope.get("is_error")
    assert payload["n_results"] >= 1
    top = payload["results"][0]
    assert "Meta" in top["name"]
    assert top["registry_id"] == "dataforgood-fb-forests"
    assert top["s3_uri"].startswith("s3://dataforgood-fb-data/forests/")
    assert top["license"] == "CC-BY-4.0"
    assert top["gsd_m"] == 1.0
    assert top["verified"] is False  # network stubbed out


def test_registry_search_marks_verified_when_live_resolves(monkeypatch):
    """When the live registry fetch resolves, the hit is flagged verified and the licence
    is refreshed from the source of truth."""
    monkeypatch.setattr(
        tools,
        "_fetch_registry_record",
        lambda rid, timeout=8.0: {
            "name": "High Resolution Canopy Height Maps by WRI and Meta",
            "license": "https://creativecommons.org/licenses/by/4.0/",
            "managed_by": "Meta",
            "resolved": True,
        },
    )

    _, payload = run_tool(
        tools.opendata_registry_search, {"keywords": ["canopy"], "factor": "canopy"}
    )
    top = payload["results"][0]
    assert top["verified"] is True
    assert top["managed_by"] == "Meta"
    assert "creativecommons.org/licenses/by/4.0" in top["license"]


def test_registry_search_no_keyword_match_returns_empty():
    _, payload = run_tool(
        tools.opendata_registry_search, {"keywords": ["definitely-not-a-dataset"]}
    )
    assert payload["n_results"] == 0
    assert payload["results"] == []


# --- write_data_manifest grounding gate ----------------------------------------------


def test_write_manifest_deselects_ungrounded_web_pick(redirect_manifest):
    """A selected access='web' entry with no source_url/asset_href is de-selected and the
    reason is surfaced in notes, so ungrounded recall can't masquerade as a real pick."""
    aoi = {"bbox": [-84.32, 33.84, -75.46, 36.59]}
    entries = [
        _entry("terrain", "cop-dem-glo-30"),
        _entry("surface", "3dep-lidar-dsm"),
        _entry("canopy", "meta-canopy-1m", access="web"),  # no source_url -> ungrounded
    ]

    _, payload = run_tool(tools.write_data_manifest, {"aoi": aoi, "entries": entries})

    assert "canopy" not in payload["factors_covered"]
    assert "canopy" in payload["factors_missing"]

    import json

    raw = json.loads(redirect_manifest.read_text())
    canopy = next(e for e in raw["entries"] if e["factor"] == "canopy")
    assert canopy["selected"] is False
    assert any("ungrounded web pick" in n for n in raw["notes"])


def test_write_manifest_keeps_grounded_web_pick(redirect_manifest):
    """A web pick carrying a source_url is grounded and stays selected."""
    aoi = {"bbox": [-84.32, 33.84, -75.46, 36.59]}
    entries = [
        _entry(
            "canopy",
            "meta-canopy-1m",
            access="web",
            source_url="s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/",
        )
    ]

    _, payload = run_tool(tools.write_data_manifest, {"aoi": aoi, "entries": entries})
    assert "canopy" in payload["factors_covered"]
