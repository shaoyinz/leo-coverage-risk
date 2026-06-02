# Analysis rationale

> Status: scaffold. Mirrors the "Analysis Rationale" deliverable. Fill in after Step-0
> pre-work (reading the Starlink Install Guide).

## From install-guide requirements to methodology

_TODO:_ For each connectivity requirement in the Install Guide (clear field of view,
elevation/azimuth obstruction limits, distance from trees/structures), state the
public dataset and computation used to model it at scale.

| Install-guide requirement | Obstruction factor | Modeled with | Tool |
|---------------------------|--------------------|--------------|------|
| _e.g. clear view of sky_  | tree canopy        | canopy-height raster | `lookup_obstruction_layer` |
| _e.g. unobstructed cone_  | terrain blocking   | DEM / slope          | `lookup_obstruction_layer` |
| _e.g. no nearby structures_ | buildings        | building footprints  | `lookup_obstruction_layer` |

## Why this approach (vs. alternatives)

_TODO:_ Justify the chosen modeling approach against alternatives (e.g. simple buffer
vs. viewshed/horizon analysis; raster sampling vs. ML obstruction detection).

## Definition of "at-risk" (for non-technical stakeholders)

_TODO:_ Plain-language definition of a location being "at risk," and what the
low/medium/high bands mean in practice.

## Known limitations vs. on-site assessment

_TODO:_ What remote/public-data analysis cannot capture that an on-site dish-pointing
assessment would (seasonal foliage, fine-scale obstructions, exact mount height/placement,
data currency/resolution).
