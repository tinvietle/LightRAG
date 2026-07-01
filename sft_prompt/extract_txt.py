#!/usr/bin/env python3
"""Extract prompt text entries from `sft.json` into per-case text files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


DEFAULT_TEXT_FIELD = "case_text"


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description="Read sft.json and write one .txt file per case into txt/."
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
        default=script_dir / "txt",
        help="Directory where .txt files will be written.",
    )
    parser.add_argument(
        "--text-field",
        default=DEFAULT_TEXT_FIELD,
        help="JSON field to extract into each text file.",
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


def write_text_files(
    rows: list[dict[str, Any]],
    output_dir: Path,
    text_field: str,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)

    written_count = 0
    for index, row in enumerate(rows, start=1):
        file_name = normalize_string(row.get("file_name")).strip()
        extracted_text = normalize_string(row.get(text_field))

        if not file_name:
            raise ValueError(f"Row {index} is missing file_name")

        if not extracted_text:
            raise ValueError(f"Row {index} is missing {text_field}")

        output_path = output_dir / f"{file_name}.txt"
        output_path.write_text(extracted_text, encoding="utf-8")
        written_count += 1

    return written_count


def main() -> int:
    args = parse_args()

    if not args.input_path.exists():
        print(f"Input file not found: {args.input_path}", file=sys.stderr)
        return 1

    try:
        rows = load_dataset(args.input_path)
        written_count = write_text_files(rows, args.output_dir, args.text_field)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        f"Wrote {written_count} text files to {args.output_dir}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
