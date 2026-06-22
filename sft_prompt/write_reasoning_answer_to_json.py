#!/usr/bin/env python3
"""Write reasoning/answer JSON text files back into `sft.json`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REASONING_KEY = "Reasoning"
ANSWER_KEY = "Answer"
DIFFERENTIAL_KEY = "Differential_diagnosis"


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description=(
            "Read JSON-formatted .txt files from reasoning_answer/ and copy their "
            f'"{REASONING_KEY}" and answer text values into sft.json.'
        )
    )
    parser.add_argument(
        "--input-path",
        type=Path,
        default=script_dir / "sft.json",
        help="Path to the source sft.json file.",
    )
    parser.add_argument(
        "--reasoning-dir",
        type=Path,
        default=script_dir / "reasoning_answer",
        help="Directory containing .txt files with JSON text payloads.",
    )
    parser.add_argument(
        "--output-path",
        type=Path,
        default=None,
        help="Optional output path. Defaults to overwriting --input-path.",
    )
    parser.add_argument(
        "--no-skip-empty",
        action="store_false",
        dest="skip_empty",
        help="Fail instead of skipping zero-byte or whitespace-only .txt files.",
    )
    parser.add_argument(
        "--fail-on-missing-row",
        action="store_true",
        help="Fail if a .txt file stem does not match any file_name in sft.json.",
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


def build_row_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row_number, row in enumerate(rows, start=1):
        file_name = normalize_string(row.get("file_name")).strip()
        if not file_name:
            raise ValueError(f"Row {row_number} is missing file_name")
        if file_name in index:
            raise ValueError(f"Duplicate file_name in sft.json: {file_name}")
        index[file_name] = row
    return index


def load_reasoning_payload(txt_path: Path) -> dict[str, Any] | None:
    raw_text = txt_path.read_text(encoding="utf-8").strip()
    if not raw_text:
        return None

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON text in {txt_path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {txt_path}")

    if REASONING_KEY not in payload:
        raise ValueError(f'Missing key "{REASONING_KEY}" in {txt_path}')
    if ANSWER_KEY not in payload and DIFFERENTIAL_KEY not in payload:
        raise ValueError(
            f'Missing key "{ANSWER_KEY}" or "{DIFFERENTIAL_KEY}" in {txt_path}'
        )

    return payload


def resolve_answer_text(payload: dict[str, Any], txt_path: Path) -> str:
    if ANSWER_KEY in payload:
        return normalize_string(payload.get(ANSWER_KEY))
    if DIFFERENTIAL_KEY in payload:
        return normalize_string(payload.get(DIFFERENTIAL_KEY))
    raise ValueError(
        f'Missing key "{ANSWER_KEY}" or "{DIFFERENTIAL_KEY}" in {txt_path}'
    )


def update_rows_from_reasoning_dir(
    rows: list[dict[str, Any]],
    reasoning_dir: Path,
    *,
    skip_empty: bool,
    fail_on_missing_row: bool,
) -> tuple[int, int, int]:
    row_index = build_row_index(rows)
    updated = 0
    skipped_empty = 0
    missing_rows = 0

    for txt_path in sorted(reasoning_dir.glob("*.txt")):
        payload = load_reasoning_payload(txt_path)
        if payload is None:
            if skip_empty:
                skipped_empty += 1
                continue
            raise ValueError(f"Empty reasoning file: {txt_path}")

        file_name = txt_path.stem
        row = row_index.get(file_name)
        if row is None:
            missing_rows += 1
            if fail_on_missing_row:
                raise ValueError(
                    f"No matching file_name in sft.json for reasoning file {txt_path.name}"
                )
            continue

        row["reasoning"] = normalize_string(payload.get(REASONING_KEY))
        row["answer"] = resolve_answer_text(payload, txt_path)
        updated += 1

    return updated, skipped_empty, missing_rows


def write_dataset(rows: list[dict[str, Any]], output_path: Path) -> None:
    output_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    output_path = args.output_path or args.input_path

    if not args.input_path.exists():
        print(f"Input file not found: {args.input_path}", file=sys.stderr)
        return 1
    if not args.reasoning_dir.exists():
        print(f"Reasoning directory not found: {args.reasoning_dir}", file=sys.stderr)
        return 1

    try:
        rows = load_dataset(args.input_path)
        updated, skipped_empty, missing_rows = update_rows_from_reasoning_dir(
            rows,
            args.reasoning_dir,
            skip_empty=args.skip_empty,
            fail_on_missing_row=args.fail_on_missing_row,
        )
        write_dataset(rows, output_path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        (
            f"Updated {updated} rows in {output_path}. "
            f"Skipped {skipped_empty} empty files. "
            f"Ignored {missing_rows} files without matching file_name."
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
