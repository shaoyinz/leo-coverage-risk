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


def _read(arr, nodata=-9999.0):
    return {"array": np.asarray(arr, dtype="float32"), "nodata": nodata}


def _win(value, *, shape=(4, 4), nodata=-9999.0, holes=()):
    """A synthetic ``_read_window_reprojected`` result (constant array on a valid transform)."""
    from rasterio.transform import from_bounds

    arr = np.full(shape, float(value), dtype="float32")
    for (r, c) in holes:
        arr[r, c] = nodata
    return {
        "array": arr,
        "transform": from_bounds(0, 0, shape[1] * 10, shape[0] * 10, shape[1], shape[0]),
        "crs": "EPSG:32617",
        "nodata": nodata,
        "gsd_m": 10.0,
        "shape": list(shape),
    }


def test_composite_surface_lidar_over_dem_fills_holes():
    """Lidar where present overrides; DEM fills the lidar holes; canopy augments DEM-only
    pixels. Provenance codes: 3 lidar, 2 dem+canopy, 1 bare dem, 0 nodata."""
    nd = -9999.0
    dem = _read([[100.0, 100.0], [100.0, 100.0]])  # complete
    lidar = _read([[150.0, nd], [nd, nd]])  # only the top-left pixel
    canopy = _read([[5.0, 8.0], [nd, 3.0]])  # top-right + bottom-right have canopy
    elev, prov = tools._composite_surface(
        {"terrain": dem, "surface": lidar, "canopy": canopy}, nd
    )
    # top-left: lidar wins (150, code 3); top-right: dem+canopy (108, code 2);
    # bottom-left: bare dem (100, code 1); bottom-right: dem+canopy (103, code 2)
    assert elev.tolist() == [[150.0, 108.0], [100.0, 103.0]]
    assert prov.tolist() == [[3, 2], [1, 2]]


def test_composite_surface_dem_only_is_full_coverage():
    nd = -9999.0
    elev, prov = tools._composite_surface({"terrain": _read([[10.0, nd]])}, nd)
    assert elev.tolist() == [[10.0, nd]]
    assert prov.tolist() == [[1, 0]]  # dem_fill where valid, nodata where the dem had a hole


def test_fetch_mosaic_fills_partial_lidar_to_full_coverage(redirect_ingestion, monkeypatch):
    """A *partial* lidar DSM (most pixels nodata) + a complete DEM composites to ~full
    coverage — the fix for spurious 'undetermined' — with a lidar/dem provenance mix."""
    nd = -9999.0

    def _fake_read(href, bbox, crs, gsd):
        if "dem" in href or "terrain" in href:
            return _win(100.0)  # complete DEM
        # lidar: a 4x4 with only the top-left 2x2 valid (rest nodata)
        holes = [(r, c) for r in range(4) for c in range(4) if not (r < 2 and c < 2)]
        return _win(150.0, holes=holes)

    monkeypatch.setattr(tools, "_sign_href", lambda h: h)
    monkeypatch.setattr(tools, "_read_window_reprojected", _fake_read)

    env, payload = _fetch(
        {
            "tile_id": "M1",
            "bbox": [-80, 35, -79, 36],
            "manifest": {"surface": "http://dsm", "dem": "http://dem"},
        }
    )
    assert not env.get("is_error")
    assert payload["surface_mode"] == "mosaic"
    assert payload["coverage_flag"] == "ok"
    assert payload["valid_fraction"] == pytest.approx(1.0)  # DEM filled every lidar hole
    fr = payload["provenance_fractions"]
    assert fr["lidar"] == pytest.approx(0.25)  # 4 of 16 pixels are lidar
    assert fr["dem_fill"] == pytest.approx(0.75)


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
    # A single href, a granule-mosaic list, and a {asset_href, vintage} dict all normalise to
    # a ``hrefs`` list (one entry per granule) so fetch_aligned_surface can mosaic them.
    out = tools._manifest_hrefs(
        {
            "dsm": "http://s",
            "dem": ["http://d1", "http://d2"],
            "canopy": {"asset_href": "http://c", "vintage": "2020"},
        }
    )
    assert out["surface"]["hrefs"] == ["http://s"]
    assert out["terrain"]["hrefs"] == ["http://d1", "http://d2"]
    assert out["canopy"] == {"hrefs": ["http://c"], "vintage": "2020"}


def test_read_window_mosaic_coalesces_granules(monkeypatch):
    """Two partial granules (each covering a different half of the tile) coalesce to full
    coverage — the granule-boundary fix. A one-href list is an unchanged passthrough."""
    nd = -9999.0
    # granule A covers the left column, granule B the right column; together they're complete.
    gran = {
        "http://A": _win(10.0, holes=[(r, c) for r in range(4) for c in range(4) if c >= 2]),
        "http://B": _win(20.0, holes=[(r, c) for r in range(4) for c in range(4) if c < 2]),
    }
    monkeypatch.setattr(tools, "_read_window_reprojected", lambda h, *a: gran[h])

    merged = tools._read_window_mosaic(["http://A", "http://B"], [-80, 35, -79, 36], "EPSG:32617", 10.0)
    arr = merged["array"]
    assert not (arr == nd).any()  # every hole filled
    assert arr[0, 0] == 10.0 and arr[0, 3] == 20.0  # left from A, right from B (first-valid-wins)

    passthrough = tools._read_window_mosaic(["http://A"], [-80, 35, -79, 36], "EPSG:32617", 10.0)
    assert passthrough is gran["http://A"]


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
    assert payload["dem_uri"] is None  # no separate DEM file — provenance is band 2 now
    assert payload["provenance_band"] == 2
    assert payload["crs"] == "EPSG:32617"
    assert patch_reads["n"] == 2  # one DEM read + one canopy read

    import rasterio

    with rasterio.open(payload["dsm_uri"]) as ds:
        assert ds.count == 2  # band 1 elevation + band 2 provenance
        assert float(ds.read(1).mean()) == pytest.approx(108.0)  # 100 DEM + 8 canopy
        assert set(np.unique(ds.read(2).astype(int))) == {2}  # all DEM+canopy provenance
    assert payload["provenance_fractions"]["pseudo"] == pytest.approx(1.0)


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
            "needs a canopy and/or buildings layer",
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


# --- building-height fusion (OpenBuildingMap) ----------------------------------------


@pytest.mark.parametrize(
    "raw, metres",
    [
        ("HHT:15.00", 15.0),          # explicit metres
        ("H:2", 6.0),                 # 2 storeys * 3 m
        ("HAPP:3", 9.0),              # approximate storeys
        ("HBET:1-5", 9.0),            # storey range -> midpoint 3 storeys * 3 m
        ("HBET:1-3", 6.0),
        ("H:1+HHT:3.50", 3.5),        # compound -> explicit metres win
        ("garbage", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_gem_height_variants(raw, metres):
    got = tools._parse_gem_height(raw)
    if metres is None:
        assert got is None
    else:
        assert got == pytest.approx(metres)


def test_bbox_quadkeys_charlotte_single_tile():
    # Charlotte uptown sits inside one level-6 quadkey tile (matches the live OBM file name).
    assert tools._bbox_quadkeys([-80.86, 35.21, -80.82, 35.25], 6) == ["032003"]
    assert tools._tile_to_quadkey(*tools._lonlat_to_tile(-80.84, 35.23, 6), 6) == "032003"


def test_building_source_urls_templates_quadkey():
    urls = tools._building_source_urls([-80.86, 35.21, -80.82, 35.25])
    assert urls == [
        "https://data.source.coop/tge-labs/openbuildingmap/building.032003.parquet"
    ]


def test_building_source_urls_honours_manifest_template_and_zoom():
    # A manifest-supplied template + zoom drive the URL, not the config default.
    urls = tools._building_source_urls(
        [-80.86, 35.21, -80.82, 35.25],
        url_template="https://example.org/obm/{quadkey}.parquet",
        zoom=6,
    )
    assert urls == ["https://example.org/obm/032003.parquet"]


def test_building_access_prefers_manifest_then_config():
    # An entry's access fields win; omitted ones fall back to the INGESTION config defaults.
    acc = tools._building_access(
        {"url_template": "https://x/{quadkey}.parquet", "quadkey_zoom": 7, "height_col": "h_m"}
    )
    assert acc["url_template"] == "https://x/{quadkey}.parquet"
    assert acc["quadkey_zoom"] == 7
    assert acc["height_col"] == "h_m"
    assert acc["geom_col"] == tools.INGESTION.building_geom_col  # default

    # A template smuggled in as an href (offline / back-compat path) is recognised.
    via_href = tools._building_access({"hrefs": ["https://y/{quadkey}.parquet"]})
    assert via_href["url_template"] == "https://y/{quadkey}.parquet"
    assert via_href["quadkey_zoom"] == tools.INGESTION.building_quadkey_zoom

    # Empty entry -> all config defaults; idempotent on an already-resolved dict.
    base = tools._building_access(None)
    assert base["url_template"] == tools.INGESTION.building_source_url
    assert tools._building_access(base) == base


def test_manifest_hrefs_carries_building_access_and_marks_presence():
    # buildings entry with only access metadata (no plain href) still registers as present,
    # and the access keys ride through for fetch_aligned_surface to resolve.
    out = tools._manifest_hrefs(
        {"buildings": {"url_template": "https://x/{quadkey}.parquet", "quadkey_zoom": 7}}
    )
    assert out["buildings"]["url_template"] == "https://x/{quadkey}.parquet"
    assert out["buildings"]["quadkey_zoom"] == 7
    assert out["buildings"]["hrefs"] == []  # presence is the on-switch, no COG href needed


def test_rasterize_building_window_burns_max_height_on_dst_grid(monkeypatch):
    """OBM footprints rasterize onto the exact ``_dst_grid`` grid, tallest-wins on overlap,
    nodata where no building — the read itself is monkeypatched (no network)."""
    from shapely.geometry import box as shp_box

    bbox = [-80.84, 35.23, -80.83, 35.24]
    minx, miny, maxx, maxy = bbox
    short = shp_box(minx, miny, minx + 0.006, maxy).wkb       # left two-thirds, 5 m
    tall = shp_box(minx, miny, minx + 0.003, maxy).wkb        # left third (overlaps), 30 m
    monkeypatch.setattr(
        tools, "_read_obm_features",
        lambda urls, bb, **kw: [(short, "HHT:5.00"), (tall, "HHT:30.00")],
    )

    out = tools._rasterize_building_window(bbox, "EPSG:32617", 10.0, urls=["x"])
    tr, w, h, _ = tools._dst_grid(bbox, "EPSG:32617", 10.0)
    assert out["shape"] == [h, w]
    assert out["transform"] == tr  # bit-identical grid -> fuses with the COG reads

    arr = out["array"]
    assert np.isclose(arr, 30.0).any()                       # overlap -> tallest wins
    assert np.isclose(arr, 5.0).any()                        # short-only middle band
    assert (arr == out["nodata"]).any()                      # right third has no building


def test_composite_surface_fuses_buildings_max_and_provenance_4():
    """Modelled fill is DEM + max(canopy, building); a building-set pixel is code 4, a
    canopy-set pixel code 2."""
    nd = -9999.0
    dem = _read([[100.0, 100.0], [100.0, 100.0]])
    canopy = _read([[5.0, nd], [8.0, 2.0]])
    bld = _read([[20.0, 15.0], [nd, 1.0]])
    elev, prov = tools._composite_surface(
        {"terrain": dem, "canopy": canopy, "buildings": bld}, nd
    )
    # (0,0) max(5,20)=20 ->120 building; (0,1) canopy nodata, bld15 ->115 building
    # (1,0) bld nodata, canopy8 ->108 canopy;  (1,1) max(2,1)=2 ->102 canopy
    assert elev.tolist() == [[120.0, 115.0], [108.0, 102.0]]
    assert prov.tolist() == [[4, 4], [2, 2]]


def _fake_bld_layer(value=30.0, shape=(4, 4), nd=-9999.0):
    from rasterio.transform import from_bounds

    return lambda bbox, crs, gsd, urls=None, access=None: {
        "array": np.full(shape, float(value), dtype="float32"),
        "transform": from_bounds(0, 0, shape[1] * 10, shape[0] * 10, shape[1], shape[0]),
        "crs": "EPSG:32617",
        "nodata": nd,
        "gsd_m": 10.0,
        "shape": list(shape),
    }


def test_fetch_mosaic_fuses_buildings_into_dem_fill(redirect_ingestion, monkeypatch):
    """Buildings fuse into the DEM-fill (non-lidar) region of a mosaic: lidar still wins where
    present, the rest is DEM + building (code 4), and the building share is reported."""
    nd = -9999.0

    def _fake_read(href, bbox, crs, gsd):
        if "dem" in href or "terrain" in href:
            return _win(100.0)
        holes = [(r, c) for r in range(4) for c in range(4) if not (r < 2 and c < 2)]
        return _win(150.0, holes=holes)  # lidar only top-left 2x2

    monkeypatch.setattr(tools, "_sign_href", lambda h: h)
    monkeypatch.setattr(tools, "_read_window_reprojected", _fake_read)
    monkeypatch.setattr(tools, "_rasterize_building_window", _fake_bld_layer(30.0))

    env, payload = _fetch(
        {
            "tile_id": "MB",
            "bbox": [-80, 35, -79, 36],
            "manifest": {"surface": "http://dsm", "dem": "http://dem", "buildings": "obm://x"},
        }
    )
    assert not env.get("is_error")
    assert payload["surface_mode"] == "mosaic"
    fr = payload["provenance_fractions"]
    assert fr["lidar"] == pytest.approx(0.25)       # top-left 2x2 still lidar
    assert fr["building"] == pytest.approx(0.75)    # the DEM-fill region now carries buildings

    import rasterio

    with rasterio.open(payload["dsm_uri"]) as ds:
        elev = ds.read(1)
        prov = ds.read(2).astype(int)
    assert np.isclose(elev[0, 0], 150.0)            # lidar wins where present
    assert np.isclose(elev[2, 2], 130.0)            # DEM 100 + building 30
    assert prov[2, 2] == 4


def test_fetch_building_read_failure_degrades_gracefully(
    redirect_ingestion, patch_reads, monkeypatch
):
    """A failed OBM read drops the building term but keeps the tile (buildings are opportunistic)."""
    def _boom(*a, **k):
        raise RuntimeError("obm unreachable")

    monkeypatch.setattr(tools, "_rasterize_building_window", _boom)
    env, payload = _fetch(
        {
            "tile_id": "BD",
            "bbox": [-80, 35, -79, 36],
            "manifest": {
                "dem": "http://dem", "canopy": "http://canopy", "buildings": "obm://x"
            },
            "surface_mode": "pseudo_dsm",
        }
    )
    assert not env.get("is_error")                  # tile survives the building failure
    assert payload["provenance_fractions"]["building"] == pytest.approx(0.0)
    assert payload["provenance_fractions"]["pseudo"] == pytest.approx(1.0)


def test_fetch_uses_manifest_building_template(redirect_ingestion, monkeypatch):
    """The buildings source is manifest-driven: a manifest url_template (not config) is what
    A2 resolves the OBM quadkey URLs from, and it carries into the rasterize call + cache key."""
    captured: dict = {}

    def _capture_bld(bbox, crs, gsd, urls=None, access=None):
        captured["urls"] = urls
        captured["access"] = access
        return _fake_bld_layer(30.0)(bbox, crs, gsd, urls=urls, access=access)

    monkeypatch.setattr(tools, "_sign_href", lambda h: h)
    monkeypatch.setattr(tools, "_read_window_reprojected", lambda href, *a: _win(100.0))
    monkeypatch.setattr(tools, "_rasterize_building_window", _capture_bld)

    env, payload = _fetch(
        {
            "tile_id": "MT",
            "bbox": [-80.86, 35.21, -80.82, 35.25],
            "manifest": {
                "dem": "http://dem",
                "buildings": {
                    "url_template": "https://example.org/obm/{quadkey}.parquet",
                    "quadkey_zoom": 6,
                    "vintage": "v9",
                },
            },
            "surface_mode": "pseudo_dsm",
        }
    )
    assert not env.get("is_error")
    assert captured["urls"] == ["https://example.org/obm/032003.parquet"]
    assert captured["access"]["url_template"] == "https://example.org/obm/{quadkey}.parquet"
    assert payload["vintage_map"]["buildings"] == "v9"  # manifest vintage flows through


def test_cache_key_tracks_building_inputs():
    base = ("32617_1_2", [-80, 35, -79, 36])
    no_b = tools._cache_key(
        *base, {"terrain": "http://d", "buildings": None}, "EPSG:32617", 10.0, "mosaic"
    )
    with_b = tools._cache_key(
        *base,
        {"terrain": "http://d", "buildings": ["obm://building.032003.parquet"]},
        "EPSG:32617", 10.0, "mosaic",
    )
    assert no_b != with_b  # building inputs change the surface -> change the cache key


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


@pytest.mark.live
@pytest.mark.skipif(not RUN_LIVE, reason="set LEO_RUN_LIVE=1 for the live OBM read")
def test_live_rasterize_building_window_charlotte():
    """Read real OpenBuildingMap footprints over an uptown-Charlotte box and rasterize heights
    onto the tile grid (exercises the live DuckDB HTTPS GeoParquet read end to end)."""
    bbox = [-80.86, 35.21, -80.82, 35.25]
    out = tools._rasterize_building_window(bbox, "EPSG:32617", 10.0)
    arr = out["array"]
    built = arr[arr != out["nodata"]]
    assert built.size > 0, "no OBM buildings rasterized over uptown Charlotte"
    assert built.max() > 10.0  # uptown has tall structures (parsed metres/storeys)
