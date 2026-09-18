#!/usr/bin/env python3
"""Persistent QuickUMLS JSON-lines worker for the isolated Python environment."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from quickumls import QuickUMLS


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index-dir", required=True)
    parser.add_argument("--threshold", type=float, default=0.9)
    parser.add_argument("--window", type=int, default=10)
    parser.add_argument("--semtypes", default="")
    return parser.parse_args()


def _flatten_matches(
    groups: list[list[dict[str, Any]]], semtypes: set[str]
) -> list[dict[str, Any]]:
    by_span: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for group in groups:
        for candidate in group:
            candidate_semtypes = {
                str(value) for value in candidate.get("semtypes", [])
            }
            if semtypes and not candidate_semtypes.intersection(semtypes):
                continue
            span = (int(candidate["start"]), int(candidate["end"]))
            by_span.setdefault(span, []).append(candidate)

    entities: list[dict[str, Any]] = []
    for (start, end), candidates in sorted(by_span.items()):
        best = max(candidates, key=lambda candidate: float(candidate["similarity"]))
        text = " ".join(str(best["ngram"]).split())
        if not text or not any(character.isalnum() for character in text):
            continue
        entities.append(
            {
                "text": text,
                "start": start,
                "end": end,
                "score": float(best["similarity"]),
                "source": "quickumls",
                "semtypes": sorted(
                    {
                        str(value)
                        for candidate in candidates
                        for value in candidate.get("semtypes", [])
                    }
                ),
                "cuis": sorted({str(candidate["cui"]) for candidate in candidates}),
            }
        )
    return entities


def main() -> int:
    args = _arguments()
    semtypes = {value for value in args.semtypes.split(",") if value}
    matcher = QuickUMLS(
        args.index_dir,
        overlapping_criteria="score",
        threshold=args.threshold,
        similarity_name="jaccard",
        window=args.window,
    )
    print(json.dumps({"status": "ready"}), flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            matches = matcher.match(
                str(request.get("text", "")),
                best_match=False,
                ignore_syntax=False,
            )
            response = {
                "id": request.get("id"),
                "matches": _flatten_matches(matches, semtypes),
            }
        except Exception as error:
            response = {
                "id": request.get("id") if "request" in locals() else None,
                "error": str(error),
            }
        print(json.dumps(response), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
