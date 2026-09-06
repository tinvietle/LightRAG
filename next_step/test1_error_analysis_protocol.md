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
