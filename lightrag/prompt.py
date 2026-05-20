from __future__ import annotations
from typing import Any


PROMPTS: dict[str, Any] = {}

# All delimiters must be formatted as "<|UPPER_CASE_STRING|>"
PROMPTS["DEFAULT_TUPLE_DELIMITER"] = "<|#|>"
PROMPTS["DEFAULT_COMPLETION_DELIMITER"] = "<|COMPLETE|>"

PROMPTS["generate_image_description"] = """You are a medical image content extractor.

Describe only medically relevant visible content from the image in short factual sentences.

Focus on:
- visible anatomy
- visible lesions, masses, collections, edema, bleeding, fracture, obstruction, deformity, or abnormal signal/density
- visible devices, tubes, catheters, drains, clips, sutures, or implants only if clinically relevant
- visible procedures or operative findings only if clearly shown
- visible labels, annotations, arrows, measurements, or modality text
- image modality or view if visible (for example MRI, CT, X-ray, ultrasound, operative photo)

Do not describe generic photography details, composition, color aesthetics, lighting, background, zoom, framing, or non-medical objects unless clinically relevant.
Do not speculate about diagnosis, mechanism, intent, or hidden findings.
Do not describe every object in the image.
If the image has little or no clear medical content, say that briefly.

Return 3 to 8 short lines, plain text only."""

PROMPTS["refine_image_description"] = """You are a strict medical relevance filter.

Re-examine the image and the draft description below.
Keep only concise, medically useful, visibly supported facts.

Rules:
- remove generic scene description
- remove speculative or inferred diagnosis
- remove low-value visual details
- keep visible anatomy, abnormalities, devices, procedure findings, labels, measurements, and modality/view
- prefer precise medical wording when clearly supported
- if uncertain, describe the visible finding without over-interpreting it
- keep the final result short and clinically useful for downstream entity extraction

DRAFT_DESCRIPTION:
{initial_output}

Return 3 to 8 short lines, plain text only."""

# PROMPTS["entity_extraction_system_prompt"] = """---Role---
#   You extract a medical knowledge graph from clinical or biomedical text.

#   ---Instructions---
#   1. Extract only clinically meaningful entities and relations that are explicitly supported by the input text.
#   2. Focus on medically relevant content such as diseases, symptoms, clinical signs, laboratory findings, pathogens, anatomy, drugs, procedures, tests, measurements, and relevant risk
#   factors.
#   3. Ignore non-clinical or low-value details such as routine logistics, generic administration, or incidental workflow text unless they are medically relevant.

#   4. Copy `entity_name`, `source_entity`, and `target_entity` exactly as they appear in the input text.
#   5. Do not normalize, translate, expand abbreviations, change capitalization, or paraphrase those fields.
#   6. Use only the provided entity types: `{entity_types}`.
#   7. If none apply, use `Other`.

#   8. Each entity must be output on one line in this exact format:
#   `entity{tuple_delimiter}entity_name{tuple_delimiter}entity_type{tuple_delimiter}entity_description`

#   9. Entity descriptions must be short, factual, and based only on the input text.
#   10. Include clinically relevant details when explicitly stated, such as severity, stage, laterality, onset, dosage, or test value.

#   11. Create relations only between extracted entities.
#   12. If one statement implies multiple pairwise relations, decompose it into separate binary relations.
#   `identified_by`, `confirms`, `equivalent_to`
#   14. Do not invent any relationship keyword outside this list.
#   15. Keep relation direction clinically sensible and directly supported by the text.

#   16. Each relation must be output on one line in this exact format:
#   `relation{tuple_delimiter}source_entity{tuple_delimiter}target_entity{tuple_delimiter}relationship_keywords{tuple_delimiter}relationship_description`

#   17. Relation descriptions must be short, factual, and based only on the input text.
#   22. If no valid entities or relations are found, output only `{completion_delimiter}`.

#   23. `{tuple_delimiter}` is an atomic field separator. Use it only to separate fields.
#   24. Output only valid extraction lines and the final `{completion_delimiter}`.
#   25. Do not output commentary, markdown, JSON, bullets, headings, or explanatory text.
#   entity{tuple_delimiter}amoxicillin-clavulanate{tuple_delimiter}Drug{tuple_delimiter}amoxicillin-clavulanate is the antibiotic started for treatment.
#   relation{tuple_delimiter}amoxicillin-clavulanate{tuple_delimiter}community-acquired pneumonia{tuple_delimiter}treats{tuple_delimiter}The text states that amoxicillin-clavulanate was
#   started for community-acquired pneumonia.
#   {completion_delimiter}
#   """

PROMPTS["entity_extraction_system_prompt"] = """---Role---
You extract a medical knowledge graph from clinical or biomedical text, including short medical image descriptions.

---Instructions---
1. Extract only clinically meaningful entities and relations that are explicitly supported by the input text.
2. Focus on high-value medical content such as:
- diseases and diagnoses
- symptoms and clinical signs
- anatomy and anatomical locations
- lesions, collections, masses, edema, hemorrhage, infarct, abscess, consolidation, thrombosis, and other abnormal findings
- pathogens
- drugs and treatments
- procedures and tests
- devices or operative findings only when clinically relevant
- measurements, labeled findings, and risk factors

3. Ignore low-value visual or narrative noise such as:
- generic scene wording like close-up, image, photo, view, background, lighting, color, layout
- incidental instruments or materials unless clinically relevant
- repeated descriptive filler
- unsupported interpretation from outside knowledge

4. Copy `entity_name`, `source_entity`, and `target_entity` exactly as they appear in the input text.
5. Do not normalize, translate, expand abbreviations, change capitalization, or paraphrase those fields.
6. Use only the provided entity types: `{entity_types}`.
7. If none apply, use `Other`.

8. Each entity must be output on one line in this exact format:
`entity{tuple_delimiter}entity_name{tuple_delimiter}entity_type{tuple_delimiter}entity_description`

9. Entity descriptions must be short, factual, and text-grounded.
10. Prefer the most clinically informative entities rather than every visible object.

11. Create relations only between extracted entities.
12. If one statement implies multiple pairwise relations, split it into separate binary relations.
13. Use only one relationship keyword per relation, chosen from this list:
`causes`, `complicates`, `treats`, `indicates`, `characterized_by`, `risk_factor_for`, `complication_of`, `contraindicated_with`, `associated_with`, `monitored_by`, `influences`, `identified_by`, `confirms`, `equivalent_to`
14. Do not invent any relationship keyword outside this list.
15. Keep relation direction clinically sensible and directly supported by the text.

16. Each relation must be output on one line in this exact format:
`relation{tuple_delimiter}source_entity{tuple_delimiter}target_entity{tuple_delimiter}relationship_keywords{tuple_delimiter}relationship_description`

17. Relation descriptions must be short, factual, and text-grounded.
18. Do not infer unsupported entities or relations from outside knowledge.

19. When the input includes image-derived text:
- prefer abnormal findings over generic visual terms
- prefer clinically relevant anatomy over broad scene labels
- extract devices or operative findings only if they matter medically
- do not create entities such as close-up, image, picture, surgical field, or generic instrument names unless they are clinically important in context

20. Output all entity lines first.
21. Then output all relation lines.
22. Then output `{completion_delimiter}` on its own final line.
23. If no valid entities or relations are found, output only `{completion_delimiter}`.

24. `{tuple_delimiter}` is an atomic field separator. Use it only to separate fields.
25. Output only valid extraction lines and the final `{completion_delimiter}`.
26. Do not output commentary, markdown, bullets, JSON, headings, or explanatory text.

---Example---
entity{tuple_delimiter}brain abscess{tuple_delimiter}Disease{tuple_delimiter}brain abscess is the abnormal finding described in the text.
entity{tuple_delimiter}ring-enhancing lesion{tuple_delimiter}LabFinding{tuple_delimiter}ring-enhancing lesion is the visible imaging abnormality described in the text.
relation{tuple_delimiter}ring-enhancing lesion{tuple_delimiter}brain abscess{tuple_delimiter}indicates{tuple_delimiter}The text states that the ring-enhancing lesion supports the presence of brain abscess.
{completion_delimiter}
"""

PROMPTS["entity_extraction_user_prompt"] = """---Task---
Extract clinically meaningful entities and relationships from the clinical case text in Data to be Processed below.

---Instructions---
1.  **Strict Adherence to Format:** Strictly adhere to all format requirements for entity and relationship lists, including output order, field delimiters, and exact-match entity naming rules, as specified in the system prompt.
2.  **Output Content Only:** Output *only* the extracted list of entities and relationships. Do not include any introductory or concluding remarks, explanations, or additional text before or after the list.
3.  **Completion Signal:** Output `{completion_delimiter}` as the final line after all relevant entities and relationships have been extracted and presented.
4.  **Output Language:** Ensure the output language is {language}.
5.  **Exact-Match Extraction:** For `entity_name`, `source_entity`, and `target_entity`, copy the exact text span from the input text. Do not normalize to canonical terminology.

---Data to be Processed---
<Entity_types>
[{entity_types}]

<Input Text>
```
{input_text}
```

<Output>
"""

PROMPTS["entity_continue_extraction_user_prompt"] = """---Task---
Based on the last extraction task, identify and extract any **missed or incorrectly formatted** clinically meaningful entities and relationships from the input text.

---Instructions---
1.  **Strict Adherence to System Format:** Strictly adhere to all format requirements for entity and relationship lists, including output order, field delimiters, and exact-match entity naming rules, as specified in the system instructions.
2.  **Focus on Corrections/Additions:**
    *   **Do NOT** re-output entities and relationships that were **correctly and fully** extracted in the last task.
    *   If a clinically significant entity or relationship was **missed** in the last task, extract and output it now according to the system format.
    *   If an entity or relationship was **truncated, had missing fields, or was otherwise incorrectly formatted** in the last task, re-output the *corrected and complete* version in the specified format.
3.  **Output Format - Entities:** Output a total of 4 fields for each entity, delimited by `{tuple_delimiter}`, on a single line. The first field *must* be the literal string `entity`.
4.  **Output Format - Relationships:** Output a total of 5 fields for each relationship, delimited by `{tuple_delimiter}`, on a single line. The first field *must* be the literal string `relation`.
5.  **Output Content Only:** Output *only* the extracted list of entities and relationships. Do not include any introductory or concluding remarks, explanations, or additional text before or after the list.
6.  **Completion Signal:** Output `{completion_delimiter}` as the final line after all relevant missing or corrected entities and relationships have been extracted and presented.
7.  **Output Language:** Ensure the output language is {language}.
8.  **Exact-Match Extraction:** For corrected or added entities/relations, ensure `entity_name`, `source_entity`, and `target_entity` are exact text matches from the input text.

---Data to be Processed---
<Entity_types>
[{entity_types}]

<Input Text>
```
{input_text}
```

<Output>
"""

PROMPTS["entity_extraction_examples"] = [
    """<Entity_types>
["Disease", "Symptom", "Drug", "Procedure", "LabTest", "LabFinding", "Anatomy", "Pathogen", "RiskFactor", "ClinicalSign", "Allergy", "Complication", "MedicalDevice", "Specialty", "Other"]

<Input Text>
```
A patient with Type 2 Diabetes Mellitus presented with fever and productive cough. Chest X-ray showed right lower lobe consolidation. Blood culture confirmed Streptococcus pneumoniae. Amoxicillin-clavulanate was started for community-acquired pneumonia.
```

<Output>
entity{tuple_delimiter}Community-Acquired Pneumonia{tuple_delimiter}Disease{tuple_delimiter}Community-Acquired Pneumonia is the primary diagnosis in this case, characterized by fever, productive cough, pleuritic chest pain, and right lower lobe consolidation on chest X-ray.
entity{tuple_delimiter}Streptococcus Pneumoniae{tuple_delimiter}Pathogen{tuple_delimiter}Streptococcus pneumoniae is a Gram-positive diplococcus identified as the causative pathogen of community-acquired pneumonia in this patient, confirmed by blood culture.
entity{tuple_delimiter}Fever{tuple_delimiter}Symptom{tuple_delimiter}High-grade fever (39.5°C) is a presenting symptom of community-acquired pneumonia, present for 5 days in this patient.
entity{tuple_delimiter}Right Lower Lobe Consolidation{tuple_delimiter}LabFinding{tuple_delimiter}Right lower lobe consolidation is a radiological finding on chest X-ray indicating airspace disease consistent with bacterial lobar pneumonia.
entity{tuple_delimiter}Chest X-Ray{tuple_delimiter}Procedure{tuple_delimiter}Chest X-ray is an imaging procedure performed in this patient that revealed right lower lobe consolidation, supporting the diagnosis of pneumonia.
entity{tuple_delimiter}Blood Culture{tuple_delimiter}Procedure{tuple_delimiter}Blood culture is a microbiological procedure performed in this patient that confirmed Streptococcus pneumoniae bacteremia two days after admission.
entity{tuple_delimiter}Amoxicillin-Clavulanate{tuple_delimiter}Drug{tuple_delimiter}Amoxicillin-clavulanate is a beta-lactam/beta-lactamase inhibitor combination antibiotic prescribed empirically for community-acquired pneumonia in this patient.
relation{tuple_delimiter}Streptococcus Pneumoniae{tuple_delimiter}Community-Acquired Pneumonia{tuple_delimiter}causes{tuple_delimiter}Streptococcus pneumoniae was confirmed by blood culture as the causative organism of community-acquired pneumonia in this patient.
relation{tuple_delimiter}Community-Acquired Pneumonia{tuple_delimiter}Fever{tuple_delimiter}characterized_by{tuple_delimiter}High-grade fever is a systemic inflammatory response to pneumococcal pneumonia.
relation{tuple_delimiter}Right Lower Lobe Consolidation{tuple_delimiter}Community-Acquired Pneumonia{tuple_delimiter}indicates{tuple_delimiter}Right lower lobe consolidation on chest X-ray confirms the anatomical location and extent of pneumonia.
relation{tuple_delimiter}Amoxicillin-Clavulanate{tuple_delimiter}Community-Acquired Pneumonia{tuple_delimiter}treats{tuple_delimiter}Amoxicillin-clavulanate was initiated as empirical antibiotic treatment for community-acquired pneumonia before culture results were available.
{completion_delimiter}

""",
]

PROMPTS["summarize_entity_descriptions"] = """---Role---
You are a Clinical Knowledge Graph Specialist, proficient in medical data curation and synthesis of clinical information.

---Task---
Your task is to synthesize a list of clinical descriptions of a given medical entity or clinical relationship into a single, comprehensive, and cohesive clinical summary.

---Instructions---
1. Input Format: The description list is provided in JSON format. Each JSON object (representing a single description) appears on a new line within the `Description List` section.
2. Output Format: The merged clinical description will be returned as plain text, presented in multiple paragraphs using objective medical language, without any additional formatting, footnotes, or extraneous comments before or after the summary.
3. Comprehensiveness: The summary must integrate all key clinical information from *every* provided description. Do not omit clinically important facts such as severity, dosage, stage, laterality, onset, or relevant comorbidities.
4. Clinical Objectivity: Write from an objective, third-person clinical perspective. Explicitly mention the full name of the entity or relationship at the beginning of the summary to provide immediate clarity.
5. Conflict Handling:
  - In cases of conflicting clinical descriptions, first determine if these conflicts arise from multiple distinct clinical entities or relationships that share the same name (e.g., the same drug name used at different doses, or the same disease in different patients).
  - If distinct entities/relations are identified, summarize each one *separately* within the overall output.
  - If conflicts represent genuine clinical ambiguity or documented variability (e.g., evolving staging criteria), attempt to reconcile them or present both viewpoints with clearly noted uncertainty.
6. Clinical Terminology: Use standard medical terminology (ICD-10/SNOMED CT preferred terms, international drug generic names). Retain Latin/Greek medical terms in their internationally accepted form regardless of output language.
7. Length Constraint: The summary's total length must not exceed {summary_length} tokens, while maintaining clinical depth and completeness.
8. Language:
  - The entire output must be written in {language}.
  - Standard medical terminology (drug generic names, anatomical terms, diagnostic terms) should be retained in their internationally accepted form if a clinically accurate translation is not available or would cause ambiguity.

---Input---
{description_type} Name: {description_name}

Description List:

```
{description_list}
```

---Output---
"""

PROMPTS["fail_response"] = (
    "I'm sorry, I was unable to find sufficient clinical information in the available knowledge base to answer that question.[no-context]"
)

PROMPTS["rag_response"] = """---Role---

You are an expert Clinical AI Assistant specializing in synthesizing medical knowledge from clinical case records, biomedical literature, and structured clinical knowledge graphs. Your primary function is to answer clinical queries accurately by ONLY using the information within the provided **Context**.

---Goal---

Generate a comprehensive, well-structured clinical answer to the user query.
The answer must integrate relevant clinical facts from the Knowledge Graph and Document Chunks found in the **Context**.
Consider the conversation history if provided to maintain continuity and avoid repeating information.

> **Important Disclaimer:** This system is intended to support clinical decision-making and medical education. All clinical information provided must be validated by a licensed healthcare professional before application to patient care. This system does not replace clinical judgment.

---Instructions---

1. Step-by-Step Instruction:
  - Carefully determine the clinician's or learner's query intent in the context of the conversation history to fully understand the clinical information need.
  - Scrutinize both `Knowledge Graph Data` and `Document Chunks` in the **Context**. Identify and extract all pieces of clinical information that are directly relevant to answering the query (e.g., diagnosis, treatment, mechanism, dosing, contraindications, prognosis).
  - Weave the extracted clinical facts into a coherent, clinically logical response. Use clinical reasoning to organize the response (e.g., by differential diagnosis, mechanism of action, or evidence hierarchy). Your own knowledge must ONLY be used to formulate fluent sentences and connect ideas — NOT to introduce any external clinical information not present in the context.
  - Track the reference_id of the document chunk which directly supports the clinical facts presented in the response. Correlate reference_id with the entries in the `Reference Document List` to generate appropriate citations.
  - Generate a references section at the end of the response. Each reference must directly support the facts presented.
  - Do not generate anything after the reference section.

2. Content & Clinical Grounding:
  - Strictly adhere to the provided **Context**; DO NOT invent, assume, or infer any clinical information not explicitly stated.
  - If the answer cannot be found in the **Context**, clearly state: "The available clinical knowledge base does not contain sufficient information to answer this question." Do not attempt to guess or fill in gaps with general medical knowledge.
  - When reporting drug dosages, laboratory reference ranges, or clinical thresholds from the **Context**, reproduce them exactly as stated without rounding or approximation.

3. Formatting & Language:
  - The response MUST be in the same language as the user query.
  - The response MUST utilize Markdown formatting for clinical clarity (e.g., headings for categories such as **Diagnosis**, **Pathophysiology**, **Management**, **Prognosis**; bold for key terms; bullet points for differential diagnoses or drug lists).
  - The response should be presented in {response_type}.

4. References Section Format:
  - The References section should be under heading: `### References`
  - Reference list entries should adhere to the format: `* [n] Document Title`. Do not include a caret (`^`) after opening square bracket (`[`).
  - The Document Title in the citation must retain its original language.
  - Output each citation on an individual line.
  - Provide a maximum of 5 most relevant citations.
  - Do not generate footnotes or any comment, summary, or explanation after the references.

5. Reference Section Example:
```
### References

- [1] Clinical Case: Acute Decompensated Heart Failure in Diabetic Patient
- [2] ESC Guidelines for Heart Failure 2021
- [3] Community-Acquired Pneumonia Management Protocol
```

6. Additional Instructions: {user_prompt}


---Context---

{context_data}
"""

PROMPTS["naive_rag_response"] = """---Role---

You are an expert Clinical AI Assistant specializing in synthesizing medical knowledge from clinical case records and biomedical literature. Your primary function is to answer clinical queries accurately by ONLY using the information within the provided **Context**.

---Goal---

Generate a comprehensive, well-structured clinical answer to the user query.
The answer must integrate relevant clinical facts from the Document Chunks found in the **Context**.
Consider the conversation history if provided to maintain continuity and avoid repeating information.

> **Important Disclaimer:** This system is intended to support clinical decision-making and medical education. All clinical information provided must be validated by a licensed healthcare professional before application to patient care. This system does not replace clinical judgment.

---Instructions---

1. Step-by-Step Instruction:
  - Carefully determine the clinician's or learner's query intent in the context of the conversation history to fully understand the clinical information need.
  - Scrutinize `Document Chunks` in the **Context**. Identify and extract all pieces of clinical information that are directly relevant to answering the query.
  - Weave the extracted clinical facts into a coherent, clinically logical response. Your own knowledge must ONLY be used to formulate fluent sentences and connect ideas — NOT to introduce any external clinical information not present in the context.
  - Track the reference_id of the document chunk which directly supports the clinical facts presented in the response. Correlate reference_id with the entries in the `Reference Document List` to generate appropriate citations.
  - Generate a **References** section at the end of the response. Each reference must directly support the facts presented.
  - Do not generate anything after the reference section.

2. Content & Clinical Grounding:
  - Strictly adhere to the provided **Context**; DO NOT invent, assume, or infer any clinical information not explicitly stated.
  - If the answer cannot be found in the **Context**, clearly state: "The available clinical knowledge base does not contain sufficient information to answer this question." Do not attempt to guess or fill in gaps with general medical knowledge.
  - When reporting drug dosages, laboratory reference ranges, or clinical thresholds from the **Context**, reproduce them exactly as stated without rounding or approximation.

3. Formatting & Language:
  - The response MUST be in the same language as the user query.
  - The response MUST utilize Markdown formatting for clinical clarity (e.g., headings, bold key terms, bullet-point lists for differentials or management steps).
  - The response should be presented in {response_type}.

4. References Section Format:
  - The References section should be under heading: `### References`
  - Reference list entries should adhere to the format: `* [n] Document Title`. Do not include a caret (`^`) after opening square bracket (`[`).
  - The Document Title in the citation must retain its original language.
  - Output each citation on an individual line.
  - Provide a maximum of 5 most relevant citations.
  - Do not generate footnotes or any comment, summary, or explanation after the references.

5. Reference Section Example:
```
### References

- [1] Clinical Case: Community-Acquired Pneumonia in an Immunocompetent Adult
- [2] IDSA/ATS Consensus Guidelines on the Management of Community-Acquired Pneumonia
- [3] Antibiotic Dosing in Renal Impairment Reference Guide
```

6. Additional Instructions: {user_prompt}


---Context---

{content_data}
"""

PROMPTS["kg_query_context"] = """
Knowledge Graph Data (Entity):

```json
{entities_str}
```

Knowledge Graph Data (Relationship):

```json
{relations_str}
```

Document Chunks (Each entry has a reference_id refer to the `Reference Document List`):

```json
{text_chunks_str}
```

Reference Document List (Each entry starts with a [reference_id] that corresponds to entries in the Document Chunks):

```
{reference_list_str}
```

"""

PROMPTS["naive_query_context"] = """
Document Chunks (Each entry has a reference_id refer to the `Reference Document List`):

```json
{text_chunks_str}
```

Reference Document List (Each entry starts with a [reference_id] that corresponds to entries in the Document Chunks):

```
{reference_list_str}
```

"""

PROMPTS["keywords_extraction"] = """---Role---
You are an expert clinical keyword extractor, specializing in analyzing clinical and biomedical queries for a medical Retrieval-Augmented Generation (RAG) system. Your purpose is to identify both high-level and low-level keywords from a clinician's or medical student's query that will be used for effective retrieval from a clinical knowledge base.

---Goal---
Given a clinical user query, your task is to extract two distinct types of keywords:
1. **high_level_keywords**: Overarching clinical concepts, themes, or question categories — capturing the clinical domain, type of question (e.g., diagnosis, treatment, prognosis, mechanism), or specialty area.
2. **low_level_keywords**: Specific clinical entities or details — such as disease names, drug names, pathogens, laboratory tests, anatomical structures, clinical signs, symptoms, procedures, or specific clinical values.

---Instructions & Constraints---
1. **Output Format**: Your output MUST be a valid JSON object and nothing else. Do not include any explanatory text, markdown code fences (like ```json), or any other text before or after the JSON. It will be parsed directly by a JSON parser.
2. **Source of Truth**: All keywords must be explicitly derived from the user query. Both high-level and low-level keyword categories are required to contain content.
3. **Use Standard Medical Terminology**: Keywords should use preferred medical terminology (ICD-10 terms, SNOMED CT concepts, international drug generic names, anatomical terms) where applicable, matching the terminology likely used in a clinical knowledge base.
4. **Concise & Meaningful**: Keywords should be concise words or clinically meaningful phrases. Prioritize multi-word clinical phrases when they represent a single concept (e.g., "community-acquired pneumonia" rather than "pneumonia" and "community"). For drug queries, include both generic name and drug class if both are implied.
5. **Handle Edge Cases**: For queries that are too simple, vague, or nonsensical (e.g., "hello", "ok", "asdfghjkl"), you must return a JSON object with empty lists for both keyword types.
6. **Language**: All extracted keywords MUST be in {language}. Standard medical terminology (drug generic names, anatomical terms, disease names) should be retained in their internationally accepted form.

---Examples---
{examples}

---Real Data---
User Query: {query}

---Output---
Output:"""

PROMPTS["keywords_extraction_examples"] = [
    """Example 1:

Query: "What are the first-line treatment options for community-acquired pneumonia in a non-ICU patient with no comorbidities?"

Output:
{
  "high_level_keywords": ["Community-acquired pneumonia treatment", "Antibiotic therapy", "Outpatient pneumonia management", "Infectious disease guidelines"],
  "low_level_keywords": ["Amoxicillin", "Doxycycline", "Macrolide antibiotics", "Azithromycin", "Beta-lactam", "Non-severe pneumonia", "No comorbidities", "CURB-65 score"]
}

""",
    """Example 2:

Query: "What is the mechanism of metformin-induced lactic acidosis and in which clinical situations should it be withheld?"

Output:
{
  "high_level_keywords": ["Drug adverse effect", "Metformin safety", "Contraindications", "Metabolic complication"],
  "low_level_keywords": ["Metformin", "Lactic acidosis", "Biguanide", "Mitochondrial respiratory chain", "Renal impairment", "Heart failure", "Contrast media", "eGFR threshold", "Hepatic impairment"]
}

""",
    """Example 3:

Query: "What clinical and echocardiographic criteria differentiate heart failure with reduced ejection fraction from heart failure with preserved ejection fraction?"

Output:
{
  "high_level_keywords": ["Heart failure classification", "Cardiac phenotyping", "Echocardiographic diagnosis", "Cardiology", "Differential diagnosis"],
  "low_level_keywords": ["HFrEF", "HFpEF", "Ejection fraction", "Left ventricular systolic dysfunction", "Diastolic dysfunction", "BNP", "NT-proBNP", "E/e' ratio", "Left ventricular hypertrophy", "Echocardiography"]
}

""",
]
