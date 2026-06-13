#!/usr/bin/env python3
"""Build an SFT prompt JSON dataset from the CSV source and LightRAG `/query/data`."""

from __future__ import annotations

import argparse
import csv
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
    csv_path: Path
    output_path: Path
    base_url: str
    api_key: str | None
    mode: str
    top_k: int | None
    chunk_top_k: int | None
    timeout: float
    limit: int | None
    overwrite: bool


class QueryDataError(RuntimeError):
    """Raised when the `/query/data` request fails."""


def parse_args() -> BuildConfig:
    script_dir = Path(__file__).resolve().parent
    default_csv = resolve_default_csv(script_dir)
    default_output = script_dir / "sft.json"

    parser = argparse.ArgumentParser(
        description=(
            "Read the SFT CSV, fetch LightRAG `/query/data` for each case, "
            "and write a JSON dataset."
        )
    )
    parser.add_argument(
        "--csv-path",
        type=Path,
        default=default_csv,
        help="Path to the source CSV file.",
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
        "--overwrite",
        action="store_true",
        help="Allow overwriting an existing output JSON file.",
    )
    args = parser.parse_args()

    return BuildConfig(
        csv_path=args.csv_path,
        output_path=args.output_path,
        base_url=args.base_url.rstrip("/"),
        api_key=args.api_key,
        mode=args.mode,
        top_k=args.top_k,
        chunk_top_k=args.chunk_top_k,
        timeout=args.timeout,
        limit=args.limit,
        overwrite=args.overwrite,
    )


def resolve_default_csv(script_dir: Path) -> Path:
    csv_candidates = [script_dir / "sft.csv", script_dir / "sft .csv"]
    for candidate in csv_candidates:
        if candidate.exists():
            return candidate
    return csv_candidates[0]


def load_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        rows: list[dict[str, str]] = []
        for row in reader:
            normalized_row = {
                (key or "").strip(): (value or "").strip()
                for key, value in row.items()
            }
            if is_empty_row(normalized_row):
                continue
            rows.append(normalized_row)
    return rows


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


def validate_headers(rows: list[dict[str, str]]) -> None:
    if not rows:
        return

    missing_headers = [header for header in CSV_HEADERS if header not in rows[0]]
    if missing_headers:
        missing = ", ".join(missing_headers)
        raise ValueError(f"CSV is missing required headers: {missing}")


def write_output(output_path: Path, rows: list[dict[str, Any]], overwrite: bool) -> None:
    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"Output file already exists: {output_path}. Use --overwrite to replace it."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_dataset(config: BuildConfig) -> list[dict[str, Any]]:
    rows = load_rows(config.csv_path)
    validate_headers(rows)

    dataset: list[dict[str, Any]] = []
    processed_rows = rows[: config.limit] if config.limit is not None else rows

    for index, row in enumerate(processed_rows, start=1):
        case_text = row.get("case_text", "")
        api_result: dict[str, Any] | None = None

        if case_text:
            print(
                f"[{index}/{len(processed_rows)}] Fetching context for case_id={row.get('case_id', '')}",
                file=sys.stderr,
            )
            api_result = fetch_query_data(case_text, config)

        dataset.append(build_output_row(row, api_result))

    return dataset


def main() -> int:
    config = parse_args()

    if not config.csv_path.exists():
        print(f"CSV file not found: {config.csv_path}", file=sys.stderr)
        return 1

    try:
        dataset = build_dataset(config)
        write_output(config.output_path, dataset, overwrite=config.overwrite)
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
