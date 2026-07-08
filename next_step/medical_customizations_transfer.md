# Medical Customizations Transfer Note

This document consolidates the custom work added in this branch so it can be recreated in a newer LightRAG version even if the file layout changes.

The important thing to preserve is the behavior, not the exact placement.

The combined behavior in this branch is:

`medical prompt profile + optional GLiNER pre-recognition + optional multimodal text-case upload + VLM-based image description + normal LightRAG ingestion and extraction`

## What This Branch Now Does

This branch now contains five connected customizations:

1. optional GLiNER pre-recognition before normal LLM extraction
2. support for both delimiter-text and JSON extraction modes when GLiNER is enabled
3. a dedicated multimodal case-upload path for one text case plus linked images
4. a medical default extraction prompt profile plus a user-overridable YAML prompt-profile mechanism
5. setup and environment changes so users can configure VLM, GLiNER, and multimodal-case limits without patching code

These are designed to work together, but each piece still degrades safely when disabled or missing.

## 1. GLiNER Is An Optional Helper Step, Not The Final Extractor

Current files:

- [lightrag/kg/ner.py](/home/tin/LightRAG/lightrag/kg/ner.py)
- [lightrag/operate.py](/home/tin/LightRAG/lightrag/operate.py)
- [lightrag/lightrag.py](/home/tin/LightRAG/lightrag/lightrag.py)
- [pyproject.toml](/home/tin/LightRAG/pyproject.toml)

The preserved behavior is:

`chunk text -> optional GLiNER pre-recognition -> recognized entity block injected into extraction prompt -> normal LLM extraction -> normal LightRAG parsing/storage`

GLiNER is not writing directly to the graph.

### Runtime contract

- `gliner` is now a normal dependency in `pyproject.toml`
- the model name remains `Ihor/gliner-biomed-base-v1.0`
- the model is loaded lazily at runtime
- the loaded model is cached in-process
- extraction must continue safely if GLiNER fails

### Config contract

Current env/config knobs:

- `ENABLE_GLINER_NER=true`
- `GLINER_MODEL_DIR=...`

`ENABLE_GLINER_NER` is optional and defaults to `true`.
If a user sets it to `false`, extraction should skip GLiNER and fall back to the normal LightRAG extraction path.

`GLINER_MODEL_DIR` controls where the downloaded model is cached.
The default in this branch is top-level `./ner_model`, not `data/*`.

### Cache directory behavior

The current resolver pattern is:

```python
DEFAULT_NER_MODEL_CACHE_DIR = Path("./ner_model")
NER_MODEL_DIR_ENV = "GLINER_MODEL_DIR"

def get_ner_model_cache_dir() -> Path:
    configured_dir = os.getenv(NER_MODEL_DIR_ENV, "").strip()
    if configured_dir:
        return Path(configured_dir).expanduser()
    return DEFAULT_NER_MODEL_CACHE_DIR
```

What to preserve:

- users can relocate the GLiNER cache with env config
- the default path lives beside `inputs/` and `rag_storage/`
- no hard-coded `data/ner_model` assumption remains

### Prompt injection behavior

The current extraction code computes GLiNER labels from the resolved entity-type guidance, then injects recognized surface forms into the prompt as:

```text
<Recognized_Entities_from_NER>
- dengue fever
- thrombocytopenia
</Recognized_Entities_from_NER>
```

The important behavior is:

- only recognized entity text is injected
- label and score are not injected
- GLiNER-derived labels come from the current resolved entity-type guidance
- this injected block is available to both extraction modes

### Both extraction modes are supported

This branch wires GLiNER into:

- delimiter-text extraction mode
- JSON extraction mode

If you port this to another version, do not implement it for only one path unless you intentionally want inconsistent extraction behavior.

## 2. The Medical Prompt Is Now The Default Fallback Profile

Current files:

- [lightrag/prompt.py](/home/tin/LightRAG/lightrag/prompt.py)
- [prompts/samples/entity_type_prompt.sample.yml](/home/tin/LightRAG/prompts/samples/entity_type_prompt.sample.yml)
- [tests/extraction/test_entity_extraction_stability.py](/home/tin/LightRAG/tests/extraction/test_entity_extraction_stability.py)

This branch no longer uses the old generic entity-type list as the fallback extraction guidance.

Instead, the built-in fallback profile is now medically styled:

- clinically relevant entity types
- clinically relevant extraction rules
- clinically relevant relationship vocabulary
- example outputs for both text and JSON extraction modes

### Important prompt behavior

The new default extraction prompt emphasizes:

- extract clinically meaningful entities and relations only
- copy entity names from the text span rather than normalizing them
- prefer clinically meaningful direction for relationships
- keep qualifiers as separate concepts when medically useful
- avoid hallucinated inference beyond the source text

This was applied to:

- text-mode prompts
- JSON-mode prompts
- the default entity-type guidance
- shipped sample prompt-profile YAML

### Prompt precedence rule

This is important.

Editing `lightrag/prompt.py` changes the built-in fallback profile, but that fallback is not always the active profile.

The current resolution order is:

1. `addon_params["entity_types_guidance"]`
2. `addon_params["entity_type_prompt_file"]` / `ENTITY_TYPE_PROMPT_FILE`
3. built-in fallback from `lightrag/prompt.py`

That means if a user sets `ENTITY_TYPE_PROMPT_FILE`, the YAML profile overrides the built-in prompt defaults.

### YAML prompt-profile behavior

The current profile loader expects user files under:

- `PROMPT_DIR/entity_type/`

with `PROMPT_DIR` defaulting to `./prompts`.

The file name sandbox is deliberate:

- only file names are allowed
- no directory traversal
- only `.yml` and `.yaml` are allowed

The current shipped sample file shows the intended structure:

```yaml
entity_types_guidance: |
  ...
entity_extraction_examples:
  - |
    ...
entity_extraction_json_examples:
  - |
    ...
```

If you port this feature, preserve the idea that users now provide:

1. a list of entity types
2. an explanation of those entity types
3. examples for the extraction mode they want to run

## 3. Multimodal Case Upload Is A Dedicated Text-Case API

Current files:

- [lightrag/api/routers/document_routes.py](/home/tin/LightRAG/lightrag/api/routers/document_routes.py)
- [lightrag/multimodal_case.py](/home/tin/LightRAG/lightrag/multimodal_case.py)
- [lightrag_webui/src/components/documents/UploadDocumentsDialog.tsx](/home/tin/LightRAG/lightrag_webui/src/components/documents/UploadDocumentsDialog.tsx)
- [lightrag_webui/src/api/lightrag.ts](/home/tin/LightRAG/lightrag_webui/src/api/lightrag.ts)

This branch adds a dedicated multimodal case-upload flow.

The preserved behavior is:

`one text file + zero or more linked images -> dedicated upload endpoint -> server stores both -> server optionally describes images with VLM -> descriptions are appended to text -> enriched text enters the normal ingestion pipeline`

### Product model

The frontend model is case-based, not flat-file-based.

One case consists of:

1. one text file
2. zero or more linked images

Images are not uploaded as standalone documents.

### API behavior

Current route:

- `POST /documents/upload_multimodal_case`

The request shape is multipart:

- one `file`
- repeated `images`

The endpoint:

1. validates the text file as a text-based case file
2. validates images as image uploads
3. saves the text file into the normal input area
4. saves case-linked images under a case-specific folder
5. schedules background indexing for the case

### File-type scope

This first implementation is for text-file case input, not raw free-text entry.

The branch explicitly supports a text-case file with linked images.

## 4. Case Images Are Persisted And Must Be Deleted With The Case

Current file:

- [lightrag/api/routers/document_routes.py](/home/tin/LightRAG/lightrag/api/routers/document_routes.py)

This was a product requirement and is part of the implemented behavior.

Images are stored under:

`<INPUT_DIR>/images/<case-hash>/`

The case folder is derived from the canonical case file path hash, not from raw user input.

What to preserve:

- images are persisted on disk
- image-to-case linkage exists independently of in-memory state
- deleting a case also deletes its linked image directory
- clearing all documents also removes the shared multimodal case image root

### Cleanup behavior

This branch adds explicit cleanup paths for:

- per-document deletion
- full document clear
- failed upload cleanup if scheduling/indexing fails

If you port only the upload path and forget the delete path, you will leak stored image artifacts.

## 5. VLM Is Reused For Image Description Instead Of Adding A Separate Helper Model

Current files:

- [lightrag/multimodal_case.py](/home/tin/LightRAG/lightrag/multimodal_case.py)
- [lightrag/prompt_multimodal.py](/home/tin/LightRAG/lightrag/prompt_multimodal.py)

This branch intentionally reuses the repo's existing VLM role/config path rather than inventing a separate multimodal provider stack.

### Runtime behavior

When multimodal case indexing runs:

1. the server reads the text case
2. if linked images exist and `VLM_PROCESS_ENABLE=true`, it uses the configured VLM role
3. each image is converted into a textual description
4. those descriptions are appended to the original case text
5. the enriched text is inserted through the normal text ingestion path

If VLM is disabled or missing, the text case still ingests normally without image descriptions.

### Multimodal prompt behavior

The multimodal prompt was also tightened for medical use.

The important rules are:

- prioritize what is visibly present in the image
- do not invent clinical findings from surrounding text
- use context only to disambiguate, not to hallucinate
- keep the description factual and remove unsupported claims

## 6. Setup Wizard And Env Surface Were Expanded For VLM And Multimodal Use

Current files:

- [scripts/setup/setup.sh](/home/tin/LightRAG/scripts/setup/setup.sh)
- [Makefile](/home/tin/LightRAG/Makefile)
- [env.example](/home/tin/LightRAG/env.example)
- [tests/setup/test_collect.py](/home/tin/LightRAG/tests/setup/test_collect.py)
- [tests/setup/test_env.py](/home/tin/LightRAG/tests/setup/test_env.py)

`make env-base` is no longer only about base LLM, embedding, and reranker.

It now also asks for VLM configuration so multimodal image description can actually run.

### New VLM setup behavior

The setup wizard now collects:

- `VLM_PROCESS_ENABLE`
- `VLM_LLM_BINDING`
- `VLM_LLM_MODEL`
- `VLM_LLM_BINDING_HOST`
- `VLM_LLM_BINDING_API_KEY`

It also provides provider-aware default VLM models.

This is important because the multimodal case-upload feature is not useful unless the user can configure a vision-capable model through the normal setup flow.

### Other relevant env knobs

This branch also adds or surfaces:

- `ENABLE_GLINER_NER`
- `GLINER_MODEL_DIR`
- `MAX_MULTIMODAL_CASE_IMAGES`
- `ENTITY_TYPE_PROMPT_FILE`

The multimodal image limit defaults to `5`.

## 7. Local Artifact And Ignore Conventions Were Updated

Current file:

- [.gitignore](/home/tin/LightRAG/.gitignore)

This branch expands ignored local artifact paths, including:

- `data_old/`
- `sft_prompt/fold1/`
- several generated reasoning/text-output directories

The important behavior to preserve is not the exact list, but the intent:

- local datasets
- generated artifacts
- prompt-generation scratch outputs

should not pollute git state by default.

## 8. Minimal Regression Coverage Added For The New Behavior

Current files:

- [tests/extraction/test_entity_extraction_stability.py](/home/tin/LightRAG/tests/extraction/test_entity_extraction_stability.py)
- [tests/extraction/test_gliner_model_dir.py](/home/tin/LightRAG/tests/extraction/test_gliner_model_dir.py)
- [tests/setup/test_collect.py](/home/tin/LightRAG/tests/setup/test_collect.py)
- [tests/setup/test_env.py](/home/tin/LightRAG/tests/setup/test_env.py)
- [tests/api/routes/test_document_routes_docx_archive.py](/home/tin/LightRAG/tests/api/routes/test_document_routes_docx_archive.py)

The test intent to preserve is:

- GLiNER can be toggled on/off
- GLiNER cache dir can be overridden by env
- prompt behavior remains stable across both extraction modes
- setup wizard writes and preserves VLM config correctly
- multimodal case-upload behavior is covered at the API layer

## Final Transfer Rule

If you recreate this work in a newer LightRAG version, preserve these decisions:

1. GLiNER is optional and defaults to enabled
2. GLiNER is a prompt-stage helper, not the final extractor
3. GLiNER must work in both text and JSON extraction modes
4. the default GLiNER cache lives at top-level `./ner_model` unless overridden
5. the multimodal feature is one text case plus linked images, not generic image upload
6. images are persisted and must be deleted with the case
7. image descriptions reuse the repo's normal VLM role/config
8. the medical extraction prompt is now the built-in fallback profile
9. user-provided YAML prompt profiles still override the built-in fallback
10. `make env-base` must expose enough VLM configuration for the multimodal path to be usable
