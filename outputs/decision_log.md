# LEO coverage-risk — assessment summary

*Generated 2026-06-04T15:35:53+00:00 · report `report-2026.06-v1` · obstruction spec `spec-2026.07-mosaic`*

## What this is

For every funded location we checked whether trees, hills, or nearby buildings would block the slice of sky a Starlink dish needs to reach its satellites — the same check the Starlink app runs at install, but from public maps instead of a rooftop scan. Treat this as a **prioritised list of places to verify on site**, not a guarantee of service: we cannot see the exact roof or how high the installer will mount the dish.

## Headline

- **4,514,477** locations assessed.
- 🟢 clear: **4,482,265** (99.3%)
- 🟡 at-risk (marginal): **30,341** (0.7%)
- 🔴 severe: **1,871** (0.0%)
- ⚪ undetermined (data too thin to judge): **0** (0.0%)

## By state

| State | Households | At-risk | At-risk rate | Undetermined |
|---|--:|--:|--:|--:|
| North Carolina | 4,674,917 | 32,814 | 0.7% | 0 |

## Priority counties (top 15 by at-risk households)

| County FIPS | State | Households | At-risk | At-risk rate |
|---|---|--:|--:|--:|
| 37119 | North Carolina | 383,211 | 8,290 | 2.2% |
| 37071 | North Carolina | 104,085 | 3,481 | 3.3% |
| 37067 | North Carolina | 157,475 | 1,731 | 1.1% |
| 37097 | North Carolina | 87,484 | 946 | 1.1% |
| 37159 | North Carolina | 68,371 | 834 | 1.2% |
| 37087 | North Carolina | 36,596 | 816 | 2.2% |
| 37045 | North Carolina | 44,343 | 790 | 1.8% |
| 37189 | North Carolina | 26,296 | 716 | 2.7% |
| 37115 | North Carolina | 12,440 | 671 | 5.4% |
| 37035 | North Carolina | 74,260 | 619 | 0.8% |
| 37133 | North Carolina | 85,732 | 601 | 0.7% |
| 37057 | North Carolina | 81,459 | 594 | 0.7% |
| 37099 | North Carolina | 18,201 | 591 | 3.2% |
| 37025 | North Carolina | 99,293 | 567 | 0.6% |
| 37179 | North Carolina | 97,367 | 534 | 0.5% |

*…and 85 more counties — see the JSON sidecar.*

## Caveats & confidence

- **0.0%** of locations are *undetermined* and **42.8%** are low-confidence — these need on-site verification before any funding decision.
- County labels are FIPS codes; join TIGER/Line for plain names.
- The 🟡/🔴 cut-points are calibration defaults (see `docs/rationale.md`) and must be tuned against the Starlink app on a labelled sample before being treated as ground truth.

## QA status (H2 gate)

- A4 flagged **1** anomaly(ies) for human review before sign-off.
  - `[warn]` low_confidence_rate (run): 42.8% of scores are low-confidence — above the 30% budget; degraded/partial surfaces dominate, so treat the run as provisional.

## Map

Interactive risk map: [`coverage_map.html`](coverage_map.html) (open in a browser; points coloured by risk tier).
