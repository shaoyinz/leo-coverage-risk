"""Deterministic unit tests for the A2 surface-ingestion tools + tiling.

No network and no API key: the windowed COG read (``_read_window_reprojected``) and
Planetary Computer signing (``_sign_href``) are monkeypatched, and the surface cache is
redirected under tmp, so these run fast and offline. One opt-in live test (gated on
``LEO_RUN_LIVE=1``) reads a real DEM window. The tools under test live in
``leo_pipeline.tools`` and back the INGESTION_AGENT (A2) allow-list.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import numpy as np
import pytest

import leo_pipeline.tiling as tiling
import leo_pipeline.tools as tools

from conftest import run_tool


# --- tiling --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lon, lat, epsg",
    [
        (-80.5, 35.5, 32617),  # Carolinas -> UTM 17N
        (-75.5, 36.0, 32618),  # eastern edge -> UTM 18N
        (-122.4, 37.8, 32610),  # San Francisco -> UTM 10N
        (-58.4, -34.6, 32721),  # Buenos Aires -> UTM 21S
    ],
)
def test_utm_epsg_for_lonlat(lon, lat, epsg):
    assert tiling.utm_epsg_for_lonlat(lon, lat) == epsg


def test_assign_tiles_bins_and_maps(tmp_path):
    """A small synthetic work list bins onto a UTM grid; the coord map covers every point
    and the tile bbox carries the requested buffer."""
    import pandas as pd

    # Three points: two within one 1 km tile, one far enough to land in another.
    df = pd.DataFrame(
        {
            "coord_id": [0, 1, 2],
            "latitude": [35.5000, 35.5001, 35.5200],
            "longitude": [-80.5000, -80.4999, -80.5000],
        }
    )
    src = tmp_path / "unique_coords.parquet"
    df.to_parquet(src, index=False)

    result = tiling.assign_tiles(
        src, tile_size_m=1000.0, buffer_m=100.0, out_dir=tmp_path
    )

    assert result.utm_epsg == 32617
    assert result.n_coords == 3
    assert result.n_tiles == 2  # two near points share a tile, the third is its own

    coord_map = pd.read_parquet(result.coord_map_path)
    assert set(coord_map["coord_id"]) == {0, 1, 2}
    assert coord_map["tile_id"].str.startswith("32617_").all()
    assert coord_map.loc[coord_map.coord_id == 0, "tile_id"].iloc[0] == (
        coord_map.loc[coord_map.coord_id == 1, "tile_id"].iloc[0]
    )

    tiles = pd.read_parquet(result.tiles_path)
    assert len(tiles) == 2
    assert tiles["n_coords"].sum() == 3
    # buffered 1 km tile spans a touch over 1 km -> > 0.009 deg lat
    assert (tiles["max_lat"] - tiles["min_lat"]).min() > 0.009


def test_assign_tiles_missing_worklist_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        tiling.assign_tiles(tmp_path / "nope.parquet", out_dir=tmp_path)


# --- pure helpers --------------------------------------------------------------------


def test_fuse_pseudo_dsm_adds_canopy_respecting_nodata():
    dem = np.array([[10.0, 20.0], [30.0, -9999.0]], dtype="float32")
    canopy = np.array([[5.0, -9999.0], [2.0, 1.0]], dtype="float32")
    fused = tools._fuse_pseudo_dsm(dem, canopy, -9999.0, -9999.0)
    # 10+5 ; 20+0 (canopy nodata->0) ; 30+2 ; dem nodata stays nodata
    assert fused.tolist() == [[15.0, 20.0], [32.0, -9999.0]]


def test_fuse_pseudo_dsm_clamps_negative_canopy():
    dem = np.array([[100.0]], dtype="float32")
    canopy = np.array([[-3.0]], dtype="float32")  # nonsense negative height -> clamp to 0
    assert tools._fuse_pseudo_dsm(dem, canopy, -9999.0, -9999.0).tolist() == [[100.0]]


def test_cache_key_stable_ignores_sas_token_but_tracks_inputs():
    base = ("32617_1_2", [-80, 35, -79, 36], {"terrain": "http://d?sas=AAA"})
    k1 = tools._cache_key(*base, "EPSG:32617", 10.0, "cover_proxy")
    k2 = tools._cache_key(
        "32617_1_2", [-80, 35, -79, 36], {"terrain": "http://d?sas=ZZZ"},
        "EPSG:32617", 10.0, "cover_proxy",
    )
    assert k1 == k2  # SAS token stripped -> stable
    # a different gsd is a different surface -> different key
    k3 = tools._cache_key(*base, "EPSG:32617", 5.0, "cover_proxy")
    assert k3 != k1


def test_resolve_target_crs_variants():
    assert tools._resolve_target_crs("auto-UTM", [-80, 35, -79, 36]) == "EPSG:32617"
    assert tools._resolve_target_crs(None, [-80, 35, -79, 36]) == "EPSG:32617"
    assert tools._resolve_target_crs("EPSG:5070", [-80, 35, -79, 36]) == "EPSG:5070"
    assert tools._resolve_target_crs(4326, [-80, 35, -79, 36]) == "EPSG:4326"


def test_manifest_hrefs_normalises_aliases_and_dicts():
    out = tools._manifest_hrefs(
        {"dsm": "http://s", "dem": "http://d", "canopy": {"asset_href": "http://c", "vintage": "2020"}}
    )
    assert out["surface"]["href"] == "http://s"
    assert out["terrain"]["href"] == "http://d"
    assert out["canopy"] == {"href": "http://c", "vintage": "2020"}


def test_next_surface_mode_walks_hierarchy():
    assert tools._next_surface_mode("true_dsm") == "pseudo_dsm"
    assert tools._next_surface_mode("pseudo_dsm") == "cover_proxy"
    assert tools._next_surface_mode("cover_proxy") is None


def test_coverage_and_confidence_downgrades_on_gaps():
    assert tools._coverage_and_confidence("true_dsm", 1.0) == ("ok", "high")
    assert tools._coverage_and_confidence("true_dsm", 0.5) == ("partial", "medium")
    assert tools._coverage_and_confidence("pseudo_dsm", 0.0) == ("empty", "low")
    assert tools._coverage_and_confidence("cover_proxy", 1.0) == ("cover_proxy", "low")


# --- fetch_aligned_surface -----------------------------------------------------------


@pytest.fixture
def patch_reads(monkeypatch, make_read):
    """Stub signing + the COG read; DEM reads return 100, canopy returns 8, DSM returns 50.
    Returns a call-counter dict so idempotency can be asserted."""
    calls = {"n": 0, "hrefs": []}

    def _fake_read(href, bbox, crs, gsd):
        calls["n"] += 1
        calls["hrefs"].append(href)
        if "dem" in href or "terrain" in href:
            return make_read(100.0)
        if "canopy" in href or "can" in href:
            return make_read(8.0)
        return make_read(50.0)  # dsm

    monkeypatch.setattr(tools, "_sign_href", lambda h: h)
    monkeypatch.setattr(tools, "_read_window_reprojected", _fake_read)
    return calls


def _fetch(args):
    return run_tool(tools.fetch_aligned_surface, args)


def test_fetch_pseudo_dsm_fuses_and_writes(redirect_ingestion, patch_reads):
    env, payload = _fetch(
        {
            "tile_id": "T1",
            "bbox": [-80, 35, -79, 36],
            "manifest": {"dem": "http://dem", "canopy": "http://canopy"},
            "surface_mode": "pseudo_dsm",
        }
    )
    assert not env.get("is_error")
    assert payload["surface_mode"] == "pseudo_dsm"
    assert payload["confidence"] == "medium"
    assert payload["coverage_flag"] == "ok"
    assert payload["dem_uri"] is not None
    assert payload["crs"] == "EPSG:32617"
    assert patch_reads["n"] == 2  # one DEM read + one canopy read

    import rasterio

    with rasterio.open(payload["dsm_uri"]) as ds:
        assert float(ds.read(1).mean()) == pytest.approx(108.0)  # 100 DEM + 8 canopy


def test_fetch_idempotent_second_call_hits_cache(redirect_ingestion, patch_reads):
    args = {
        "tile_id": "T1",
        "bbox": [-80, 35, -79, 36],
        "manifest": {"dem": "http://dem", "canopy": "http://canopy"},
        "surface_mode": "pseudo_dsm",
    }
    _, first = _fetch(args)
    assert first["cache_hit"] is False
    n_after_first = patch_reads["n"]

    _, second = _fetch(args)
    assert second["cache_hit"] is True
    assert patch_reads["n"] == n_after_first  # no new reads on a cache hit


def test_fetch_auto_picks_true_dsm_when_available(redirect_ingestion, patch_reads):
    _, payload = _fetch(
        {"tile_id": "T2", "bbox": [-80, 35, -79, 36], "manifest": {"surface": "http://dsm"}}
    )
    assert payload["surface_mode"] == "true_dsm"
    assert payload["confidence"] == "high"
    assert payload["dem_uri"] is None


def test_fetch_cover_proxy_is_dem_only_low_confidence(redirect_ingestion, patch_reads):
    _, payload = _fetch(
        {
            "tile_id": "T3",
            "bbox": [-80, 35, -79, 36],
            "manifest": {"dem": "http://dem"},
            "surface_mode": "cover_proxy",
        }
    )
    assert payload["surface_mode"] == "cover_proxy"
    assert payload["coverage_flag"] == "cover_proxy"
    assert payload["confidence"] == "low"


def test_fetch_read_failure_suggests_next_fallback(redirect_ingestion, monkeypatch):
    monkeypatch.setattr(tools, "_sign_href", lambda h: h)

    def _boom(*a, **k):
        raise RuntimeError("HTTP 404: tile not found")

    monkeypatch.setattr(tools, "_read_window_reprojected", _boom)

    env, text = _fetch(
        {
            "tile_id": "T4",
            "bbox": [-80, 35, -79, 36],
            "manifest": {"surface": "http://dsm"},
            "surface_mode": "true_dsm",
        }
    )
    assert env.get("is_error")
    assert "read/reproject failed" in text
    assert "pseudo_dsm" in text  # names the next mode down the hierarchy


@pytest.mark.parametrize(
    "args, needle",
    [
        ({"tile_id": "", "bbox": [-80, 35, -79, 36], "manifest": {"dem": "x"}}, "tile_id is required"),
        ({"tile_id": "T", "bbox": [1, 2, 3], "manifest": {"dem": "x"}}, "bbox must be"),
        ({"tile_id": "T", "bbox": [-79, 35, -80, 36], "manifest": {"dem": "x"}}, "min_lon<max_lon"),
        ({"tile_id": "T", "bbox": [-80, 35, -79, 36], "manifest": {}}, "non-empty"),
        (
            {"tile_id": "T", "bbox": [-80, 35, -79, 36], "manifest": {"dem": "x"}, "surface_mode": "bogus"},
            "surface_mode must be one of",
        ),
        (
            {"tile_id": "T", "bbox": [-80, 35, -79, 36], "manifest": {"dem": "x"}, "surface_mode": "pseudo_dsm"},
            "needs manifest factor(s) ['canopy']",
        ),
    ],
)
def test_fetch_validation_errors(redirect_ingestion, args, needle):
    env, text = _fetch(args)
    assert env.get("is_error")
    assert needle in text


# --- stac_item_read ------------------------------------------------------------------


class _FakeSearch:
    def __init__(self, items):
        self._items = items

    def items(self):
        return list(self._items)


def _fake_stac_client(items):
    class _FakeClient:
        @staticmethod
        def open(url):
            return _FakeClient()

        def search(self, **kwargs):
            return _FakeSearch(items)

    return _FakeClient


def test_stac_item_read_picks_finest_and_signs(monkeypatch, make_stac_item):
    items = [
        make_stac_item("coarse", {"gsd": 30.0, "datetime": "2020"}, {"data": "http://coarse"}),
        make_stac_item("fine", {"gsd": 1.0, "datetime": "2023"}, {"data": "http://fine"}),
    ]
    monkeypatch.setattr("pystac_client.Client", _fake_stac_client(items))
    monkeypatch.setattr(tools, "_sign_href", lambda h: f"{h}?sig=TOKEN")

    env, payload = run_tool(
        tools.stac_item_read,
        {"collection": "3dep-lidar-dsm", "bbox": [-80, 35, -79, 36]},
    )
    assert not env.get("is_error")
    assert payload["item_id"] == "fine"  # smallest gsd wins
    assert payload["asset_href"] == "http://fine?sig=TOKEN"
    assert payload["gsd_m"] == 1.0
    assert payload["n_candidates"] == 2
    assert payload["signed"] is True


def test_stac_item_read_no_items(monkeypatch):
    monkeypatch.setattr("pystac_client.Client", _fake_stac_client([]))
    _, payload = run_tool(
        tools.stac_item_read, {"collection": "nasadem", "bbox": [-80, 35, -79, 36]}
    )
    assert payload["asset_href"] is None
    assert payload["n_candidates"] == 0


def test_stac_item_read_bad_bbox():
    env, text = run_tool(tools.stac_item_read, {"collection": "x", "bbox": [1, 2, 3]})
    assert env.get("is_error")
    assert "bbox must be" in text


# --- cache_rw ------------------------------------------------------------------------


def test_cache_rw_check_and_list(redirect_ingestion):
    redirect_ingestion.mkdir(parents=True)
    (redirect_ingestion / "T9__abc.json").write_text(
        json.dumps({"tile_id": "T9", "surface_mode": "pseudo_dsm", "dsm_uri": "x"})
    )

    env, payload = run_tool(tools.cache_rw, {"op": "check", "tile_id": "T9"})
    assert not env.get("is_error")
    assert payload["n_found"] == 1
    assert payload["surfaces"][0]["tile_id"] == "T9"

    _, miss = run_tool(tools.cache_rw, {"op": "check", "tile_id": "T_absent"})
    assert miss["n_found"] == 0

    _, listing = run_tool(tools.cache_rw, {"op": "list"})
    assert listing["n_found"] == 1


def test_cache_rw_validates_op(redirect_ingestion):
    env, text = run_tool(tools.cache_rw, {"op": "delete"})
    assert env.get("is_error")
    assert "op must be" in text


# --- opt-in live read ----------------------------------------------------------------

RUN_LIVE = os.getenv("LEO_RUN_LIVE") == "1"


@pytest.mark.live
@pytest.mark.skipif(not RUN_LIVE, reason="set LEO_RUN_LIVE=1 for the live COG read")
def test_live_fetch_reads_real_dem_window(redirect_ingestion):
    """Read a real Copernicus DEM window over a tiny AOI and write a cover_proxy surface.

    Resolves a concrete DEM asset for the tile via stac_item_read (the real A2 path), then
    fetches/reprojects it — exercising the live rasterio read + reproject + write end to end.
    """
    bbox = [-80.02, 35.50, -79.98, 35.54]
    _, item = run_tool(tools.stac_item_read, {"collection": "cop-dem-glo-30", "bbox": bbox})
    assert item["asset_href"], "no DEM asset resolved for the live AOI"

    env, payload = _fetch(
        {
            "tile_id": "live_dem",
            "bbox": bbox,
            "manifest": {"dem": item["asset_href"]},
            "surface_mode": "cover_proxy",
        }
    )
    assert not env.get("is_error"), payload
    assert payload["crs"].startswith("EPSG:326")
    assert payload["gsd_m"] == 10.0

    import rasterio

    with rasterio.open(payload["dsm_uri"]) as ds:
        assert ds.crs.to_epsg() // 100 == 326  # a UTM-north zone
        assert ds.read(1).size > 0
