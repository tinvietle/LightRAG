#!/usr/bin/env python3
"""LLM step for the simple RAG: top-5 diseases plus reasoning from retrieved cases.

Takes a retrieval artifact written by ``simple_rag.py query`` and, for each test
case, asks an OpenAI-compatible chat endpoint for a ranked differential
diagnosis. The prompt is assembled exactly as LightRAG's chunks-only ("naive")
mode assembles it, using the prompt file of the LightRAG checkout it runs in:

    system   PROMPTS["naive_rag_response"] filled with response_type
             "Multiple Paragraphs", user_prompt "n/a" and PROMPTS["naive_query_context"]
             (one JSON line per retrieved case + the reference list)
    user     the test case's question text (same layout the datasets use)

so any difference from a LightRAG run comes from retrieval, not from prompting.
Sampling options are not set unless asked for, matching LightRAG's default
call to the same model server. Progress is checkpointed to ``<output>.partial.jsonl``
after every case, so an interrupted run resumes where it stopped.

    python next_step/simple_rag/generate_answers.py \
        --retrieval lightrag/evaluation/results/simple_rag_v2.json \
        --output    lightrag/evaluation/results/simple_rag_v2_answers.json \
        --llm-url http://127.0.0.1:8080/v1 --model ministral3:3b
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
RESPONSE_TYPE = "Multiple Paragraphs"
USER_PROMPT = "n/a"  # what LightRAG substitutes when no extra user prompt is given

# Text for the prompt's "Additional Instructions: {user_prompt}" slot, the
# mechanism LightRAG provides for steering (QueryParam.user_prompt). Without it
# the 3B model reads "use ONLY the Context" literally, notices the retrieved
# records describe *other* patients, and often declines to answer
# (measured: 4 of the first 14 answers were refusals, 3 more ignored the
# required format). This text frames the records as reference cases.
USER_PROMPT_PRESETS = {
    "none": None,
    "reference_cases": (
        "The Context contains records of other, previously documented patients that "
        "were retrieved because they resemble the query patient; they will rarely "
        "describe the same person. Treat them as reference cases: compare their "
        "findings and confirmed diagnoses with the query patient's presentation to "
        "rank the most plausible diagnoses. Always answer in the required Top 5 "
        "format; do not decline because the query patient is not described in the "
        "Context."
    ),
}


def load_prompts(lightrag_dir: Path) -> dict[str, str]:
    """Import lightrag/prompt.py by path (no package install needed)."""
    path = lightrag_dir / "lightrag" / "prompt.py"
    spec = importlib.util.spec_from_file_location("lightrag_prompt_file", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Cannot load prompts from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PROMPTS


def build_system_prompt(
    prompts: dict[str, str],
    chunks: list[dict[str, Any]],
    user_prompt: str | None = None,
) -> str:
    """The system prompt LightRAG's naive mode would send for these chunks."""
    text_units = "\n".join(
        json.dumps(
            {"reference_id": c["reference_id"], "content": c["content"]},
            ensure_ascii=False,
        )
        for c in chunks
    )
    references = "\n".join(f"[{c['reference_id']}] {c['file_path']}" for c in chunks)
    context = prompts["naive_query_context"].format(
        text_chunks_str=text_units, reference_list_str=references
    )
    return prompts["naive_rag_response"].format(
        response_type=RESPONSE_TYPE,
        # LightRAG formats a supplied user prompt as "\n\n<text>", else "n/a".
        user_prompt=f"\n\n{user_prompt}" if user_prompt else USER_PROMPT,
        content_data=context,
    )


def call_llm(
    client: httpx.Client,
    url: str,
    model: str,
    system: str,
    user: str,
    options: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        **options,
    }
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = client.post(f"{url.rstrip('/')}/chat/completions", json=body)
            response.raise_for_status()
            payload = response.json()
            return payload["choices"][0]["message"]["content"] or "", payload.get(
                "usage", {}
            )
        except (httpx.HTTPError, KeyError, ValueError) as error:
            last_error = error
            time.sleep(5 * attempt)
    raise RuntimeError(f"LLM call failed after 3 attempts: {last_error!r}")


def load_partial(path: Path) -> dict[str, dict[str, Any]]:
    done: dict[str, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                done[row["file_name"]] = row
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--retrieval", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--llm-url", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--model", default="ministral3:3b")
    parser.add_argument("--api-key", default="none")
    parser.add_argument("--lightrag-dir", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--workers", type=int, default=1, help="Parallel requests (server slots)."
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Only the first N cases (smoke test)."
    )
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument(
        "--user-prompt-preset",
        choices=sorted(USER_PROMPT_PRESETS),
        default="none",
        help="Text for the prompt's Additional Instructions slot "
        "(see USER_PROMPT_PRESETS).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Build prompts, call nothing."
    )
    args = parser.parse_args()

    prompts = load_prompts(args.lightrag_dir)
    user_prompt = USER_PROMPT_PRESETS[args.user_prompt_preset]
    artifact = json.loads(args.retrieval.read_text(encoding="utf-8"))
    cases = artifact["results"][: args.limit] if args.limit else artifact["results"]
    options = {
        k: v
        for k, v in (("temperature", args.temperature), ("max_tokens", args.max_tokens))
        if v is not None
    }

    if args.dry_run:
        case = cases[0]
        system = build_system_prompt(prompts, case["retrieval"]["chunks"], user_prompt)
        print(
            f"system prompt: {len(system)} chars | user message: {len(case['question'])} chars"
        )
        print(
            system[:600],
            "\n...\n",
            system[-700:],
            "\n--- user ---\n",
            case["question"][:300],
        )
        return 0

    partial_path = args.output.with_suffix(args.output.suffix + ".partial.jsonl")
    partial_path.parent.mkdir(parents=True, exist_ok=True)
    done = load_partial(partial_path)
    todo = [c for c in cases if c["file_name"] not in done]
    print(
        f"{len(cases)} cases | {len(done)} already done | {len(todo)} to run | "
        f"workers={args.workers} | model={args.model}",
        file=sys.stderr,
        flush=True,
    )

    def work(case: dict[str, Any]) -> dict[str, Any]:
        system = build_system_prompt(prompts, case["retrieval"]["chunks"], user_prompt)
        started = time.time()
        with httpx.Client(
            timeout=args.timeout, headers={"Authorization": f"Bearer {args.api_key}"}
        ) as client:
            answer, usage = call_llm(
                client, args.llm_url, args.model, system, case["question"], options
            )
        return {
            "file_name": case["file_name"],
            "answer": answer,
            "request_latency_seconds": round(time.time() - started, 2),
            "usage": usage,
        }

    started = time.time()
    failures = 0
    with (
        partial_path.open("a", encoding="utf-8") as sink,
        ThreadPoolExecutor(args.workers) as pool,
    ):
        futures = {pool.submit(work, c): c for c in todo}
        for count, future in enumerate(as_completed(futures), start=1):
            case = futures[future]
            try:
                row = future.result()
            except (
                Exception
            ) as error:  # keep going; the case is retried on the next run
                failures += 1
                print(
                    f"  FAILED {case['file_name']}: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
            sink.flush()
            done[row["file_name"]] = row
            rate = count / (time.time() - started)
            print(
                f"  [{len(done)}/{len(cases)}] {row['file_name']} {row['request_latency_seconds']:.0f}s "
                f"| ~{(len(todo) - count) / rate / 60:.0f} min left",
                file=sys.stderr,
                flush=True,
            )

    results = []
    for case in cases:
        row = done.get(case["file_name"])
        merged = dict(case)
        merged["answer"] = row["answer"] if row else ""
        merged["error"] = None if row else "no answer generated"
        if row:
            merged["request_latency_seconds"] = row["request_latency_seconds"]
            merged["usage"] = row["usage"]
        results.append(merged)
    successful = sum(1 for r in results if r["error"] is None)
    artifact.update(
        {
            "collection_type": "simple_rag_answers_and_contexts",
            "timestamp": datetime.now().isoformat(),
            "total_cases": len(results),
            "successful_cases": successful,
            "failed_cases": len(results) - successful,
            "results": results,
        }
    )
    artifact.setdefault("experiment", {}).update(
        {
            "name": "simple-rag+llm",
            "llm_url": args.llm_url,
            "llm_model": args.model,
            "llm_options": options or "server defaults",
            "prompt": "PROMPTS['naive_rag_response'] + PROMPTS['naive_query_context']",
            "prompt_sha1": hashlib.sha1(
                prompts["naive_rag_response"].encode()
            ).hexdigest()[:12],
            "response_type": RESPONSE_TYPE,
            "user_prompt_preset": args.user_prompt_preset,
            "user_prompt": user_prompt or USER_PROMPT,
        }
    )
    args.output.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    print(
        f"wrote {args.output}: {successful}/{len(results)} answered, {failures} failed this run"
    )
    return 0 if successful == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
