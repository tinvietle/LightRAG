#!/usr/bin/env python3
"""Semantic (SapBERT) similarity between top-5 predicted diagnoses and ground truth.

Complements the strict-string-match hit@k metrics in eval_saved_contexts.py:
strict matching can't distinguish a clinically-related near-miss ("Necrotizing
fasciitis with secondary abscess formation") from a wildly wrong guess, when
neither is an exact string match to the ground truth ("Skin abscess"). This
computes the max SapBERT cosine similarity between the ground truth and each
of a case's up-to-5 parsed diagnoses, then reports a "semantic hit@5" rate at
several thresholds alongside the strict rate, for direct comparison.

Input: a baseline_metrics.json-style file (from eval_saved_contexts.py),
using its `per_case[].ground_truth` and `per_case[].parsed_diagnoses`.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import numpy as np
import torch
from transformers import AutoModel, AutoTokenizer

MODEL_NAME = "cambridgeltl/SapBERT-from-PubMedBERT-fulltext"
_SUPPORTED_BY_RE = re.compile(r"\(supported by.*$", re.IGNORECASE | re.DOTALL)
_PAREN_RE = re.compile(r"\([^)]*\)")
_QUALIFIER_RE = re.compile(
    r"\b(with|due to|secondary to|associated with|complicated by|caused by|"
    r"from|resulting from|in the setting of)\b.*$",
    re.IGNORECASE,
)
_TRAILING_PUNCT_RE = re.compile(r"[\s;:,.\-]+$")
_LEADING_PUNCT_RE = re.compile(r"^[\s;:,.\-]+")


def clean_diagnosis_name(raw: str) -> str:
    """Isolate the core disease name for a fair comparison against the short
    ground-truth label: drop the '(supported by ...)' evidence clause, drop
    any other parenthetical, then cut at the first qualifying connector
    ('with', 'due to', 'secondary to', ...) so e.g. "Necrotizing fasciitis
    with secondary abscess formation" becomes "Necrotizing fasciitis"
    instead of embedding the full descriptive phrase, which SapBERT (tuned
    on short entity names) does not represent well.
    """
    text = _SUPPORTED_BY_RE.sub("", raw)
    text = _PAREN_RE.sub("", text)
    text = _QUALIFIER_RE.sub("", text)
    text = _TRAILING_PUNCT_RE.sub("", text.strip())
    text = _LEADING_PUNCT_RE.sub("", text).strip()
    return text


def embed_texts(texts: list[str], device: str, batch_size: int = 64) -> np.ndarray:
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModel.from_pretrained(MODEL_NAME).to(device).eval()

    all_vecs = []
    with torch.no_grad():
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            enc = tokenizer(
                batch, padding=True, truncation=True, max_length=25, return_tensors="pt"
            ).to(device)
            out = model(**enc)
            cls = out.last_hidden_state[:, 0, :]  # SapBERT's documented CLS embedding
            all_vecs.append(cls.cpu().numpy())
    vecs = np.concatenate(all_vecs, axis=0)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / np.maximum(norms, 1e-12)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metrics_file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument(
        "--thresholds", type=float, nargs="+", default=[0.8, 0.85, 0.9, 0.95]
    )
    args = parser.parse_args()

    data = json.loads(args.metrics_file.read_text(encoding="utf-8"))
    per_case = data["per_case"]

    # Collect every unique string needing an embedding: all ground truths and
    # all cleaned predicted diagnosis names, across all cases.
    unique_texts: dict[str, int] = {}

    def text_id(text: str) -> int:
        if text not in unique_texts:
            unique_texts[text] = len(unique_texts)
        return unique_texts[text]

    case_rows = []
    for case in per_case:
        gt = case["ground_truth"]
        gt_id = text_id(gt)
        pred_ids = []
        for raw in case.get("parsed_diagnoses", []):
            cleaned = clean_diagnosis_name(raw)
            if cleaned:
                pred_ids.append(text_id(cleaned))
        case_rows.append(
            {
                "test_number": case.get("test_number"),
                "ground_truth": gt,
                "gt_id": gt_id,
                "pred_ids": pred_ids,
                "strict_hit_at_5": case.get("answer_ground_truth_rank") is not None,
            }
        )

    texts = list(unique_texts.keys())
    print(f"Embedding {len(texts)} unique strings with {MODEL_NAME} on {args.device}...")
    embeddings = embed_texts(texts, device=args.device)

    results = []
    for row in case_rows:
        gt_vec = embeddings[row["gt_id"]]
        sims = [float(gt_vec @ embeddings[pid]) for pid in row["pred_ids"]]
        best = max(sims) if sims else 0.0
        best_rank = (sims.index(best) + 1) if sims else None
        results.append(
            {
                "test_number": row["test_number"],
                "ground_truth": row["ground_truth"],
                "strict_hit_at_5": row["strict_hit_at_5"],
                "max_semantic_similarity": round(best, 4),
                "rank_of_max_similarity": best_rank,
                "all_similarities": [round(s, 4) for s in sims],
            }
        )

    n = len(results)
    strict_hits = sum(1 for r in results if r["strict_hit_at_5"])
    summary = {
        "model": MODEL_NAME,
        "case_count": n,
        "strict_hit_at_5_rate": round(strict_hits / n, 4),
        "mean_max_semantic_similarity": round(
            sum(r["max_semantic_similarity"] for r in results) / n, 4
        ),
        "semantic_hit_at_5_by_threshold": {},
        "near_miss_recovery_by_threshold": {},
    }
    for t in args.thresholds:
        sem_hits = sum(1 for r in results if r["max_semantic_similarity"] >= t)
        # Of the strict misses, how many are "near misses" at this threshold?
        strict_misses = [r for r in results if not r["strict_hit_at_5"]]
        near_miss = sum(1 for r in strict_misses if r["max_semantic_similarity"] >= t)
        summary["semantic_hit_at_5_by_threshold"][str(t)] = round(sem_hits / n, 4)
        summary["near_miss_recovery_by_threshold"][str(t)] = {
            "count": near_miss,
            "of_strict_misses": len(strict_misses),
            "rate": round(near_miss / len(strict_misses), 4) if strict_misses else None,
        }

    output = {"summary": summary, "cases": results}
    args.output.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
