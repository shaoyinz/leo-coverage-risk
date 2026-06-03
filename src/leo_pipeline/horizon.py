"""Deterministic sky-obstruction engine for the A3 analysis agent.

This is the analytical core the architecture keeps OUT of the LLM (architecture.md §2:
"LLM reasons, tools compute"). It turns one aligned surface (the A2 pseudo-/true-DSM for a
tile) plus a point into the dwell-time-weighted ``obstruction_pct`` the Starlink app would
report, via the **per-azimuth horizon profile** justified in docs/rationale.md:

    H(φ) = max_r  arctan( (Z_surface(φ, r) − Z_dish) / r )

A sky direction ``(φ, e)`` is blocked iff ``e < H(φ)``; ``obstruction_pct`` is the
azimuth-weighted share of the required sky region (elevations ``[θ_min, 90°]`` over the
dish's azimuth cone) that falls below its local horizon. Everything here is pure
numpy/rasterio/pyproj and vectorised over azimuths × radii, so cost stays O(locations) in
compute with no per-point LLM call (architecture.md §6).

Nothing in this module fetches data or reasons about policy — it is the math the
``compute_sky_obstruction`` / ``find_clear_sky_spot`` tools wrap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import numpy as np

# Mean Earth radius (m) and the standard refraction coefficient for the optional
# curvature/refraction correction (rationale `curvature_refraction`, k ≈ 0.13).
_EARTH_RADIUS_M = 6_371_000.0
_REFRACTION_K = 0.13


@dataclass(frozen=True)
class SkySpec:
    """Resolved reception geometry for one scoring call (a flattened view of config.Analysis
    + the per-call overrides the tool accepts). Distances in metres, angles in degrees."""

    min_elevation_deg: float = 25.0
    az_center_deg: float = 0.0
    az_halfwidth_deg: float = 55.0
    az_weighting: str = "north_biased"
    gso_keepout_halfwidth_deg: float = 18.0
    band_clear_max_pct: float = 1.0
    band_severe_min_pct: float = 10.0
    azimuth_step_deg: float = 2.0
    max_radius_m: float = 1500.0
    earth_curvature: bool = False


# --- surface sampling ----------------------------------------------------------------


class SurfaceSampler:
    """Bilinear sampler over one cached DSM, with lon/lat → surface-CRS reprojection.

    The whole tile array is read into memory once (a precise-pass tile is ~hundreds of
    pixels a side at 10 m), then sampled vectorised at metric ``(x, y)``. Sampling is
    nodata-aware: a sample is valid only where it can interpolate from valid neighbours, so
    gaps in a degraded surface propagate to a lower confidence rather than a fake elevation.
    """

    def __init__(self, dsm_uri: str):
        import rasterio
        from pyproj import Transformer

        self.dsm_uri = dsm_uri
        with rasterio.open(dsm_uri) as src:
            self.array = src.read(1).astype("float64")
            self.transform = src.transform
            self.crs = src.crs
            self.nodata = float(src.nodata) if src.nodata is not None else -9999.0
            self.gsd_m = float(abs(src.transform.a))
        self.height, self.width = self.array.shape
        self._inv = ~self.transform
        # Valid-pixel mask once; bilinear weights are zeroed on invalid corners.
        self._valid = np.isfinite(self.array) & (self.array != self.nodata)
        self._to_xy = Transformer.from_crs("EPSG:4326", self.crs, always_xy=True)
        self._to_ll = Transformer.from_crs(self.crs, "EPSG:4326", always_xy=True)

    def lonlat_to_xy(self, lon: float, lat: float) -> tuple[float, float]:
        x, y = self._to_xy.transform(lon, lat)
        return float(x), float(y)

    def xy_to_lonlat(self, x: float, y: float) -> tuple[float, float]:
        lon, lat = self._to_ll.transform(x, y)
        return float(lon), float(lat)

    def sample(self, x: Any, y: Any) -> tuple[np.ndarray, np.ndarray]:
        """Bilinearly sample the surface at metric coords. Returns ``(values, valid)``
        broadcast to the input shape; ``valid`` is False where all four neighbours are
        nodata/out-of-bounds (and ``values`` is left at nodata there)."""
        x = np.asarray(x, dtype="float64")
        y = np.asarray(y, dtype="float64")
        # Affine inverse → fractional pixel index; shift by half a pixel so integer indices
        # land on pixel *centres* (array[i, j] is the value at centre of pixel j, row i).
        fcol = self._inv.a * x + self._inv.b * y + self._inv.c - 0.5
        frow = self._inv.d * x + self._inv.e * y + self._inv.f - 0.5

        c0 = np.floor(fcol).astype(np.int64)
        r0 = np.floor(frow).astype(np.int64)
        tc = fcol - c0
        tr = frow - r0

        out = np.full(x.shape, self.nodata, dtype="float64")
        valid_any = np.zeros(x.shape, dtype=bool)
        acc = np.zeros(x.shape, dtype="float64")
        wsum = np.zeros(x.shape, dtype="float64")
        for dr, wr in ((0, 1.0 - tr), (1, tr)):
            for dc, wc in ((0, 1.0 - tc), (1, tc)):
                rr = r0 + dr
                cc = c0 + dc
                in_b = (rr >= 0) & (rr < self.height) & (cc >= 0) & (cc < self.width)
                rr_c = np.clip(rr, 0, self.height - 1)
                cc_c = np.clip(cc, 0, self.width - 1)
                vals = self.array[rr_c, cc_c]
                ok = in_b & self._valid[rr_c, cc_c]
                w = np.where(ok, wr * wc, 0.0)
                acc += np.where(ok, vals * w, 0.0)
                wsum += w
                valid_any |= ok
        good = wsum > 0
        out = np.where(good, np.divide(acc, wsum, out=np.full_like(acc, self.nodata), where=good), self.nodata)
        return out, good & valid_any


# --- horizon geometry ----------------------------------------------------------------


def azimuths_for(step_deg: float) -> np.ndarray:
    """Evenly spaced compass bearings [0, 360) at ``step_deg`` resolution (north = 0)."""
    step = max(0.1, float(step_deg))
    n = max(1, int(round(360.0 / step)))
    return (np.arange(n) * (360.0 / n)).astype("float64")


def horizon_profile(
    sampler: SurfaceSampler,
    x0: float,
    y0: float,
    z_dish: float,
    azimuths: np.ndarray,
    *,
    max_radius_m: float,
    step_m: float | None = None,
    earth_curvature: bool = False,
) -> tuple[np.ndarray, float]:
    """Per-azimuth horizon angle (degrees) seen from ``(x0, y0, z_dish)``.

    Marches each bearing outward in ``step_m`` increments to ``max_radius_m``, sampling the
    surface, and takes the maximum elevation angle — exactly ``H(φ)`` above. Returns
    ``(H, sampled_fraction)`` where ``sampled_fraction`` is the share of ray samples that hit
    valid surface (feeds the confidence flag). Vectorised over azimuths × radii.
    """
    step = float(step_m) if step_m else max(1.0, sampler.gsd_m)
    radii = np.arange(step, float(max_radius_m) + step, step, dtype="float64")
    if radii.size == 0:
        radii = np.array([step], dtype="float64")

    az_rad = np.deg2rad(azimuths)[:, None]  # (A, 1); north = 0, clockwise
    r = radii[None, :]  # (1, R)
    xs = x0 + r * np.sin(az_rad)
    ys = y0 + r * np.cos(az_rad)

    z, valid = sampler.sample(xs, ys)  # (A, R)
    dz = z - z_dish
    if earth_curvature:
        # Apparent drop of a distant point below the tangent plane, refraction-corrected.
        dz = dz - (1.0 - _REFRACTION_K) * (r * r) / (2.0 * _EARTH_RADIUS_M)
    angle = np.degrees(np.arctan2(dz, r))
    # Invalid samples must never set the horizon: push them below any real angle.
    angle = np.where(valid, angle, -90.0)
    H = angle.max(axis=1)
    sampled_fraction = float(valid.mean()) if valid.size else 0.0
    return H, sampled_fraction


def _wrap180(delta: np.ndarray) -> np.ndarray:
    """Wrap an angle difference (deg) into [-180, 180]."""
    return (delta + 180.0) % 360.0 - 180.0


def azimuth_weights(azimuths: np.ndarray, spec: SkySpec) -> np.ndarray:
    """Dwell-time weight per azimuth over the required sky cone (rationale: a *gradient*,
    not a hard north-only mask). Zero outside the cone; inside, 'uniform' is flat while
    'north_biased' peaks toward the pointing bearing and is suppressed in the southern
    GSO-arc keep-out band. 'tle_derived' is a documented v2 — it falls back to north_biased.
    """
    delta = _wrap180(azimuths - spec.az_center_deg)
    in_cone = np.abs(delta) <= spec.az_halfwidth_deg + 1e-9
    if spec.az_weighting == "uniform":
        w = np.where(in_cone, 1.0, 0.0)
        return w
    # north_biased (and tle_derived fallback): smooth peak at the boresight bearing.
    w = 0.5 * (1.0 + np.cos(np.deg2rad(delta)))
    # Southern GSO-arc keep-out: de-weight (don't zero — high-elevation southern passes can
    # still be usable) azimuths near due-south of the observer (180° from north).
    south_delta = _wrap180(azimuths - 180.0)
    in_keepout = np.abs(south_delta) <= spec.gso_keepout_halfwidth_deg
    w = np.where(in_keepout, w * 0.15, w)
    return np.where(in_cone, w, 0.0)


def obstruction_fraction(
    H: np.ndarray, azimuths: np.ndarray, spec: SkySpec
) -> tuple[float, list[float]]:
    """Dwell-weighted share of the required sky region below the horizon → (obstruction_pct,
    blocked_azimuths). Per azimuth the required elevation column is ``[θ_min, 90°]``; the
    blocked span is ``clip(min(H, 90) − θ_min, 0, 90 − θ_min)``, weighted by
    :func:`azimuth_weights` and normalised over the cone.
    """
    theta = spec.min_elevation_deg
    span = max(1e-9, 90.0 - theta)
    blocked = np.clip(np.minimum(H, 90.0) - theta, 0.0, span) / span
    w = azimuth_weights(azimuths, spec)
    wsum = float(w.sum())
    pct = 100.0 * float((w * blocked).sum()) / wsum if wsum > 0 else 0.0
    blocked_az = [
        round(float(a), 1)
        for a, b, ww in zip(azimuths, blocked, w)
        if ww > 0 and b > 1e-6
    ]
    return pct, blocked_az


def classify_tier(pct: float, spec: SkySpec) -> str:
    """obstruction_pct → tier (rationale band cut-points): ≤ clear_max → 'clear' 🟢,
    < severe_min → 'at_risk' 🟡, else 'severe' 🔴. 'undetermined' is decided upstream when
    the dish height / surface cannot be established — it is not a function of pct."""
    if pct <= spec.band_clear_max_pct:
        return "clear"
    if pct < spec.band_severe_min_pct:
        return "at_risk"
    return "severe"


def confidence_flag(
    surface_confidence: str | None,
    sampled_fraction: float,
    pct: float,
    spec: SkySpec,
) -> str:
    """Fold the surface's own confidence, ray-sampling coverage, and σ_H borderline-ness into
    one high/medium/low flag (architecture.md §5: degraded results stay usable + auditable).

    σ_H is handled here as a *borderline* signal rather than a full probabilistic margin
    (a documented next step): when ``pct`` sits within a few-metre-equivalent band of a tier
    cut-point — i.e. the verdict could flip under the surfaces' multi-metre vertical error —
    the confidence is knocked down a notch.
    """
    order = ["low", "medium", "high"]
    base = surface_confidence if surface_confidence in order else "medium"
    level = order.index(base)
    # Poorly-sampled rays (gaps / edge tiles) cost confidence.
    if sampled_fraction < 0.5:
        level -= 2
    elif sampled_fraction < 0.9:
        level -= 1
    # σ_H borderline: a pct hugging a cut-point could flip tier under the surfaces' multi-metre
    # vertical error → knock down. The bands are one-sided enough not to penalise an
    # unambiguous 0% (which cannot flip to at_risk under small error).
    near_cut = (
        0.5 <= pct <= spec.band_clear_max_pct + 0.5
        or abs(pct - spec.band_severe_min_pct) <= 2.0
    )
    if near_cut:
        level -= 1
    return order[max(0, min(len(order) - 1, level))]


def derived_max_radius(h_b_max_m: float, h_a_m: float, theta_min_deg: float) -> float:
    """Physical outer query bound d_max = (H_b,max − H_a) / tan θ_min (rationale: the search
    radius is *derived*, not a fixed buffer). Returns 0 for a non-positive excess height."""
    excess = float(h_b_max_m) - float(h_a_m)
    if excess <= 0:
        return 0.0
    return excess / math.tan(math.radians(max(1e-3, theta_min_deg)))


@lru_cache(maxsize=16)
def _cached_sampler(dsm_uri: str) -> SurfaceSampler:
    """Reuse a SurfaceSampler across the many points that share one tile surface."""
    return SurfaceSampler(dsm_uri)


def score_xy(
    sampler: SurfaceSampler,
    x0: float,
    y0: float,
    mount_height_m: float,
    spec: SkySpec,
) -> dict[str, Any]:
    """Full obstruction score at metric coords ``(x0, y0)`` in the surface CRS: sample the
    dish surface, build the horizon profile, weight it into obstruction_pct, classify the
    tier. Returns 'undetermined' when no valid elevation sits under the point (no datum to
    mount on). :func:`score_point` is the lon/lat wrapper around this."""
    z_surf, valid = sampler.sample(np.array(x0), np.array(y0))
    if not bool(valid):
        return {
            "obstruction_pct": None,
            "risk_tier": "undetermined",
            "blocked_azimuths": [],
            "horizon_profile": None,
            "confidence": "low",
            "reason": "no_surface_under_point",
            "dish_height_m": mount_height_m,
        }
    z_dish = float(z_surf) + float(mount_height_m)
    azimuths = azimuths_for(spec.azimuth_step_deg)
    H, sampled_fraction = horizon_profile(
        sampler,
        x0,
        y0,
        z_dish,
        azimuths,
        max_radius_m=spec.max_radius_m,
        earth_curvature=spec.earth_curvature,
    )
    pct, blocked_az = obstruction_fraction(H, azimuths, spec)
    tier = classify_tier(pct, spec)
    return {
        "obstruction_pct": round(pct, 2),
        "risk_tier": tier,
        "blocked_azimuths": blocked_az,
        "horizon_profile": {
            "azimuth_step_deg": spec.azimuth_step_deg,
            "max_horizon_deg": round(float(H.max()), 2),
            "mean_horizon_deg": round(float(H.mean()), 2),
        },
        "sampled_fraction": round(sampled_fraction, 4),
        "dish_height_m": float(mount_height_m),
        "z_surface_m": round(float(z_surf), 2),
    }


def score_point(
    sampler: SurfaceSampler,
    lon: float,
    lat: float,
    mount_height_m: float,
    spec: SkySpec,
) -> dict[str, Any]:
    """lon/lat wrapper around :func:`score_xy` — reprojects the point into the surface CRS."""
    x0, y0 = sampler.lonlat_to_xy(lon, lat)
    return score_xy(sampler, x0, y0, mount_height_m, spec)
