# LEO coverage-risk — assessment summary

*Generated 2026-06-05T03:53:22+00:00 · report `report-2026.06-v1` · obstruction spec `spec-2026.07-roof0`*

## What this is

For every funded location we checked whether trees, hills, or nearby buildings would block the slice of sky a Starlink dish needs to reach its satellites — the same check the Starlink app runs at install, but from public maps instead of a rooftop scan. Treat this as a **prioritised list of places to verify on site**, not a guarantee of service: we cannot see the exact roof or how high the installer will mount the dish.

## Headline

- **4,514,477** locations assessed.
- 🟢 clear: **4,351,862** (96.4%)
- 🟡 at-risk (marginal): **146,203** (3.2%)
- 🔴 severe: **16,412** (0.4%)
- ⚪ undetermined (data too thin to judge): **0** (0.0%)

## By state

| State | Households | At-risk | At-risk rate | Undetermined |
|---|--:|--:|--:|--:|
| North Carolina | 4,674,917 | 169,900 | 3.6% | 0 |

## Priority counties (top 15 by at-risk households)

| County FIPS | State | Households | At-risk | At-risk rate |
|---|---|--:|--:|--:|
| 37119 | North Carolina | 383,211 | 26,062 | 6.8% |
| 37183 | North Carolina | 435,497 | 18,157 | 4.2% |
| 37071 | North Carolina | 104,085 | 10,065 | 9.7% |
| 37081 | North Carolina | 198,520 | 9,415 | 4.7% |
| 37067 | North Carolina | 157,475 | 7,852 | 5.0% |
| 37063 | North Carolina | 115,857 | 5,891 | 5.1% |
| 37051 | North Carolina | 145,033 | 5,733 | 4.0% |
| 37021 | North Carolina | 123,621 | 4,850 | 3.9% |
| 37097 | North Carolina | 87,484 | 4,080 | 4.7% |
| 37159 | North Carolina | 68,371 | 3,584 | 5.2% |
| 37035 | North Carolina | 74,260 | 3,174 | 4.3% |
| 37057 | North Carolina | 81,459 | 3,120 | 3.8% |
| 37025 | North Carolina | 99,293 | 3,013 | 3.0% |
| 37179 | North Carolina | 97,367 | 3,004 | 3.1% |
| 37045 | North Carolina | 44,343 | 2,763 | 6.2% |

*…and 85 more counties — see the JSON sidecar.*

## Caveats & confidence

- **0.0%** of locations are *undetermined* and **30.9%** are low-confidence — these need on-site verification before any funding decision.
- County labels are FIPS codes; join TIGER/Line for plain names.
- The 🟡/🔴 cut-points are calibration defaults (see `docs/rationale.md`) and must be tuned against the Starlink app on a labelled sample before being treated as ground truth.

## QA status (H2 gate)

- A4 flagged **1** anomaly(ies) for human review before sign-off.
  - `[warn]` low_confidence_rate (run): 30.9% of scores are low-confidence — above the 30% budget; degraded/partial surfaces dominate, so treat the run as provisional.

## Map

Interactive risk map: [`coverage_map.html`](coverage_map.html) (open in a browser; points coloured by risk tier).
