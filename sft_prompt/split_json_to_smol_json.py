#!/usr/bin/env python3
"""Split `sft.json` into one JSON file per row."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description="Read sft.json and write one JSON object per file into smol_json/."
    )
    parser.add_argument(
        "--input-path",
        type=Path,
        default=script_dir / "sft.json",
        help="Path to the source sft.json file.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=script_dir / "smol_json",
        help="Directory where per-row JSON files will be written.",
    )
    return parser.parse_args()


def load_dataset(input_path: Path) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {input_path}: {exc}") from exc

    if not isinstance(parsed, list):
        raise ValueError(f"Expected a JSON array in {input_path}")

    rows: list[dict[str, Any]] = []
    for index, item in enumerate(parsed, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Row {index} in {input_path} is not a JSON object")
        rows.append(item)
    return rows


def normalize_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def write_split_files(rows: list[dict[str, Any]], output_dir: Path) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    seen_names: set[str] = set()
    for index, row in enumerate(rows, start=1):
        file_name = normalize_string(row.get("file_name")).strip()
        if not file_name:
            raise ValueError(f"Row {index} is missing file_name")
        if file_name in seen_names:
            raise ValueError(f"Duplicate file_name in dataset: {file_name}")
        
        reasoning = normalize_string(row.get("reasoning")).strip()
        answer = normalize_string(row.get("answer")).strip()
        
        if not reasoning and not answer:
            print(f"Warning: Row {index} ({file_name}) has empty reasoning and answer fields, skipping.")
            continue

        output_path = output_dir / f"{file_name}.json"
        output_path.write_text(
            json.dumps(row, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        seen_names.add(file_name)
        written += 1

    return written


def main() -> int:
    args = parse_args()

    if not args.input_path.exists():
        print(f"Input file not found: {args.input_path}", file=sys.stderr)
        return 1

    try:
        rows = load_dataset(args.input_path)
        written = write_split_files(rows, args.output_dir)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"Wrote {written} JSON files to {args.output_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
