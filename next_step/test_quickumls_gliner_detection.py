#!/usr/bin/env python3
"""Test QuickUMLS detection plus GLiNER hints without entity normalization.

The built-in case has two explicit disease labels that act as a tiny gold set:
``Liver abscess`` and ``Abscess``. QuickUMLS is used only to find source spans;
its CUI is diagnostic metadata and is never used to rename or merge a span.

Run from the LightRAG repository:

    .venv/bin/python next_step/test_quickumls_gliner_detection.py
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable


CASE_JSON = r'''{
  "case_text": "A 44-year-old male consumed more than 150 g of alcohol and smoked 20 cigarettes per day for 20 years. He was admitted to our hospital due to pain in the right upper abdomen for 14 days with normal body temperature. On a physical examination, he displayed right lateral upper abdominal tenderness. The liver edge was palpated 5 and 10 cm under the right costal margin and the xiphoid process, respectively, with surface protuberance and tenderness. Laboratory tests showed an elevated inflammatory reaction [white blood cell count (WBC) 1.7 x 1010/L, neutrophil 81.6%, C-reactive protein (CRP) level 325 mg/L] and anemia (RBC 3.28 x 1012/L, HGB 112 g/L). Abnormal liver function parameters were also noted [aspartate aminotransferase 47 IU/L, gamma-glutamyl transpeptidase 139 IU/L, and alkaline phosphatase 133 IU/L]. Ultrasonography detected a large abscess (82 mm x 78 mm) within the right liver lobe (<PMC5403815_fmed-04-00048-g001_undivided_1_1.webp>).\nIntravenous injections of cefodizime sodium (1.5 g, b.i.d) and metronidazole (0.5 g, b.i.d) were administered daily from the first day after his admission to the end of the hospitalization (20 days). The patient underwent percutaneous abscess drainage using an 8.5-Fr catheter guided by ultrasonography on the fifth day of his hospitalization, which was left in place for 7 days. The patient fully recovered with the combination of antibiotic therapy and percutaneous abscess drainage. This was demonstrated by normal blood WBC count, CRP, negative blood, and abscess culture.\nUsing BD BACTEC 9240 auto blood culture system, P. phragmitetus strain 31801 was isolated from a blood sample collected within 24 h after his admission. The identity of P. phragmitetus strain 31801 was verified by 16S rRNA gene sequencing (GenBank number FJ882624.1) and whole genome sequencing strategy (accession number CP013068). The antimicrobial susceptibility test (AST) was conducted with the Kirby-Bauer disk diffusion test (OXOID, England) and BD Phoenix  100 Automated Microbiology System using NMIC/ID-109 identification/antibiotic susceptibility cards (Becton, Dickinson and Company). P. phragmitetus 31801 was sensitive to amikacin, imipenem, ceftazidime, cefepime, amoxicillin, piperacillin/tazobactam, gatifloxacin, and levofloxacin, while it was immediately sensitive to cefotaxime and ceftriaxone. It was resistant against gentamicin, tobramycin, piperacillin, trimethoprim/sulfamethoxazole, furazolidone, and tetracycline. Further studies showed that this strain was positive for extra-extended-spectrum beta-lactamase as shown by the Kirby-Bauer test. It was shown to carry the ampicillin-inducible beta-lactamase with some clones in the zone of CAZ + CA disk and ant (3'')-I that were verified by polymerase chain reaction. Using a blood agar plate, S. oralis was cultured from the liver abscess and collected on the fifth day after the patient's admission when the abscess drainage was performed. It was unable to be recovered from the pus and blood sample collected later.",
  "extracted_disease_name": "Liver abscess",
  "grouped_disease_name": "Abscess"
}'''

DISEASE_TUIS = {
    "T019",  # Congenital Abnormality
    "T020",  # Acquired Abnormality
    "T037",  # Injury or Poisoning
    "T046",  # Pathologic Function
    "T047",  # Disease or Syndrome
    "T048",  # Mental or Behavioral Dysfunction
    "T049",  # Cell or Molecular Dysfunction
    "T050",  # Experimental Model of Disease
    "T190",  # Anatomical Abnormality
    "T191",  # Neoplastic Process
}
# Conservative subset for automatic prompt hints. Broad disease-related TUIs
# such as T037/T184 can misclassify words like "strain" as disease concepts.
HIGH_PRECISION_DISEASE_TUIS = {
    "T019", "T020", "T047", "T048", "T050", "T190", "T191"
}
CLINICAL_TUIS = DISEASE_TUIS | {
    # Anatomy and biological structures
    "T017", "T018", "T021", "T022", "T023", "T024", "T025", "T026",
    "T028", "T029", "T030", "T031",
    # Organisms and pathogens
    "T001", "T002", "T004", "T005", "T007", "T008", "T009", "T010",
    "T011", "T012", "T013", "T014", "T015", "T016",
    # Findings, signs, symptoms, and laboratory results
    "T033", "T034", "T040", "T041", "T042", "T043", "T044", "T045",
    "T184",
    # Diagnostic and therapeutic procedures
    "T058", "T059", "T060", "T061", "T062", "T063", "T064", "T065",
    # Drugs and clinically relevant substances
    "T109", "T110", "T111", "T114", "T115", "T116", "T118", "T119",
    "T121", "T122", "T123", "T124", "T125", "T126", "T127", "T129",
    "T130", "T131", "T195", "T196", "T197", "T200",
}
GOLD_FIELD_RE = re.compile(
    r'"(?:extracted_disease_name|grouped_disease_name)"\s*:\s*'
    r'(?P<value>"(?:[^"\\]|\\.)*")'
)


@dataclass(frozen=True, slots=True)
class Detection:
    text: str
    start: int
    end: int
    score: float
    source: str
    labels: tuple[str, ...]
    cuis: tuple[str, ...] = ()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--text-file",
        type=Path,
        help="Optional UTF-8 text/JSON input; defaults to the built-in case.",
    )
    parser.add_argument(
        "--quickumls-index",
        type=Path,
        default=Path("../umls/quickumls_data"),
    )
    parser.add_argument(
        "--quickumls-python",
        type=Path,
        default=Path("../umls/.venv/bin/python"),
    )
    parser.add_argument("--gliner-threshold", type=float, default=0.9)
    parser.add_argument("--quickumls-threshold", type=float, default=0.9)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/detection/liver_abscess_detection.json"),
    )
    return parser.parse_args()


def detect_quickumls(
    text: str,
    index: Path,
    quickumls_python: Path,
    threshold: float,
) -> list[Detection]:
    """Return original text spans; do not normalize them to UMLS terms."""

    # QuickUMLS and GLiNER have incompatible compiled NumPy dependency sets in
    # their local environments, so QuickUMLS runs in its own interpreter.
    worker = r'''
import json
import sys
from quickumls import QuickUMLS

index, threshold = sys.argv[1], float(sys.argv[2])
text = sys.stdin.read()
matcher = QuickUMLS(
    index,
    overlapping_criteria="score",
    threshold=threshold,
    similarity_name="jaccard",
    window=10,
)
print(json.dumps(
    matcher.match(text, best_match=False, ignore_syntax=False), default=list
))
'''
    try:
        result = subprocess.run(
            [
                str(quickumls_python.absolute()),
                "-c",
                worker,
                str(index.resolve()),
                str(threshold),
            ],
            input=text,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(error.stderr.strip() or "QuickUMLS worker failed") from error

    by_span: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for group in json.loads(result.stdout):
        for candidate in group:
            if float(candidate["similarity"]) < threshold:
                continue
            candidate_semtypes = {
                str(semtype) for semtype in candidate.get("semtypes", [])
            }
            if not CLINICAL_TUIS.intersection(candidate_semtypes):
                continue
            span = (int(candidate["start"]), int(candidate["end"]))
            by_span.setdefault(span, []).append(candidate)

    detections = []
    for (start, end), candidates in by_span.items():
        source_text = text[start:end].strip()
        if not source_text or not any(character.isalnum() for character in source_text):
            continue
        semtypes = sorted(
            {
                str(semtype)
                for candidate in candidates
                for semtype in candidate.get("semtypes", [])
            }
        )
        detections.append(
            Detection(
                text=source_text,
                start=start,
                end=end,
                score=max(float(candidate["similarity"]) for candidate in candidates),
                source="quickumls",
                labels=tuple(semtypes),
                cuis=tuple(sorted({str(candidate["cui"]) for candidate in candidates})),
            )
        )
    return sorted(detections, key=lambda detection: (detection.start, detection.end))


def detect_gliner(text: str, threshold: float) -> list[Detection]:
    from gliner import GLiNER

    from lightrag.kg.ner import extract_entity_labels_from_guidance
    from lightrag.prompt import PROMPTS

    labels = extract_entity_labels_from_guidance(
        PROMPTS["default_entity_types_guidance"]
    )
    model = GLiNER.from_pretrained(
        "Ihor/gliner-biomed-base-v1.0", cache_dir="./data/ner_model"
    )
    raw = model.predict_entities(
        text, labels, flat_ner=True, threshold=threshold
    )
    return [
        Detection(
            text=str(item["text"]),
            start=int(item["start"]),
            end=int(item["end"]),
            score=float(item["score"]),
            source="gliner",
            labels=(str(item["label"]),),
        )
        for item in raw
    ]


def unique_hint_texts(detections: Iterable[Detection]) -> list[str]:
    """Combine detectors by surface text while preserving the first occurrence."""

    seen: set[str] = set()
    hints = []
    for detection in sorted(detections, key=lambda item: (item.start, item.end)):
        key = " ".join(detection.text.split()).casefold()
        if key and key not in seen:
            seen.add(key)
            hints.append(" ".join(detection.text.split()))
    return hints


def gold_diseases(text: str) -> list[str]:
    return [json.loads(match.group("value")) for match in GOLD_FIELD_RE.finditer(text)]


def recall(gold: list[str], hints: list[str]) -> dict[str, Any]:
    hint_keys = {hint.casefold() for hint in hints}
    found = [name for name in gold if name.casefold() in hint_keys]
    return {
        "found": found,
        "missed": [name for name in gold if name not in found],
        "recall": round(len(found) / len(gold), 4) if gold else None,
    }


def main() -> int:
    args = parse_args()
    text = (
        args.text_file.read_text(encoding="utf-8")
        if args.text_file
        else CASE_JSON
    )
    if not args.quickumls_index.exists():
        raise ValueError(f"QuickUMLS index not found: {args.quickumls_index}")

    gliner = detect_gliner(text, args.gliner_threshold)
    quickumls = detect_quickumls(
        text,
        args.quickumls_index,
        args.quickumls_python,
        args.quickumls_threshold,
    )
    gliner_hints = unique_hint_texts(gliner)
    quickumls_hints = unique_hint_texts(quickumls)
    combined_hints = unique_hint_texts([*gliner, *quickumls])
    gold = gold_diseases(text)

    quickumls_disease_detections = [
        detection
        for detection in quickumls
        if DISEASE_TUIS.intersection(detection.labels)
    ]
    high_precision_umls = [
        detection
        for detection in quickumls
        if HIGH_PRECISION_DISEASE_TUIS.intersection(detection.labels)
    ]
    high_precision_umls_hints = unique_hint_texts(high_precision_umls)
    recommended_hints = unique_hint_texts([*gliner, *high_precision_umls])
    report = {
        "configuration": {
            "gliner_threshold": args.gliner_threshold,
            "quickumls_threshold": args.quickumls_threshold,
            "quickumls_normalization_used": False,
        },
        "gold_disease_fields": gold,
        "counts": {
            "gliner_unique_hints": len(gliner_hints),
            "quickumls_unique_hints": len(quickumls_hints),
            "combined_unique_hints": len(combined_hints),
            "high_precision_umls_disease_hints": len(
                high_precision_umls_hints
            ),
            "recommended_combined_unique_hints": len(recommended_hints),
            "quickumls_additions_over_gliner": len(
                {hint.casefold() for hint in quickumls_hints}
                - {hint.casefold() for hint in gliner_hints}
            ),
        },
        "explicit_disease_recall": {
            "gliner": recall(gold, gliner_hints),
            "quickumls": recall(gold, quickumls_hints),
            "combined": recall(gold, combined_hints),
            "recommended_gliner_plus_high_precision_umls": recall(
                gold, recommended_hints
            ),
        },
        "quickumls_disease_detections": [
            asdict(detection) for detection in quickumls_disease_detections
        ],
        "high_precision_quickumls_disease_detections": [
            asdict(detection) for detection in high_precision_umls
        ],
        "gliner_detections": [asdict(detection) for detection in gliner],
        "quickumls_detections": [asdict(detection) for detection in quickumls],
        "combined_hint_block": "\n".join(
            f"- {hint}" for hint in combined_hints
        ),
        "recommended_combined_hint_block": "\n".join(
            f"- {hint}" for hint in recommended_hints
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps({key: report[key] for key in ("configuration", "gold_disease_fields", "counts", "explicit_disease_recall", "quickumls_disease_detections")}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as error:
        raise SystemExit(f"error: {error}") from error
