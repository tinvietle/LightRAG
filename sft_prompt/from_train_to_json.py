from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def iter_json_files(input_dir: Path) -> list[Path]:
    return sorted(path for path in input_dir.rglob("*.json") if path.is_file())


def read_json(json_path: Path) -> Any:
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {json_path}: {exc}") from exc


def extract_case_record(json_path: Path) -> dict[str, str] | None:
    data = read_json(json_path)

    if not isinstance(data, list) or not data:
        print(f"Warning: Expected non-empty list in {json_path}, skipping.")
        return None

    first_item = data[0]

    if not isinstance(first_item, dict):
        print(f"Warning: Expected first item to be an object in {json_path}, skipping.")
        return None

    case_text = first_item.get("full_text_cutoff", "")

    if not case_text:
        print(f"Warning: No 'full_text_cutoff' found in {json_path}, skipping.")
        return None

    return {
        "file_name": json_path.stem,
        "case_id": first_item.get("case_id", ""),
        "case_text": case_text,
        "reasoning": "",
        "answer": "",
    }


def build_sft_file(input_dir: Path, output_file: Path) -> None:
    json_files = iter_json_files(input_dir)

    records: list[dict[str, str]] = []
    skipped_count = 0

    for json_file in json_files:
        record = extract_case_record(json_file)

        if record is None:
            skipped_count += 1
            continue

        records.append(record)

    output_file.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        f"Total files: {len(json_files)}, "
        f"successfully written: {len(records)}, "
        f"skipped: {skipped_count}"
    )


def main() -> None:
    folder_path = Path("/home/tin/LightRAG/sft_prompt/fold1")
    output_file = folder_path.parent / "sft.json"

    build_sft_file(folder_path, output_file)


if __name__ == "__main__":
    main()