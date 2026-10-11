#!/usr/bin/env python3
"""Score a saved LightRAG answer artifact by SNOMED CT concept, not by string.

For each case: parse the answer's numbered top-5 diagnoses, normalize every
predicted diagnosis to a UMLS CUI / SNOMED CT concept (falling back to
ICD-10-CM / MeSH through the CUI), normalize the ground-truth label the same
way, and count a hit when the predicted and true concepts coincide (same
SCTID, or any shared CUI). The result is reported next to the existing
string-matching metric so disagreements between the two can be inspected.

Prediction and ground truth are grounded with ``ground_to_snomed`` from
``extract_ground_truth_diagnoses.py`` -- the function that produced the
dataset's own SNOMED labels -- so both sides get identical normalization.

Run with the UMLS project's interpreter (it provides quickumls + ollama):

    ../umls/.venv/bin/python next_step/eval_answers_snomed.py \
        lightrag/evaluation/results/rag_split_contexts.json \
        --output lightrag/evaluation/results/rag_split_snomed_metrics.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
K_VALUES = (1, 3, 5)
STRICT_COVERAGE = 0.8


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("artifact", type=Path, help="Saved answers-and-contexts JSON.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--umls-dir",
        type=Path,
        default=REPO_ROOT.parent / "umls",
        help="UMLS project directory (default: ../umls).",
    )
    parser.add_argument("--quickumls-index", type=Path, default=None)
    parser.add_argument("--snomed-db", type=Path, default=None)
    parser.add_argument("--alt-db", type=Path, default=None)
    parser.add_argument(
        "--ancestors-db",
        type=Path,
        default=REPO_ROOT / "data/snomed_ancestors.sqlite",
        help="SCTID -> ancestors lookup from build_snomed_ancestors.py; "
        "enables the is-a hierarchy metrics.",
    )
    parser.add_argument("--threshold", type=float, default=0.85)
    parser.add_argument("--fallback-threshold", type=float, default=0.6)
    parser.add_argument("--examples", type=int, default=15)
    parser.add_argument(
        "--truth-field",
        default="ground_truth",
        help="Artifact field holding the true label: ground_truth (specific disease) "
        "or ground_truth_group (grouped disease, as written by simple_rag.py).",
    )
    parser.add_argument(
        "--broad-label-threshold",
        type=int,
        default=100,
        help="A true label with more SNOMED descendants than this is 'broad'; the "
        "narrow-labels metric gives no descendant credit for such labels.",
    )
    return parser.parse_args()


def strip_markup(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("*", "").replace("_", " ")).strip()


class Normalizer:
    """CUI / SNOMED normalization shared by predictions and ground truth."""

    def __init__(
        self,
        grounder: Any,
        matcher: Any,
        fallback: Any,
        snomed: sqlite3.Connection,
        alt: sqlite3.Connection,
        hierarchy: sqlite3.Connection | None = None,
    ) -> None:
        self.grounder = grounder
        self.matcher = matcher
        self.fallback = fallback
        self.snomed = snomed
        self.alt = alt
        self.hierarchy = hierarchy
        self.alt_by_term: dict[str, set[str]] | None = None
        self._cache: dict[str, dict[str, Any]] = {}
        self._ancestors: dict[str, set[str]] = {}

    def ancestors(self, sctid: str) -> set[str]:
        """All SNOMED is-a ancestors of a concept (empty without a hierarchy db)."""
        if self.hierarchy is None:
            return set()
        if sctid not in self._ancestors:
            row = self.hierarchy.execute(
                "SELECT ancestors FROM anc WHERE sctid = ?", (sctid,)
            ).fetchone()
            self._ancestors[sctid] = set(row[0].split()) if row else set()
        return self._ancestors[sctid]

    def sibling_cuis(self, sctid: str) -> set[str]:
        rows = self.snomed.execute(
            "SELECT DISTINCT cui FROM snomed WHERE sctid = ?", (sctid,)
        ).fetchall()
        return {row[0] for row in rows}

    def alt_term(self, cui: str) -> dict[str, str] | None:
        rows = self.alt.execute(
            "SELECT sab, code, tty, term FROM alt_term WHERE cui = ?", (cui,)
        ).fetchall()
        for sab in ("ICD10CM", "MSH"):
            for row_sab, code, _tty, term in rows:
                if row_sab == sab:
                    return {"sab": sab, "code": code, "term": term}
        return None

    def alt_cuis_for_term(self, term: str) -> set[str]:
        if self.alt_by_term is None:
            self.alt_by_term = {}
            for cui, row_term in self.alt.execute("SELECT cui, term FROM alt_term"):
                self.alt_by_term.setdefault(row_term.casefold(), set()).add(cui)
        return self.alt_by_term.get(term.casefold(), set())

    def predicted(self, raw: str) -> dict[str, Any]:
        """Ground one predicted diagnosis (cached; the name is cleaned first)."""
        text = strip_markup(raw)
        name = self.grounder.clean_diagnosis_name(text) or text
        if name in self._cache:
            return self._cache[name]
        result = self.grounder.ground_to_snomed(
            self.matcher, self.fallback, self.snomed, name
        )
        sctids: set[str] = set()
        cuis: set[str] = set()
        if result.get("grounded"):
            sctids.add(result["snomed_sctid"])
            cuis |= self.sibling_cuis(result["snomed_sctid"])
            cuis.add(result["cui"])
        elif result.get("reason") == "no_snomed_entry_for_cui" and result.get("cui"):
            cuis.add(result["cui"])
            result["alt_terminology"] = self.alt_term(result["cui"])
        result["sctids"] = sorted(sctids)
        result["cuis"] = sorted(cuis)
        result["normalized_name"] = name
        self._cache[name] = result
        return result

    def truth(self, label: str) -> dict[str, Any]:
        """Resolve a ground-truth label to SCTIDs/CUIs; record how it resolved."""
        rows = self.snomed.execute(
            "SELECT DISTINCT sctid FROM snomed WHERE term = ? COLLATE NOCASE", (label,)
        ).fetchall()
        sctids = {row[0] for row in rows}
        if sctids:
            cuis: set[str] = set()
            for sctid in sctids:
                cuis |= self.sibling_cuis(sctid)
            return {"source": "snomed_exact", "sctids": sctids, "cuis": cuis}
        alt_cuis = self.alt_cuis_for_term(label)
        if alt_cuis:
            return {
                "source": "alt_terminology_exact",
                "sctids": set(),
                "cuis": alt_cuis,
            }
        grounded = self.predicted(label)
        if grounded["cuis"] or grounded["sctids"]:
            return {
                "source": "quickumls_fuzzy",
                "sctids": set(grounded["sctids"]),
                "cuis": set(grounded["cuis"]),
            }
        return {"source": "unresolved", "sctids": set(), "cuis": set()}


def concept_match(prediction: dict[str, Any], truth: dict[str, Any]) -> bool:
    if truth["source"] == "unresolved":
        return False
    return bool(
        set(prediction["sctids"]) & truth["sctids"]
        or set(prediction["cuis"]) & truth["cuis"]
    )


def hierarchy_relation(
    normalizer: Normalizer, prediction: dict[str, Any], truth: dict[str, Any]
) -> str | None:
    """How the predicted concept relates to the true one in SNOMED's is-a tree.

    equal          same concept (SCTID or shared CUI)
    more_specific  the prediction is a descendant of the truth ("Lepromatous
                   leprosy" for "Leprosy") -- a correct, finer diagnosis
    more_generic   the prediction is an ancestor of the truth ("Pneumonia" for
                   "COVID-19 pneumonia") -- true but too coarse to credit
    """
    if truth["source"] == "unresolved":
        return None
    if concept_match(prediction, truth):
        return "equal"
    predicted, expected = set(prediction["sctids"]), truth["sctids"]
    if not predicted or not expected:
        return None
    if any(expected & normalizer.ancestors(p) for p in predicted):
        return "more_specific"
    if any(p in normalizer.ancestors(t) for p in predicted for t in expected):
        return "more_generic"
    return None


RELATION_PRIORITY = {None: 0, "more_generic": 1, "more_specific": 2, "equal": 3}
CREDITED_RELATIONS = ("equal", "more_specific")

# Parenthetical text often restates the diagnosis ("(cholangitis)", "(DLBCL)"),
# but is just as often a hedge ("(possibly related to X)"), a negation
# ("(non-PML)"), a list of alternatives, or a citation artifact. Only the
# first kind is safe to ground as a second candidate for the prediction.
_PAREN_SKIP = re.compile(
    r"^(e\.?g\.?|i\.?e\.?|possibl|likely|probabl|potential|primarily|mainly|"
    r"predominantly|if\b|such as|vs\.?|versus|or\b|and\b|with\b|due\b|related|"
    r"secondary|associated|suspected|consider|rule|including|especially|often|"
    r"typically|mimic|may\b|might\b|from\b|caused|following|post|non[-\s]|not\b|"
    r"no\b|without|exclud|ref(erence)?\b|source|see\b|per\b|\+|\d)",
    re.IGNORECASE,
)


def parenthetical_candidates(raw: str) -> list[tuple[str, str]]:
    """(text, kind) for parentheticals safe to ground; kind is name|abbreviation."""
    candidates = []
    for text in re.findall(r"\(([^()]*)\)", strip_markup(raw)):
        text = text.strip()
        if (
            len(text) < 3
            or _PAREN_SKIP.match(text)
            or re.search(r"\bor\b|/", text, re.I)
        ):
            continue
        is_abbreviation = text.isupper() and re.fullmatch(r"[A-Za-z0-9/\-]{2,8}", text)
        candidates.append((text, "abbreviation" if is_abbreviation else "name"))
    return candidates


def best_relation(*relations: str | None) -> str | None:
    return max(relations, key=lambda relation: RELATION_PRIORITY[relation])


def first_rank(flags: list[bool]) -> int | None:
    for index, flag in enumerate(flags, start=1):
        if flag:
            return index
    return None


def summarize(ranks: list[int | None]) -> dict[str, float]:
    total = len(ranks) or 1
    summary = {
        f"hit_at_{k}": round(sum(r is not None and r <= k for r in ranks) / total, 6)
        for k in K_VALUES
    }
    summary["mrr_at_5"] = round(sum(1.0 / r for r in ranks if r is not None) / total, 6)
    return summary


def main() -> int:
    args = parse_args()
    umls_dir = args.umls_dir.resolve()
    index = args.quickumls_index or umls_dir / "quickumls_data"
    snomed_db = args.snomed_db or umls_dir / "snomed_index.sqlite"
    alt_db = args.alt_db or umls_dir / "alt_terminology_index.sqlite"
    for path in (
        umls_dir / "extract_ground_truth_diagnoses.py",
        index,
        snomed_db,
        alt_db,
    ):
        if not path.exists():
            raise SystemExit(f"Missing required UMLS asset: {path}")

    grounder = load_module(
        "extract_ground_truth_diagnoses", umls_dir / "extract_ground_truth_diagnoses.py"
    )
    scorer = load_module(
        "eval_saved_contexts", REPO_ROOT / "lightrag/evaluation/eval_saved_contexts.py"
    )
    from quickumls import QuickUMLS  # imported late: slow, needs the UMLS venv

    matcher = QuickUMLS(str(index), threshold=args.threshold, similarity_name="jaccard")
    fallback = QuickUMLS(
        str(index), threshold=args.fallback_threshold, similarity_name="jaccard"
    )
    hierarchy = (
        sqlite3.connect(str(args.ancestors_db)) if args.ancestors_db.exists() else None
    )
    if hierarchy is None:
        print(
            f"No ancestors db at {args.ancestors_db}; skipping is-a metrics.",
            file=sys.stderr,
        )
    normalizer = Normalizer(
        grounder,
        matcher,
        fallback,
        sqlite3.connect(str(snomed_db)),
        sqlite3.connect(str(alt_db)),
        hierarchy,
    )

    artifact = json.loads(args.artifact.read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = []
    for position, result in enumerate(artifact.get("results", []), start=1):
        truth_label = str(result.get(args.truth_field, ""))
        truth = normalizer.truth(truth_label)
        diagnoses = scorer.extract_ranked_diagnoses(str(result.get("answer", "")))
        predictions = []
        for raw in diagnoses[:5]:
            grounded = normalizer.predicted(raw)
            predictions.append(
                {
                    "raw": raw,
                    "normalized_name": grounded["normalized_name"],
                    "grounded": bool(grounded.get("grounded")),
                    "match_method": grounded.get("match_method"),
                    "reason": grounded.get("reason"),
                    "snomed_sctid": grounded.get("snomed_sctid"),
                    "snomed_term": grounded.get("snomed_preferred_term"),
                    "cui": grounded.get("cui"),
                    "alt_terminology": grounded.get("alt_terminology"),
                    "coverage": grounded.get("coverage"),
                    "string_match": bool(scorer.answer_labels_match(truth_label, raw)),
                    "concept_match": concept_match(grounded, truth),
                }
            )
            # A fuzzy match covering little of the name can collapse a specific
            # diagnosis onto a generic parent ("Toxoplasmic encephalitis" ->
            # "Encephalitis"); the strict variant only trusts exact matches or
            # fuzzy matches covering most of the string.
            exactish = grounded.get("match_method") in (
                "exact",
                "exact_on_cleaned_name",
            )
            predictions[-1]["concept_match_strict"] = predictions[-1][
                "concept_match"
            ] and (
                exactish or float(grounded.get("coverage") or 0.0) >= STRICT_COVERAGE
            )
            head_relation = hierarchy_relation(normalizer, grounded, truth)
            relation, source = head_relation, "head"
            for text, kind in parenthetical_candidates(raw):
                candidate = normalizer.predicted(text)
                candidate_relation = hierarchy_relation(normalizer, candidate, truth)
                if RELATION_PRIORITY[candidate_relation] > RELATION_PRIORITY[relation]:
                    relation, source = candidate_relation, f"parenthetical_{kind}"
            predictions[-1]["relation_head"] = head_relation
            predictions[-1]["relation"] = relation
            predictions[-1]["relation_source"] = source
        cases.append(
            {
                "test_number": result.get("test_number", position),
                "file_name": result.get("file_name"),
                "ground_truth": truth_label,
                "truth_source": truth["source"],
                "truth_sctids": sorted(truth["sctids"]),
                "predictions": predictions,
            }
        )
        if position % 50 == 0:
            print(f"  normalized {position} cases", file=sys.stderr, flush=True)

    # How broad each true label is: the number of SNOMED concepts below it.
    # Descendant credit is generous for very broad labels ("Disorder of brain").
    wanted = {sctid for case in cases for sctid in case["truth_sctids"]}
    descendants: Counter[str] = Counter()
    if hierarchy is not None and wanted:
        for _sctid, ancestor_text in hierarchy.execute(
            "SELECT sctid, ancestors FROM anc"
        ):
            for ancestor in ancestor_text.split():
                if ancestor in wanted:
                    descendants[ancestor] += 1
    for case in cases:
        case["truth_descendants"] = (
            max(descendants[s] for s in case["truth_sctids"])
            if case["truth_sctids"]
            else None
        )

    def credited(
        case: dict[str, Any], prediction: dict[str, Any], limit: int | None
    ) -> bool:
        relation = prediction["relation"]
        if relation == "equal":
            return True
        if relation != "more_specific":
            return False
        return limit is None or (case["truth_descendants"] or 0) <= limit

    string_ranks = [
        first_rank([p["string_match"] for p in c["predictions"]]) for c in cases
    ]
    concept_ranks = [
        first_rank([p["concept_match"] for p in c["predictions"]]) for c in cases
    ]
    union_ranks = [
        first_rank([p["string_match"] or p["concept_match"] for p in c["predictions"]])
        for c in cases
    ]
    strict_ranks = [
        first_rank([p["concept_match_strict"] for p in c["predictions"]]) for c in cases
    ]
    specific_ranks = [
        first_rank([credited(c, p, None) for p in c["predictions"]]) for c in cases
    ]
    specific_head_ranks = [
        first_rank([p["relation_head"] in CREDITED_RELATIONS for p in c["predictions"]])
        for c in cases
    ]
    narrow_ranks = [
        first_rank(
            [credited(c, p, args.broad_label_threshold) for p in c["predictions"]]
        )
        for c in cases
    ]
    any_isa_ranks = [
        first_rank([p["relation"] is not None for p in c["predictions"]]) for c in cases
    ]
    for case, s, c in zip(cases, string_ranks, concept_ranks):
        case["string_rank"], case["concept_rank"] = s, c

    def in_top5(rank: int | None) -> bool:
        return rank is not None

    agreement = Counter(
        (
            "both"
            if in_top5(s) and in_top5(c)
            else "concept_only"
            if in_top5(c)
            else "string_only"
            if in_top5(s)
            else "neither"
        )
        for s, c in zip(string_ranks, concept_ranks)
    )
    flat = [p for case in cases for p in case["predictions"]]
    methods = Counter(
        p["match_method"] if p["grounded"] else f"ungrounded:{p['reason']}"
        for p in flat
    )
    alt_hits = sum(1 for p in flat if p["alt_terminology"])
    truth_sources = Counter(case["truth_source"] for case in cases)

    def examples(kind: str) -> list[dict[str, Any]]:
        picked = []
        for case in cases:
            s, c = case["string_rank"], case["concept_rank"]
            if (kind == "concept_only" and c and not s) or (
                kind == "string_only" and s and not c
            ):
                hit = next(
                    p
                    for p in case["predictions"]
                    if p["concept_match"] or p["string_match"]
                )
                picked.append(
                    {
                        "ground_truth": case["ground_truth"],
                        "predicted": hit["raw"][:140],
                        "normalized_to": hit["snomed_term"] or hit["normalized_name"],
                        "method": hit["match_method"] or hit["reason"],
                        "rank": c or s,
                    }
                )
            if len(picked) >= args.examples:
                break
        return picked

    report = {
        "artifact": str(args.artifact),
        "case_count": len(cases),
        "thresholds": {"match": args.threshold, "fallback": args.fallback_threshold},
        "umls_assets": {
            "quickumls_index": str(index),
            "snomed_db": str(snomed_db),
            "alt_db": str(alt_db),
        },
        "metrics": {
            "string_match": summarize(string_ranks),
            "snomed_concept_match": summarize(concept_ranks),
            "snomed_concept_match_strict": summarize(strict_ranks),
            "string_or_concept_match": summarize(union_ranks),
            "snomed_equal_or_more_specific": summarize(specific_ranks),
            "snomed_equal_or_more_specific_head_only": summarize(specific_head_ranks),
            "snomed_equal_or_more_specific_narrow_labels_only": summarize(narrow_ranks),
            "snomed_any_isa_relation_upper_bound": summarize(any_isa_ranks),
        },
        "top5_agreement_string_vs_concept": dict(agreement),
        "top5_agreement_string_vs_equal_or_more_specific": dict(
            Counter(
                "both"
                if s and h
                else "hierarchy_only"
                if h
                else "string_only"
                if s
                else "neither"
                for s, h in zip(string_ranks, specific_ranks)
            )
        ),
        "parenthetical_grounding": {
            "predictions_credited_via_parenthetical": sum(
                p["relation_source"] != "head"
                and p["relation"] in CREDITED_RELATIONS
                and p["relation_head"] not in CREDITED_RELATIONS
                for c in cases
                for p in c["predictions"]
            ),
            "cases_newly_credited": sum(
                h is None and s is not None
                for h, s in zip(specific_head_ranks, specific_ranks)
            ),
        },
        "label_breadth": {
            "broad_label_threshold": args.broad_label_threshold,
            "broad_label_cases": sum(
                (c["truth_descendants"] or 0) > args.broad_label_threshold
                for c in cases
            ),
            "cases_credited_only_via_descendant": (
                descendant_only := [
                    c
                    for c in cases
                    if any(p["relation"] == "more_specific" for p in c["predictions"])
                    and not any(p["relation"] == "equal" for p in c["predictions"])
                ]
            )
            and len(descendant_only),
            "of_which_broad_label": sum(
                (c["truth_descendants"] or 0) > args.broad_label_threshold
                for c in descendant_only
            ),
            "broadest_true_labels": sorted(
                {
                    (c["ground_truth"], c["truth_descendants"])
                    for c in cases
                    if c["truth_descendants"]
                },
                key=lambda item: -item[1],
            )[:10],
        },
        "string_hits_not_credited_by_hierarchy": dict(
            Counter(
                next(
                    (
                        p["relation"]
                        or ("ungrounded" if not p["grounded"] else "unrelated")
                        for p in c["predictions"]
                        if p["string_match"]
                    ),
                    "none",
                )
                for c, s, h in zip(cases, string_ranks, specific_ranks)
                if s and not h
            )
        ),
        "prediction_relation_counts": dict(
            Counter(p["relation"] or "none" for c in cases for p in c["predictions"])
        ),
        "ground_truth_resolution": dict(truth_sources),
        "prediction_normalization": {
            "predictions": len(flat),
            "grounded_to_snomed": sum(p["grounded"] for p in flat),
            "grounded_percent": round(
                100 * sum(p["grounded"] for p in flat) / max(len(flat), 1), 2
            ),
            "by_method": dict(methods),
            "ungrounded_with_alt_terminology": alt_hits,
        },
        "examples_concept_only_hits": examples("concept_only"),
        "examples_string_only_hits": examples("string_only"),
        "examples_more_specific_hits": [
            {
                "ground_truth": c["ground_truth"],
                "predicted": p["raw"][:120],
                "normalized_to": p["snomed_term"],
            }
            for c in cases
            for p in c["predictions"]
            if p["relation"] == "more_specific"
        ][: args.examples],
        "per_case": cases,
    }
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "per_case"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
