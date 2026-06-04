"""Deterministic unit tests for the A3 analysis tools + horizon engine.

No network and no API key: the tools score against tiny REAL GeoTIFF surfaces written to
tmp (in a metric UTM CRS, like the A2 cache), so the per-azimuth horizon math runs end to
end and offline. The tools under test live in ``leo_pipeline.tools`` and back the
GEO_ANALYSIS_AGENT (A3) allow-list; the geometry lives in ``leo_pipeline.horizon``.
"""

from __future__ import annotations

import numpy as np
import pytest

import leo_pipeline.horizon as horizon
import leo_pipeline.tools as tools

from conftest import run_tool

EPSG = 32617
GSD = 10.0
X0 = 500_000.0  # SW corner easting (UTM 17N)
Y0 = 3_900_000.0  # SW corner northing
NODATA = -9999.0
BASE = 100.0


def write_surface(path, array, *, gsd=GSD, x0=X0, y0=Y0):
    """Write a north-up single-band float32 GeoTIFF in EPSG:32617 (a stand-in A2 surface)."""
    import rasterio
    from rasterio.transform import from_origin

    h, w = array.shape
    transform = from_origin(x0, y0 + h * gsd, gsd, gsd)  # west, north, xres, yres
    with rasterio.open(
        path, "w", driver="GTiff", height=h, width=w, count=1, dtype="float32",
        crs=f"EPSG:{EPSG}", transform=transform, nodata=NODATA,
    ) as dst:
        dst.write(array.astype("float32"), 1)
    return path


@pytest.fixture(autouse=True)
def _clear_sampler_cache():
    """The sampler is lru_cached by uri; tmp paths are unique per test, but clear to be safe."""
    horizon._cached_sampler.cache_clear()
    yield
    horizon._cached_sampler.cache_clear()


def _center_lonlat(path):
    """Geographic centre of a written surface, via the sampler's own CRS transform."""
    s = horizon.SurfaceSampler(str(path))
    xc = X0 + (s.width / 2.0) * GSD
    yc = Y0 + (s.height / 2.0) * GSD
    return s.xy_to_lonlat(xc, yc)


def _flat(n=41, value=BASE):
    return np.full((n, n), value, dtype="float32")


# --- horizon engine ------------------------------------------------------------------


def test_horizon_profile_recovers_single_obstacle_angle(tmp_path):
    """One tall cell due north at a known distance → H(0°) ≈ arctan(Δh / d)."""
    n = 41
    arr = _flat(n)
    cx = n // 2
    # North = decreasing row. Put a +30 m cell ~50 m north of centre (5 cells up).
    arr[cx - 5, cx] = BASE + 30.0
    path = write_surface(tmp_path / "ob1.tif", arr)

    s = horizon.SurfaceSampler(str(path))
    xc = X0 + (cx + 0.5) * GSD
    yc = Y0 + (n - (cx + 0.5)) * GSD  # row cx -> northing
    az = horizon.azimuths_for(2.0)
    prof = horizon.horizon_profile(s, xc, yc, BASE, az, max_radius_m=200.0)
    expected = np.degrees(np.arctan2(30.0, 50.0))
    i0 = int(np.argmin(np.abs(az - 0.0)))
    assert prof.H[i0] == pytest.approx(expected, abs=3.0)
    assert prof.sampled_fraction > 0.9


def test_derived_max_radius_matches_geometry():
    # (40 - 2) / tan(25°) ≈ 81.5 m
    r = horizon.derived_max_radius(40.0, 2.0, 25.0)
    assert r == pytest.approx(38.0 / np.tan(np.radians(25.0)), rel=1e-6)
    assert horizon.derived_max_radius(2.0, 5.0, 25.0) == 0.0  # obstacle below dish


def test_confidence_flag_provenance_drives_base():
    """Provenance under the point is the dominant term, with full near-field coverage and a
    robust (wide) clearance so neither knock-down fires: lidar→high, pseudo→medium, fill→low."""
    spec = horizon.SkySpec()
    assert horizon.confidence_flag("lidar", 1.0, 50.0, 0.0, spec) == "high"
    assert horizon.confidence_flag("pseudo", 1.0, 50.0, 0.0, spec) == "medium"
    assert horizon.confidence_flag("dem_fill", 1.0, 50.0, 0.0, spec) == "low"


def test_confidence_flag_open_sky_is_not_penalised():
    """A wide-open lidar point (obstruction 0%, clearance ≫ σ_H) stays HIGH — the old whole-ray
    sampled_fraction rule wrongly knocked these to low."""
    spec = horizon.SkySpec()
    assert horizon.confidence_flag("lidar", 1.0, 99.0, 0.0, spec) == "high"


def test_confidence_flag_near_field_gap_knocks_down_but_distant_gap_does_not():
    spec = horizon.SkySpec()
    # near-field coverage 0.4 (< med 0.5) → −2 notches from high → low
    assert horizon.confidence_flag("lidar", 0.4, 50.0, 0.0, spec) == "low"
    # near-field coverage 0.7 (< high 0.9) → −1 → medium
    assert horizon.confidence_flag("lidar", 0.7, 50.0, 0.0, spec) == "medium"
    # full near-field coverage → no penalty even if the *whole* ray had distant gaps
    assert horizon.confidence_flag("lidar", 1.0, 50.0, 0.0, spec) == "high"


def test_confidence_flag_sigma_h_borderline_clearance_knocks_down():
    spec = horizon.SkySpec(sigma_h_m=3.0)
    # clearance within ±σ_H of the θ-line → verdict could flip → −1
    assert horizon.confidence_flag("lidar", 1.0, 1.0, 0.0, spec) == "medium"
    # a breaching obstacle far past the line (−20 m) is unambiguous severe → no penalty
    assert horizon.confidence_flag("lidar", 1.0, -20.0, 50.0, spec) == "high"
    # inf clearance (no obstacle considered) → no penalty
    assert horizon.confidence_flag("lidar", 1.0, float("inf"), 0.0, spec) == "high"


def test_azimuth_weights_zero_outside_cone_and_suppress_south():
    spec = horizon.SkySpec(az_center_deg=0.0, az_halfwidth_deg=55.0, az_weighting="north_biased")
    az = np.array([0.0, 50.0, 90.0, 180.0])
    w = horizon.azimuth_weights(az, spec)
    assert w[0] == pytest.approx(1.0)  # boresight (north) peaks
    assert w[1] > 0  # within cone
    assert w[2] == 0.0  # 90° is outside the ±55 north cone
    assert w[3] == 0.0  # due south also outside the cone


# --- compute_sky_obstruction ---------------------------------------------------------


def test_flat_surface_is_clear(tmp_path):
    path = write_surface(tmp_path / "flat.tif", _flat())
    lon, lat = _center_lonlat(path)
    env, payload = run_tool(
        tools.compute_sky_obstruction,
        {"points": [{"location_id": "p", "lat": lat, "lon": lon}], "dsm_uri": str(path)},
    )
    assert not env.get("is_error")
    res = payload["results"][0]
    assert res["obstruction_pct"] == pytest.approx(0.0, abs=0.5)
    assert res["risk_tier"] == "clear"
    assert payload["spec_version"]


def test_north_wall_blocks_but_south_wall_does_not(tmp_path):
    """A tall wall inside the northern reception cone drives obstruction up; the SAME wall to
    the south (outside the ±55° north cone) does not — the directional model in action."""
    n = 41
    cx = n // 2

    north = _flat(n)
    north[cx - 5 : cx - 1, :] = BASE + 60.0  # broad wall ~20-50 m north
    npath = write_surface(tmp_path / "north.tif", north)
    nlon, nlat = _center_lonlat(npath)
    _, npay = run_tool(
        tools.compute_sky_obstruction,
        {"points": [{"location_id": "n", "lat": nlat, "lon": nlon}], "dsm_uri": str(npath)},
    )
    nres = npay["results"][0]
    assert nres["obstruction_pct"] > 5.0
    assert nres["risk_tier"] in ("at_risk", "severe")
    assert any(a <= 55 or a >= 305 for a in nres["blocked_azimuths"])

    south = _flat(n)
    south[cx + 2 : cx + 6, :] = BASE + 60.0  # same wall, to the south
    spath = write_surface(tmp_path / "south.tif", south)
    slon, slat = _center_lonlat(spath)
    _, spay = run_tool(
        tools.compute_sky_obstruction,
        {"points": [{"location_id": "s", "lat": slat, "lon": slon}], "dsm_uri": str(spath)},
    )
    sres = spay["results"][0]
    assert sres["obstruction_pct"] == pytest.approx(0.0, abs=0.5)
    assert sres["risk_tier"] == "clear"


def test_omni_spec_picks_up_southern_wall(tmp_path):
    """Widen the cone to omni (az_halfwidth=180) and the southern wall now counts — proves the
    spec override threads through and the cone gating, not a bug, hid it before."""
    n = 41
    cx = n // 2
    south = _flat(n)
    south[cx + 2 : cx + 6, :] = BASE + 60.0
    path = write_surface(tmp_path / "south2.tif", south)
    lon, lat = _center_lonlat(path)
    _, payload = run_tool(
        tools.compute_sky_obstruction,
        {
            "points": [{"location_id": "s", "lat": lat, "lon": lon}],
            "dsm_uri": str(path),
            "sky_spec": {"az_halfwidth_deg": 180.0, "az_weighting": "uniform"},
        },
    )
    assert payload["results"][0]["obstruction_pct"] > 5.0


def test_raising_dish_height_lowers_obstruction(tmp_path):
    n = 41
    cx = n // 2
    arr = _flat(n)
    arr[cx - 5 : cx - 1, :] = BASE + 60.0
    path = write_surface(tmp_path / "wall.tif", arr)
    lon, lat = _center_lonlat(path)

    def pct(h):
        _, p = run_tool(
            tools.compute_sky_obstruction,
            {"points": [{"location_id": "p", "lat": lat, "lon": lon}],
             "dsm_uri": str(path), "dish_height_m": h},
        )
        return p["results"][0]["obstruction_pct"]

    assert pct(2.0) > pct(30.0)  # masting the dish well above the wall clears sky


def test_undetermined_when_no_surface_under_point(tmp_path):
    arr = _flat()
    cx = arr.shape[0] // 2
    arr[cx - 1 : cx + 2, cx - 1 : cx + 2] = NODATA  # hole under the point
    path = write_surface(tmp_path / "hole.tif", arr)
    lon, lat = _center_lonlat(path)
    _, payload = run_tool(
        tools.compute_sky_obstruction,
        {"points": [{"location_id": "p", "lat": lat, "lon": lon}], "dsm_uri": str(path)},
    )
    res = payload["results"][0]
    assert res["risk_tier"] == "undetermined"
    assert res["obstruction_pct"] is None


@pytest.mark.parametrize(
    "args, needle",
    [
        ({"points": [], "dsm_uri": "x"}, "non-empty list"),
        ({"points": [{"lat": 1, "lon": 2}], "dsm_uri": ""}, "dsm_uri is required"),
        ({"points": [{"lat": 1, "lon": 2}], "dsm_uri": "/no/such.tif"}, "not found"),
    ],
)
def test_compute_validation_errors(args, needle):
    env, text = run_tool(tools.compute_sky_obstruction, args)
    assert env.get("is_error")
    assert needle in text


# --- find_clear_sky_spot -------------------------------------------------------------


def test_find_clear_sky_spot_recommends_a_better_option(tmp_path):
    """Next to a tall northern wall, find_clear_sky_spot should surface a candidate (a higher
    mount and/or a position away from the wall) that improves on the baseline."""
    n = 61
    cx = n // 2
    arr = _flat(n)
    arr[cx - 4 : cx - 1, :] = BASE + 80.0  # tall wall ~10-40 m north
    path = write_surface(tmp_path / "fix.tif", arr)
    lon, lat = _center_lonlat(path)

    env, payload = run_tool(
        tools.find_clear_sky_spot,
        {
            "lat": lat, "lon": lon, "dsm_uri": str(path),
            "buffer_m": 60.0, "candidate_grid_m": 20.0,
            "dish_height_candidates_m": [2.0, 10.0, 40.0],
        },
    )
    assert not env.get("is_error")
    base_pct = payload["baseline"]["obstruction_pct"]
    assert base_pct > 0
    best = payload["candidates"][0]
    assert best["obstruction_pct"] <= base_pct
    # a real fix exists: at least one candidate strictly beats the current install
    assert any(c["improvement_pct"] > 0 for c in payload["candidates"])


def test_find_clear_sky_spot_validation():
    env, text = run_tool(tools.find_clear_sky_spot, {"lat": 1, "lon": 2, "dsm_uri": ""})
    assert env.get("is_error")
    assert "dsm_uri is required" in text
