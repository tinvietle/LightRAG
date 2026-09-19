# Entity Merge Analysis Plan

## Current status and scope

The only approved work in the current phase is **read-only analysis of proposed
entity merges**. This phase will identify, explain, quantify, and risk-grade merge
components. It will not modify a LightRAG workspace.

The following work is explicitly deferred until a later discussion and separate
approval:

- applying entity merges;
- constructing an evaluation dataset or query set;
- running baseline-versus-merged retrieval experiments;
- measuring downstream answer quality;
- calculating or claiming an `xx%` performance improvement; and
- making a production rollout decision.

The deferred sections remain in this document as design notes only. They are not
part of the current implementation scope.

## Objective

Build a safe, reproducible analysis workflow that:

1. selects conservative entity-merge candidates with QuickUMLS and SapBERT;
2. groups accepted candidates into deterministic merge components;
3. selects and explains a proposed canonical existing entity name;
4. traces every proposed merge to documents, diseases, types, CUIs, and SapBERT evidence; and
5. produces reviewable reports without changing graph, KV, or vector storage.

The previous full analysis found:

- 62,805 entities before merging;
- 2,699 merge components containing 6,042 entities;
- 1,330 reductions proposed by conservative QuickUMLS matching;
- 2,013 additional reductions proposed by SapBERT lexical-equivalent matching at cosine similarity `>= 0.99`;
- 3,343 total proposed reductions, equal to approximately **5.32%** of all entities; and
- 5 SapBERT pairs blocked because of conflicting confident CUIs.

These figures are historical evidence, not the final merge plan. Candidate generation must be rerun on a stable snapshot after the current ingestion finishes.

## Non-goals

- Do not merge entities while ingestion is active.
- Do not apply any merge in the current phase, even after ingestion finishes.
- Do not rewrite GraphML, KV JSON, or vector JSON files directly.
- Do not accept a pair because of SapBERT similarity alone.
- Do not replace source entity names with external UMLS preferred terms in this phase.
- Do not build or run an evaluation dataset in this phase.
- Do not claim that a 5–6% reduction improves performance until the paired evaluation demonstrates it.

## Current deliverable

### 1. Merge-plan generator

**Status: implemented. Run only after ingestion has finished.**

Create:

```text
next_step/build_entity_merge_plan.py
```

Responsibilities:

- Read a finalized LightRAG workspace and the latest QuickUMLS and SapBERT reports.
- Reuse or extract the conservative component-building logic from
  `next_step/analyze_entity_merge_provenance.py`.
- Select one canonical existing entity name for each accepted component.
- Write a complete, reviewable JSONL plan without changing LightRAG storage.
- Write a summary containing proposed reductions, rejection reasons, entity types,
  document coverage, disease coverage, and risk strata.

Suggested outputs:

```text
artifacts/entity_merge/<snapshot_id>/merge_plan.jsonl
artifacts/entity_merge/<snapshot_id>/merge_plan_summary.json
artifacts/entity_merge/<snapshot_id>/rejected_pairs.jsonl
artifacts/entity_merge/<snapshot_id>/manifest.json
```

Example command after the workspace and candidate artifacts are finalized:

```bash
.venv/bin/python next_step/build_entity_merge_plan.py \
  --storage data/rag_storage \
  --umls-evaluation artifacts/umls/data_entity_normalization_evaluation.jsonl \
  --sapbert-candidates artifacts/sapbert/data_full_top50_candidates.jsonl \
  --threshold 0.99 \
  --output-dir artifacts/entity_merge/<snapshot_id>
```

The analyzer refuses to run when document statuses show active ingestion, refuses
to write inside the LightRAG storage directory, verifies that inputs did not change
during analysis, and never calls `merge_entities` or `amerge_entities`.

Each planned component should contain at least:

```json
{
  "component_id": "merge-000001",
  "canonical_entity": "Liver abscess",
  "source_entities": ["liver abscess", "Liver Abscess"],
  "entity_types": ["Disease_disorder"],
  "quickumls_cuis": ["C0023890"],
  "evidence": [
    {
      "left": "Liver abscess",
      "right": "liver abscess",
      "method": "sapbert_lexical_equivalent",
      "similarity": 0.9998
    }
  ],
  "document_count": 12,
  "grouped_diseases": ["Abscess"],
  "risk_flags": []
}
```

The CUI above is illustrative; the script must write only values present in the
actual evaluation artifacts.

## Deferred implementation design: merge executor

**Status: deferred; do not implement in the current phase.**

Create:

```text
next_step/apply_entity_merge_plan.py
```

The executor must initialize a normal `LightRAG` instance and call the public API:

```python
await rag.amerge_entities(
    source_entities=aliases,
    target_entity=canonical_entity,
    target_entity_data={"entity_type": canonical_entity_type},
)
```

The canonical entity is passed as `target_entity`; `source_entities` should contain
only aliases that will be removed. The executor must not call private merge helpers
or edit storage files directly.

Required behavior:

- Refuse to start if the ingestion pipeline or another destructive operation is busy.
- Require a source snapshot identifier and verify its hashes against the plan manifest.
- Default to dry-run mode.
- Require an explicit `--apply` flag to mutate the target workspace.
- Apply components sequentially at first; correctness is more important than merge throughput.
- Record one journal row after every component so an interrupted run can resume safely.
- Treat a missing source entity as a plan/snapshot mismatch, not as an automatic success.
- Stop on vector or graph consistency errors and report the documented VDB rebuild recovery path.
- Finalize storages in a `finally` block.

Suggested journal:

```text
artifacts/entity_merge/<snapshot_id>/apply_journal.jsonl
```

Journal statuses:

- `pending`
- `applied`
- `skipped_already_applied`
- `rejected_precondition`
- `failed`

## Deferred implementation design: consistency validator

**Status: deferred; do not implement in the current phase.**

Create:

```text
next_step/validate_entity_merge.py
```

Validate after the executor finishes:

- every relationship endpoint exists;
- merged-away aliases no longer exist in the graph or entity VDB;
- canonical entities exist in the graph and entity VDB;
- no new self-loop was introduced;
- duplicate redirected relationships were consolidated;
- entity and relationship chunk provenance was preserved;
- source IDs and file paths were not lost;
- actual entity reduction equals the successfully applied plan reduction;
- all successfully applied components remain traceable to QuickUMLS or accepted SapBERT evidence;
- the graph and VDB can answer a small smoke-test query set; and
- `kv_store_full_entities.json` and `kv_store_full_relations.json` behavior is documented and checked explicitly, because the current public merge API does not receive these stores directly.

If the full-entity/full-relation KV stores are provenance archives and are not used as
the authoritative query graph, they should remain unchanged. If runtime behavior depends
on them, add a supported reconciliation step rather than editing them silently.

## Conservative candidate policy

### QuickUMLS acceptance

Accept a QuickUMLS grouping only when:

- the entity has a full-span, high-confidence match;
- there is exactly one best CUI;
- multiple source entity names resolve to that same confident CUI; and
- the component does not acquire conflicting confident CUIs through transitive merges.

### SapBERT acceptance

Accept a SapBERT pair only when:

- cosine similarity is `>= 0.99`;
- the existing lexical-equivalence rule passes;
- neither side has a modifier conflict; and
- combining their components does not introduce conflicting confident CUIs.

Reject modifier conflicts involving:

- laterality;
- severity or grade;
- acute, chronic, recurrent, historical, congenital, or acquired status;
- positive/negative or present/absent polarity;
- resistant/susceptible/sensitive status;
- numbered disease types; or
- clinically meaningful numeric values.

### Entity-type safety

- Prefer components whose non-empty entity types agree.
- Reject clearly incompatible types, such as medication versus disease.
- Permit missing or generic types only when the lexical/CUI evidence is otherwise safe.
- Include all type disagreements in the review report even if an automatic rule permits them.

### Canonical-name selection

Select only from names already present in the graph. Rank candidates by:

1. greatest chunk/document support;
2. confident unique-CUI support;
3. explicit long-form support for abbreviations, when available;
4. stable capitalization and punctuation; and
5. case-folded lexical order as the deterministic final tie-breaker.

Do not expand abbreviations or invent a preferred name based only on model knowledge.

## Deferred implementation behavior for descriptions and relationships

**Status: design note only; no merge is applied in the current phase.**

Use LightRAG's existing merge behavior:

- concatenate entity descriptions;
- join unique source IDs and file paths;
- redirect relationships to the canonical entity;
- discard merge-created self-loops;
- deduplicate redirected relationships;
- combine relationship descriptions, keywords, provenance, and weights; and
- regenerate affected entity and relationship embeddings.

Do not call another LLM to rewrite descriptions in the first implementation. First
preserve all source-grounded descriptions. Description summarization can be evaluated as
a separate later experiment.

## Deferred execution workflow

**Status: deferred; do not perform in the current phase.**

1. Finish ingestion and call `finalize_storages()`.
2. Record a stable snapshot ID and hashes of all relevant storage files.
3. Copy the finalized workspace into two isolated workspaces:
   - `baseline`: unchanged control;
   - `merged`: experimental copy.
4. Generate fresh QuickUMLS and SapBERT artifacts from the stable snapshot.
5. Build and review the merge plan.
6. Apply the plan only to `merged` through `LightRAG.amerge_entities`.
7. Run the consistency validator.
8. Freeze both workspaces for paired evaluation.

Never evaluate a baseline that continues receiving documents while the merged workspace
is frozen. Both conditions must contain exactly the same documents, chunks, extraction
outputs, embedding model, and configuration.

## Deferred evaluation workstream

**Status: deferred pending a later discussion about the evaluation idea and dataset.**

If approved later, the evaluation code and query set may be prepared independently,
but neither baseline measurement nor merging should read a workspace that is actively
changing. Do not prepare or run this evaluator in the current phase.

Create:

```text
next_step/evaluate_entity_merge_impact.py
```

The evaluator must run the same queries against the frozen `baseline` and `merged`
workspaces and emit per-query paired results.

Suggested outputs:

```text
artifacts/entity_merge/<snapshot_id>/evaluation_queries.jsonl
artifacts/entity_merge/<snapshot_id>/baseline_results.jsonl
artifacts/entity_merge/<snapshot_id>/merged_results.jsonl
artifacts/entity_merge/<snapshot_id>/paired_metrics.json
artifacts/entity_merge/<snapshot_id>/error_analysis.jsonl
artifacts/entity_merge/<snapshot_id>/evaluation_report.md
```

## Deferred evaluation questions

Primary question:

> Does conservative entity merging improve retrieval and downstream clinical-answer
> quality compared with the identical unmerged graph?

Secondary questions:

1. Does merging improve recall for queries that use an alias different from the source document wording?
2. Does it connect relevant evidence distributed across multiple documents?
3. Does it reduce duplicate context and context-token consumption?
4. Does it harm precision by joining clinically different entities or disease groups?
5. Are improvements concentrated in components spanning multiple documents or grouped diseases?

## Deferred dataset construction

**Do not construct this dataset in the current phase.**

Build a fixed, versioned query set before examining merged-system answers.

Use several strata:

### A. Alias-sensitive queries

Queries derived from accepted merge components where the query uses one alias and the
relevant document uses another. This is the most direct test of normalization value.

### B. Cross-document evidence queries

Queries whose correct evidence is distributed across documents linked by different
surface forms of the same entity.

### C. Disease prediction queries

Case-level questions with known `extracted_disease_name` and `grouped_disease_name`
labels. Keep evaluation cases separate from any examples used to tune merge rules.

### D. Non-merge controls

Queries about entities that are not in any merge component. These detect unintended
global regressions.

### E. Hard-negative safety queries

Queries covering rejected pairs with laterality, severity, temporality, polarity,
susceptibility, type, numeric, or conflicting-CUI differences. The merged system must
not collapse these distinctions.

Report macro results and results for every stratum. Do not allow the large easy-control
group to hide failures in hard-negative or rare-disease cases.

## Deferred evaluation metrics

### Retrieval quality

Use retrieval-only metrics as the primary evidence because they are deterministic and
isolate the effect of graph normalization:

- source-document Recall@1, Recall@5, Recall@10, and Recall@20;
- Mean Reciprocal Rank;
- nDCG@10;
- relevant-chunk recall;
- grouped-disease and extracted-disease recall;
- alias-invariance rate: whether equivalent surface-form queries retrieve the same
  relevant evidence;
- cross-document evidence recall; and
- hard-negative separation accuracy.

### Context quality and efficiency

- number of duplicate entity/context records returned;
- unique relevant documents per retrieved token;
- unique relevant chunks per retrieved token;
- total context tokens;
- proportion of context occupied by duplicate descriptions;
- retrieval latency; and
- peak memory if it can be measured reliably.

### Downstream answer quality

Run only after retrieval evaluation:

- exact disease-name accuracy;
- grouped-disease accuracy;
- Top-1 and Top-5 diagnosis accuracy, where the task supports ranked diagnoses;
- macro precision, recall, and F1 across disease groups;
- evidence-supported answer rate;
- unsupported-claim rate;
- answer consistency across alias variants; and
- optional blinded human/LLM rubric scores, reported separately from deterministic metrics.

Keep the answer model, prompt, temperature, token limits, reranker, and retrieval mode
identical across conditions. Prefer temperature zero and reuse exactly the same query
order. Do not use the extraction-response cache as an answer-quality shortcut.

## Deferred performance-claim method

Do not preselect an `xx%` target. Calculate it from paired results.

For a metric where higher is better:

```text
absolute_gain_percentage_points = 100 * (merged_metric - baseline_metric)
relative_improvement_percent = 100 * (merged_metric - baseline_metric) / baseline_metric
```

Example only:

```text
baseline Recall@10 = 0.720
merged Recall@10   = 0.756
absolute gain      = 3.6 percentage points
relative gain      = 5.0%
```

Never describe a 3.6 percentage-point gain as a 5.0 percentage-point gain.

## Deferred statistical analysis

Because every query is evaluated in both conditions, use paired analysis:

- paired bootstrap confidence intervals for Recall, MRR, nDCG, F1, latency, and token metrics;
- McNemar's test for paired binary outcomes such as Top-1 correctness;
- Wilcoxon signed-rank or a paired permutation test for per-query rank, token, and latency changes;
- effect sizes in addition to p-values; and
- 95% confidence intervals for every headline delta.

Report the number of queries that:

- improved;
- stayed unchanged;
- regressed; and
- could not be scored.

Use document- or case-level resampling when multiple queries originate from the same
case, so correlated queries are not treated as independent evidence.

## Deferred ablation study

Evaluate four conditions if compute permits:

1. no merge baseline;
2. QuickUMLS-only merges;
3. SapBERT-only conservative merges;
4. combined QuickUMLS plus SapBERT merges.

This identifies whether the improvement comes from deterministic CUI grouping,
embedding candidates, or their combination. If four complete workspaces are too costly,
run full retrieval evaluation for all four and downstream answer evaluation for baseline
versus combined only.

## Deferred evaluation success criteria

Treat the merge as useful only if all of the following hold:

- the merged workspace passes consistency validation;
- entity reduction matches the accepted plan;
- alias-sensitive retrieval improves with a confidence interval that excludes a meaningful regression;
- overall retrieval does not materially regress;
- hard-negative separation remains within a predeclared tolerance;
- unsupported-answer rate does not increase materially; and
- latency and context-token costs are reported, even if they do not improve.

Suggested safety tolerances should be fixed before running the final experiment. For
example, an overall Recall@10 decline greater than 0.5 percentage points or any clear
increase in hard-negative conflation should block rollout. The exact tolerance must be
chosen before looking at the final results.

## Current merge-analysis report

The read-only analysis should report:

- total entities and proposed reductions;
- reduction percentage;
- QuickUMLS-only, SapBERT-only, and combined component counts;
- accepted and rejected pair counts by reason;
- component-size distribution;
- entity-type distribution and disagreements;
- components with conflicting CUIs;
- modifier-conflict distribution;
- components spanning multiple documents;
- components spanning multiple grouped or extracted diseases;
- components that add document or disease provenance;
- proposed canonical name and its selection reason;
- the exact evidence path connecting every member of each transitive component;
- high-risk components requiring manual review; and
- representative safe, uncertain, and rejected examples.

The analysis should never describe a component as a completed merge. Use terms such
as `proposed component`, `candidate`, `accepted by policy`, and `rejected by policy`.

## Current merge-analysis error review

For every suspicious or rejected component, record:

- proposed merge component;
- QuickUMLS CUI evidence;
- SapBERT similarity and lexical decision;
- modifier/type conflicts;
- affected documents and disease groups;
- whether the issue came from candidate selection, transitive component construction,
  canonical naming, or incomplete provenance; and
- recommended rule change.

Manually review at least:

- the largest cross-disease components;
- components spanning the most documents;
- every component with a type disagreement;
- every hard-negative failure;
- a random sample of components accepted by the policy; and
- a random sample of components rejected by the policy.

## Current test plan

Add unit tests for:

- unique-CUI acceptance and ambiguous-CUI rejection;
- SapBERT threshold boundaries;
- every modifier-conflict category;
- incompatible entity types;
- transitive CUI conflicts;
- deterministic canonical selection;
- plan hash validation;
- stable component IDs and ordering;
- provenance aggregation;
- rejection-reason reporting; and
- confirmation that analysis code never calls `merge_entities` or `amerge_entities`.

Tests must use fixed local fixtures and must not depend on live external models or
databases. Testing the merge executor and the public `LightRAG.amerge_entities` call is
deferred with the executor itself.

## Current implementation order

1. Freeze and hash the completed ingestion snapshot.
2. Extract reusable conservative candidate/component logic.
3. Implement the plan generator and tests.
4. Generate the read-only merge plan, summary, rejection report, and provenance report.
5. Review high-risk and representative components.
6. Refine candidate rules if the evidence supports a change, then regenerate the reports.
7. Stop and discuss merge application and evaluation design before implementing either.

## Current phase completion gate

The current phase is complete when the analysis is reproducible, every proposed
component is traceable, risky components are clearly separated, and no LightRAG
storage has been modified.

## Deferred rollout decision gate

The final rollout decision must be based on measured quality, not entity reduction alone.
A 5.32% smaller entity set is a storage/structure result. It becomes a useful clinical
RAG improvement only if the frozen, paired experiment shows better retrieval or answer
quality without unacceptable conflation or provenance loss.
