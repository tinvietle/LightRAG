#!/usr/bin/env python3
"""Build an SFT prompt JSON dataset from case JSON files and LightRAG `/query/data`."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request


CSV_HEADERS = [
    "id",
    "file_name",
    "case_id",
    "case_text",
    "sub_folder",
    "context(entity)",
    "context(relation)",
    "context(chunks)",
    "full_prompt",
    "reasoning",
    "answer",
]

DEFAULT_SEPARATOR = "=" * 10


@dataclass(slots=True)
class BuildConfig:
    input_dir: Path
    output_path: Path
    base_url: str
    api_key: str | None
    mode: str
    top_k: int | None
    chunk_top_k: int | None
    timeout: float
    limit: int | None
    resume: bool
    overwrite: bool


class QueryDataError(RuntimeError):
    """Raised when the `/query/data` request fails."""


def parse_args() -> BuildConfig:
    script_dir = Path(__file__).resolve().parent
    default_input_dir = script_dir / "fold1" / "train"
    default_output = script_dir / "sft.json"

    parser = argparse.ArgumentParser(
        description=(
            "Read case JSON files from a training folder, fetch LightRAG `/query/data` "
            "for each case, "
            "and write a JSON dataset."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=default_input_dir,
        help="Path to the source training folder.",
    )
    parser.add_argument(
        "--output-path",
        type=Path,
        default=default_output,
        help="Path to the output JSON file.",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:9621",
        help="LightRAG API base URL.",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Optional `X-API-Key` value for the REST API.",
    )
    parser.add_argument(
        "--mode",
        default="hybrid",
        help="Query mode sent to `/query/data`.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=20,
        help="Optional `top_k` value sent to `/query/data`.",
    )
    parser.add_argument(
        "--chunk-top-k",
        type=int,
        default=10,
        help="Optional `chunk_top_k` value sent to `/query/data`.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Request timeout in seconds.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional row limit for partial runs.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_false",
        dest="resume",
        help="Do not resume from an existing output JSON file.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow overwriting an existing output JSON file.",
    )
    args = parser.parse_args()

    return BuildConfig(
        input_dir=args.input_dir,
        output_path=args.output_path,
        base_url=args.base_url.rstrip("/"),
        api_key=args.api_key,
        mode=args.mode,
        top_k=args.top_k,
        chunk_top_k=args.chunk_top_k,
        timeout=args.timeout,
        limit=args.limit,
        resume=args.resume,
        overwrite=args.overwrite,
    )


def load_rows(input_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    for json_path in iter_case_files(input_dir):
        row = load_case_row(json_path, input_dir)
        if row is None or is_empty_row(row):
            continue
        row["id"] = str(len(rows) + 1)
        rows.append(row)

    return rows


def iter_case_files(input_dir: Path) -> list[Path]:
    return sorted(path for path in input_dir.rglob("*.json") if path.is_file())


def load_case_row(
    json_path: Path,
    input_dir: Path,
) -> dict[str, str] | None:
    try:
        raw_value = json.loads(json_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {json_path}: {exc}") from exc

    case_record = extract_case_record(raw_value, json_path)
    case_id = normalize_string(case_record.get("case_id"))
    case_text = normalize_string(case_record.get("full_text_cutoff"))
    if not case_text:
        return None

    disease_name = resolve_disease_name(json_path, input_dir)

    row = {header: "" for header in CSV_HEADERS}
    row["file_name"] = json_path.stem
    row["case_id"] = case_id
    row["case_text"] = case_text
    row["sub_folder"] = disease_name
    return row


def extract_case_record(raw_value: Any, json_path: Path) -> dict[str, Any]:
    if isinstance(raw_value, list):
        if not raw_value:
            raise ValueError(f"No case records found in {json_path}")
        record = raw_value[0]
    else:
        record = raw_value

    if not isinstance(record, dict):
        raise ValueError(f"Expected a JSON object in {json_path}")

    return record


def resolve_disease_name(json_path: Path, input_dir: Path) -> str:
    relative_parts = json_path.relative_to(input_dir).parts
    return relative_parts[0] if relative_parts else ""


def normalize_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def is_empty_row(row: dict[str, str]) -> bool:
    return not any(value for value in row.values())


def fetch_query_data(case_text: str, config: BuildConfig) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query": case_text,
        "mode": config.mode
    }
    if config.top_k is not None:
        payload["top_k"] = config.top_k
    if config.chunk_top_k is not None:
        payload["chunk_top_k"] = config.chunk_top_k

    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["X-API-Key"] = config.api_key

    http_request = request.Request(
        f"{config.base_url}/query/data",
        data=body,
        headers=headers,
        method="POST",
    )

    try:
        with request.urlopen(http_request, timeout=config.timeout) as response:
            response_body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise QueryDataError(
            f"HTTP {exc.code} from /query/data: {response_body}"
        ) from exc
    except error.URLError as exc:
        raise QueryDataError(f"Unable to reach /query/data: {exc.reason}") from exc

    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise QueryDataError("Received non-JSON response from /query/data") from exc

    status = parsed.get("status")
    if status != "success":
        message = parsed.get("message", "Unknown API failure")
        raise QueryDataError(f"/query/data returned status={status!r}: {message}")

    return parsed


def json_block(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def build_full_prompt(
    case_text: str,
    context_entity: str,
    context_relation: str,
    context_chunks: str,
) -> str:
    sections = [
        ("Case Text", case_text),
        ("Knowledge Graph Entity", context_entity),
        ("Knowledge Graph Relation", context_relation),
        ("Knowledge Graph Related Chunks", context_chunks),
    ]

    rendered_sections: list[str] = []
    for index, (title, content) in enumerate(sections):
        rendered_sections.append(f"{title}:\n{content}")
        if index != len(sections) - 1:
            rendered_sections.append(DEFAULT_SEPARATOR)
    return "\n".join(rendered_sections)


def build_output_row(
    source_row: dict[str, str],
    api_result: dict[str, Any] | None,
) -> dict[str, Any]:
    data = api_result.get("data", {}) if api_result else {}
    entities = data.get("entities", [])
    relationships = data.get("relationships", [])
    chunks = data.get("chunks", [])

    context_entity = json_block(entities)
    context_relation = json_block(relationships)
    context_chunks = json_block(chunks)
    case_text = source_row.get("case_text", "")

    output_row: dict[str, Any] = {header: source_row.get(header, "") for header in CSV_HEADERS}
    output_row["context(entity)"] = context_entity
    output_row["context(relation)"] = context_relation
    output_row["context(chunks)"] = context_chunks
    output_row["full_prompt"] = (
        build_full_prompt(
            case_text=case_text,
            context_entity=context_entity,
            context_relation=context_relation,
            context_chunks=context_chunks,
        )
        if case_text
        else ""
    )
    output_row["reasoning"] = ""
    output_row["answer"] = ""
    return output_row


def row_key(row: dict[str, Any]) -> tuple[str, str]:
    return (
        normalize_string(row.get("file_name")),
        normalize_string(row.get("case_id")),
    )


def assign_row_ids(rows: list[dict[str, Any]]) -> None:
    for index, row in enumerate(rows, start=1):
        row["id"] = str(index)


def validate_headers(rows: list[dict[str, str]]) -> None:
    if not rows:
        raise ValueError("No case JSON files were found in the input folder.")

    missing_headers = [header for header in CSV_HEADERS if header not in rows[0]]
    if missing_headers:
        missing = ", ".join(missing_headers)
        raise ValueError(f"Generated rows are missing required headers: {missing}")


def load_existing_output(config: BuildConfig) -> list[dict[str, Any]]:
    if config.overwrite:
        return []

    if not config.output_path.exists():
        return []

    if not config.resume:
        raise FileExistsError(
            f"Output file already exists: {config.output_path}. "
            "Use --overwrite to replace it or omit --no-resume to continue."
        )

    try:
        parsed = json.loads(config.output_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Existing output is not valid JSON: {config.output_path}"
        ) from exc

    if not isinstance(parsed, list):
        raise ValueError(f"Existing output must be a JSON array: {config.output_path}")

    rows: list[dict[str, Any]] = []
    for index, item in enumerate(parsed, start=1):
        if not isinstance(item, dict):
            raise ValueError(
                f"Existing output row {index} is not a JSON object: {config.output_path}"
            )
        rows.append(dict(item))

    assign_row_ids(rows)
    return rows


def write_output(output_path: Path, rows: list[dict[str, Any]]) -> None:
    assign_row_ids(rows)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(output_path)


def build_dataset(config: BuildConfig) -> list[dict[str, Any]]:
    rows = load_rows(config.input_dir)
    validate_headers(rows)

    dataset = load_existing_output(config)
    completed_keys = {row_key(row) for row in dataset}
    processed_rows = rows[: config.limit] if config.limit is not None else rows

    for index, row in enumerate(processed_rows, start=1):
        current_key = row_key(row)
        if current_key in completed_keys:
            print(
                f"[{index}/{len(processed_rows)}] Skipping existing case_id={row.get('case_id', '')}",
                file=sys.stderr,
            )
            continue

        case_text = row.get("case_text", "")
        api_result: dict[str, Any] | None = None

        if case_text:
            print(
                f"[{index}/{len(processed_rows)}] Fetching context for case_id={row.get('case_id', '')}",
                file=sys.stderr,
            )
            api_result = fetch_query_data(case_text, config)

        dataset.append(build_output_row(row, api_result))
        completed_keys.add(current_key)
        write_output(config.output_path, dataset)

    return dataset


def main() -> int:
    config = parse_args()

    if not config.input_dir.exists():
        print(f"Input folder not found: {config.input_dir}", file=sys.stderr)
        return 1

    if not config.input_dir.is_dir():
        print(f"Input path is not a folder: {config.input_dir}", file=sys.stderr)
        return 1

    try:
        dataset = build_dataset(config)
    except (FileExistsError, QueryDataError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        f"Wrote {len(dataset)} rows to {config.output_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
