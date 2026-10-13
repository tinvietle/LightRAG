#!/usr/bin/env python3
"""MedEmbed (BERT, 512 tokens) as the case-text retriever: index, query, play.

MedEmbed (abhinand/MedEmbed-*-v0.1) is a BGE model fine-tuned for medical retrieval. Per
its Hub files it is a BertModel with CLS pooling followed by L2 normalisation and a
512-token limit, which is reproduced here with plain ``transformers`` + ``torch``.
Our cases average ~800 tokens, so two strategies are available:

  truncate    embed the first 512 tokens of the case text (the naive baseline; the lab
              and imaging fields at the end are cut off for long cases)
  chunk-mean  split the case into <=500-token chunks on sentence boundaries, embed each,
              and average the vectors (token-count weighted), re-normalised

Embeddings are cached in SQLite (per model + strategy + text), so every command resumes.

    .venv/bin/python next_step/medembed/medembed_rag.py lengths --dataset structured_rag_split_v2
    .venv/bin/python next_step/medembed/medembed_rag.py play    --dataset structured_rag_split_v2 --n 5
    .venv/bin/python next_step/medembed/medembed_rag.py query   --dataset structured_rag_split_v2 \
        --output lightrag/evaluation/results/medembed_v2.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "concept_rag"))
from common import CAPTION_FIELD, FIELD_ORDER, load_cases, stratified_sample  # noqa: E402
from medembed_core import (  # noqa: E402
    combine_vectors,
    pack_units,
    rank_neighbours,
    split_units,
)

DEFAULT_MODEL = "abhinand/MedEmbed-base-v0.1"
MAX_TOKENS = 512
CHUNK_TOKENS = 480  # headroom for [CLS]/[SEP]
STRATEGIES = ("truncate", "chunk-mean")


def case_text(fields: dict[str, str]) -> str:
    """The same text the other retrievers embed (labels are not part of it)."""
    lines = [
        f"{name.replace('_', ' ').capitalize()}: {fields[name]}"
        for name in FIELD_ORDER
        if fields.get(name)
    ]
    if fields.get(CAPTION_FIELD):
        lines.append(f"Image findings: {fields[CAPTION_FIELD]}")
    return "\n".join(lines)


class MedEmbedder:
    """CLS-pooled, normalised BERT embeddings with a SQLite cache."""

    def __init__(
        self, model: str, strategy: str, device: str, cache: Path, batch: int = 16
    ):
        import torch
        from transformers import AutoModel, AutoTokenizer

        self.torch = torch
        self.model_name, self.strategy, self.batch = model, strategy, batch
        self.device = device
        self.tokenizer = AutoTokenizer.from_pretrained(model)
        self.model = AutoModel.from_pretrained(model).to(device).eval()
        cache.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(cache))
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS vec (key TEXT PRIMARY KEY, blob BLOB NOT NULL)"
        )

    def token_count(self, text: str) -> int:
        return len(self.tokenizer(text, add_special_tokens=False)["input_ids"])

    def _key(self, text: str) -> str:
        return hashlib.sha256(
            f"{self.model_name}\n{self.strategy}\n{text}".encode()
        ).hexdigest()

    def _encode(self, texts: list[str]) -> np.ndarray:
        """Raw BERT forward pass: CLS vector, L2-normalised."""
        out = []
        with self.torch.no_grad():
            for start in range(0, len(texts), self.batch):
                enc = self.tokenizer(
                    texts[start : start + self.batch],
                    padding=True,
                    truncation=True,
                    max_length=MAX_TOKENS,
                    return_tensors="pt",
                ).to(self.device)
                cls = self.model(**enc).last_hidden_state[:, 0]
                cls = self.torch.nn.functional.normalize(cls, dim=-1)
                out.append(cls.float().cpu().numpy())
        return np.concatenate(out)

    def _case_vector(self, text: str) -> np.ndarray:
        if self.strategy == "truncate":
            return self._encode([text])[0]
        units = split_units(text)
        lengths = [self.token_count(u) for u in units]
        chunks = pack_units(units, lengths, CHUNK_TOKENS) or [text or " "]
        weights = [max(1, self.token_count(c)) for c in chunks]
        return combine_vectors(self._encode(chunks), weights)

    def embed(self, texts: list[str], label: str) -> np.ndarray:
        keys = [self._key(t) for t in texts]
        have: dict[str, np.ndarray] = {}
        for start in range(0, len(keys), 500):
            part = keys[start : start + 500]
            marks = ",".join("?" * len(part))
            for key, blob in self.db.execute(
                f"SELECT key, blob FROM vec WHERE key IN ({marks})", part
            ):
                have[key] = np.frombuffer(blob, dtype=np.float32)
        missing = [i for i, k in enumerate(keys) if k not in have]
        print(
            f"[{label}] {len(texts)} texts, {len(texts) - len(missing)} cached, {len(missing)} to embed",
            file=sys.stderr,
            flush=True,
        )
        started = time.time()
        for done, i in enumerate(missing, start=1):
            vector = self._case_vector(texts[i]).astype(np.float32)
            have[keys[i]] = vector
            self.db.execute(
                "INSERT OR REPLACE INTO vec VALUES (?, ?)", (keys[i], vector.tobytes())
            )
            if done % 25 == 0 or done == len(missing):
                self.db.commit()
                rate = done / (time.time() - started)
                print(
                    f"  [{label}] {done}/{len(missing)} ({rate:.1f} docs/s, ~{(len(missing) - done) / rate / 60:.1f} min left)",
                    file=sys.stderr,
                    flush=True,
                )
        self.db.commit()
        return np.stack([have[k] for k in keys])


def pick_device(requested: str) -> str:
    if requested != "auto":
        return requested
    return "cpu"  # the laptop GPU (2 GB) is shared with Ollama; ask for cuda explicitly


def load_split(dataset: Path, split: str) -> list[dict[str, Any]]:
    paths = {p.name: p for p in sorted((dataset / split).rglob("*.json"))}
    cases = load_cases(dataset, split)
    for case in cases:
        case["path"] = paths[case["file_name"]]
        case["text"] = case_text(case["fields"])
    return cases


def common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--strategy", choices=STRATEGIES, default="chunk-mean")
    p.add_argument("--device", default="auto", help="auto (= cpu) | cpu | cuda")
    p.add_argument(
        "--cache",
        type=Path,
        default=HERE.parents[1] / "artifacts/medembed/embeddings.sqlite",
    )
    p.add_argument("--batch", type=int, default=16)


def cmd_lengths(args: argparse.Namespace) -> int:
    """How many cases exceed MedEmbed's 512-token limit, and which fields get cut."""
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(args.model)
    cases = load_split(args.dataset, "test")
    lengths = np.array(
        [len(tok(c["text"], add_special_tokens=False)["input_ids"]) for c in cases]
    )
    print(
        f"{len(cases)} test cases | tokens: mean {lengths.mean():.0f}, median {np.median(lengths):.0f}, p90 {np.percentile(lengths, 90):.0f}, max {lengths.max()}"
    )
    print(f"over {MAX_TOKENS}: {(lengths > MAX_TOKENS).mean():.1%} of cases")
    cut = {name: 0 for name in FIELD_ORDER + (CAPTION_FIELD,)}
    for case in cases:
        used = 0
        for name in FIELD_ORDER + (CAPTION_FIELD,):
            if not case["fields"].get(name):
                continue
            used += (
                len(
                    tok(f"{name}: {case['fields'][name]}", add_special_tokens=False)[
                        "input_ids"
                    ]
                )
                + 1
            )
            if used > MAX_TOKENS:
                cut[name] += 1
    print("field that crosses the 512-token line (first field cut), cases:")
    for name, count in cut.items():
        if count:
            print(f"  {name:28s} {count:4d}  ({count / len(cases):.1%})")
    return 0


def build_matrix(
    embedder: MedEmbedder, cases: list[dict[str, Any]], label: str
) -> np.ndarray:
    return embedder.embed([c["text"] for c in cases], label)


def cached_qwen(
    texts: list[str], model: str, cache: Path, num_ctx: int = 4096
) -> np.ndarray | None:
    """Qwen embeddings that simple_rag already cached for these texts (None if any is missing)."""
    if not cache.exists():
        return None
    db = sqlite3.connect(str(cache))
    rows = []
    for text in texts:
        key = hashlib.sha256(f"{model}\n{num_ctx}\n{text}".encode()).hexdigest()
        found = db.execute("SELECT blob FROM vec WHERE key = ?", (key,)).fetchone()
        if not found:
            return None
        vector = np.frombuffer(found[0], dtype=np.float32)
        rows.append(vector / max(np.linalg.norm(vector), 1e-12))
    return np.stack(rows)


def small_pool(
    train: list[dict[str, Any]],
    chosen: list[dict[str, Any]],
    size: int,
    same: int,
    seed: int,
) -> list[dict[str, Any]]:
    """Training cases of each chosen case's disease (up to ``same``) plus a stratified fill."""
    rng = random.Random(seed)
    picked: dict[str, dict[str, Any]] = {}
    for case in chosen:
        siblings = [c for c in train if c["specific"] == case["specific"]]
        rng.shuffle(siblings)
        for sibling in siblings[:same]:
            picked[sibling["case_id"]] = sibling
    rest = [c for c in train if c["case_id"] not in picked]
    for case in stratified_sample(rest, max(0, size - len(picked)), seed):
        picked[case["case_id"]] = case
    return list(picked.values())


def cmd_play(args: argparse.Namespace) -> int:
    """A small demo: a few random test cases against a SMALL pool of training cases.

    The pool holds up to ``--same-disease`` training cases of each chosen case's disease
    plus a stratified fill to ``--pool-size``, so the right answer is findable. Results
    are illustrative, not an evaluation (the pool is a fraction of the real corpus).
    """
    train_all = load_split(args.dataset, "train")
    test = load_split(args.dataset, "test")
    chosen = random.Random(args.seed).sample(test, args.n)
    pool_cases = small_pool(
        train_all, chosen, args.pool_size, args.same_disease, args.seed
    )
    pool_texts = [c["text"] for c in pool_cases]
    query_texts = [c["text"] for c in chosen]
    print(
        f"{len(chosen)} random test cases | pool of {len(pool_cases)} training cases "
        f"({len({c['specific'] for c in pool_cases})} diseases)\n"
    )

    systems: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    strategies = [args.strategy] + (
        [] if args.no_truncate or args.strategy == "truncate" else ["truncate"]
    )
    for strategy in strategies:
        embedder = MedEmbedder(
            args.model, strategy, pick_device(args.device), args.cache, args.batch
        )
        systems[f"MedEmbed {strategy}"] = (
            embedder.embed(pool_texts, f"pool/{strategy}"),
            embedder.embed(query_texts, f"queries/{strategy}"),
        )
    for qwen in ("qwen3-embedding:0.6b", "qwen3-embedding:4b"):
        pool_vec = cached_qwen(pool_texts, qwen, args.qwen_cache)
        query_vec = cached_qwen(query_texts, qwen, args.qwen_cache)
        if pool_vec is not None and query_vec is not None:
            systems[qwen] = (pool_vec, query_vec)
        else:
            print(f"(no cached vectors for {qwen} on these cases; skipped)")

    totals = {name: 0 for name in systems}
    for number, case in enumerate(chosen):
        possible = sum(c["specific"] == case["specific"] for c in pool_cases)
        print("=" * 100)
        print(
            f"{case['file_name']}   TRUE: {case['specific']}   (group: {case['group']})"
        )
        print("  " + case["text"].replace("\n", " | ")[:300] + " ...")
        print(f"  same-disease cases in this pool: {possible}")
        lists: dict[str, list[tuple[int, float]]] = {}
        for name, (pool_vec, query_vec) in systems.items():
            lists[name] = rank_neighbours(query_vec[number], pool_vec, args.k)
            right = sum(
                pool_cases[i]["specific"] == case["specific"] for i, _ in lists[name]
            )
            totals[name] += right
            print(f"  {name:26s} correct in top-{args.k}: {right}")
        primary = f"MedEmbed {args.strategy}"
        print(f"  {primary} neighbours:")
        for rank, (i, score) in enumerate(lists[primary], start=1):
            mark = "✓" if pool_cases[i]["specific"] == case["specific"] else " "
            print(f"    {rank:2d}. {score:.3f} {mark} {pool_cases[i]['specific'][:62]}")
        for name, ranked in lists.items():
            if name != primary:
                shared = len({i for i, _ in ranked} & {i for i, _ in lists[primary]})
                print(f"  shares {shared}/{args.k} neighbours with {name}")
    print("\n" + "=" * 100)
    print(f"total correct in top-{args.k} over {len(chosen)} cases:")
    for name, count in totals.items():
        print(f"  {name:26s} {count}")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    train = load_split(args.dataset, "train")
    test = load_split(args.dataset, "test")
    embedder = MedEmbedder(
        args.model, args.strategy, pick_device(args.device), args.cache, args.batch
    )
    pool = build_matrix(embedder, train, "train")
    queries = build_matrix(embedder, test, "test")
    results = []
    for number, (case, vector) in enumerate(zip(test, queries), start=1):
        chunks = []
        for rank, (i, score) in enumerate(
            rank_neighbours(vector, pool, args.k), start=1
        ):
            chunks.append(
                {
                    "rank": rank,
                    "reference_id": str(rank),
                    "content": train[i]["path"].read_text(encoding="utf-8"),
                    "file_path": train[i]["file_name"],
                    "chunk_id": train[i]["case_id"],
                    "score": round(score, 6),
                }
            )
        results.append(
            {
                "test_number": number,
                "question": case["text"],
                "retrieval_query_text": case["text"],
                "answer": "",
                "ground_truth": case["specific"],
                "ground_truth_group": case["group"],
                "file_name": case["file_name"],
                "retrieval": {"chunks": chunks},
                "error": None,
            }
        )
    artifact = {
        "schema_version": 1,
        "collection_type": "simple_rag_retrieval",
        "metrics_computed": False,
        "timestamp": datetime.now().isoformat(),
        "dataset": args.dataset.name,
        "experiment": {
            "name": f"medembed-{args.strategy}",
            "embedding_model": args.model,
            "k": args.k,
        },
        "total_cases": len(results),
        "successful_cases": len(results),
        "failed_cases": 0,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    print(f"Retrieved top-{args.k} for {len(results)} test cases -> {args.output}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("lengths")
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.set_defaults(func=cmd_lengths)
    p = sub.add_parser("play")
    common_args(p)
    p.add_argument("--n", type=int, default=5, help="Random test cases to show.")
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument(
        "--pool-size", type=int, default=150, help="Training cases in the demo pool."
    )
    p.add_argument(
        "--same-disease",
        type=int,
        default=8,
        help="Pool cases of each chosen case's disease.",
    )
    p.add_argument(
        "--no-truncate", action="store_true", help="Skip the truncate comparison."
    )
    p.add_argument(
        "--qwen-cache",
        type=Path,
        default=HERE.parents[1] / "artifacts/simple_rag/embeddings.sqlite",
    )
    p.set_defaults(func=cmd_play)
    p = sub.add_parser("query")
    common_args(p)
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--output", type=Path, required=True)
    p.set_defaults(func=cmd_query)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
