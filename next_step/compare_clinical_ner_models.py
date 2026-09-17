#!/usr/bin/env python3
"""Compare span-based GLiNER with biomedical token-classification NER models.

The cases below are short adaptations of publicly available clinical case
reports. They are deliberately not gold-standard NER annotations: target
phrases provide a small, transparent check of multiword entity coverage.

Examples:
    python next_step/compare_clinical_ner_models.py --models gliner
    python next_step/compare_clinical_ner_models.py --models all
    python next_step/compare_clinical_ner_models.py --models all --json
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable


GLINER_MODEL = "Ihor/gliner-biomed-base-v1.0"
TOKEN_MODELS = {
    "biomedical-ner-all": "d4data/biomedical-ner-all",
    "biomedbert-bc5cdr": "Glasgow-AI4BioMed/bioner_bc5cdr",
}
GLINER_LABELS = [
    "Disease",
    "Sign or symptom",
    "Drug",
    "Treatment",
    "Diagnostic procedure",
    "Laboratory test",
    "Laboratory value",
    "Anatomical structure",
    "Pathogen",
    "Demographic information",
]


@dataclass(frozen=True, slots=True)
class ClinicalCase:
    name: str
    source: str
    text: str
    target_phrases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Entity:
    text: str
    label: str
    score: float
    start: int
    end: int


CASES = (
    ClinicalCase(
        name="secondary dengue with severe thrombocytopenia",
        source="https://pmc.ncbi.nlm.nih.gov/articles/PMC6040507/",
        text=(
            "A 30-year-old woman with severe dengue developed oral and gingival "
            "bleeding and a platelet count of 17 × 10^9/L. Repeated testing "
            "confirmed dengue infection with life-threatening thrombocytopenia. "
            "Suspected immune thrombocytopenic purpura was treated with "
            "intravenous immunoglobulin after platelet transfusions failed."
        ),
        target_phrases=(
            "severe dengue",
            "life-threatening thrombocytopenia",
            "dengue infection",
            "immune thrombocytopenic purpura",
            "intravenous immunoglobulin",
            "platelet transfusions",
        ),
    ),
    ClinicalCase(
        name="metoclopramide-triggered pheochromocytoma crisis",
        source="https://pmc.ncbi.nlm.nih.gov/articles/PMC9624996/",
        text=(
            "A 41-year-old woman with primary hypothyroidism and hypertension "
            "received intravenous metoclopramide for a severe headache. She then "
            "developed generalized tonic-clonic seizures and a hypertensive crisis. "
            "Computed tomography demonstrated a large right adrenal mass, and "
            "biochemical testing supported pheochromocytoma multisystem crisis."
        ),
        target_phrases=(
            "primary hypothyroidism",
            "intravenous metoclopramide",
            "generalized tonic-clonic seizures",
            "hypertensive crisis",
            "computed tomography",
            "large right adrenal mass",
            "pheochromocytoma multisystem crisis",
        ),
    ),
    ClinicalCase(
        name="dengue-associated thrombotic thrombocytopenic purpura",
        source="https://pmc.ncbi.nlm.nih.gov/articles/PMC5416791/",
        text=(
            "A patient with dengue viral infection developed fever, "
            "microangiopathic hemolytic anemia, thrombocytopenia, impaired "
            "consciousness, hemiparesis, and seizures. The presentation suggested "
            "thrombotic thrombocytopenic purpura and improved after plasma exchange "
            "therapy, corticosteroid therapy, and rituximab injection."
        ),
        target_phrases=(
            "dengue viral infection",
            "microangiopathic hemolytic anemia",
            "thrombotic thrombocytopenic purpura",
            "plasma exchange therapy",
            "corticosteroid therapy",
            "rituximab injection",
        ),
    ),
)


def normalize_text(text: str) -> str:
    return " ".join(text.casefold().split())


def deduplicate_entities(entities: list[Entity]) -> list[Entity]:
    """Keep the highest-confidence instance of each normalized surface form."""
    best: dict[str, Entity] = {}
    for entity in entities:
        key = normalize_text(entity.text)
        if key and (key not in best or entity.score > best[key].score):
            best[key] = entity
    return sorted(best.values(), key=lambda entity: (entity.start, entity.end))


def run_gliner(case: ClinicalCase, threshold: float) -> list[Entity]:
    from gliner import GLiNER

    model = get_cached_model(
        f"gliner:{GLINER_MODEL}",
        lambda: GLiNER.from_pretrained(GLINER_MODEL, cache_dir="./ner_model"),
    )
    predictions = model.predict_entities(
        case.text,
        GLINER_LABELS,
        threshold=threshold,
        flat_ner=True,
    )
    return deduplicate_entities(
        [
            Entity(
                text=item["text"],
                label=item["label"],
                score=float(item["score"]),
                start=int(item["start"]),
                end=int(item["end"]),
            )
            for item in predictions
        ]
    )


def run_token_classifier(
    case: ClinicalCase, model_name: str, threshold: float
) -> list[Entity]:
    import torch
    from transformers import pipeline

    ner_pipeline = get_cached_model(
        f"transformers:{model_name}",
        lambda: pipeline(
            "token-classification",
            model=model_name,
            aggregation_strategy="simple",
            device=0 if torch.cuda.is_available() else -1,
        ),
    )
    predictions = ner_pipeline(case.text)
    return deduplicate_entities(
        [
            Entity(
                text=item["word"],
                label=item["entity_group"],
                score=float(item["score"]),
                start=int(item["start"]),
                end=int(item["end"]),
            )
            for item in predictions
            if float(item["score"]) >= threshold
        ]
    )


_MODEL_CACHE: dict[str, Any] = {}


def get_cached_model(key: str, loader: Callable[[], Any]) -> Any:
    if key not in _MODEL_CACHE:
        print(f"Loading {key} ...", flush=True)
        _MODEL_CACHE[key] = loader()
    return _MODEL_CACHE[key]


def target_is_covered(target: str, entities: list[Entity]) -> bool:
    normalized_target = normalize_text(target)
    return any(normalize_text(entity.text) == normalized_target for entity in entities)


def count_overlaps(entities: list[Entity]) -> int:
    return sum(
        first.start < second.end and second.start < first.end
        for index, first in enumerate(entities)
        for second in entities[index + 1 :]
    )


def summarize(case: ClinicalCase, entities: list[Entity], elapsed: float) -> dict[str, Any]:
    word_lengths = [len(re.findall(r"\b\w+\b", entity.text)) for entity in entities]
    covered = [
        target for target in case.target_phrases if target_is_covered(target, entities)
    ]
    return {
        "elapsed_seconds": round(elapsed, 3),
        "entity_count": len(entities),
        "overlapping_pairs": count_overlaps(entities),
        "multiword_entities": sum(length > 1 for length in word_lengths),
        "longest_entity_words": max(word_lengths, default=0),
        "target_exact_recall": round(len(covered) / len(case.target_phrases), 3),
        "covered_targets": covered,
        "missed_targets": [
            target for target in case.target_phrases if target not in covered
        ],
        "entities": [asdict(entity) for entity in entities],
    }


def selected_models(selection: str) -> list[str]:
    if selection == "all":
        return ["gliner", *TOKEN_MODELS]
    requested = [item.strip() for item in selection.split(",") if item.strip()]
    unknown = set(requested) - {"gliner", *TOKEN_MODELS}
    if unknown:
        raise ValueError(f"Unknown models: {', '.join(sorted(unknown))}")
    return requested


def print_report(results: dict[str, Any]) -> None:
    for case_name, case_result in results["cases"].items():
        print(f"\n=== {case_name} ===")
        print(f"Source: {case_result['source']}")
        for model_name, result in case_result["models"].items():
            print(
                f"\n{model_name}: {result['entity_count']} entities, "
                f"{result['multiword_entities']} multiword, "
                f"longest={result['longest_entity_words']} words, "
                f"target recall={result['target_exact_recall']:.1%}, "
                f"time={result['elapsed_seconds']:.3f}s"
            )
            for entity in result["entities"]:
                print(
                    f"  [{entity['start']:>3}:{entity['end']:<3}] "
                    f"{entity['text']!r} -> {entity['label']} "
                    f"({entity['score']:.3f})"
                )
            if result["missed_targets"]:
                print("  Missed targets: " + "; ".join(result["missed_targets"]))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--models",
        default="all",
        help="all or comma-separated: gliner, biomedical-ner-all, biomedbert-bc5cdr",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.9,
        help="Minimum confidence for every model (default: 0.9)",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    parser.add_argument(
        "--list-cases", action="store_true", help="Print cases without loading models"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not 0.0 <= args.threshold <= 1.0:
        raise SystemExit("--threshold must be between 0 and 1")
    if args.list_cases:
        for case in CASES:
            print(f"{case.name}\n  {case.source}\n  {case.text}\n")
        return

    try:
        model_names = selected_models(args.models)
    except ValueError as error:
        raise SystemExit(str(error)) from error

    results: dict[str, Any] = {
        "threshold": args.threshold,
        "models": model_names,
        "cases": {},
    }
    for case in CASES:
        case_result: dict[str, Any] = {"source": case.source, "models": {}}
        results["cases"][case.name] = case_result
        for model_name in model_names:
            started = time.perf_counter()
            if model_name == "gliner":
                entities = run_gliner(case, args.threshold)
            else:
                entities = run_token_classifier(
                    case, TOKEN_MODELS[model_name], args.threshold
                )
            elapsed = time.perf_counter() - started
            case_result["models"][model_name] = summarize(case, entities, elapsed)

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print_report(results)


if __name__ == "__main__":
    main()
