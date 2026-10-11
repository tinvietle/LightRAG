"""Tests for deterministic evaluation of saved contexts."""

from __future__ import annotations

from collections import Counter
import json

from lightrag.evaluation.eval_saved_contexts import (
    evaluate,
    extract_ranked_diagnoses,
    extract_source_labels,
)


def test_extract_source_labels_ignores_appended_image_descriptions() -> None:
    content = (
        json.dumps(
            {
                "case_text": "Clinical text",
                "extracted_disease_name": "Liver abscess",
                "grouped_disease_name": "Abscess",
            }
        )
        + "\n\nAttached image descriptions:\n- ultrasound"
    )

    assert extract_source_labels(content) == ("Liver abscess", "Abscess")


def test_extract_ranked_diagnoses_supports_semicolon_lists() -> None:
    answer = (
        "Top 5 possible diseases are: "
        "1. Pyogenic liver abscess; "
        "2. Hepatocellular carcinoma; "
        "3. Metastatic disease.\n"
        "### Differential Diagnosis\nDetails"
    )

    assert extract_ranked_diagnoses(answer) == [
        "Pyogenic liver abscess",
        "Hepatocellular carcinoma",
        "Metastatic disease.",
    ]


def test_evaluate_computes_answer_and_retrieval_ranks() -> None:
    relevant_content = json.dumps(
        {
            "case_text": "Relevant",
            "extracted_disease_name": "Liver abscess",
            "grouped_disease_name": "Abscess",
        }
    )
    irrelevant_content = json.dumps(
        {
            "case_text": "Irrelevant",
            "extracted_disease_name": "Hepatitis",
            "grouped_disease_name": "Liver disease",
        }
    )
    artifact = {
        "results": [
            {
                "test_number": 1,
                "file_name": "test.json",
                "ground_truth": "Liver abscess",
                "answer": (
                    "Top 5 possible diseases are: 1. Hepatitis; "
                    "2. Pyogenic liver abscess."
                ),
                "retrieval": {
                    "chunks": [
                        {
                            "file_path": "wrong.json",
                            "content": irrelevant_content,
                        },
                        {
                            "file_path": "right.json",
                            "content": relevant_content,
                        },
                    ]
                },
            }
        ]
    }

    report = evaluate(artifact, Counter({"liver abscess": 1}))

    assert report["answer_metrics"]["hit_at_1"] == 0.0
    assert report["answer_metrics"]["hit_at_3"] == 1.0
    assert report["answer_metrics"]["mrr_at_5"] == 0.5
    assert report["retrieval_metrics"]["hit_at_1"] == 0.0
    assert report["retrieval_metrics"]["hit_at_3"] == 1.0
    assert report["retrieval_metrics"]["mrr_at_10"] == 0.5


def test_extract_ranked_diagnoses_reads_numbers_inside_markdown_headings() -> None:
    answer = (
        "### Differential Diagnosis\n"
        "#### **1. Bacterial pneumonia with empyema**\n- evidence\n"
        "#### **2. Sepsis from Klebsiella pneumoniae**\n- evidence\n"
        "#### **3. Necrotizing pneumonia**\n- evidence\n"
        "#### **4. Aspiration pneumonia**\n- evidence\n"
        "#### **5. Lung abscess**\n- evidence\n"
    )

    diagnoses = extract_ranked_diagnoses(answer)

    assert [item.split(" - ")[0] for item in diagnoses] == [
        "Bacterial pneumonia with empyema",
        "Sepsis from Klebsiella pneumoniae",
        "Necrotizing pneumonia",
        "Aspiration pneumonia",
        "Lung abscess",
    ]


def test_extract_ranked_diagnoses_reaches_item_five_in_long_answers() -> None:
    filler = "   - supporting evidence sentence. " * 60
    items = [f"{rank}. **Diagnosis {rank}**\n{filler}\n" for rank in range(1, 6)]
    answer = "Top 5 possible diseases are:\n" + "".join(items)
    assert answer.index("5. **Diagnosis 5**") > 6000

    diagnoses = extract_ranked_diagnoses(answer)

    assert len(diagnoses) == 5
    assert diagnoses[4].startswith("Diagnosis 5")
