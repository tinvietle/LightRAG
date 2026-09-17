#!/usr/bin/env python3
"""Run a read-only SapBERT entity-similarity demo on LightRAG entities.

The QuickUMLS evaluation supplies weak labels:

* same unique CUI: known-positive synonym pair;
* different unique CUIs: hard negative (imperfect, but useful for calibration);
* either entity lacks a unique CUI: unknown pair for later review.

This script never changes LightRAG storage. It embeds names using SapBERT's
documented last-layer CLS representation, performs cosine k-nearest-neighbor
search, and writes candidate pairs plus threshold diagnostics.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import random
import re
import sys
from typing import Any, Iterable, Sequence

import numpy as np


DEFAULT_MODEL = "cambridgeltl/SapBERT-from-PubMedBERT-fulltext"
THRESHOLDS = (0.80, 0.85, 0.90, 0.92, 0.94, 0.95, 0.96, 0.97, 0.98, 0.99)
MODIFIER_PATTERNS: dict[str, re.Pattern[str]] = {
    "laterality": re.compile(r"\b(left|right|bilateral|unilateral)\b", re.I),
    "severity": re.compile(r"\b(mild|moderate|severe|grade\s*[ivx0-9]+)\b", re.I),
    "temporality": re.compile(
        r"\b(acute|chronic|recurrent|previous|history of|congenital|acquired)\b",
        re.I,
    ),
    "polarity": re.compile(r"\b(positive|negative|present|absent)\b", re.I),
    "susceptibility": re.compile(
        r"\b(resistant|susceptible|sensitive|non[- ]?responsive)\b", re.I
    ),
    "type": re.compile(r"\btype\s*(?:[ivx]+|\d+)\b", re.I),
    "number": re.compile(r"(?<!\w)\d+(?:\.\d+)?(?:\s*%)?", re.I),
}
LEXICAL_NORMALIZATION_RE = re.compile(r"[^a-z0-9]+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evaluation",
        type=Path,
        default=Path("artifacts/umls/entity_normalization_evaluation.jsonl"),
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-entities", type=int, default=5000)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--max-length", type=int, default=25)
    parser.add_argument("--seed", type=int, default=8712)
    parser.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda"),
        default="auto",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/sapbert/demo_candidates.jsonl"),
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=Path("artifacts/sapbert/demo_summary.json"),
    )
    parser.add_argument(
        "--embedding-cache",
        type=Path,
        default=Path("artifacts/sapbert/demo_embeddings.npz"),
    )
    args = parser.parse_args()
    if args.max_entities < 2:
        parser.error("--max-entities must be at least 2")
    if args.batch_size < 1 or args.top_k < 1 or args.max_length < 2:
        parser.error("batch size/top-k must be positive and max-length >= 2")
    return args


def read_evaluation(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as source:
            return [json.loads(line) for line in source if line.strip()]
    except FileNotFoundError as error:
        raise ValueError(f"QuickUMLS evaluation not found: {path}") from error


def unique_cui(row: dict[str, Any]) -> str | None:
    cuis = row.get("best_cuis", [])
    return cuis[0] if row.get("auto_lexical_candidate") and len(cuis) == 1 else None


def select_demo_rows(
    rows: Sequence[dict[str, Any]], max_entities: int, seed: int
) -> list[dict[str, Any]]:
    """Prioritize all multi-name CUI groups, then fill a deterministic sample."""
    by_cui: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        cui = unique_cui(row)
        if cui:
            by_cui[cui].append(row)

    selected_names = {
        row["entity_name"]
        for group in by_cui.values()
        if len(group) > 1
        for row in group
    }
    if len(selected_names) > max_entities:
        raise ValueError(
            f"--max-entities={max_entities} cannot include all "
            f"{len(selected_names)} known duplicate-group entities"
        )

    remaining = [row for row in rows if row["entity_name"] not in selected_names]
    random.Random(seed).shuffle(remaining)
    selected_names.update(
        row["entity_name"] for row in remaining[: max_entities - len(selected_names)]
    )
    return sorted(
        (row for row in rows if row["entity_name"] in selected_names),
        key=lambda row: row["entity_name"].casefold(),
    )


def extract_modifiers(text: str) -> dict[str, tuple[str, ...]]:
    return {
        category: tuple(sorted({match.group(0).casefold() for match in pattern.finditer(text)}))
        for category, pattern in MODIFIER_PATTERNS.items()
        if pattern.search(text)
    }


def modifier_conflicts(left: str, right: str) -> list[str]:
    left_values = extract_modifiers(left)
    right_values = extract_modifiers(right)
    return sorted(
        category
        for category in MODIFIER_PATTERNS
        if left_values.get(category, ()) != right_values.get(category, ())
        and (category in left_values or category in right_values)
    )


def lexical_key(text: str) -> str:
    """Create a narrow key for case and punctuation-equivalent names."""
    return LEXICAL_NORMALIZATION_RE.sub(" ", text.casefold()).strip()


def l2_normalize(embeddings: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    return embeddings / np.maximum(norms, np.finfo(embeddings.dtype).eps)


def embed_names(
    names: Sequence[str],
    model_name: str,
    batch_size: int,
    max_length: int,
    requested_device: str,
) -> tuple[np.ndarray, str]:
    try:
        import torch
        from transformers import AutoModel, AutoTokenizer
    except ImportError as error:
        raise RuntimeError("SapBERT demo requires torch and transformers") from error

    device = (
        "cuda"
        if requested_device == "auto" and torch.cuda.is_available()
        else "cpu"
        if requested_device == "auto"
        else requested_device
    )
    if device == "cuda" and not torch.cuda.is_available():
        raise ValueError("CUDA was requested but is not available")

    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModel.from_pretrained(model_name).to(device).eval()
    batches: list[np.ndarray] = []
    with torch.inference_mode():
        for start in range(0, len(names), batch_size):
            batch = names[start : start + batch_size]
            tokens = tokenizer(
                batch,
                padding="max_length",
                truncation=True,
                max_length=max_length,
                return_tensors="pt",
            )
            tokens = {key: value.to(device) for key, value in tokens.items()}
            cls_embeddings = model(**tokens).last_hidden_state[:, 0, :]
            batches.append(cls_embeddings.float().cpu().numpy())
            completed = min(start + batch_size, len(names))
            print(f"Embedded {completed}/{len(names)}", file=sys.stderr, flush=True)
    return l2_normalize(np.concatenate(batches)), device


def save_embedding_cache(path: Path, names: Sequence[str], vectors: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, names=np.asarray(names), vectors=vectors)


def load_embedding_cache(
    path: Path, expected_names: Sequence[str]
) -> np.ndarray | None:
    if not path.exists():
        return None
    with np.load(path) as cache:
        cached_names = cache["names"].tolist()
        if cached_names != list(expected_names):
            return None
        return cache["vectors"]


def nearest_neighbor_pairs(
    embeddings: np.ndarray, top_k: int
) -> Iterable[tuple[int, int, float]]:
    try:
        import faiss
    except ImportError as error:
        raise RuntimeError("SapBERT demo requires faiss-cpu") from error

    search_k = min(top_k + 1, len(embeddings))
    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(np.ascontiguousarray(embeddings, dtype=np.float32))
    similarities, neighbors = index.search(
        np.ascontiguousarray(embeddings, dtype=np.float32), search_k
    )
    seen: set[tuple[int, int]] = set()
    for left, (neighbor_row, similarity_row) in enumerate(
        zip(neighbors, similarities, strict=True)
    ):
        for right, similarity in zip(neighbor_row, similarity_row, strict=True):
            if right < 0 or left == right:
                continue
            pair = tuple(sorted((left, int(right))))
            if pair in seen:
                continue
            seen.add(pair)
            yield pair[0], pair[1], float(similarity)


def classify_pair(left_cui: str | None, right_cui: str | None) -> str:
    if left_cui and right_cui:
        return "same_cui" if left_cui == right_cui else "different_cui"
    return "unknown"


def evaluate_thresholds(
    pairs: Sequence[dict[str, Any]], known_positive_total: int
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for threshold in THRESHOLDS:
        accepted = [
            pair
            for pair in pairs
            if pair["similarity"] >= threshold and not pair["modifier_conflicts"]
        ]
        labels = Counter(pair["label"] for pair in accepted)
        known_decisions = labels["same_cui"] + labels["different_cui"]
        results[f"{threshold:.2f}"] = {
            "accepted_pairs": len(accepted),
            "same_cui_pairs": labels["same_cui"],
            "different_cui_pairs": labels["different_cui"],
            "unknown_pairs": labels["unknown"],
            "lexical_equivalent_pairs": sum(
                pair["lexical_equivalent"] for pair in accepted
            ),
            "known_pair_precision_proxy": (
                round(labels["same_cui"] / known_decisions, 4)
                if known_decisions
                else None
            ),
            "known_positive_neighbor_recall": (
                round(labels["same_cui"] / known_positive_total, 4)
                if known_positive_total
                else None
            ),
        }
    return results


def main() -> int:
    args = parse_args()
    rows = read_evaluation(args.evaluation)
    selected = select_demo_rows(rows, args.max_entities, args.seed)
    names = [row["entity_name"] for row in selected]
    cuis = [unique_cui(row) for row in selected]

    embeddings = load_embedding_cache(args.embedding_cache, names)
    device = "cache"
    if embeddings is None:
        embeddings, device = embed_names(
            names,
            args.model,
            args.batch_size,
            args.max_length,
            args.device,
        )
        save_embedding_cache(args.embedding_cache, names, embeddings)

    pairs = []
    for left, right, similarity in nearest_neighbor_pairs(embeddings, args.top_k):
        conflicts = modifier_conflicts(names[left], names[right])
        pairs.append(
            {
                "left": names[left],
                "right": names[right],
                "similarity": round(similarity, 6),
                "left_cui": cuis[left],
                "right_cui": cuis[right],
                "label": classify_pair(cuis[left], cuis[right]),
                "modifier_conflicts": conflicts,
                "lexical_equivalent": lexical_key(names[left])
                == lexical_key(names[right]),
            }
        )
    pairs.sort(key=lambda pair: pair["similarity"], reverse=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as destination:
        for pair in pairs:
            destination.write(json.dumps(pair, ensure_ascii=False) + "\n")

    cui_counts = Counter(cui for cui in cuis if cui)
    known_positive_total = sum(
        count * (count - 1) // 2 for count in cui_counts.values() if count > 1
    )
    known_positive_retrieved = sum(pair["label"] == "same_cui" for pair in pairs)
    summary = {
        "configuration": {
            "model": args.model,
            "device": device,
            "entity_count": len(names),
            "top_k": args.top_k,
            "max_length": args.max_length,
            "seed": args.seed,
        },
        "sample": {
            "known_cui_entities": sum(cui is not None for cui in cuis),
            "unknown_cui_entities": sum(cui is None for cui in cuis),
            "candidate_pairs": len(pairs),
            "modifier_conflict_pairs": sum(
                bool(pair["modifier_conflicts"]) for pair in pairs
            ),
            "known_positive_pairs_total": known_positive_total,
            "known_positive_pairs_retrieved_top_k": known_positive_retrieved,
            "known_positive_top_k_recall": round(
                known_positive_retrieved / known_positive_total, 4
            ),
        },
        "thresholds": evaluate_thresholds(pairs, known_positive_total),
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
