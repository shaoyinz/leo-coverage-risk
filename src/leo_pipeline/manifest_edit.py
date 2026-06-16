"""Human edits to the H1 data manifest — add / remove a dataset, or delete the file.

A1 writes ``data/interim/data_manifest.json`` for the **H1 human-review gate**, but until now
the only way to change it was to re-run the agent or hand-edit JSON. This module makes the gate
*actionable*: a reviewer can add a dataset the agent missed (e.g. a building-height source it
couldn't discover), remove one they reject, list what's there, or delete the manifest to start
over — all with the same validation/grounding rules the ``write_data_manifest`` tool enforces, so
a human edit can't put the manifest in a state A2 would choke on.

The manifest is the **single source of truth** consumed by A2: for buildings (a non-COG,
quadkey-tiled GeoParquet source) the access pattern travels in ``access_params``
(``url_template``/``quadkey_zoom``/column names), so adding OpenBuildingMap — or any other
quadkey source — here is all A2 needs to fuse it (see ``docs/DESIGN.md`` §9.3).

Examples
--------
    # See what A1 (or a prior edit) left in the manifest:
    python -m leo_pipeline.manifest_edit list

    # Add OpenBuildingMap as the selected building-height source (manifest-driven access):
    python -m leo_pipeline.manifest_edit add \\
        --factor buildings --dataset-id "OpenBuildingMap (GFZ/GEM)" --access web --selected \\
        --source-url https://www.openbuildingmap.org/ --license ODbL-1.0 \\
        --url-template "https://data.source.coop/tge-labs/openbuildingmap/building.{quadkey}.parquet" \\
        --quadkey-zoom 6 --vintage "GHSL 2023A" --rationale "per-building height; US-covered"

    # Drop a candidate the reviewer rejects, then delete the whole manifest:
    python -m leo_pipeline.manifest_edit remove --dataset-id ms-buildings
    python -m leo_pipeline.manifest_edit delete
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from leo_pipeline.config import DISCOVERY
from leo_pipeline.state import DataManifest, DatasetCandidate

# Manifest-carried access keys for a quadkey-tiled (non-COG) source; mirrors
# tools._BUILDING_ACCESS_KEYS. Collected into the entry's ``access_params``.
ACCESS_PARAM_KEYS = ("url_template", "quadkey_zoom", "geom_col", "height_col", "bbox_col")


def _manifest_path(path: str | None) -> Path:
    return Path(path) if path else DISCOVERY.manifest_path


def load_manifest(path: Path) -> dict[str, Any]:
    """Read the manifest JSON, or return an empty scaffold if the file does not exist."""
    if path.exists():
        return json.loads(path.read_text())
    return {"aoi_bbox": [], "generated_at": "", "entries": [], "notes": []}


def save_manifest(manifest: dict[str, Any], path: Path) -> None:
    """Persist the manifest dict, refreshing ``generated_at`` so edits are timestamped."""
    manifest["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, default=str))


def entry_from_args(args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    """Build one validated manifest entry from CLI args. Returns (entry_dict, notes).

    Applies the same grounding gate as ``write_data_manifest``: a selected ``access='web'``
    pick must carry a resolvable source (``source_url``/``asset_href`` or an ``url_template``
    in ``access_params``), else it is de-selected with a note rather than silently trusted.
    """
    access_params = {
        k.replace("-", "_"): getattr(args, k.replace("-", "_"))
        for k in ACCESS_PARAM_KEYS
        if getattr(args, k.replace("-", "_"), None) is not None
    }
    if "quadkey_zoom" in access_params:
        access_params["quadkey_zoom"] = int(access_params["quadkey_zoom"])

    cand = DatasetCandidate(
        factor=args.factor,
        dataset_id=args.dataset_id,
        access=args.access,
        selected=args.selected,
        rank=args.rank or 0,
        collection=args.collection,
        catalog=args.catalog,
        asset_href=args.asset_href,
        source_url=args.source_url,
        gsd_m=args.gsd_m,
        vintage=args.vintage,
        coverage_pct=args.coverage_pct,
        license=args.license,
        rationale=args.rationale or "",
        access_params=access_params or None,
    )

    notes: list[str] = []
    grounded = cand.source_url or cand.asset_href or (access_params.get("url_template"))
    if cand.access == "web" and cand.selected and not grounded:
        cand.selected = False
        notes.append(
            f"de-selected ungrounded web pick {cand.dataset_id!r} ({cand.factor}): "
            "no source_url/asset_href/url_template"
        )
    return dataclasses.asdict(cand), notes


def cmd_list(args: argparse.Namespace) -> int:
    path = _manifest_path(args.path)
    if not path.exists():
        print(f"No manifest at {path}")
        return 1
    manifest = load_manifest(path)
    entries = manifest.get("entries", [])
    print(f"{path}  (aoi_bbox={manifest.get('aoi_bbox')}, {len(entries)} entries)")
    for e in entries:
        mark = "✓" if e.get("selected") else "·"
        src = e.get("source_url") or e.get("asset_href") or ""
        ap = e.get("access_params") or {}
        tmpl = f"  template={ap['url_template']}" if ap.get("url_template") else ""
        print(
            f"  {mark} [{e.get('factor'):9}] {e.get('dataset_id')}  "
            f"({e.get('access')}; {e.get('license') or 'license?'}){(' ' + src) if src else ''}{tmpl}"
        )
    for n in manifest.get("notes", []):
        print(f"  note: {n}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    path = _manifest_path(args.path)
    manifest = load_manifest(path)
    if not manifest.get("aoi_bbox") and args.bbox:
        manifest["aoi_bbox"] = list(args.bbox)

    entry, notes = entry_from_args(args)
    entries = manifest.setdefault("entries", [])
    # Replace an existing same-(factor, dataset_id) entry rather than duplicating it.
    entries[:] = [
        e
        for e in entries
        if not (e.get("factor") == entry["factor"] and e.get("dataset_id") == entry["dataset_id"])
    ]
    entries.append(entry)
    manifest.setdefault("notes", []).extend(notes)
    save_manifest(manifest, path)

    print(f"Added [{entry['factor']}] {entry['dataset_id']} (selected={entry['selected']}) → {path}")
    for n in notes:
        print(f"  note: {n}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    path = _manifest_path(args.path)
    if not path.exists():
        print(f"No manifest at {path}")
        return 1
    manifest = load_manifest(path)
    before = manifest.get("entries", [])
    kept = [
        e
        for e in before
        if not (
            e.get("dataset_id") == args.dataset_id
            and (args.factor is None or e.get("factor") == args.factor)
        )
    ]
    removed = len(before) - len(kept)
    if not removed:
        print(f"No entry matched dataset_id={args.dataset_id!r}"
              + (f" factor={args.factor!r}" if args.factor else ""))
        return 1
    manifest["entries"] = kept
    save_manifest(manifest, path)
    print(f"Removed {removed} entr{'y' if removed == 1 else 'ies'} → {path}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    path = _manifest_path(args.path)
    if not path.exists():
        print(f"No manifest at {path} (nothing to delete)")
        return 1
    path.unlink()
    print(f"Deleted {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="leo-manifest",
        description="Human edits to the H1 data manifest: list / add / remove / delete.",
    )
    p.add_argument("--path", help="manifest path (default: config DISCOVERY.manifest_path)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show the manifest entries").set_defaults(func=cmd_list)

    a = sub.add_parser("add", help="add (or replace) a dataset entry")
    a.add_argument("--factor", required=True,
                   choices=["terrain", "surface", "canopy", "buildings"])
    a.add_argument("--dataset-id", required=True, help="collection id or dataset name")
    a.add_argument("--access", default="web", choices=["stac", "web"])
    sel = a.add_mutually_exclusive_group()
    sel.add_argument("--selected", dest="selected", action="store_true", default=True)
    sel.add_argument("--no-selected", dest="selected", action="store_false")
    a.add_argument("--rank", type=int, default=0)
    a.add_argument("--collection")
    a.add_argument("--catalog")
    a.add_argument("--asset-href")
    a.add_argument("--source-url")
    a.add_argument("--gsd-m", type=float)
    a.add_argument("--vintage")
    a.add_argument("--coverage-pct", type=float)
    a.add_argument("--license")
    a.add_argument("--rationale")
    a.add_argument("--bbox", nargs=4, type=float, metavar=("MINLON", "MINLAT", "MAXLON", "MAXLAT"),
                   help="set aoi_bbox when creating a new manifest")
    # quadkey-tiled (non-COG) access params -> access_params
    a.add_argument("--url-template", dest="url_template",
                   help="tiled-source URL with a {quadkey} placeholder (e.g. OpenBuildingMap)")
    a.add_argument("--quadkey-zoom", dest="quadkey_zoom", type=int)
    a.add_argument("--geom-col", dest="geom_col")
    a.add_argument("--height-col", dest="height_col")
    a.add_argument("--bbox-col", dest="bbox_col")
    a.set_defaults(func=cmd_add)

    r = sub.add_parser("remove", help="remove dataset entr(y/ies) by id")
    r.add_argument("--dataset-id", required=True)
    r.add_argument("--factor", choices=["terrain", "surface", "canopy", "buildings"],
                   help="restrict removal to this factor")
    r.set_defaults(func=cmd_remove)

    sub.add_parser("delete", help="delete the whole manifest file").set_defaults(func=cmd_delete)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
