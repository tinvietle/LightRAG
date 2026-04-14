# LightRAG From Source: Build + Clinical Customization Guide

This guide is tailored for building LightRAG from source and then specializing it for medical clinical case workflows.

## 1. Repository Scan Summary

## Core package and orchestration
- `lightrag/lightrag.py`
  - Main orchestrator class: `LightRAG`
  - Lifecycle: `initialize_storages()`, `finalize_storages()`
  - Ingestion: `ainsert()`, `ainsert_custom_kg()`
  - Query: `aquery()`, `aquery_data()`, `aquery_llm()`

## Extraction and retrieval internals
- `lightrag/operate.py`
  - Chunking: `chunking_by_token_size()`
  - Entity extraction: `_handle_single_entity_extraction()`
  - Relationship extraction: `_handle_single_relationship_extraction()`
  - Pipeline orchestration: `extract_entities()`
  - Keyword extraction: `extract_keywords_only()`
  - KG retrieval: `_perform_kg_search()`

## Prompt templates
- `lightrag/prompt.py`
  - Extraction prompts and examples
  - Query response prompts (`rag_response`, `naive_rag_response`)
  - Keyword extraction prompt

## API service entry and routers
- `lightrag/api/lightrag_server.py`
  - App initialization and router registration
  - Launch function: `main()`
- `lightrag/api/routers/query_routes.py`
  - Query API models and endpoints
  - Query mode and per-request tuning
- `lightrag/api/routers/document_routes.py`
  - Upload/insert routes and document processing entry

## Storage backends
- `lightrag/base.py` for storage interfaces and `QueryParam`
- `lightrag/kg/` for concrete backends (JSON/Nano/NetworkX, Neo4j, Postgres, Mongo, Milvus, Qdrant, Redis, etc.)

## Build/run metadata
- `pyproject.toml`
  - console scripts: `lightrag-server`, `lightrag-gunicorn`
- `lightrag/api/README.md`
  - server startup and `.env` behavior
- `env.example`
  - default tuning knobs (`ENTITY_TYPES`, `CHUNK_SIZE`, token budgets, rerank toggles)

## Useful examples
- `examples/insert_custom_kg.py`
- `examples/rerank_example.py`

---

## 2. Build LightRAG From Source (Windows-first)

## Prerequisites
- Python 3.10+
- Bun (for WebUI build)
- Git
- Optional: uv (recommended package workflow)

## Option A: uv workflow (recommended)

```powershell
# 1) Clone
cd D:\
git clone https://github.com/HKUDS/LightRAG.git
cd LightRAG

# 2) Install with API extras
uv python pin 3.12
uv sync --extra api

# 3) Activate venv
.venv\Scripts\activate

# 4) Build WebUI artifacts
cd lightrag_webui
bun install --frozen-lockfile
bun run build
cd ..

# 5) Create runtime env
copy env.example .env
# Edit .env with your LLM + embedding settings

# 6) Start service
lightrag-server
```

## Option B: pip editable workflow

```powershell
cd D:\LightRAG
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[api]"

cd lightrag_webui
bun install --frozen-lockfile
bun run build
cd ..

copy env.example .env
lightrag-server
```

## Validate startup
- Open API docs at `http://localhost:9621/docs`
- If WebUI build is missing, server starts API but warns about frontend artifacts

---

## 3. Where to Build Your Specialized Clinical Service

There are two practical patterns.

## Pattern 1: In-repo specialization (fast iteration)
- Keep service entrypoint in this repo
- Customize prompts + extraction + query guards
- Start via existing `lightrag-server`

Best files to change first:
- `lightrag/prompt.py`
- `lightrag/constants.py`
- `lightrag/operate.py`
- `lightrag/api/routers/query_routes.py`
- `lightrag/api/routers/document_routes.py`

## Pattern 2: Wrapper package/service (cleaner long-term)
- Create a new package (for example `lightrag_medical/`) that imports LightRAG
- Keep upstream LightRAG as base dependency
- Override behavior by:
  - custom prompt set
  - custom preprocessing
  - custom post-response safety checks
  - custom API routes if needed

Use Pattern 1 first, then migrate to Pattern 2 once your clinical behavior stabilizes.

---

## 4. Exactly What to Change for Clinical Cases

## 4.1 Clinical ontology and extraction schema

### Files
- `lightrag/constants.py` (`DEFAULT_ENTITY_TYPES`)
- `env.example` (`ENTITY_TYPES` runtime override)
- `lightrag/prompt.py` (`entity_extraction_system_prompt`, `entity_extraction_examples`)

### Why
Default entity types are generic. Clinical cases need domain entities and medically meaningful relations.

### Suggested clinical entity types
- Patient
- Symptom
- Diagnosis
- DifferentialDiagnosis
- LabResult
- VitalSign
- Medication
- Dosage
- Procedure
- ImagingFinding
- Allergy
- Contraindication
- Comorbidity
- ClinicalOutcome
- Guideline

### Suggested relation keywords
- presents_with
- diagnosed_with
- ruled_out
- treated_with
- contraindicated_for
- monitored_by
- improved_after
- worsened_by

## 4.2 Prompt behavior and examples

### Files
- `lightrag/prompt.py`

### What to modify
- Add medical extraction examples using realistic case-note style text
- Add stricter extraction instructions:
  - do not infer diagnosis beyond explicit text
  - preserve uncertainty terms (possible/probable/suspected)
  - keep units and value ranges exactly
- Add response safety language to `rag_response`:
  - evidence-grounded only
  - no definitive treatment decision claims without source support
  - always include references when available

## 4.3 Ingestion preprocessing for clinical notes

### Files
- `lightrag/api/routers/document_routes.py`
- `lightrag/operate.py` (chunking behavior)

### What to add
- PHI-safe normalization layer before insertion:
  - normalize dates and formats
  - preserve medical abbreviations dictionary
  - optional redaction pipeline for identifiers
- Section-aware chunking hints:
  - chief complaint
  - HPI
  - past medical history
  - medications
  - assessment/plan

Clinical note structure is often semantically sectioned; better chunks improve retrieval quality.

## 4.4 Query-time clinical controls

### Files
- `lightrag/base.py` (`QueryParam` defaults)
- `lightrag/api/routers/query_routes.py` (request schema + defaults)

### What to tune
- Prefer `mode="mix"` or `mode="hybrid"`
- Increase context budgets for longer notes:
  - `max_entity_tokens`
  - `max_relation_tokens`
  - `max_total_tokens`
- Keep rerank enabled for clinical precision when reranker is configured
- Add optional API field like `clinical_use_case` (diagnosis summary, timeline, medication reconciliation) and map it to specialized `user_prompt` snippets

## 4.5 Retrieval/rerank quality

### Files
- `lightrag/rerank.py`
- Example baseline: `examples/rerank_example.py`

### What to do
- Configure a reranker suited for medical text semantics
- Use stricter rerank top-N for high precision tasks
- Evaluate retrieval quality before response quality

## 4.6 API safety and policy layer

### Files
- `lightrag/api/routers/query_routes.py`
- (optionally) custom middleware in `lightrag/api/lightrag_server.py`

### What to add
- Guardrails on unsafe asks (for example direct dosing decisions without references)
- Consistent disclaimer strategy for clinical decision support
- Structured response mode for auditability (problem list, evidence list, confidence statement)

## 4.7 Workspace isolation for patient/project segregation

### Files
- Runtime config and API startup args
- docs: `lightrag/api/README.md`

### How
- Use distinct `workspace` values per project/tenant/cohort
- Avoid mixing unrelated patient cohorts in one workspace

---

## 5. Minimal Clinical Implementation Plan

## Phase 1: Build + baseline run
1. Build from source and confirm API runs.
2. Insert a small synthetic clinical dataset.
3. Test `/query`, `/query/data` in `mix` mode.

## Phase 2: Domain extraction
1. Set clinical `ENTITY_TYPES`.
2. Replace/add extraction examples in `lightrag/prompt.py`.
3. Re-index and inspect extracted entities/relations.

## Phase 3: Safety and quality
1. Add response safety instructions to prompt templates.
2. Enable rerank and tune top-k/chunk budgets.
3. Add API-level checks for high-risk query categories.

## Phase 4: Hardening
1. Add test cases under `tests/` for extraction and query behavior.
2. Add regression set using representative clinical narratives.
3. Move to wrapper package or service module if needed.

---

## 6. Concrete Starting Patch Suggestions

If you want the quickest path, start in this order:
1. `env.example` (or your `.env`): set `ENTITY_TYPES` to clinical list.
2. `lightrag/prompt.py`: add 2-5 medical extraction examples.
3. `lightrag/prompt.py`: tighten `rag_response` safety wording.
4. `lightrag/api/routers/query_routes.py`: expose a clinical mode hint parameter.
5. `lightrag/operate.py`: improve chunking for note sections if needed.

---

## 7. Important Operational Notes

- Always call `initialize_storages()` before insert/query when using core programmatic API.
- If embedding model dimensions change, rebuild affected vector storage.
- Keep extraction and query language behavior explicit in prompts.
- For clinical usage, avoid real PHI in early testing. Use synthetic/de-identified notes.

---

## 8. Quick Command Reference

```powershell
# Install from source with API
uv sync --extra api
.venv\Scripts\activate

# Build frontend
cd lightrag_webui
bun install --frozen-lockfile
bun run build
cd ..

# Run server
lightrag-server

# Optional production mode (not typical on Windows)
lightrag-gunicorn --workers 4

# Run tests
python -m pytest tests

# Lint
ruff check .
```

---

## 9. Next Step

If you want, the next action can be generating a first clinical patch set that includes:
- a clinical entity type set,
- a medical extraction prompt pack,
- and a clinical-safe response prompt variant.
