#!/usr/bin/env python3
"""Build a SCTID -> all-ancestor-SCTIDs lookup from the UMLS SNOMED CT hierarchy.

MRHIER.RRF stores, for every SNOMED CT atom (AUI) and every context it appears
in, the full root-to-parent path of ancestor AUIs (``PTR``). MRCONSO.RRF maps
each AUI to its SCTID (``CODE``). This script joins the two, unions the ancestors
across all of a concept's atoms and paths, and writes a small SQLite table:

    anc(sctid TEXT PRIMARY KEY, ancestors TEXT)   -- space-separated SCTIDs

It reads the UMLS release read-only (default ``../umls/2026AA/META``) and writes
under the repo's gitignored ``data/`` directory.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SAB = "SNOMEDCT_US"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--meta-dir", type=Path, default=REPO_ROOT.parent / "umls/2026AA/META"
    )
    parser.add_argument(
        "--output", type=Path, default=REPO_ROOT / "data/snomed_ancestors.sqlite"
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def snomed_lines(path: Path):
    """Stream only the SNOMED rows (grep is far faster than filtering in Python)."""
    process = subprocess.Popen(
        ["grep", "-a", f"|{SAB}|", str(path)],
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1 << 20,
    )
    assert process.stdout is not None
    yield from process.stdout
    process.wait()


def main() -> int:
    args = parse_args()
    mrconso = args.meta_dir / "MRCONSO.RRF"
    mrhier = args.meta_dir / "MRHIER.RRF"
    for path in (mrconso, mrhier):
        if not path.exists():
            raise SystemExit(f"Missing UMLS file: {path}")
    if args.output.exists():
        if not args.overwrite:
            raise SystemExit(f"{args.output} exists (pass --overwrite to rebuild)")
        args.output.unlink()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    started = time.time()
    # AUI -> SCTID. Intern the SCTID strings so each concept's ancestor sets
    # share objects instead of holding millions of duplicate strings.
    aui_to_sctid: dict[str, str] = {}
    interned: dict[str, str] = {}
    for line in snomed_lines(mrconso):
        fields = line.split("|")
        if len(fields) > 13 and fields[11] == SAB:
            sctid = interned.setdefault(fields[13], fields[13])
            aui_to_sctid[fields[7]] = sctid
    print(f"AUI->SCTID: {len(aui_to_sctid):,} atoms, {len(interned):,} concepts "
          f"({time.time() - started:.0f}s)", flush=True)

    ancestors: dict[str, set[str]] = {}
    rows = missing_paths = 0
    for line in snomed_lines(mrhier):
        fields = line.split("|")
        if len(fields) < 7 or fields[4] != SAB:
            continue
        rows += 1
        sctid = aui_to_sctid.get(fields[1])
        if sctid is None:
            missing_paths += 1
            continue
        bucket = ancestors.setdefault(sctid, set())
        for aui in fields[6].split("."):
            ancestor = aui_to_sctid.get(aui)
            if ancestor is not None and ancestor != sctid:
                bucket.add(ancestor)
        if rows % 5_000_000 == 0:
            print(f"  {rows:,} hierarchy rows ({time.time() - started:.0f}s)",
                  file=sys.stderr, flush=True)
    print(f"MRHIER: {rows:,} SNOMED rows, {len(ancestors):,} concepts with a path, "
          f"{missing_paths:,} rows with an unmapped AUI ({time.time() - started:.0f}s)",
          flush=True)

    connection = sqlite3.connect(str(args.output))
    connection.execute("CREATE TABLE anc (sctid TEXT PRIMARY KEY, ancestors TEXT NOT NULL)")
    batch = []
    for sctid, bucket in ancestors.items():
        batch.append((sctid, " ".join(sorted(bucket))))
        if len(batch) >= 50_000:
            connection.executemany("INSERT INTO anc VALUES (?, ?)", batch)
            batch.clear()
    if batch:
        connection.executemany("INSERT INTO anc VALUES (?, ?)", batch)
    connection.commit()
    connection.close()
    print(f"Wrote {args.output} ({args.output.stat().st_size / 1e6:.0f} MB, "
          f"{time.time() - started:.0f}s total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
