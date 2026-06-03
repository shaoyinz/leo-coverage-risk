# Analysis rationale

> Status: complete.

## From install-guide requirements to methodology

From the PDF, we know that Starlink must be installed on the roof of a building and meet the following requirement:

No nearby tree or building rises above the dish's minimum reception line. For a single obstacle, the clear-view (installable) condition is:

$$ H_b - H_a < \text{Dist}_{ab} \tan \theta $$

where $a$ is the current building (where the dish sits) and $b$ is a nearby obstacle, $H$ is the height of the top of the building or obstacle (relative to a common datum), $\text{Dist}_{ab}$ is the horizontal ground distance between them, and $\theta$ is the **minimum reception (elevation) angle measured from the horizontal**.

**Derivation.** The elevation angle from the dish up to the top of the obstacle is
$\alpha = \arctan\!\big((H_b - H_a) / \text{Dist}_{ab}\big)$. The obstacle blocks the view only when it
rises above the minimum reception line, i.e. $\alpha > \theta$. Rearranging the clear-view case
$\alpha < \theta$ gives the inequality above. It degrades sensibly: if $H_b < H_a$ the left side is
negative and the condition always holds (a shorter obstacle never obstructs).

**Scope of the formula.** This is the per-obstacle, single-direction kernel — *not* a full
clear-view test on its own. Starlink needs an unobstructed cone across a range of azimuths
(and a specific sky region by latitude), so the methodology applies this inequality to *every*
candidate obstacle within the dish's relevant azimuth/distance window; a location is clear only
if the condition holds for all of them. This is what motivates a viewshed/horizon-style analysis
over a simple buffer (see "Why this approach" below).

**Assumptions and simplifications** (expanded under "Known limitations"):
- $\theta$ is referenced to the horizon (Starlink's published minimum elevation, ~25°). If it were
  measured from zenith, the relation would invert to $\cot\theta$.
- Heights are taken from a common datum, so terrain, canopy, and building heights must be referenced
  consistently (e.g. all DEM-relative). Because $H_b$ and $H_a$ are each datum-referenced (ground
  elevation + object/mount height), **terrain slope between the dish and the obstacle is captured
  automatically**: an uphill obstacle breaches the reception line at a lower physical height than a
  downhill one, with no separate slope term needed.
- $H_a$ should ideally include the dish mount height above the roof, not the roof alone. In a bulk/remote
  assessment the mount height is usually *unknown* — handled via the vertical-uncertainty budget below.
- Earth curvature is ignored — negligible at the few-hundred-metre obstacle distances here (sub-centimetre
  drop). It would matter only for kilometre-scale terrain masking, which is out of scope for this kernel.


| Install-guide requirement | Obstruction factor | Modeled with | Tool |
|---------------------------|--------------------|--------------|------|
| _e.g. clear view of sky_  | tree canopy        | canopy-height raster | `lookup_obstruction_layer` |
| _e.g. unobstructed cone_  | terrain blocking   | DEM / slope          | `lookup_obstruction_layer` |
| _e.g. no nearby structures_ | buildings        | building footprints  | `lookup_obstruction_layer` |

## Starlink reception geometry (parameter defaults)

The kernel above needs two physical inputs from the Starlink system: the **minimum reception
(elevation) angle** $\theta$ and the **azimuth window** over which the dish must see clear sky.
Both are hardware/regulatory parameters, not constants we should bury in code — we expose them and
adopt conservative defaults.

### Minimum reception angle $\theta$

Whether an obstacle matters is an *angular* test, not a fixed distance: it obstructs only if its
elevation from the dish exceeds $\theta$ (the $\alpha > \theta$ condition above).

- **Default: $\theta = 25°$** above the horizon. This is the classic FCC-licensed minimum below
  which satellites were not used, and the value baked into Starlink's own obstruction-check tool —
  so it is the conservative, well-documented choice.
- **Recently relaxed (FCC approval, Apr 2026):** down to **10°** for satellites below 400 km,
  **20°** for 400–500 km, and as low as **5°** at high latitudes (>62°N). A *lower* $\theta$ means
  each satellite is usable longer but the ground exclusion zone grows — so this is a knob, not a
  free win. We keep $\theta$ a parameter and can re-score at 10–20° to reflect the new rules.

Because $d_{max} = (H_{b,max} - H_a)/\tan\theta$ (see search-window assumption below), the choice of
$\theta$ directly sets how far out an obstacle of given excess height $\Delta H = H_b - H_a$ can still
breach the reception line. The maximum blocking distance is $\Delta H / \tan\theta$:

| $\theta$ | $1/\tan\theta$ (distance factor) | Blocking range for $\Delta H = 10$ m |
|----------|----------------------------------|--------------------------------------|
| **25°** (default) | ≈ 2.1× | ~21 m |
| 20°      | ≈ 2.7× | ~27 m |
| **10°** (new low) | ≈ 5.7× | ~57 m |

The 25° factor (≈ 2.1 m of clear horizontal distance per 1 m of obstacle height above the dish)
matches the common field rule of thumb of "2–3 m per metre of height" — it is simply $1/\tan 25°$.

### Reception cone (azimuth/FOV window)

Starlink's phased-array antenna scans a *cone* of sky and reports obstructions as a **percentage of
that cone blocked**, not a binary clear/blocked. That percentage should be weighted by **satellite
dwell-time across the sky**, not by raw solid angle: a patch the constellation transits often (mid/high
elevations toward the assigned pointing direction) costs far more when blocked than an equal-area patch
the dish rarely uses. The impact metric is therefore *fraction of usable sky-**time** removed*, which
makes obstruction a graded penalty rather than a binary flag. The full cone angle is hardware-dependent:

| Dish | Field of view (full cone) | Half-angle from boresight |
|------|---------------------------|---------------------------|
| Gen2 standard (round/rectangular) | ~100° | ~50° |
| Gen3 standard | ~110° | ~55° |
| Flat High Performance | ~140° | ~70° |

Two consequences for the methodology:
- **The cone is tilted, not zenith-centred.** The dish aims its boresight toward the constellation
  region it is assigned to; in the continental US that is generally **north**, and elsewhere it is
  location-specific (the Starlink app reports the optimal direction). The obstruction-relevant azimuth
  arc is therefore centred on that pointing direction, not a full 360°. This is what justifies an
  azimuth-gated viewshed over a plain radial buffer (see "Why this approach").
- **Why north — and how much the south is *really* excluded.** Two distinct mechanisms bias CONUS usage
  northward, and conflating them overstates the exclusion:
  1. *Orbital geometry.* The main shells are inclined ~53°, so for any CONUS latitude (<53°N) the
     time-integrated satellite density is higher toward the **poleward (northern)** sky.
  2. *Regulation (EPFD / GSO-arc avoidance).* Terminals must not transmit to satellites within ~18° of
     the **geostationary arc**, which for a CONUS observer sits across the **southern** sky at moderate
     elevation — carving a regulatory keep-out *band* around that arc, not the whole hemisphere.

  The correct read is therefore **not** "the southern sky is unusable." It is: the **mid/low-elevation
  southern band around the GSO arc is de-weighted/excluded**, while **higher-elevation southern passes
  outside that band remain usable**. So a tall, close obstacle due south can still remove usable sky and
  is not safe to ignore — the azimuth weighting is a *gradient* (heaviest in the north, lightest in the
  GSO-arc band), not a hard north-only mask.
- **Default cone width: 110° (Gen3 standard);** 140° for High Performance hardware. We treat this,
  like $\theta$, as a parameter so a site can be re-evaluated for a wider-FOV dish.

### Risk banding context

Field guidance: obstructions blocking **1–5%** of the cone are typically tolerable for normal use;
**~10%+** begins causing meaningful dropouts. This informs the `at-risk` band thresholds rather than
a binary verdict (see "Definition of at-risk").

### Vertical uncertainty — why the output is banded, not sharp

The kernel $H_b - H_a < \text{Dist}_{ab}\tan\theta$ is exact geometry, but its inputs are not; treating it
as a crisp threshold would manufacture false precision:

- **Surface heights carry multi-metre error.** Canopy-height models, DSMs and building layers typically
  have vertical RMSE of a few metres, and may be several epochs stale (trees grow, structures appear).
- **Dish height $H_a$ is usually unknown** for a bulk/remote assessment — we know a roof exists, not the
  mount height above it. We default to roof-only and sweep one or two mast heights, but the residual is real.
- **Horizontal position/distance** also carry error, which propagates through $\tan\theta$.

We therefore propagate a **vertical-uncertainty budget $\sigma_H$** (a parameter, from the source layers'
stated accuracy) and report a **banded / probabilistic** result rather than a single clear/blocked verdict.
With clearance margin $m = (H_b - H_a) - \text{Dist}_{ab}\tan\theta$: a comfortably negative $m$ (by several
$\sigma_H$) is `clear`, a margin within a few $\sigma_H$ of zero is `at-risk` (too close to call given the
data), and a case where $H_a$ cannot be established at all is `undetermined`. This is the link between the
geometry and the three-state output defined under "Assumptions."

### Recommended defaults (summary)

- **Min elevation $\theta$:** 25° (conservative default); parameterised and **dated** — re-scorable at
  10–20° (5° above 62°N) per the 2026 rules, and expected to keep falling as low-altitude shells come
  online. Store it with an "as-of" date: a *lower* $\theta$ *widens* the cone and *grows* the search
  radius, so it is a knob, not a free win.
- **Cone half-angle:** 55° (Gen3 standard, 110° full); 70° for High Performance.
- **Azimuth weighting:** a *gradient* centred on the dish pointing direction (north-ish in CONUS),
  heaviest at mid/high northern elevations and lightest in the southern GSO-arc keep-out band — not a
  hard north-only mask, and not a full 360°.
- **Impact metric:** dwell-time-weighted **% of usable sky removed**, banded into `clear` / `at-risk`
  thresholds (≈1–5% tolerable, ≈10%+ disruptive) — never a bare binary.
- **Vertical-uncertainty budget $\sigma_H$:** from the source layers' stated accuracy, propagated into
  the clearance margin so borderline cases fall to `at-risk` rather than a false `clear`.
- **Per-feature screening radius:** computed, not hard-coded — $\Delta H / \tan\theta$ per obstacle;
  coarse pre-filter at $H_{b,max}/\tan(10°)$ (≈ 5.7× tallest plausible obstacle) bounds the query.

**Sources:**
[DishyTech – obstructions & field of view](https://www.dishytech.com/starlink-obstructions-how-much-is-too-much/),
[DishyTech – dish pointing direction](https://www.dishytech.com/starlink-dish-placement-which-way-should-it-face/),
[InstallersPH – Gen3 specs (FOV)](https://installersph.com/starlink-performance-gen-3-specs-overview/),
[Space Internet Solutions – 25°→10° elevation change](https://www.spaceinternetsolutions.com/post/starlink-lowers-angle-to-10-degrees),
[Gear Musk – FCC elevation approval (Apr 2026)](https://gearmusk.com/2026/04/16/fcc-approval-starlink-dish/),
[5gstore – Starlink wider view / lower min-elevation (Apr 2026)](https://5gstore.com/blog/2026/04/21/starlink-dishes-wider-view-fcc-approval/),
[FCC – EPFD limits & GSO-arc avoidance framework (FCC-26-26)](https://docs.fcc.gov/public/attachments/FCC-26-26A1.pdf),
[Starlink Help Center – fixing obstructions](https://starlink.com/support/article/64009737-3768-0003-2838-4786c5a850ea).

## Assumptions & open questions

### Assumptions (defaults we adopt unless told otherwise)

- **Roof install on an existing building.** Following the PDF, the dish is mounted on the roof of
  the building at the requested location; $H_a$ is the roof height (ideally plus mount height).
- **Mount/pole height is a parameter, not a fixed value.** We evaluate at a roof-only default and
  expose one or two standard mast heights, so the agent can report "clear if raised to $X$ m"
  rather than hard-coding a single pole height.
- **Alternative-location suggestions are clipped to the input parcel.** If we recommend better-visibility
  spots within an {X} m buffer, candidates are constrained to the requester's own parcel (or building
  footprint + immediate curtilage when no parcel is available). Cross-parcel suggestions are out of
  scope — they would require easement/permission data we do not model.
- **Three-state assessment, not binary.** Outcomes are `clear` / `at-risk` / `undetermined`. A location
  is only scored when we can establish the dish height; otherwise it is `undetermined` with a reason code.
- **"No building found" is disambiguated by cross-source corroboration, with a confidence flag.**
  A missing footprint alone cannot distinguish (1) a genuinely vacant lot from (2) an unreliable/stale
  data source. We corroborate against an independent height surface (nDSM = DSM − DEM), a second
  footprint source, and dataset coverage metadata: agreement → high confidence; disagreement → flagged
  as low-confidence/unreliable rather than asserted as fact. We never silently assume $H_a = 0$.
- **Input locations are in WGS84 and inside the service footprint.** The CSV `latitude`/`longitude`
  are EPSG:4326 degrees; we assume each point already lies within Starlink's LEO service footprint
  and assess *obstruction* risk only, not footprint membership or latitude-band eligibility.
  Horizontal distances ($\text{Dist}_{ab}$) are computed in a local metric CRS (or geodesically),
  and all heights share one vertical datum.
- **Bounded azimuth/distance search window.** Obstacles are only considered within the azimuth arc
  and out to a maximum horizontal distance relevant to the dish's view cone. Beyond a cutoff
  $d_{max} = (H_{b,max} - H_a)/\tan\theta$ — the range at which even a tallest-plausible obstacle
  can no longer breach the minimum reception line — candidates are ignored. This bounds the per-point
  neighbourhood query rather than scanning all features.
- **Units are metres and degrees.** Heights and distances in metres, angles ($\theta$, $\alpha$) in
  degrees; rasters are resampled to a common resolution before sampling.
- **One dish per location; mast height swept, not multiplied.** We model a single dish per
  `location_id`. Multiple mount options are explored only through the mast-height parameter sweep
  (roof-only default plus standard masts), not as independent simultaneous installs.
- **Conservative, single-epoch surfaces.** We use the most recent available raster epoch and treat
  canopy as leaf-on (worst case), so `clear` verdicts are conservative; we do not model seasonal
  foliage change or future growth (tracked under "Known limitations").



## Why this approach (vs. alternatives)

The kernel above is a *per-obstacle, single-direction* angular test. Aggregating it over every
bearing around the dish is exactly a **per-azimuth horizon profile** $H(\varphi)$ — for each compass
bearing $\varphi$, the largest obstruction elevation angle produced by any feature in that direction:

$$ H(\varphi) = \max_{r}\ \arctan\!\Big( \frac{Z_{\text{surface}}(\varphi, r) - Z_{\text{dish}}}{r} \Big) $$

where $Z_{\text{surface}}(\varphi, r)$ is the fused-surface elevation at bearing $\varphi$ and ground
distance $r$, and $Z_{\text{dish}}$ is the dish-top elevation ($H_a$ in the kernel). A sky direction
$(\varphi, \theta)$ is blocked iff $\theta < H(\varphi)$, and `obstruction_pct` is the dwell-time-weighted
share of the required sky region that falls below its local horizon. Choosing this primitive — rather
than the obvious shortcuts — is the central modeling decision, so each rejected alternative is defended
explicitly below.

**vs. a simple radial buffer** ("any tall canopy within $X$ m → at-risk"). Two independent failures:

1. *It is omnidirectional, but the problem is not.* The dish sees a tilted cone centred on its pointing
   direction (north-ish in CONUS), with a graded de-weighting in the southern GSO-arc band (see the
   reception-geometry section). A buffer weights *every* bearing equally, when the cost of a blocked
   patch is strongly direction-dependent — heaviest in the north, lightest in the GSO keep-out band. It
   therefore mislabels a large share of the directional edge cases (dense stand to the south vs. to the
   north), which is the single most likely "debug this wrong answer" case in a live review.
2. *A fixed radius is physically wrong.* Blocking range scales as $\Delta H / \tan\theta$, so it depends
   jointly on each obstacle's excess height $\Delta H = H_b - H_a$ and on $\theta$: a 20 m tree matters
   out to ~55 m at $\theta = 20°$, but a 300 m ridge matters out to ~1.7 km at $\theta = 10°$. No single
   radius covers both the near-field regime (canopy/buildings, tens of metres) and the far-field regime
   (terrain, kilometres). The horizon profile is radius-free — it integrates outward until the surface
   can no longer breach $\theta$ — and our search window is *derived* ($d_{max} = (H_{b,\max} - H_a)/\tan\theta$),
   not guessed.

**vs. a binary viewshed.** A classic viewshed answers "is target point $T$ visible from the dish?" — a
boolean for one target. We instead need a *graded* quantity (% of the usable sky cone removed,
dwell-time-weighted) compared against an *elevation threshold* over a *distribution* of sky directions.
The horizon profile yields that directly: compare $H(\varphi)$ to $\theta$ across all $\varphi$ and
integrate the blocked solid angle. A viewshed would have to be run against a dense fan of synthetic sky
targets to approximate the same answer, at higher cost and lower fidelity. Tooling that computes the
profile natively: **GRASS `r.horizon`** (closest conceptual fit — returns $H(\varphi)$ per azimuth
step), **WhiteboxTools `HorizonAngle`**, with **`gdal_viewshed`** as the boolean fallback.

**Why it needs a fused surface, not bare earth.** $Z_{\text{surface}}$ must combine bare-earth terrain
**+** canopy **+** structures. Where a lidar-derived DSM (first-return) exists we use it directly;
elsewhere we synthesise a pseudo-DSM $= \text{DEM} + \max(\text{canopy height}, \text{building height})$.
This is also why canopy *cover fraction* (e.g. NLCD percent-cover) is not a substitute for canopy
*height*: 100% cover by 3 m shrubs and 100% cover by 30 m conifers give identical cover but wildly
different horizons. Cover is used only as a fallback / cross-check (see [data-sources](data-sources.md)).

**vs. ML obstruction detection** (e.g. a CNN over aerial or street imagery). Such a model could classify
"obstructed/clear" or segment canopy and rooftops, but we reject it as the *primary* method for three
reasons: (1) *interpretability* — the physics kernel gives an auditable clearance margin
$m = (H_b - H_a) - \text{Dist}_{ab}\tan\theta$ that a broadband officer can be walked through, whereas a
classifier's verdict is opaque and hard to defend on a funding decision; (2) *label scarcity* — there is
no national ground-truth set of "Starlink-obstructed roofs" to train on, and the true label depends on
the *unknown mount height*, which no overhead image reveals; (3) *generalisation / currency* — a model
trained on one region's imagery and vintage degrades elsewhere. ML is better aimed at *improving the
inputs* (building-height regression, canopy-height super-resolution) that feed the same geometric kernel
— a refinement, not a replacement.

**The more principled version (v2).** Our north-weighted azimuth gradient is a *static* stand-in for a
moving constellation. The rigorous alternative is to *derive* the sky-occupancy distribution instead of
assuming it: propagate the Starlink TLEs with **Skyfield/sgp4**, accumulate satellite dwell time per
$(\varphi, \theta)$ bin as a function of observer latitude (and the active min-elevation rule), then
intersect that empirical distribution with $H(\varphi)$. That turns "at-risk" from a hand-tuned cone into
a physically-grounded *fraction of usable sky-time removed*, and it automatically tracks shell changes and
the 2026 elevation-rule relaxation. It is the natural answer to "how would you make this more principled?"
and is gated to v2 only because the static gradient is adequate for screening-grade triage.

## Definition of "at-risk" (for non-technical stakeholders)

**What we compute.** For each location we estimate `obstruction_pct` — the dwell-time-weighted share of
the sky the dish actually uses that is blocked by terrain, canopy, or structures at the modeled mount
height. This is the same quantity the Starlink app reports as "% obstructed," produced from public maps
instead of a rooftop scan. It feeds the 0..1 score in `compute_risk_score`, which we band as follows:

| Output state | `obstruction_pct` (default cut-points) | Plain meaning |
|--------------|----------------------------------------|---------------|
| 🟢 **clear** | < ~1%, with clearance margin comfortably negative beyond the data error | The sky the dish needs is open; service quality likely good. |
| 🟡 **at-risk — marginal** | ~1–10% (or margin within a few $\sigma_H$ of zero) | Periodic dropouts possible; the lower end (≲5%) is usually tolerable, the upper end trends to degraded. Often fixable by raising the mount or nudging placement. |
| 🔴 **at-risk — severe** | > ~10% | Meaningful, persistent obstruction; likely needs re-siting or a higher mount. |
| ⚪ **undetermined** | any | Dish height could not be established, or a source layer was missing / stale / degraded — needs human review, not a verdict. |

The 🟡 and 🔴 rows are both reported under the single engineering state **`at-risk`** — the doc's
three-state model is `clear` / `at-risk` / `undetermined`; the severity split is a sub-label for
prioritisation, not a fourth state. The cut-points (1%, 10%) are **calibration defaults**: they must be
tuned on a sample against the Starlink app before being treated as ground truth (see
[Tuneable parameters](#tuneable-parameters) and [Known limitations](#known-limitations-vs-on-site-assessment)).

**Why a band, not a yes/no.** The geometry is exact, but its inputs are not: surface heights carry
multi-metre vertical error, and the mount height $H_a$ is usually unknown for a remote assessment. A crisp
threshold would manufacture false precision. The bands — and especially the `undetermined` state — are how
the output stays honest about what public data can and cannot settle (see "Vertical uncertainty" above).

**For a state broadband officer:**

> For every funded household we checked whether trees, hills, or nearby buildings would block the slice of
> sky a Starlink dish needs to reach its satellites — the same check the Starlink app runs during install,
> except we do it from public maps instead of climbing onto the roof. A 🔴 household will probably not get
> the promised speeds unless the dish is moved or mounted higher; a 🟡 household is borderline and worth a
> closer look; a ⚪ household is one where the public data simply wasn't good enough to judge. Because we
> can't see the exact roof or how high the installer will mount the dish, please treat this as a
> **prioritised list of homes to verify on site** — not a final guarantee of service.

## Tuneable parameters

Everything below is a *knob*, not a buried constant. Several are physical/regulatory values that change
over time (Starlink is actively relaxing its elevation rules in 2026); others trade accuracy against cost,
or encode an assumption a reviewer may want to challenge. This section is the single registry of those
knobs; the "Recommended defaults (summary)" list above gives the rationale for the geometry ones, and
[`config.py`](../src/leo_pipeline/config.py) is where the implemented ones live. Defaults here match the
values used elsewhere in this doc.

**Why expose them rather than hard-code:** a hard-coded threshold cannot be re-scored when the rules
change, cannot be calibrated against the Starlink app, and cannot carry an "as-of" date. Parameterising
also makes the sensitivity analysis the methodology promises *runnable* rather than hypothetical.

### Starlink reception geometry (physical / regulatory)

| Parameter | Default | Why a knob / sensitivity |
|-----------|---------|--------------------------|
| `min_elevation_deg` ($\theta$) | **25°**, with an *as-of* date | Conservative, app-documented value; re-scorable to **10–20°** (5° above 62°N) per the Apr-2026 FCC rules. **High sensitivity:** $d_{max} \propto 1/\tan\theta$, so a lower $\theta$ both widens the required cone and grows the search radius — a knob, not a free win. |
| `cone_half_angle_deg` | **55°** (Gen3, 110° full cone) | 70° for Flat High Performance. Sets the angular extent of the required sky region; re-evaluate a site for a wider-FOV dish without code changes. |
| `pointing_azimuth_deg` | north-ish in CONUS (else app-reported per site) | Boresight bearing that the azimuth weighting is centred on; location-specific, so it must not be a global constant. |
| `azimuth_weight_profile` | static north-weighted gradient | The $(\varphi,\theta) \to$ weight map. Default is the static gradient; the v2 alternative is the TLE-derived dwell-time map. Swappable so the weighting can be upgraded without touching the kernel. |
| `gso_keepout_halfwidth_deg` | ~18° | Half-width of the GSO-arc keep-out band in the southern sky; defines where the southern weighting is suppressed. |
| `shell_inclination_deg` | ~53° | Main-shell inclination; used only by the v2 TLE/dwell-time weighting to set the poleward bias by latitude. |

### Install / siting

| Parameter | Default | Why a knob / sensitivity |
|-----------|---------|--------------------------|
| `mount_height_m` ($H_a$ above roof) | **roof-only (0 m)**, swept over e.g. `{0, 1.5, 3.0}` | The single largest unknown. Sweeping it lets the agent report "clear if raised to $X$ m" instead of hard-coding one pole height. |
| `alt_location_search_radius_m` | e.g. **15–30 m**, parcel-clipped | Radius for better-visibility suggestions; candidates are constrained to the requester's parcel (or footprint + curtilage). Larger radius → more candidates but more cross-parcel risk. |

### Risk banding & uncertainty

| Parameter | Default | Why a knob / sensitivity |
|-----------|---------|--------------------------|
| `band_cutpoints_pct` | `{clear_max: 1, severe_min: 10}` | The 🟢/🟡/🔴 boundaries on `obstruction_pct`. **Must be calibrated** against the Starlink app on a labelled sample — see open questions. |
| `sigma_H_m` ($\sigma_H$) | per source layer, from stated RMSE | Vertical-uncertainty budget propagated into the clearance margin; differs by layer (lidar DSM ≪ global building height). |
| `clear_margin_sigma` | ~2–3 $\sigma_H$ | How many $\sigma_H$ the margin must clear to score `clear` rather than fall to `at-risk`. Controls the conservatism of the verdict. |
| `risk_score_weights` | implementation default | How canopy / terrain / building factors combine into the 0..1 score in `compute_risk_score`. |
| `canopy_state` | **leaf-on** (worst case) | Toggle for seasonal foliage; leaf-on keeps `clear` verdicts conservative. |

### Search & computation

| Parameter | Default | Why a knob / sensitivity |
|-----------|---------|--------------------------|
| `h_b_max_m` ($H_{b,\max}$) | tallest plausible obstacle, **per class** | Bounds the coarse pre-filter radius $H_{b,\max}/\tan(10°)$ (≈ 5.7× at the lowest $\theta$) so neighbourhood queries don't scan all features. The outer query bound is the max across classes (terrain dominates); see the per-class table below. |
| `azimuth_step_deg` / `n_azimuths` | e.g. 1–2° | Angular resolution of the horizon profile — accuracy vs. compute. |
| `raster_resolution_m` | common resample grid | Layers are resampled to one resolution before sampling; finer captures small obstacles but costs more. |
| `metric_crs` | per-point UTM / local ENU | Distances and azimuths must be computed in metres, not raw degrees — choosing this badly is a classic silent bug. |
| `curvature_refraction` | **off** near-field; $k \approx 0.13$ when on | Earth-curvature + atmospheric-refraction correction; negligible at the few-hundred-metre obstacle scale, engaged only for the kilometre-scale terrain horizon pass. |

### Data handling

| Parameter | Default | Why a knob / sensitivity |
|-----------|---------|--------------------------|
| `dedup_precision` | **6** (~0.11 m) — *live* | Coordinate rounding before de-duplication ([`deduplicate_coordinates`](../src/leo_pipeline/tools/__init__.py)); lower values snap near-coincident points to a shared cell, trading spatial fidelity for fewer sampling jobs. |
| `corroboration_thresholds` | agreement among nDSM, 2nd footprint source, coverage metadata | Decides whether "no building found" is a genuine vacant lot or an unreliable source, and sets the confidence flag. We never silently assume $H_a = 0$. |
| `surface_source_preference` | lidar DSM where available, else pseudo-DSM | Which surface model wins when several cover a point; drives the low-confidence flag where only coarse data exists. |

### Per-obstacle-class search radius

How far out to look is naturally **per obstacle class** (terrain / building / canopy), because the
distance at which a class can still breach the reception line scales with its height.

| Class | `max_search_radius_m` (default) | Notes |
|-------|---------------------------------|-------|
| **terrain** (DEM) | *derived*, km-scale: $(H_{\text{relief,max}} - H_a)/\tan\theta_{\min}$ | Solid earth, always a full block; the class that needs the kilometre window and the curvature/refraction correction. |
| **building** (footprints) | *derived*, ~tens of m: $(H_{\text{bldg,max}} - H_a)/\tan\theta_{\min}$ | Blockage is confident; the *height* is the weak input, handled by $\sigma_H$. |
| **canopy** (height raster) | *derived*, ~tens–150 m: $(H_{\text{canopy,max}} - H_a)/\tan\theta_{\min}$ | Tens-of-metres window; tall mature canopy extends it. |

**A single global radius is the wrong knob.** "Ignore anything past radius $R$" is the fixed-buffer
anti-pattern rejected under [Why this approach](#why-this-approach-vs-alternatives): a 20 m tree stops
mattering at ~55 m, but a 300 m ridge still blocks at ~1.7 km, so one radius is either too small for
terrain or wastefully large for canopy. We therefore make the radius **per class and *derived*, not
hand-set**: $R_{\max,\text{class}} = (H_{\text{class,max}} - H_a)/\tan\theta_{\min}$. That is the
*outer query bound* only — inside it, every individual feature is still kept or dropped by its **exact**
cutoff $d_{max}(H_b) = (H_b - H_a)/\tan\theta$ (a feature beyond it cannot geometrically breach the
reception line, so this is exact, not a heuristic). A manual `max_search_radius_m` override is retained as
a safety clamp to bound query cost in pathologically dense tiles, but it should only ever *tighten* the
derived value, and tightening it is logged as an approximation.

## Known limitations vs. on-site assessment

A remote, public-data assessment cannot fully replace an on-site dish-pointing check. The main gaps:

- **Vertical accuracy & currency.** Canopy/DSM/building layers carry multi-metre vertical error and are
  often one to several years stale. Trees grow, get cleared, and leaf out seasonally; new structures
  appear. We use the most recent epoch and treat canopy as leaf-on (worst case), but a `clear` verdict is
  only as fresh as the surface. (Absorbed into the $\sigma_H$ budget and the `at-risk` band.)
- **Unknown dish mount height.** We rarely know how high the installer will actually mast the dish; the
  roof-only default plus a mast sweep brackets this, but the exact on-site placement is unmodelled.
- **Fine-scale / near-field obstructions.** Chimneys, vents, railings, single overhanging branches, power
  lines and eaves can block a phased-array cone yet fall below raster resolution. On-site the Starlink app
  sees these directly; we cannot.
- **Exact pointing & a moving constellation.** The app picks the optimal boresight per site from live
  ephemerides; our azimuth-weighting gradient is a static approximation of a constellation that is still
  densifying and whose min-elevation rules are actively changing (2026).
- **Micro-siting freedom.** An installer can move a few metres, raise a pole, or pick a different roof
  face to clear an obstacle. Our parcel-clipped suggestions approximate this but cannot replicate a
  technician walking the site.

Net effect: the analysis is **conservative and screening-grade** — strong for prioritising and flagging
likely-degraded locations at scale, not a substitute for final on-site verification.
