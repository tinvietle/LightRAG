You are extracting model input boundaries for disease prediction from clinical case reports.

Task:
Identify the latest PRE-DIAGNOSIS presentation span that remains diagnostically
useful without revealing the final disease label.

Keep clinically useful, non-leaking evidence, including:
- Initial complaint, history, symptoms, timeline, and examination findings
- Exposure, travel, medication, and relevant host-risk history
- Descriptive imaging findings and non-specific laboratory abnormalities
- Tests ordered before their results are known, only when the order itself does
  not name, strongly imply, or make the final disease near-certain

Do NOT include:
1. The final confirmed diagnosis or an unambiguous synonym
2. A disease-identifying microbiology, molecular, serologic, genetic, or pathology result
3. Definitive disease-specific treatment, surgery, or treatment response/outcome
4. A differential, suspected diagnosis, or initial misdiagnosis that names the final disease

Boundary rules:
- The retained span must be one continuous source span. Do not skip a leaking
  sentence and resume the span later.
- Keep the latest complete non-leaking sentence before the first prohibited
  sentence that would reveal the final disease.
- Never cut in the middle of a sentence.
- End the span immediately before a sentence containing prohibited information.
- A disease-specific test order may be retained only when its wording does not
  explicitly name, strongly imply, or uniquely target the final disease.

Evidence-sufficiency gate:
- Before returning anchors, decide whether the retained span gives a model enough
  information to distinguish the final disease at the evaluation's required
  granularity from plausible alternatives. If exact subtype, species, or site is
  required, the retained evidence must support that exact granularity.
- A sufficient span normally contains the presentation plus at least two relevant,
  non-leaking discriminators, such as objective findings, exposure/risk history,
  temporal pattern, or descriptive imaging/laboratory findings.
- If sufficient non-leaking evidence is unavailable, set both anchors and both
  offsets to null and set evidence_sufficient to false. Do not compensate by
  including diagnosis leakage.

Output requirements:
- Return only valid JSON.
- Anchors must be exact source substrings of 8-20 consecutive words.
- `start_char` and `end_char` are zero-based source-character offsets, with
  `end_char` exclusive. They are the authoritative extraction boundaries; anchors
  are retained for human audit.
- start_anchor begins the kept span.
- end_anchor is the final words of the kept span and ends at a sentence boundary.
- leakage_type must be one of: "none", "diagnosis", "diagnostic_result",
  "treatment_or_outcome", "target_in_differential", or "insufficient_evidence".
- Use `insufficient_evidence` only if no adequate non-leaking span exists.
  Otherwise, report the first prohibited information that determines the boundary.

{
  "start_anchor": "<8-20 exact words or null>",
  "end_anchor": "<8-20 exact words or null>",
  "start_char": 0,
  "end_char": 0,
  "evidence_sufficient": true,
  "leakage_type": "<allowed value>",
  "split_note": "<one concise sentence explaining the boundary or insufficiency>"
}
