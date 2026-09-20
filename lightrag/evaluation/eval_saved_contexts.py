#!/usr/bin/env python3
"""Evaluate a saved LightRAG answer-and-context artifact offline."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import re
from statistics import mean
from typing import Any


EVALUATION_K = (1, 3, 5, 10)
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
NUMBERED_DIAGNOSIS_RE = re.compile(
    r"(?:^|[;\n])\s*([1-5])\s*[.)]\s*(.+?)"
    r"(?=(?:[;\n]\s*[1-5]\s*[.)])|(?:\n\s*#{1,6}\s)|$)",
    re.DOTALL,
)
TOP_LIST_MARKER_RE = re.compile(
    r"top\s+5\s+(?:possible\s+)?(?:diseases|diagnoses)\s+are\s*:",
    re.IGNORECASE,
)


def normalize_label(value: str) -> str:
    return " ".join(NON_ALNUM_RE.sub(" ", value.casefold()).split())


def labels_match(left: str, right: str) -> bool:
    return bool(left and right and normalize_label(left) == normalize_label(right))


def answer_labels_match(ground_truth: str, prediction: str) -> bool:
    """Allow a specific answer modifier around the complete reference label."""
    truth = normalize_label(ground_truth)
    predicted = normalize_label(prediction)
    if not truth or not predicted:
        return False
    return truth == predicted or f" {truth} " in f" {predicted} "


def extract_source_labels(content: str) -> tuple[str, str]:
    """Decode the leading JSON object before attached-image descriptions."""
    try:
        decoded, _ = json.JSONDecoder().raw_decode(content.lstrip())
    except (json.JSONDecodeError, TypeError):
        return "", ""
    if isinstance(decoded, list) and decoded and isinstance(decoded[0], dict):
        decoded = decoded[0]
    if not isinstance(decoded, dict):
        return "", ""
    return (
        str(decoded.get("extracted_disease_name", "")),
        str(decoded.get("grouped_disease_name", "")),
    )


def extract_ranked_diagnoses(answer: str) -> list[str]:
    """Extract the first numbered top-five block from the generated answer."""
    compact = answer.replace("**", "").replace("__", "")
    marker = TOP_LIST_MARKER_RE.search(compact)
    candidate = compact[marker.end() :] if marker else compact
    candidate = candidate[:6000]
    diagnoses: dict[int, str] = {}
    for match in NUMBERED_DIAGNOSIS_RE.finditer(candidate):
        rank = int(match.group(1))
        if rank in diagnoses:
            continue
        diagnosis = " ".join(match.group(2).split()).strip(" ;:-")
        if diagnosis:
            diagnoses[rank] = diagnosis
        if len(diagnoses) == 5:
            break
    return [diagnoses[rank] for rank in sorted(diagnoses)]


def reciprocal_rank(relevance: list[bool]) -> float:
    for rank, relevant in enumerate(relevance, start=1):
        if relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(relevance: list[bool], relevant_in_corpus: int, k: int) -> float:
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, relevant in enumerate(relevance[:k], start=1)
        if relevant
    )
    ideal_relevant = min(relevant_in_corpus, k)
    if ideal_relevant == 0:
        return 0.0
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_relevant + 1))
    return dcg / ideal


def load_corpus_label_counts(corpus_path: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    if corpus_path.is_file():
        try:
            stored_docs = json.loads(corpus_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return counts
        if not isinstance(stored_docs, dict):
            return counts
        label_sources: dict[str, set[str]] = defaultdict(set)
        for document in stored_docs.values():
            if not isinstance(document, dict):
                continue
            extracted, _ = extract_source_labels(str(document.get("content", "")))
            label = normalize_label(extracted)
            if label:
                label_sources[label].add(str(document.get("file_path", "")))
        counts.update({label: len(sources) for label, sources in label_sources.items()})
        return counts

    for path in corpus_path.rglob("custom_case_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        record = data[0] if isinstance(data, list) and data else data
        if isinstance(record, dict):
            label = normalize_label(str(record.get("extracted_disease_name", "")))
            if label:
                counts[label] += 1
    return counts


def _average(values: list[float]) -> float:
    return round(mean(values), 6) if values else 0.0


def evaluate(
    artifact: dict[str, Any], corpus_counts: Counter[str]
) -> dict[str, Any]:
    per_case: list[dict[str, Any]] = []
    answer_hits: dict[int, list[float]] = defaultdict(list)
    retrieval_hits: dict[int, list[float]] = defaultdict(list)
    retrieval_precision: dict[int, list[float]] = defaultdict(list)
    retrieval_ndcg: dict[int, list[float]] = defaultdict(list)
    answer_rr: list[float] = []
    retrieval_rr: list[float] = []
    eligible_retrieval_rr: list[float] = []
    eligible_retrieval_hits: dict[int, list[float]] = defaultdict(list)
    eligible_retrieval_precision: dict[int, list[float]] = defaultdict(list)
    eligible_retrieval_ndcg: dict[int, list[float]] = defaultdict(list)
    duplicate_document_rates: list[float] = []
    duplicate_label_rates: list[float] = []
    label_parse_coverage: list[float] = []
    macro_answer_top1: dict[str, list[float]] = defaultdict(list)
    macro_retrieval_hit10: dict[str, list[float]] = defaultdict(list)

    for result in artifact.get("results", []):
        truth = str(result.get("ground_truth", ""))
        truth_key = normalize_label(truth)
        diagnoses = extract_ranked_diagnoses(str(result.get("answer", "")))
        answer_relevance = [answer_labels_match(truth, item) for item in diagnoses]

        chunks = result.get("retrieval", {}).get("chunks", [])
        chunk_labels = [
            extract_source_labels(str(chunk.get("content", ""))) for chunk in chunks
        ]
        retrieval_relevance = [
            labels_match(truth, extracted) for extracted, _grouped in chunk_labels
        ]
        parsed_labels = sum(bool(extracted) for extracted, _ in chunk_labels)
        label_parse_coverage.append(parsed_labels / len(chunks) if chunks else 0.0)

        file_paths = [str(chunk.get("file_path", "")) for chunk in chunks]
        duplicate_document_rates.append(
            1.0 - len(set(file_paths)) / len(file_paths) if file_paths else 0.0
        )
        normalized_labels = [
            normalize_label(extracted) for extracted, _ in chunk_labels if extracted
        ]
        duplicate_label_rates.append(
            1.0 - len(set(normalized_labels)) / len(normalized_labels)
            if normalized_labels
            else 0.0
        )

        case_answer_rr = reciprocal_rank(answer_relevance)
        case_retrieval_rr = reciprocal_rank(retrieval_relevance)
        retrieval_eligible = corpus_counts[truth_key] > 0
        answer_rr.append(case_answer_rr)
        retrieval_rr.append(case_retrieval_rr)
        if retrieval_eligible:
            eligible_retrieval_rr.append(case_retrieval_rr)
        for k in EVALUATION_K:
            answer_hits[k].append(float(any(answer_relevance[:k])))
            retrieval_hits[k].append(float(any(retrieval_relevance[:k])))
            retrieval_precision[k].append(
                sum(retrieval_relevance[:k]) / min(k, len(retrieval_relevance))
                if retrieval_relevance
                else 0.0
            )
            retrieval_ndcg[k].append(
                ndcg_at_k(retrieval_relevance, corpus_counts[truth_key], k)
            )
            if retrieval_eligible:
                eligible_retrieval_hits[k].append(
                    float(any(retrieval_relevance[:k]))
                )
                eligible_retrieval_precision[k].append(
                    sum(retrieval_relevance[:k])
                    / min(k, len(retrieval_relevance))
                    if retrieval_relevance
                    else 0.0
                )
                eligible_retrieval_ndcg[k].append(
                    ndcg_at_k(retrieval_relevance, corpus_counts[truth_key], k)
                )

        macro_answer_top1[truth_key].append(float(any(answer_relevance[:1])))
        macro_retrieval_hit10[truth_key].append(float(any(retrieval_relevance[:10])))
        per_case.append(
            {
                "test_number": result.get("test_number"),
                "file_name": result.get("file_name"),
                "ground_truth": truth,
                "ground_truth_documents_in_corpus": corpus_counts[truth_key],
                "parsed_diagnoses": diagnoses,
                "answer_ground_truth_rank": (
                    next(
                        (
                            rank
                            for rank, relevant in enumerate(answer_relevance, start=1)
                            if relevant
                        ),
                        None,
                    )
                ),
                "retrieval_ground_truth_rank": (
                    next(
                        (
                            rank
                            for rank, relevant in enumerate(
                                retrieval_relevance, start=1
                            )
                            if relevant
                        ),
                        None,
                    )
                ),
                "retrieved_source_labels": [
                    {
                        "rank": rank,
                        "extracted_disease_name": extracted,
                        "grouped_disease_name": grouped,
                        "exact_relevant": retrieval_relevance[rank - 1],
                    }
                    for rank, (extracted, grouped) in enumerate(chunk_labels, start=1)
                ],
            }
        )

    parsed_answer_cases = sum(bool(case["parsed_diagnoses"]) for case in per_case)
    disease_count = len(macro_answer_top1)
    return {
        "evaluation_version": 1,
        "methodology": {
            "answer_matching": (
                "Deterministic numbered top-five parsing; a prediction matches when "
                "it equals or contains the complete normalized ground-truth label."
            ),
            "retrieval_relevance": (
                "Strict normalized equality between the test ground truth and the "
                "retrieved source's extracted_disease_name."
            ),
            "no_evaluator_llm": True,
            "k_values": list(EVALUATION_K),
        },
        "coverage": {
            "case_count": len(per_case),
            "unique_ground_truth_labels": disease_count,
            "answer_parse_cases": parsed_answer_cases,
            "answer_parse_rate": round(
                parsed_answer_cases / len(per_case) if per_case else 0.0, 6
            ),
            "retrieved_chunk_label_parse_rate": _average(label_parse_coverage),
            "ground_truth_labels_present_in_corpus": sum(
                corpus_counts[label] > 0 for label in macro_answer_top1
            ),
            "cases_eligible_for_exact_retrieval": sum(
                corpus_counts[normalize_label(case["ground_truth"])] > 0
                for case in per_case
            ),
        },
        "answer_metrics": {
            **{f"hit_at_{k}": _average(answer_hits[k]) for k in EVALUATION_K},
            "mrr_at_5": _average(answer_rr),
            "macro_hit_at_1": _average(
                [_average(values) for values in macro_answer_top1.values()]
            ),
        },
        "retrieval_metrics": {
            **{f"hit_at_{k}": _average(retrieval_hits[k]) for k in EVALUATION_K},
            **{
                f"precision_at_{k}": _average(retrieval_precision[k])
                for k in EVALUATION_K
            },
            **{f"ndcg_at_{k}": _average(retrieval_ndcg[k]) for k in EVALUATION_K},
            "mrr_at_10": _average(retrieval_rr),
            "macro_hit_at_10": _average(
                [_average(values) for values in macro_retrieval_hit10.values()]
            ),
        },
        "eligible_retrieval_metrics": {
            "denominator": "Cases whose exact ground-truth label exists in the indexed corpus",
            **{
                f"hit_at_{k}": _average(eligible_retrieval_hits[k])
                for k in EVALUATION_K
            },
            **{
                f"precision_at_{k}": _average(eligible_retrieval_precision[k])
                for k in EVALUATION_K
            },
            **{
                f"ndcg_at_{k}": _average(eligible_retrieval_ndcg[k])
                for k in EVALUATION_K
            },
            "mrr_at_10": _average(eligible_retrieval_rr),
        },
        "diversity_metrics": {
            "mean_duplicate_document_rate_at_10": _average(
                duplicate_document_rates
            ),
            "mean_duplicate_disease_label_rate_at_10": _average(
                duplicate_label_rates
            ),
        },
        "per_case": per_case,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path("dataset/train"),
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    artifact = json.loads(args.artifact.read_text(encoding="utf-8"))
    corpus_counts = load_corpus_label_counts(args.corpus)
    report = evaluate(artifact, corpus_counts)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    temporary.replace(args.output)
    print(json.dumps({key: value for key, value in report.items() if key != "per_case"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
