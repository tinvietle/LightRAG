#!/usr/bin/env python3
"""Small, commented lesson for the SapBERT merge-candidate algorithm.

Default mode uses tiny hand-written vectors, so it runs immediately:

    python next_step/learn_sapbert_normalization.py

To encode the names with the actual SapBERT model instead:

    python next_step/learn_sapbert_normalization.py --real-model

The production evaluation used FAISS to find only the nearest neighbors among
62,805 entities. This lesson compares every pair because six names are easier to
understand and do not need FAISS. It does not modify LightRAG storage.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import re
from typing import Sequence

import numpy as np


MODEL_NAME = "cambridgeltl/SapBERT-from-PubMedBERT-fulltext"
LEXICAL_RE = re.compile(r"[^a-z0-9]+")
MODIFIER_PATTERNS = {
    "laterality": re.compile(r"\b(left|right|bilateral|unilateral)\b", re.I),
    "temporality": re.compile(r"\b(acute|chronic|recurrent|congenital)\b", re.I),
    "severity": re.compile(r"\b(mild|moderate|severe)\b", re.I),
    "number": re.compile(r"(?<!\w)\d+(?:\.\d+)?(?:\s*%)?"),
}


@dataclass(frozen=True, slots=True)
class Entity:
    name: str
    # This comes from the safe QuickUMLS stage. None means "not known".
    cui: str | None = None


@dataclass(frozen=True, slots=True)
class PairDecision:
    left: str
    right: str
    similarity: float
    action: str
    reason: str


def normalize_rows(vectors: np.ndarray) -> np.ndarray:
    """Make every vector length 1, turning dot product into cosine similarity."""

    lengths = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(lengths, np.finfo(vectors.dtype).eps)


def lexical_key(text: str) -> str:
    """Ignore only letter case, punctuation, and spacing—not medical words."""

    return LEXICAL_RE.sub(" ", text.casefold()).strip()


def modifiers(text: str) -> dict[str, set[str]]:
    return {
        kind: {match.group(0).casefold() for match in pattern.finditer(text)}
        for kind, pattern in MODIFIER_PATTERNS.items()
        if pattern.search(text)
    }


def conflicting_modifiers(left: str, right: str) -> list[str]:
    """Catch clinically meaningful differences hidden by high similarity."""

    left_values = modifiers(left)
    right_values = modifiers(right)
    return sorted(
        kind
        for kind in MODIFIER_PATTERNS
        if left_values.get(kind, set()) != right_values.get(kind, set())
    )


def decide_pair(
    left: Entity,
    right: Entity,
    similarity: float,
    threshold: float = 0.99,
) -> PairDecision:
    """Apply the safety gates used for the conservative merge proposal.

    SapBERT similarity generates candidates; it is not by itself proof that two
    nodes are identical. The ordering below makes that distinction explicit.
    """

    if similarity < threshold:
        return PairDecision(left.name, right.name, similarity, "reject", "below threshold")

    conflicts = conflicting_modifiers(left.name, right.name)
    if conflicts:
        return PairDecision(
            left.name,
            right.name,
            similarity,
            "reject",
            f"different modifiers: {conflicts}",
        )

    # Two confident but different UMLS concepts must never be joined.
    if left.cui and right.cui and left.cui != right.cui:
        return PairDecision(
            left.name, right.name, similarity, "reject", "conflicting UMLS CUIs"
        )

    # Our automatic SapBERT set was deliberately narrow: spelling/case/punctuation
    # variants only. Non-lexical semantic synonyms go to review unless UMLS already
    # proves that they share one CUI.
    if lexical_key(left.name) == lexical_key(right.name):
        return PairDecision(
            left.name, right.name, similarity, "auto-merge", "lexical variants"
        )

    if left.cui and left.cui == right.cui:
        return PairDecision(
            left.name, right.name, similarity, "merge via UMLS", "same confident CUI"
        )

    return PairDecision(
        left.name,
        right.name,
        similarity,
        "review",
        "semantic similarity alone is insufficient",
    )


def encode_with_sapbert(names: Sequence[str]) -> np.ndarray:
    """Encode names exactly as in our experiment: last-layer CLS embeddings."""

    try:
        import torch
        from transformers import AutoModel, AutoTokenizer
    except ImportError as error:
        raise RuntimeError("real mode requires torch and transformers") from error

    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModel.from_pretrained(MODEL_NAME).eval()
    encoded = tokenizer(
        list(names), padding=True, truncation=True, max_length=25, return_tensors="pt"
    )
    with torch.no_grad():
        # [:, 0, :] selects the CLS token, one vector representing each name.
        vectors = model(**encoded).last_hidden_state[:, 0, :].cpu().numpy()
    return vectors.astype(np.float32)


def toy_vectors() -> np.ndarray:
    """Vectors chosen to demonstrate each branch, not to imitate model quality."""

    return np.asarray(
        [
            [1.000, 0.000, 0.000],  # MRI
            [1.000, 0.001, 0.000],  # mri: lexical auto-merge
            [0.000, 1.000, 0.000],  # left renal failure
            [0.000, 1.000, 0.001],  # right renal failure: modifier conflict
            [0.700, 0.000, 0.700],  # heart attack
            [0.701, 0.000, 0.699],  # myocardial infarction: semantic synonym
        ],
        dtype=np.float32,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument("--threshold", type=float, default=0.99)
    args = parser.parse_args()

    entities = [
        Entity("MRI", "C0024485"),
        Entity("mri", "C0024485"),
        Entity("left renal failure"),
        Entity("right renal failure"),
        Entity("heart attack"),
        Entity("myocardial infarction"),
    ]
    names = [entity.name for entity in entities]
    raw_vectors = encode_with_sapbert(names) if args.real_model else toy_vectors()
    vectors = normalize_rows(raw_vectors)

    # For the large experiment, FAISS supplied the top 50 neighbors instead of
    # materializing every possible pair. The decision rules afterward are the same.
    decisions = []
    for left_index, left in enumerate(entities):
        for right_index in range(left_index + 1, len(entities)):
            right = entities[right_index]
            similarity = float(vectors[left_index] @ vectors[right_index])
            if similarity >= args.threshold:
                decisions.append(decide_pair(left, right, similarity, args.threshold))

    decisions.sort(key=lambda decision: decision.similarity, reverse=True)
    print("\nHigh-similarity candidate decisions:")
    for decision in decisions:
        print(
            f"  {decision.similarity:.4f}  {decision.left!r} <-> "
            f"{decision.right!r}: {decision.action} ({decision.reason})"
        )


if __name__ == "__main__":
    main()
