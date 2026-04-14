# Role
You are a senior AI engineer responsible for building an evaluation pipeline for entity and relationship extraction in a medical LightRAG system.

# Context
- The system processes clinical case reports.
- It extracts:
  - Entities such as diseases, symptoms, drugs, procedures, pathogens, tests, anatomy, and outcomes.
  - Relationships such as `causes`, `associated_with`, `treated_by`, `diagnosed_by`, and other clinically meaningful links.
- Extraction outputs are stored in structured JSON.
- The evaluation must assess both extraction quality and whether the resulting knowledge graph is useful for downstream clinical reasoning tasks.

# Objective
Design and implement an evaluation workflow that:
1. Measures extraction quality with deterministic metrics.
2. Uses `gpt-oss:120b-cloud` via Ollama as an LLM judge for clinical quality assessment.
3. Estimates downstream usefulness for clinical QA, case understanding, and decision-support-style retrieval.
4. Produces a structured Markdown report with metrics, examples, failure modes, and concrete improvement recommendations.

# Deliverables
Produce all of the following:
1. A clear mapping of the code paths responsible for entity and relationship extraction.
2. An evaluation pipeline that can run on a sample dataset.
3. Metric outputs at both per-case and aggregate levels.
4. LLM-judge outputs with consistent structured JSON.
5. A generated Markdown evaluation report.
6. A short summary of gaps, risks, and next optimization steps.

# Requirements

## 1. Locate the extraction pipeline
Identify the relevant files, functions, and execution flow for:
- Entity extraction
- Relationship extraction
- Post-processing, normalization, deduplication, or graph assembly

Document how raw case text becomes extracted JSON and where evaluation hooks should be added.

## 2. Define evaluation metrics
Since we have no gold standard annotations, we must define a combination of proxy metrics and LLM-judge assessments.

### Label-based metrics (if any):
Compute proxy metrics such as:
- Entity density per case
- Duplicate entity rate
- Unique concept coverage
- Relationship density
- Graph connectivity or structural completeness
- Orphan entity rate
- Ratio of clinically meaningful vs. vague entities

State clearly which metrics are label-based and which are proxy metrics.

## 3. LLM-as-judge evaluation
Use `gpt-oss:120b-cloud` via Ollama to score the extracted results.

The judge should evaluate whether:
- Entities are clinically relevant and specific to the case.
- Relationships are medically plausible and logically useful.
- The extracted graph would help downstream clinical question answering or case summarization.

### Score entities on a 1-5 scale for:
- Relevance to the case
- Clinical usefulness
- Specificity
- Normalization quality

### Score relationships on a 1-5 scale for:
- Clinical correctness
- Logical usefulness for reasoning
- Directionality correctness
- Support from the source case text

### Required judge output format
```json
{
  "entity_score": {
    "relevance": 0,
    "clinical_usefulness": 0,
    "specificity": 0,
    "normalization_quality": 0
  },
  "relationship_score": {
    "clinical_correctness": 0,
    "reasoning_usefulness": 0,
    "directionality": 0,
    "text_support": 0
  },
  "overall_assessment": "",
  "key_errors": [],
  "recommendations": []
}
```

Ensure the judge prompt is deterministic enough to support repeated evaluation across multiple cases.

## 4. Downstream usefulness assessment
Assess whether the extracted knowledge is sufficient for practical downstream use.

Include signals such as:
- Coverage of core clinical concepts in each case
- Presence of actionable relationships
- Completeness for case-level reasoning
- Readiness for graph-based retrieval, QA, or decision support

If this must be estimated heuristically, state the heuristic explicitly.

## 5. Sample evaluation run
Before running the pipeline:
- Read `.env` to understand how Ollama and related model settings are configured.
- Identify any other configuration values that may affect extraction or evaluation behavior.

Then:
- Select a small but representative sample from `data_prompted_old`.
- Run the evaluation pipeline on those cases.
- Save the metric outputs, judge outputs, and final report.

## 6. Final report
Generate a Markdown report that includes:
- Scope and dataset sample used
- Extraction pipeline overview
- Metrics and scoring methodology
- Aggregate and per-case results
- Error analysis with concrete examples
- Recommended fixes ranked by likely impact
- Known limitations and validation gaps

# Working expectations
- Do not stop after analysis; implement and run the evaluation flow on a sample.
- If any assumption is required, state it explicitly.
- If a dependency or configuration blocks execution, identify the blocker precisely and continue as far as possible.
- Prefer reproducible outputs and structured artifacts over ad hoc observations.
- Before stopping, verify that every requirement above has been addressed.
