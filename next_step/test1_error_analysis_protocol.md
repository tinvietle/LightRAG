# `test1.json` Error Analysis Protocol

## Purpose

Classify *incorrect* differential-diagnosis answers from
`lightrag/evaluation/results/test1.json` using the four categories from the
PLOS Digital Health paper stored in `next_step/journal.pdig.0001474.pdf`:

1. Context irrelevant
2. Insufficient internal knowledge
3. Reasoning error
4. Question misinterpretation

The paper assigns these labels after expert review of an incorrect answer. They
are not metrics that RAGAS can determine on its own. In particular,
`context_recall` is a retrieval-support metric and is not a diagnosis accuracy
or error-category label.

## Current evidence available

Each result in `test1.json` retains:

- the clinical-case query;
- the reference diagnosis in `ground_truth`;
- the raw RAG answer;
- ordered retrieved chunks, including chunk text, source filename, and a
  reference identifier; and
- RAGAS `context_recall`.

Each case file under `dataset/fold1` contains a `case_text`, an
`extracted_disease_name`, and a `grouped_disease_name`. Retrieved chunk
filenames can therefore be mapped to source disease labels and groups.

Known limitations of the current result file:

- `predicted_disease` is empty; the diagnosis must be parsed from the raw
  answer or adjudicated by a reviewer.
- It retains no retrieval/reranker scores.
- It does not retain the originating source-case path, effective prompt, or
  full runtime configuration.
- Questions are truncated case narratives. In the currently checked run, 86 of
  120 questions can be uniquely linked to a `fold1` case by normalized-prefix
  matching; the remaining 34 need explicit source provenance on a future run.

## Step 1: decide whether an answer is incorrect

This evaluator asks for a differential diagnosis, rather than a single label.
Record both measures before assigning an error category:

- `top_1_correct`: the first diagnosis matches the reference diagnosis (with
  clinically accepted synonym/parent-child matching).
- `gold_in_differential`: the reference diagnosis appears anywhere in the
  requested differential, such as the Top 5.

Answers judged clinically correct are recorded as `correct` and receive no
failure label. Ambiguous aliases, partial matches, or an uncertain rank should
be kept for clinical review rather than forced into an error class.

## Agreed interpretation of the four categories

### 1. Context irrelevant

Incorrect context is not automatically harmful. A neighbouring diagnosis can
be useful when it helps rule out a plausible differential, highlights a
contradictory feature, or explains why it is less likely than the final answer.

Mark `context_irrelevant` only when the answer is incorrect **and** the
retrieved material is so excessive, unrelated, or clinically implausible that
it plausibly contributes to the model proposing impossible or inappropriate
Top-5 diagnoses. The review should consider the full clinical case, not only a
filename match.

Useful automated triage signals:

- no chunks from the gold disease or an appropriately related disease group;
- a high fraction of chunks from unrelated groups;
- early/ranked chunks focused on diseases incompatible with decisive case
  findings; or
- the answer's leading differential mirrors those incompatible retrieved cases.

Final decision: expert review of the query, retrieved chunks, and answer.
The reviewer should note which chunks were harmful and why. Do **not** mark a
case merely because retrieved contexts are different from the final diagnosis.

### 2. Insufficient internal knowledge

This category cannot be assigned confidently from the present RAG-only run.
It will be evaluated after a matched pure-model run is produced.

For each case, compare:

1. the pure-model answer using the same clinical query and answer instruction,
   but no retrieved context; and
2. the RAG-assisted answer in `test1.json`.

Mark `insufficient_internal_knowledge` when:

- the pure-model and RAG-assisted answers are materially similar in their
  wrong conclusion or their key diagnostic gap; **and**
- review finds that the retrieved context did not provide useful evidence that
  should have changed that conclusion.

If the RAG context does contain useful evidence but the RAG answer fails to
use it, do not call this insufficient internal knowledge; it is instead a
candidate reasoning error. If the RAG context is clinically distracting and
appears to push the model toward an impossible diagnosis, prefer
`context_irrelevant`.

### 3. Reasoning error

Reasoning errors are not a primary classification target yet. Do not assign
this as a final label in the first review pass.

Instead, add `uncertain_claims` notes whenever an answer contains a claim that
appears unsupported, contradictory, overconfident, temporally inconsistent, or
clinically questionable in light of the query and retrieved evidence. This
creates an auditable queue for a later clinical reasoning review.

Potential later rule: if relevant, useful context is present and the model
still makes an incorrect inference or ignores decisive evidence, classify the
case as `reasoning_error`.

### 4. Question misinterpretation

This category is considered ruled out for this experiment. The intended task is
fixed: identify a differential diagnosis from the clinical case and provide
relevant supporting information. Record `question_misinterpretation = false`
unless a future dataset or prompt changes the task definition.

## Review order

Use this order to avoid assigning multiple causal labels to the same answer:

1. Determine answer correctness (`top_1_correct` and
   `gold_in_differential`).
2. If correct, record `correct` and stop.
3. Review retrieval material for clinically harmful irrelevance. If present,
   record `context_irrelevant`.
4. After the matched pure-model run is available, compare it with the RAG
   answer. If both are similarly wrong and RAG context was not useful, record
   `insufficient_internal_knowledge`.
5. Otherwise leave the final category as `needs_review`, while recording any
   `uncertain_claims`. Reasoning errors will be evaluated later.

## Suggested review record

Keep the original `test1.json` unchanged. Create a separate review artifact
with one record per test case, for example:

```json
{
  "test_number": 1,
  "source_case_path": "dataset/fold1/test/Abscess/custom_case_1077.json",
  "ground_truth": "Brain abscess",
  "ground_truth_group": "Abscess",
  "top_1_diagnosis": "Odontogenic brain abscess",
  "top_1_correct": true,
  "gold_in_differential": true,
  "retrieval_label_summary": {
    "exact_disease_hits": 0,
    "same_group_hits": 0,
    "unrelated_group_hits": 0
  },
  "harmful_context_chunks": [],
  "pure_model_answer": null,
  "uncertain_claims": [],
  "final_error_category": "correct",
  "reviewer_notes": ""
}
```

The label-summary values above are placeholders; calculate them from the
actual retrieved filenames before review. Keep `harmful_context_chunks` as a
list of filename/chunk-id/reason entries so a future ingestion or retrieval
change can be evaluated against the same evidence.

## Requirements for the next evaluation run

Persist the following with every result so comparisons are reproducible:

- `source_case_path` and a stable source case id;
- the gold disease group;
- the model name and complete query settings (`mode`, `top_k`, `chunk_top_k`,
  reranker state, and context-placement option);
- the effective system/user prompt;
- the parsed Top-1 and full differential; and
- retrieval rank and similarity/reranker scores when the API exposes them.

For the pure-model comparison, retain the same query text, answer instruction,
model, temperature, and decoding settings as the RAG run. The absence of
retrieved context must be the only intentional difference.

## Personal note

The key principle is to judge the *clinical effect* of retrieval, not simple
label overlap. A wrong-disease chunk can be useful negative evidence; a
same-group chunk can still be harmful if it anchors the model on an impossible
diagnosis. Automated disease/group mapping should prioritize cases for review,
while the final `context_irrelevant` decision remains evidence-based expert
judgment.

## Lessons retained from cases 1 and 2

The first two completed reviews establish the following working rules for all
later cases:

| Observation | Review rule | Case-1 / case-2 evidence |
|---|---|---|
| A correct diagnosis can coexist with poor retrieval. | Do not assign a failure category solely because chunks are noisy. Preserve `retrieval_quality_concern` and unsupported-answer claims separately. | Case 1 ranked brain abscess first despite four poor matches. |
| A gold diagnosis somewhere in a Top 5 is not the same as a correct leading diagnosis. | Always record both `top_1_correct` and `gold_in_differential`. | Case 2 included breast abscess at rank 2 but incorrectly promoted lymphoma to rank 1. |
| Filename/group agreement is only a triage signal. | Read the full chunk and judge whether its patient facts, organ, mechanism, and timing apply to the queried case. | A brain-abscess chunk can be useful at diagnosis level, while another patient's surgery or microbiology must not be transferred. |
| The query can itself support a diagnosis. | A correct RAG answer may reflect the clinical narrative and model knowledge rather than useful retrieval. Do not credit retrieval without evidence. | Case 2 had no clinically matched chunk, yet the query's tender rim-enhancing breast lesions supported abscess as a rank-2 diagnosis. |
| Cross-case facts are strong harm evidence. | Flag investigations, imaging, pathology, comorbidities, or histories absent from the query when they resemble retrieved cases. | Case 2 invented PET/CT SUV, mediastinal nodes, arm-biopsy findings, and bone-marrow results. |
| External/source-case verification matters. | Map the truncated question to its source case and verify the eventual diagnosis in the primary report when available. Treat the dataset label as evidence to check, not an infallible assertion. | Case 1's source confirmed odontogenic brain abscesses; case 2's pathology confirmed sterile breast abscesses and excluded malignancy. |
| Retrieval causation is an expert inference, not a logged metric. | Use “plausibly contributes” when answer content mirrors irrelevant chunks. Reserve `context_irrelevant` for an incorrect answer with clinically harmful retrieval; do not claim mathematical proof. | Case 2 cited unrelated contexts and used their cancer/infection narratives to support the wrong lead diagnosis. |
| Reasoning assessment remains deferred. | Record unsupported, contradictory, or temporally expanded statements in a dedicated claims table even when final diagnosis is correct. | Case 1 had source-attribution and timepoint errors; case 2 had fabricated evidence. |

## Standardized reviewer system prompt and output contract

Use the following prompt for any model or reviewer producing a new review
record. It is a review-format prompt only; it does not change LightRAG's RAG
generation prompt or query behavior.

```text
You are reviewing one clinical RAG differential-diagnosis result for the
context-irrelevance study. Use the clinical query, raw RAG answer, ordered
retrieved chunks, mapped source-case labels, and verified source report when
available. Do not treat filename or disease-group overlap as proof of clinical
relevance. Do not transfer patient-specific facts between cases.

First determine answer correctness. Record both whether the Top-1 diagnosis is
clinically equivalent to the verified reference diagnosis and whether the
reference diagnosis appears anywhere in the requested differential. A correct
answer can still have poor retrieval or unsupported explanation claims.

Mark context_irrelevant only if the answer is incorrect AND irrelevant,
excessive, or incompatible retrieved material plausibly contributes to an
inappropriate leading diagnosis or clearly inappropriate Top-5 candidate. This
is an evidence-based causal inference, not proof from retrieval scores. Do not
assign insufficient_internal_knowledge without a matched pure-model answer.
Do not assign reasoning_error as the final category in this first pass; record
reasoning concerns as claims for later review. Set question_misinterpretation
to No unless the task itself was misunderstood.

Output exactly the four sections and Markdown tables specified below. Preserve
all 10 retrieved ranks when 10 chunks are available. Be concise but clinical:
state what evidence supports or contradicts each decision and identify any
cross-case contamination.
```

### Required section 1 — Case and answer verification

| Table field | Required content | Meaning |
|---|---|---|
| Heading | `### Case and answer verification` | Establishes the evidence being adjudicated before judging retrieval. |
| `Result record` | Result index, answer length, retrieved-chunk count | Makes the reviewed result reproducible. |
| `Dataset label` | Reference disease and disease group, plus mapped source-case path | Records the dataset's expected diagnosis and provenance. |
| `External verification` | Verified / unavailable / ambiguous, with primary-report citation where found | Checks the final diagnosis rather than assuming the dataset label is always correct. |
| `Top-1 diagnosis` | Parsed leading answer diagnosis | Identifies the model's primary conclusion. |
| `top_1_correct` | Yes / No / clinically ambiguous, with reason | Main diagnostic accuracy measure. |
| `gold_in_differential` | Yes / No and rank if present | Separates a partially useful differential from a correct leading answer. |
| `Question misinterpretation` | Normally No, with reason | Preserves the experiment's agreed scope. |

### Required section 2 — Retrieved-context audit

| Table field | Required content | Meaning |
|---|---|---|
| Heading | `### Retrieved-context audit` | Reviews retrieval clinically, rank by rank. |
| Per-rank table | `Rank`, `Retrieved file`, `Source diagnosis / group`, `Relation to case`, `Contribution to answer`, `Review finding` | Includes every retrieved chunk. `Relation to case` must be clinical, not filename-only. |
| `Relation to case` | Exact diagnosis match / plausible adjacent / weak generic overlap / poor match / harmful poor match | Indicates whether the source can reasonably inform this patient's differential. |
| `Contribution to answer` | What it could support, distract toward, or why it is unnecessary | Links retrieval content to the answer without assuming causation. |
| `Review finding` | Patient-specific limits: wrong organ, pathogen, timing, population, or non-transferable history | Prevents cross-case fact transfer. |
| Retrieval-summary table | Exact hits, plausible adjacent chunks, poor matches, and answer-cited chunks when available | Gives counts while retaining the rank-level evidence above. |

### Required section 3 — Claims requiring later reasoning review

| Table field | Required content | Meaning |
|---|---|---|
| Heading | `### Claims requiring later reasoning review` | Separates explanation-quality issues from the first-pass error category. |
| Per-claim table | `Answer claim`, `Support in queried case`, `Retrieval risk / issue`, `Review action` | Records each relevant unsupported, contradictory, or overconfident statement. |
| `Support in queried case` | Direct / partial / weak / none | Uses only evidence available in the supplied query; later source facts must be identified as later. |
| `Retrieval risk / issue` | Cross-case contamination, fabricated investigation, causal overreach, temporal expansion, or weak alternative | Explains why the claim needs future reasoning review. |
| `Review action` | Record, retain as weak alternative, or defer | Does not assign `reasoning_error` prematurely. |

### Required section 4 — Final classification

| Table field | Required content | Meaning |
|---|---|---|
| Heading | `### Final classification` | States the first-pass result after all evidence has been audited. |
| `Answer incorrect?` | Yes / No / ambiguous, with reason | Anchors the causal category to diagnostic outcome. |
| `Harmful context present?` | Yes / No / explanation level only, with reason | Distinguishes noisy retrieval from clinically harmful retrieval. |
| `Clearly inappropriate Top-5 diagnosis caused by retrieval?` | Yes / No / uncertain, with reason | Apply the agreed high threshold; use “plausibly” rather than asserting proof. |
| `context_irrelevant` | Yes / No | Yes only when the answer is incorrect and harmful retrieval plausibly contributed. |
| `retrieval_quality_concern` | Yes / No | May be Yes even for a correct answer. |
| `final_error_category` | `correct`, `context_irrelevant`, or `needs_review` in this pass | Use `needs_review` when pure-model comparison or later reasoning adjudication is still required. |

## Matched bypass-comparison protocol (required once `test1_bypass.json` exists)

Use this protocol for every case that has a RAG result and a matched bypass
result. It updates the preliminary RAG-only classification; it does not modify
`test1.json`, `test1_bypass.json`, the dataset, or LightRAG code.

### Preconditions

Before comparing, verify all of the following. If any are unknown, record the
limitation and keep the final category as `needs_review` unless the RAG answer
is already correct.

| Check | Required rule |
|---|---|
| Pairing | Match `test1.json → results[n]` to `test1_bypass.json → results[n]` by test number and confirm the same source case / ground truth. |
| Controlled difference | The intended difference must be retrieved context only. Model, clinical query, response instruction, temperature, and decoding settings should otherwise match. |
| Answer parsing | Extract the RAG and bypass Top-1 diagnosis and whether the gold diagnosis occurs anywhere in each Top 5. Do not compare prose length or style. |
| Retrieval audit | Re-read all RAG chunks and the original query. Determine whether any chunk is useful evidence, merely noise, or plausibly harmful. |
| Reference diagnosis | Use the verified source report when available. If the query is truncated or the label is clinically doubtful, explicitly state that limitation. |

### Mandatory decision order

Apply these steps in order. Do not assign more than one primary final category.

1. Record RAG correctness: `top_1_correct` and `gold_in_differential`.
2. Record bypass correctness using the same definitions.
3. Compare the *key diagnostic conclusion*, not wording. State whether the answers are materially similar: same wrong leading diagnosis, same disease family/aetiology gap, or same decisive omission.
4. Audit whether RAG context supplied useful evidence that should have changed the conclusion.
5. Audit whether RAG context plausibly introduced or reinforced an inappropriate leading/Top-5 diagnosis, especially through copied patient-specific facts.
6. Apply the decision matrix below and update the review index and the case's final category when the bypass result supersedes a preliminary RAG-only label.

### Classification decision matrix

| Observed pattern | Required classification | Reason |
|---|---|---|
| RAG Top-1 is correct, regardless of bypass result | `correct` | A failure category is for an incorrect RAG diagnosis. Record whether retrieval was helpful, neutral, or explanation-contaminating. |
| Bypass wrong; RAG correct; retrieved evidence is relevant or plausibly useful | `correct` | Retrieval delivered material diagnostic benefit. Do **not** call insufficient knowledge. Case 1 is the model example. |
| Bypass correct; RAG wrong; irrelevant RAG chunks plausibly displaced the correct lead or supplied cross-case facts | `context_irrelevant` | Retrieval harmed a model that could otherwise answer correctly. Case 2 is the model example. |
| Bypass and RAG materially share the same wrong diagnosis or decisive aetiology gap; RAG had no useful corrective evidence | `insufficient_internal_knowledge` | The shared failure is attributable to missing model knowledge, not retrieval. Case 3 is the model example. |
| Bypass and RAG differ; RAG is wrong; useful retrieval evidence was present but ignored or misapplied | `needs_review` | Preserve the evidence for later `reasoning_error` adjudication; do not assign it now. |
| Bypass and RAG differ; RAG is wrong; no plausible retrieval-causation evidence and no shared key error | `needs_review` | The comparison is inconclusive. |
| RAG correct but explanation contains copied or unsupported facts | `correct` | Record the facts in claims review. Do not convert a correct diagnostic answer to `context_irrelevant`. Case 4 is the model example. |

### Strict causation rules

- A low-quality retrieval set alone is not `context_irrelevant`.
- Matching disease labels alone is not useful evidence; assess organ, pathogen,
  patient population, timing, imaging, and mechanism.
- Treat cross-case facts as strong retrieval-harm evidence only when the RAG
  answer repeats or relies on them and they are absent from the query.
- A RAG-only error that is more specific than the bypass error is not
  automatically retrieval-caused. Identify a chunk that plausibly supports the
  added error; otherwise preserve `insufficient_internal_knowledge` or
  `needs_review`.
- If the bypass answer is correct, it is strong evidence against
  `insufficient_internal_knowledge` for that case.
- If the bypass answer is wrong, it does not itself prove insufficient
  knowledge: first check whether RAG had useful evidence that it failed to use.

### Required addition to each completed case record

Keep the four existing case-review sections unchanged. After `### Final
classification`, add exactly one `### Matched pure-model comparison` table:

| Comparison field | Required content |
|---|---|
| Bypass result record | Exact bypass file and result index. |
| Pure-model Top-1 diagnosis | Parsed diagnosis and correct/incorrect judgment. |
| RAG Top-1 diagnosis | Parsed diagnosis and correct/incorrect judgment. |
| Material similarity of the two answers | Yes / No, followed by the shared conclusion or key gap. |
| Useful retrieved evidence available to RAG | Yes / No, with rank(s) and clinical reason. |
| `insufficient_internal_knowledge` | Yes / No, with the decision-matrix reason. |
| Retrieval-causation evidence | State harmful chunk ranks and copied facts, or explicitly state that causation is absent. |
| Final classification after bypass comparison | The final category, and whether it supersedes the RAG-only assessment. |

### Updating requirements

After a bypass adjudication:

1. Update the existing case record; do not create a duplicate case heading.
2. If the category changes, retain the original RAG-only decision as
   “superseded” and state why it changed.
3. Update the Review index row to the final category.
4. Run structural validation: four original headings remain present, all ten
   retrieval ranks remain present where available, and the bypass table is
   present exactly once for the reviewed case.
