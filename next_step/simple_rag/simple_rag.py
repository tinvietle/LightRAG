#!/usr/bin/env python3
"""Plain dense-retrieval baseline: no LLM, no knowledge graph.

Embeds every *train* case (structured fields + image captions, never the
diagnosis labels) with qwen3-embedding:0.6b through Ollama, stores the vectors in
a FAISS index (cosine similarity), then retrieves the k nearest train cases for
every *test* case and writes them in the same artifact layout LightRAG's
evaluation tools read, so both systems can be scored by identical code.

Setup (isolated venv, versions pinned in requirements.txt):

    uv venv --python 3.12 next_step/simple_rag/.venv
    uv pip install --python next_step/simple_rag/.venv/bin/python -r next_step/simple_rag/requirements.txt

Usage:

    PY=next_step/simple_rag/.venv/bin/python
    $PY next_step/simple_rag/simple_rag.py index --dataset DIR --index-dir OUT
    $PY next_step/simple_rag/simple_rag.py query --dataset DIR --index-dir OUT --output FILE.json

Embeddings are cached in SQLite (default artifacts/simple_rag/embeddings.sqlite), so an
interrupted run resumes and a case shared between datasets is embedded once.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

warnings.filterwarnings("ignore", message=".*langchain-community.*sunset.*")

from langchain_community.vectorstores import FAISS  # noqa: E402
from langchain_community.vectorstores.utils import DistanceStrategy  # noqa: E402
from langchain_ollama import OllamaEmbeddings  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_MODEL = "qwen3-embedding:0.6b"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
FIELD_ORDER = (
    "demographics",
    "chief_complaint",
    "history_of_present_illness",
    "past_medical_history",
    "exposures_risk_factors",
    "physical_exam_findings",
    "lab_findings",
    "imaging_findings",
)
# Cosine similarity == inner product of unit vectors. LangChain ignores
# normalize_L2 for this strategy (it warns "not applicable"), so vectors are
# normalised in CachedEmbedder instead. The strategy is not persisted by
# FAISS.save_local, so it is passed again on every load.
FAISS_KWARGS = {"distance_strategy": DistanceStrategy.MAX_INNER_PRODUCT}


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return "; ".join(filter(None, (as_text(item) for item in value)))
    if isinstance(value, dict):
        return "; ".join(f"{k}: {as_text(v)}" for k, v in value.items() if as_text(v))
    return str(value)


def case_text(record: dict[str, Any]) -> str:
    """Embedding text: structured fields + image captions. Labels are excluded."""
    fields = record.get("structured_fields") or {}
    lines = []
    for key in FIELD_ORDER:
        value = as_text(fields.get(key))
        if value:
            lines.append(f"{key.replace('_', ' ').capitalize()}: {value}")
    captions = as_text(record.get("image_captioning"))
    if captions:
        lines.append(f"Image findings: {captions}")
    return "\n".join(lines)


# The exact layout the evaluation datasets use for a test question (see
# web-wrapper/parse_structured_rag_captioning_to_json.py). The LLM receives this,
# as LightRAG did, while retrieval embeds the compact case_text() above.
QUESTION_LABELS = (
    ("demographics", "Demographics"),
    ("chief_complaint", "Chief Complaint"),
    ("history_of_present_illness", "History of Present Illness"),
    ("past_medical_history", "Past Medical History"),
    ("exposures_risk_factors", "Exposures/Risk Factors"),
    ("physical_exam_findings", "Physical Exam Findings"),
    ("lab_findings", "Lab Findings"),
    ("imaging_findings", "Imaging Findings"),
)


def question_text(record: dict[str, Any]) -> str:
    fields = record.get("structured_fields") or {}
    sections = []
    for key, label in QUESTION_LABELS:
        value = fields.get(key)
        if isinstance(value, list):
            text = "; ".join(str(item) for item in value if str(item).strip())
        else:
            text = str(value or "").strip()
        if text:
            sections.append(f"{label}: {text}")
    captions = record.get("image_captioning") or []
    if captions:
        lines = "\n".join(f"Image {i}: {c}" for i, c in enumerate(captions, start=1))
        sections.append(f"Image Findings:\n{lines}")
    return "\n".join(sections)


def load_cases(dataset: Path, split: str) -> list[dict[str, Any]]:
    root = dataset / split
    if not root.is_dir():
        raise SystemExit(f"Missing {split} split: {root}")
    cases = []
    for path in sorted(root.rglob("*.json")):
        raw = path.read_text(encoding="utf-8")
        record = json.loads(raw)
        record = record[0] if isinstance(record, list) and record else record
        cases.append(
            {
                "case_id": path.stem,
                "file_name": path.name,
                "relpath": str(path.relative_to(dataset)),
                "specific": str(record.get("snomed_extracted_disease_name") or ""),
                "group": str(record.get("snomed_grouped_disease_name") or ""),
                "text": case_text(record),
                "question": question_text(record),
                "raw": raw,
            }
        )
    return cases


class CachedEmbedder:
    """OllamaEmbeddings plus a SQLite vector cache and explicit batching.

    ``OllamaEmbeddings.embed_documents`` sends its whole list in one request, so
    batching is done here (8 per request measured fastest on this machine).
    """

    def __init__(
        self,
        model: str,
        base_url: str,
        cache_path: Path,
        batch_size: int = 8,
        num_ctx: int = 4096,
    ) -> None:
        self.model, self.batch_size, self.num_ctx = model, batch_size, num_ctx
        # keep_alive is an integer number of seconds in langchain-ollama 1.x.
        self.embeddings = OllamaEmbeddings(
            model=model, base_url=base_url, num_ctx=num_ctx, keep_alive=1800
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(cache_path))
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS vec (key TEXT PRIMARY KEY, blob BLOB NOT NULL)"
        )

    def key(self, text: str) -> str:
        # num_ctx is part of the key: it changes how long inputs are truncated.
        return hashlib.sha256(
            f"{self.model}\n{self.num_ctx}\n{text}".encode()
        ).hexdigest()

    def embed(self, texts: list[str], label: str) -> np.ndarray:
        keys = [self.key(t) for t in texts]
        have: dict[str, np.ndarray] = {}
        for start in range(0, len(keys), 500):
            chunk = keys[start : start + 500]
            marks = ",".join("?" * len(chunk))
            for key, blob in self.db.execute(
                f"SELECT key, blob FROM vec WHERE key IN ({marks})", chunk
            ):
                have[key] = np.frombuffer(blob, dtype=np.float32)
        missing = [i for i, k in enumerate(keys) if k not in have]
        print(
            f"[{label}] {len(texts)} texts, {len(texts) - len(missing)} cached, "
            f"{len(missing)} to embed",
            file=sys.stderr,
            flush=True,
        )
        started = time.time()
        for start in range(0, len(missing), self.batch_size):
            batch = missing[start : start + self.batch_size]
            vectors = self.embeddings.embed_documents([texts[i] for i in batch])
            for index, vector in zip(batch, vectors):
                array = np.asarray(vector, dtype=np.float32)
                have[keys[index]] = array
                self.db.execute(
                    "INSERT OR REPLACE INTO vec VALUES (?, ?)",
                    (keys[index], array.tobytes()),
                )
            self.db.commit()
            done = start + len(batch)
            if done % 96 < self.batch_size or done == len(missing):
                rate = done / (time.time() - started)
                eta = (len(missing) - done) / rate / 60
                print(
                    f"  [{label}] {done}/{len(missing)} embedded "
                    f"({rate:.1f} docs/s, ~{eta:.1f} min left)",
                    file=sys.stderr,
                    flush=True,
                )
        matrix = np.stack([have[k] for k in keys]).astype(np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        return matrix / np.clip(norms, 1e-12, None)


def build_embedder(args: argparse.Namespace) -> CachedEmbedder:
    return CachedEmbedder(args.model, args.ollama_url, args.cache, args.batch_size)


def cmd_index(args: argparse.Namespace) -> int:
    cases = load_cases(args.dataset, "train")
    embedder = build_embedder(args)
    vectors = embedder.embed([c["text"] for c in cases], "train")
    store = FAISS.from_embeddings(
        list(zip((c["text"] for c in cases), vectors.tolist())),
        embedder.embeddings,
        metadatas=[
            {k: c[k] for k in ("case_id", "file_name", "relpath", "specific", "group")}
            for c in cases
        ],
        ids=[c["case_id"] for c in cases],
        **FAISS_KWARGS,
    )
    args.index_dir.mkdir(parents=True, exist_ok=True)
    store.save_local(str(args.index_dir))
    (args.index_dir / "build_info.json").write_text(
        json.dumps(
            {
                "dataset": str(args.dataset),
                "train_cases": len(cases),
                "model": args.model,
                "dimension": int(vectors.shape[1]),
                "built_at": datetime.now().isoformat(timespec="seconds"),
                "text": "structured_fields + image captions (labels excluded)",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Indexed {len(cases)} train cases -> {args.index_dir}")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    embedder = build_embedder(args)
    # The index file is one we built ourselves; load_local unpickles its docstore.
    store = FAISS.load_local(
        str(args.index_dir),
        embedder.embeddings,
        allow_dangerous_deserialization=True,
        **FAISS_KWARGS,
    )
    tests = load_cases(args.dataset, "test")
    vectors = embedder.embed([c["text"] for c in tests], "test")
    results = []
    for number, (case, vector) in enumerate(zip(tests, vectors), start=1):
        hits = store.similarity_search_with_score_by_vector(vector.tolist(), k=args.k)
        chunks = []
        for rank, (document, score) in enumerate(hits, start=1):
            meta = document.metadata
            source = (args.dataset / meta["relpath"]).read_text(encoding="utf-8")
            chunks.append(
                {
                    "rank": rank,
                    "reference_id": str(rank),
                    "content": source,
                    "file_path": meta["file_name"],
                    "chunk_id": meta["case_id"],
                    "score": round(float(score), 6),
                }
            )
        results.append(
            {
                "test_number": number,
                "question": case["question"],
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
            "name": "simple-rag-faiss",
            "embedding_model": args.model,
            "similarity": "cosine (inner product on L2-normalised vectors)",
            "k": args.k,
            "index_dir": str(args.index_dir),
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
    for name, handler in (("index", cmd_index), ("query", cmd_query)):
        p = sub.add_parser(name)
        p.set_defaults(func=handler)
        p.add_argument(
            "--dataset",
            type=Path,
            required=True,
            help="Folder containing train/ and test/ case JSON files.",
        )
        p.add_argument("--index-dir", type=Path, required=True)
        p.add_argument("--model", default=DEFAULT_MODEL)
        p.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
        p.add_argument(
            "--cache",
            type=Path,
            default=HERE.parents[1] / "artifacts/simple_rag/embeddings.sqlite",
        )
        p.add_argument("--batch-size", type=int, default=8)
        if name == "query":
            p.add_argument("--output", type=Path, required=True)
            p.add_argument(
                "--k",
                type=int,
                default=10,
                help="Chunks retrieved per query (LightRAG used 10).",
            )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
