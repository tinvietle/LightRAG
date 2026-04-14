# Medical LightRAG Evaluation Report

## Scope and Dataset
- Evaluated 3 samples from `data/rag_storage/kv_store_llm_response_cache.json`
- Judge Model: gpt-oss:120b-cloud

## Extraction Pipeline Overview
Entity and Relationship extraction is performed by `lightrag/operate.py:extract_entities()`. It invokes the LLM using prompts defined in `lightrag/prompt.py`. Output is passed through parsers relying on the `<|#|>` delimiter. 

## Aggregate Insights
- Average Entities per Chunk: 30.3
- Average Relationships per Chunk: 20.3

## Per-Case Results
### Sample `chunk-4a79a66b00337542f5feefae79e86ae6`
**Proxy Metrics:**
- Entities: 41, Relations: 23
- Duplicate Entity Rate: 0.0%
- Orphan Entity Rate: 43.9%
- Actionable Relations: 14
- Downstream Readiness Heuristic: ✅ Ready

**LLM Judge Assessment:**
- Overall: The extraction captures most key clinical concepts and their inter‑relations, but contains a few classification inconsistencies and omits some important facts such as the patient’s refusal of chest drainage and outcome details.
- Entity Scores: {"relevance": 8, "clinical_usefulness": 8, "specificity": 7, "normalization_quality": 6}
- Relationship Scores: {"clinical_correctness": 7, "reasoning_usefulness": 7, "directionality": 7, "text_support": 8}
- Errors Found: ‘palliative care’ labeled as a Procedure instead of a Service/Intervention category, Missing entities: patient death, blood and stool cultures, decision to decline tube thoracostomy, duration of ceftriaxone therapy, Redundant or overly generic entities such as “oxygen requirement (4 L…)” classified as ClinicalAttribute rather than a specific Intervention, Some relationships duplicate information already expressed in entity descriptions (e.g., empyema ↔ pleural fluid pH)
- Recommendations: Adopt a stricter ontology mapping (e.g., SNOMED CT) to improve normalization of entities like procedures, services, and clinical attributes., Include outcome‑related entities (death, palliative‑care unit admission) and patient‑decision statements (declined chest drain)., Consolidate redundant entities and ensure each captures a distinct clinical concept., Verify relationship directionality and ensure each relation adds novel clinical insight beyond the entity definitions.

### Sample `chunk-64851c447e8032b8aa4e8926c8a06bbb`
**Proxy Metrics:**
- Entities: 47, Relations: 27
- Duplicate Entity Rate: 0.0%
- Orphan Entity Rate: 46.8%
- Actionable Relations: 17
- Downstream Readiness Heuristic: ✅ Ready

**LLM Judge Assessment:**
- Overall: The extraction captures many pertinent findings, labs, and procedures, but omits the primary disease entity (Empyema due to Salmonella) and introduces several inferred relationships not explicitly stated in the source text, reducing precision and standardization.
- Entity Scores: {"relevance": 8, "clinical_usefulness": 7, "specificity": 6, "normalization_quality": 4}
- Relationship Scores: {"clinical_correctness": 6, "reasoning_usefulness": 6, "directionality": 7, "text_support": 5}
- Errors Found: Missing disease entity for Empyema (especially Empyema due to Salmonella enterica)., Missing entity for Left Ventricular Heart Failure referenced in relationships., Relationships such as Hypertension → Ischaemic Heart Disease and Atrial Fibrillation → Elevated Jugular Venous Pressure are not directly mentioned in the text., Lack of standardized coding (e.g., ICD‑10, SNOMED) for diseases and findings.
- Recommendations: Add a specific disease entity for Empyema due to Salmonella enterica and map it to appropriate coding (e.g., ICD‑10 J86.0 + infectious agent code)., Include the Left Ventricular Heart Failure entity when it appears in relationships., Restrict relationships to those explicitly supported by the narrative; remove or flag speculative links., Normalize all disease, procedure, and lab entities to standard clinical vocabularies to improve interoperability., Review and correct entity types where needed (e.g., ensure LabFinding vs LabValue distinctions are consistent).

### Sample `chunk-64851c447e8032b8aa4e8926c8a06bbb`
**Proxy Metrics:**
- Entities: 3, Relations: 11
- Duplicate Entity Rate: 0.0%
- Orphan Entity Rate: 0.0%
- Actionable Relations: 0
- Downstream Readiness Heuristic: ❌ Insufficient

**LLM Judge Assessment:**
- Overall: Extraction is entirely unrelated to the source case; no correct clinical entities or relationships were captured.
- Entity Scores: {"relevance": 1, "clinical_usefulness": 1, "specificity": 1, "normalization_quality": 1}
- Relationship Scores: {"clinical_correctness": 1, "reasoning_usefulness": 1, "directionality": 1, "text_support": 1}
- Errors Found: All extracted entities (hospital, ethics reference, consent) are absent from the source text., No patient‑specific clinical entities (age, sex, diabetes, hypertension, heart failure, BNP, ejection fraction, medications) were identified., Relationships describe unrelated infectious disease and procedural scenarios not present in the text., Missing normalization to standard clinical vocabularies (e.g., SNOMED CT, LOINC)., Directionality and textual support of relationships are absent.
- Recommendations: Parse the source narrative to extract accurate clinical entities: patient demographics, diagnoses, lab values, imaging findings, and treatments., Map extracted entities to standardized terminologies (SNOMED CT for conditions, LOINC for labs, RxNorm for medications)., Generate relationships that reflect true clinical connections (e.g., 'diabetes mellitus' → 'risk factor for' → 'heart failure', 'IV furosemide' → 'treats' → 'acute decompensated heart failure')., Remove any irrelevant entities or relationships not supported by the text., Implement validation checks to ensure each extracted item is directly supported by a text span.

## Error Analysis and Fixes
### Findings
Based on standard LightRAG implementation, potential pitfalls include high orphan entity rates or inaccurate relation directionalities in complex clinical logic.

### Recommended Optimization Fixes
1. **Entity Normalization:** Enforce strict medical ontology parsing (e.g. SNOMED-CT matching).
2. **Missing Relationships:** Consider multi-hop Gleaning or increasing `entity_extract_max_gleaning`.
3. **Judge Model Constraints:** If local `gpt-oss:120b-cloud` fails to load, ensure appropriate hardware or quantizations.

