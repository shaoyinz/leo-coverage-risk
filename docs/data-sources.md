# Data sourcing & quality

> Status: scaffold. Mirrors the "Data Sourcing & Quality" deliverable.

## Dataset selection (linked to obstruction factors)

_TODO:_ For each candidate public geospatial dataset, record source, resolution,
coverage, licence, currency, and which obstruction factor it models.

| Dataset | Models | Resolution / coverage | Source / licence |
|---------|--------|-----------------------|------------------|
| _e.g. ETH/Meta canopy height_ | tree canopy | ~1 m / global | _link_ |
| _e.g. Copernicus / USGS DEM_   | terrain     | 30 m / global | _link_ |
| _e.g. OSM / Overture buildings_ | structures | varies        | _link_ |

## Quality issues in the provided locations CSV

_TODO:_ Populate from `notebooks/00_data_inspection.ipynb`. Track counts:

- Null / missing coordinates
- Out-of-range lat/lon
- Duplicate coordinates
- Points in implausible locations (ocean, null island 0,0)
- Encoding / dtype problems

## What public data cannot model

_TODO:_ Obstruction factors that public datasets miss (exact mount height/placement,
seasonal foliage variation, fine-scale or recent changes, indoor/roofline specifics).
