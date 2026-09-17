#!/usr/bin/env python3
"""Measure what the second (gleaning) extraction call adds to a LightRAG graph."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Any

import json_repair

from lightrag.utils import sanitize_and_normalize_extracted_text


GLEAN_PROMPT_PREFIX = "---Task---\nBased on the last extraction task"
EXPECTED_TYPES = {
    "anatomical_location",
    "biological_structure",
    "clinical_event",
    "date",
    "diagnostic_procedure",
    "disease_disorder",
    "lab_test",
    "lab_value",
    "medication",
    "organism",
    "other",
    "pathogen",
    "sign_symptom",
    "therapeutic_procedure",
    "transmission_vector",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage", type=Path, default=Path("data/rag_storage"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/gleaning/data_gleaning_evaluation.json"),
    )
    parser.add_argument(
        "--umls-evaluation",
        type=Path,
        default=Path(
            "artifacts/umls/data_entity_normalization_evaluation.jsonl"
        ),
    )
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"Missing input: {path}") from error


def parse_response(response: str) -> tuple[dict[str, dict[str, str]], set[tuple[str, str]]]:
    """Parse the same useful fields that LightRAG keeps from JSON extraction."""

    parsed = json_repair.loads(response.strip().removeprefix("```json").removesuffix("```").strip())
    if not isinstance(parsed, dict):
        return {}, set()

    entities: dict[str, dict[str, str]] = {}
    for raw in parsed.get("entities", []):
        if not isinstance(raw, dict):
            continue
        name = sanitize_and_normalize_extracted_text(
            str(raw.get("name", "")), remove_inner_quotes=True
        )
        entity_type = sanitize_and_normalize_extracted_text(str(raw.get("type", "")))
        description = sanitize_and_normalize_extracted_text(
            str(raw.get("description", ""))
        )
        if name and entity_type and description:
            entities[name] = {
                "type": entity_type.replace(" ", "").lower(),
                "description": description,
            }

    relationships: set[tuple[str, str]] = set()
    for raw in parsed.get("relationships", []):
        if not isinstance(raw, dict):
            continue
        source = sanitize_and_normalize_extracted_text(
            str(raw.get("source", "")), remove_inner_quotes=True
        )
        target = sanitize_and_normalize_extracted_text(
            str(raw.get("target", "")), remove_inner_quotes=True
        )
        description = sanitize_and_normalize_extracted_text(
            str(raw.get("description", ""))
        )
        if source and target and source != target and description:
            relationships.add(tuple(sorted((source, target))))
    return entities, relationships


def is_grounded(name: str, chunk_text: str) -> bool:
    """Conservative evidence check: the entity name occurs literally, ignoring case."""

    return name.casefold() in chunk_text.casefold()


def percentage(numerator: int, denominator: int) -> float:
    return round(100 * numerator / denominator, 2) if denominator else 0.0


def umls_metrics(path: Path, names: set[str]) -> dict[str, float | int] | None:
    """Use the existing QuickUMLS run as an independent vocabulary signal."""

    if not path.exists():
        return None
    matching_rows = []
    with path.open(encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            if row.get("entity_name") in names:
                matching_rows.append(row)
    return {
        "evaluated_names": len(matching_rows),
        "matched_any_percent": percentage(
            sum(row.get("matched_any", False) for row in matching_rows),
            len(matching_rows),
        ),
        "full_span_match_percent": percentage(
            sum(row.get("has_full_span_match", False) for row in matching_rows),
            len(matching_rows),
        ),
        "safe_unique_cui_percent": percentage(
            sum(row.get("auto_lexical_candidate", False) for row in matching_rows),
            len(matching_rows),
        ),
    }


def main() -> int:
    args = parse_args()
    cache = load_json(args.storage / "kv_store_llm_response_cache.json")
    chunks = load_json(args.storage / "kv_store_text_chunks.json")
    final_entities = load_json(args.storage / "kv_store_entity_chunks.json")
    final_relations = load_json(args.storage / "kv_store_relation_chunks.json")

    responses_by_chunk: dict[str, dict[str, str]] = defaultdict(dict)
    for record in cache.values():
        if not isinstance(record, dict) or record.get("cache_type") != "extract":
            continue
        chunk_id = record.get("chunk_id")
        prompt = record.get("original_prompt", "")
        if not chunk_id:
            continue
        stage = "glean" if prompt.startswith(GLEAN_PROMPT_PREFIX) else "initial"
        responses_by_chunk[chunk_id][stage] = record.get("return", "")

    initial_occurrences = glean_occurrences = 0
    glean_new_occurrences = glean_repeated_occurrences = 0
    glean_corrections = 0
    initial_grounded = glean_new_grounded = 0
    glean_new_survived = 0
    initial_relation_occurrences = glean_relation_occurrences = 0
    glean_new_relation_occurrences = glean_new_relations_survived = 0
    initial_global_names: set[str] = set()
    glean_new_global_names: set[str] = set()
    glean_type_counts: Counter[str] = Counter()
    initial_nonstandard_types = glean_new_nonstandard_types = 0
    initial_entity_count_distribution: Counter[int] = Counter()
    glean_entity_count_distribution: Counter[int] = Counter()
    ungrounded_examples: list[dict[str, str]] = []
    parse_failures = 0

    for chunk_id, stages in responses_by_chunk.items():
        if "initial" not in stages or "glean" not in stages:
            continue
        try:
            initial_entities, initial_relations = parse_response(stages["initial"])
            glean_entities, glean_relations = parse_response(stages["glean"])
        except (ValueError, TypeError):
            parse_failures += 1
            continue

        chunk_text = str(chunks.get(chunk_id, {}).get("content", ""))
        initial_occurrences += len(initial_entities)
        glean_occurrences += len(glean_entities)
        initial_entity_count_distribution[len(initial_entities)] += 1
        glean_entity_count_distribution[len(glean_entities)] += 1
        initial_relation_occurrences += len(initial_relations)
        glean_relation_occurrences += len(glean_relations)
        initial_global_names.update(initial_entities)
        initial_nonstandard_types += sum(
            data["type"] not in EXPECTED_TYPES for data in initial_entities.values()
        )
        initial_grounded += sum(
            is_grounded(name, chunk_text) for name in initial_entities
        )

        for name, data in glean_entities.items():
            if name in initial_entities:
                glean_repeated_occurrences += 1
                if len(data["description"]) > len(initial_entities[name]["description"]):
                    glean_corrections += 1
                continue
            glean_new_occurrences += 1
            glean_new_global_names.add(name)
            glean_type_counts[data["type"]] += 1
            glean_new_nonstandard_types += data["type"] not in EXPECTED_TYPES
            grounded = is_grounded(name, chunk_text)
            glean_new_grounded += grounded
            final_chunks = final_entities.get(name, {}).get("chunk_ids", [])
            glean_new_survived += chunk_id in final_chunks
            if not grounded and len(ungrounded_examples) < 30:
                ungrounded_examples.append(
                    {"chunk_id": chunk_id, "entity": name, "type": data["type"]}
                )

        new_relations = glean_relations - initial_relations
        glean_new_relation_occurrences += len(new_relations)
        for source, target in new_relations:
            key = f"{source}<SEP>{target}"
            final_chunks = final_relations.get(key, {}).get("chunk_ids", [])
            glean_new_relations_survived += chunk_id in final_chunks

    exclusive_glean_names = glean_new_global_names - initial_global_names
    exclusive_glean_names_in_graph = exclusive_glean_names & set(final_entities)
    paired_chunks = sum(
        "initial" in stages and "glean" in stages
        for stages in responses_by_chunk.values()
    )
    summary = {
        "dataset": str(args.storage),
        "paired_chunks": paired_chunks,
        "parse_failures": parse_failures,
        "entities": {
            "initial_occurrences": initial_occurrences,
            "initial_unique_names": len(initial_global_names),
            "glean_output_occurrences": glean_occurrences,
            "glean_new_occurrences": glean_new_occurrences,
            "glean_repeated_occurrences": glean_repeated_occurrences,
            "glean_longer_description_corrections": glean_corrections,
            "additional_occurrences_percent": percentage(
                glean_new_occurrences, initial_occurrences
            ),
            "mean_initial_entities_per_chunk": round(
                initial_occurrences / paired_chunks, 2
            ),
            "mean_new_glean_entities_per_chunk": round(
                glean_new_occurrences / paired_chunks, 2
            ),
            "chunks_where_initial_reached_40_entity_cap": (
                initial_entity_count_distribution[40]
            ),
            "chunks_where_glean_returned_no_entities": (
                glean_entity_count_distribution[0]
            ),
            "initial_literal_grounding_percent": percentage(
                initial_grounded, initial_occurrences
            ),
            "glean_new_literal_grounding_percent": percentage(
                glean_new_grounded, glean_new_occurrences
            ),
            "glean_new_same_chunk_survival_percent": percentage(
                glean_new_survived, glean_new_occurrences
            ),
            "globally_unique_names_only_introduced_by_gleaning": len(
                exclusive_glean_names
            ),
            "exclusive_glean_names_present_in_final_graph": len(
                exclusive_glean_names_in_graph
            ),
            "exclusive_glean_share_of_final_vocabulary_percent": percentage(
                len(exclusive_glean_names_in_graph), len(final_entities)
            ),
            "initial_nonstandard_type_percent": percentage(
                initial_nonstandard_types, initial_occurrences
            ),
            "glean_new_nonstandard_type_percent": percentage(
                glean_new_nonstandard_types, glean_new_occurrences
            ),
            "glean_new_type_counts": glean_type_counts.most_common(),
        },
        "relationships": {
            "initial_occurrences": initial_relation_occurrences,
            "glean_output_occurrences": glean_relation_occurrences,
            "glean_new_occurrences": glean_new_relation_occurrences,
            "additional_occurrences_percent": percentage(
                glean_new_relation_occurrences, initial_relation_occurrences
            ),
            "glean_new_same_chunk_survival_percent": percentage(
                glean_new_relations_survived, glean_new_relation_occurrences
            ),
        },
        "quickumls_vocabulary_signal": {
            "initial_unique_names": umls_metrics(
                args.umls_evaluation, initial_global_names
            ),
            "names_exclusive_to_gleaning": umls_metrics(
                args.umls_evaluation, exclusive_glean_names
            ),
        },
        "ungrounded_glean_examples": ungrounded_examples,
        "interpretation_note": (
            "Literal grounding is conservative: valid abbreviations, synonyms, and "
            "reasonable inferences may fail it. Survival proves ingestion, not correctness."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        raise SystemExit(f"error: {error}") from error
