# GLiNER Integration Transfer Note

This document is for reimplementing the same GLiNER behavior in a newer or different LightRAG version, where file layout and line numbers may no longer match.

The important thing to preserve is the behavior, not the exact placement:

`chunk text -> GLiNER pre-recognition -> recognized entity block in prompt -> normal LLM extraction -> parsed entities/relations`

## What This Version Does

In this branch, GLiNER is not the final extractor. It is a helper step before the normal LLM entity extraction.

The flow is:

1. split document into chunks
2. for each chunk, call GLiNER with the chunk text and configured entity labels
3. convert the GLiNER result into a simple bullet-list block
4. inject that block into the entity extraction prompt
5. let the normal LLM extraction produce entities and relations
6. parse and store the LLM result as graph nodes and edges

So if you port this to another version, do not wire GLiNER directly into graph storage. Wire it into the prompt-building stage of entity extraction.

## Code 1: Dedicated GLiNER Module

Current file: [lightrag/kg/ner.py](/home/tin/LightRAG/lightrag/kg/ner.py)

This is the core pattern that must exist somewhere in the target version:

```python
import asyncio
from pathlib import Path
from typing import Optional
import pipmaster as pm

if not pm.is_installed("gliner"):
    pm.install("gliner")

from gliner import GLiNER

from lightrag.utils import logger


NER_MODEL_CACHE_DIR = Path("data/ner_model")
NER_MODEL_NAME = "Ihor/gliner-biomed-base-v1.0"
_ner_model_cache: Optional[GLiNER] = None
```

What to preserve:

- runtime dependency on `gliner`
- model name `Ihor/gliner-biomed-base-v1.0`
- a local cache directory for the downloaded model
- process-level singleton cache for the loaded model

### Model loader pattern

```python
async def _load_ner_model(force_reload: bool = False) -> GLiNER:
    global _ner_model_cache

    if _ner_model_cache is not None and not force_reload:
        logger.debug("Using cached GLiNER model")
        return _ner_model_cache

    logger.info(f"Loading GLiNER model from {NER_MODEL_NAME}...")
    NER_MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_event_loop()
    model = await loop.run_in_executor(
        None,
        lambda: GLiNER.from_pretrained(
            NER_MODEL_NAME,
            cache_dir=str(NER_MODEL_CACHE_DIR),
        ),
    )

    _ner_model_cache = model
    logger.info("GLiNER model loaded successfully")
    return model
```

Why this matters:

- loading is lazy, not startup-time
- model loading is offloaded to an executor so async code does not block
- target version should keep this non-blocking pattern if extraction is async

### NER output formatting pattern

```python
def _format_ner_entities(entities: list[dict]) -> str:
    if not entities:
        return ""

    lines = []
    for ent in entities:
        entity_text = ent.get("text", "")
        lines.append(f"- {entity_text}")

    return "\n".join(lines)
```

Important behavior:

- only the recognized entity text is kept
- label and score are discarded
- output is a plain bullet list

That means the next implementation should not overcomplicate this unless intentionally changing behavior.

### Recognizer function pattern

```python
async def recognize_entities(
    text: str,
    labels: list[str],
    threshold: float = 0.9,
) -> tuple[str, list[dict]]:
    if not text or not labels:
        logger.warning("Empty text or labels provided to NER, skipping")
        return "", []

    try:
        model = await _load_ner_model()

        loop = asyncio.get_event_loop()
        entities = await loop.run_in_executor(
            None,
            lambda: model.predict_entities(
                text,
                labels,
                flat_ner=False,
                threshold=threshold,
            ),
        )

        formatted = _format_ner_entities(entities)
        logger.debug(f"NER recognized {len(entities)} entities")
        return formatted, entities

    except Exception as e:
        logger.error(f"Error during NER recognition: {str(e)}")
        return "", []
```

What must stay true:

- input is raw chunk text plus entity label list
- return value is `(formatted_prompt_string, raw_entity_list)`
- failure should degrade safely to `("", [])` instead of breaking extraction

### Exact recognized-entity generation behavior

This part is important enough to restate very explicitly.

The recognized entity generation in this branch is a 3-step transformation:

1. call GLiNER on a single chunk
2. receive a raw list of entity dictionaries from `model.predict_entities(...)`
3. convert only the entity text spans into a plain bullet-list string for prompt injection

The effective implementation logic is:

```python
async def recognize_entities(
    text: str,
    labels: list[str],
    threshold: float = 0.9,
) -> tuple[str, list[dict]]:
    if not text or not labels:
        return "", []

    model = await _load_ner_model()

    loop = asyncio.get_event_loop()
    entities = await loop.run_in_executor(
        None,
        lambda: model.predict_entities(
            text,
            labels,
            flat_ner=False,
            threshold=threshold,
        ),
    )

    if not entities:
        return "", []

    lines = []
    for ent in entities:
        entity_text = ent.get("text", "")
        lines.append(f"- {entity_text}")

    formatted = "\n".join(lines)
    return formatted, entities
```

### Expected raw GLiNER result shape

The downstream code assumes that each recognized item behaves like a dictionary and that at minimum `ent.get("text", "")` works.

The current formatter comments also imply GLiNER entities have keys like:

```python
{
    "text": "...",
    "label": "...",
    "score": 0.97,
    ...
}
```

The current integration only consumes `text`.

It does **not** currently use:

- `label`
- `score`
- character offsets
- token offsets
- nested span metadata

So for faithful reimplementation, the minimum safe assumption is:

- the recognizer output must be iterable
- each item must expose the recognized surface span under a `text` key

If the newer GLiNER version returns a slightly different structure, adapt it back into this contract before prompt injection.

### Exact prompt-ready string generation

The current integration does **not** serialize JSON, tuples, labels, or scores.
It creates a very simple human-readable block.

Given raw entities like:

```python
[
    {"text": "dengue fever", "label": "Disease_disorder", "score": 0.98},
    {"text": "thrombocytopenia", "label": "Sign_symptom", "score": 0.95},
    {"text": "Aedes aegypti", "label": "Transmission_vector", "score": 0.91},
]
```

The formatter produces:

```text
- dengue fever
- thrombocytopenia
- Aedes aegypti
```

Then the extraction code wraps that string as:

```text
<Recognized_Entities_from_NER>
- dengue fever
- thrombocytopenia
- Aedes aegypti
</Recognized_Entities_from_NER>
```

That full wrapped block is what gets inserted into the LLM prompt.

### Important non-obvious details

These details matter because another agent may otherwise "improve" the behavior and accidentally change extraction quality.

1. The formatter keeps duplicates if GLiNER returns duplicates.
   This branch does not deduplicate recognized spans before prompt injection.

2. The formatter keeps the original surface text from GLiNER.
   It does not normalize case, map to canonical terminology, or strip punctuation.

3. Empty or missing `text` fields are not explicitly filtered before formatting.
   The original code uses:

```python
entity_text = ent.get("text", "")
lines.append(f"- {entity_text}")
```

So if the future implementation wants exact parity, it should not add aggressive cleanup unless intentional.

4. The raw GLiNER output is returned alongside the formatted string even though the current extraction path ignores it:

```python
return formatted, entities
```

This should be preserved because it gives future code a stable extension point for:

- debugging
- score-aware filtering
- span offset handling
- audit logging

### Exact call-site expectation

At the call site, the current code expects:

```python
recognized_entities_str, _ = await recognize_entities(
    content,
    entity_types,
    threshold=0.3,
)
```

This tells us several things that should be preserved:

1. the call runs once per chunk, not once per full document
2. the input text is exactly the chunk content passed to LLM extraction
3. the entity type list comes from the configured extraction types
4. the runtime threshold is intentionally `0.3`, which is much lower than the function default `0.9`
5. only the formatted prompt string is used by the extraction path

### Suggested compatibility wrapper for the new version

If the new version has a different GLiNER API or returns a different object shape, use an adapter layer so the rest of the extraction flow can stay the same.

For example:

```python
def _normalize_gliner_result(raw_entities: list[object]) -> list[dict]:
    normalized = []
    for item in raw_entities:
        if isinstance(item, dict):
            normalized.append(
                {
                    "text": item.get("text", ""),
                    "label": item.get("label"),
                    "score": item.get("score"),
                }
            )
        else:
            normalized.append(
                {
                    "text": getattr(item, "text", ""),
                    "label": getattr(item, "label", None),
                    "score": getattr(item, "score", None),
                }
            )
    return normalized
```

Then the formatting logic can stay identical:

```python
def _format_ner_entities(entities: list[dict]) -> str:
    if not entities:
        return ""
    return "\n".join(f"- {ent.get('text', '')}" for ent in entities)
```

This adapter approach is recommended if the new branch has library-version drift.

## Code 2: Import GLiNER Into The Entity Extraction Stage

Current import location is inside [lightrag/operate.py](/home/tin/LightRAG/lightrag/operate.py), but the exact file may differ in the target version.

The pattern to preserve is:

```python
from lightrag.kg.ner import recognize_entities
```

You need this import in the module that already performs chunk-level entity extraction with the LLM.

Do not place it only in insertion code.
Do not place it only in API code.
It has to be available in the actual extraction routine that builds the LLM prompts.

## Code 3: Call GLiNER Before LLM Extraction

This is the most important transplant point.

In this branch, the entity extraction routine has a per-chunk inner function. Inside that function, before the LLM call, it does this:

```python
chunk_key = chunk_key_dp[0]
chunk_dp = chunk_key_dp[1]
content = chunk_dp["content"]
file_path = chunk_dp.get("file_path", "unknown_source")

cache_keys_collector = []

recognized_entities_str, _ = await recognize_entities(
    content,
    entity_types,
    threshold=0.3
)

if recognized_entities_str:
    recognized_entities_section = (
        f"<Recognized_Entities_from_NER>\n"
        f"{recognized_entities_str}\n"
        f"</Recognized_Entities_from_NER>\n"
    )
else:
    recognized_entities_section = ""
```

This is the behavior to copy.

Critical details:

- it uses chunk `content`
- it uses `entity_types`
- it overrides the recognizer default and passes `threshold=0.3`
- it wraps the formatted result in a tagged block named `<Recognized_Entities_from_NER>`
- if nothing is recognized, it uses an empty string

If the newer version has different variable names, keep the same logic with the local equivalents.

## Code 4: Inject The GLiNER Block Into Both Extraction Prompts

This branch injects the GLiNER block into:

- the first entity extraction prompt
- the follow-up gleaning / continue extraction prompt

The current code pattern is:

```python
entity_extraction_user_prompt = PROMPTS["entity_extraction_user_prompt"].format(
    **{
        **context_base,
        "input_text": content,
        "recognized_entities_section": recognized_entities_section,
    }
)

entity_continue_extraction_user_prompt = PROMPTS[
    "entity_continue_extraction_user_prompt"
].format(
    **{
        **context_base,
        "input_text": content,
        "recognized_entities_section": recognized_entities_section,
    }
)
```

This is mandatory for parity with this version.

If you only inject GLiNER into the first prompt but not the gleaning prompt, behavior changes.

## Code 5: Prompt Templates Must Support NER Guidance

Current file: [lightrag/prompt.py](/home/tin/LightRAG/lightrag/prompt.py)

There are two required parts.

### Part A: system prompt instruction

The system prompt contains guidance like this:

```text
**NER Pre-Recognition Guidance** (if available): If pre-recognized entities from an NER model are provided below, use them as starting points for your extraction. Verify each recognized entity and extract it if it meets clinical relevance criteria. You may also identify additional entities not recognized by the NER model if they are clinically meaningful.
```

What matters is not exact wording. What matters is that the LLM is instructed:

- NER output is a hint, not guaranteed truth
- recognized entities should be checked
- the LLM may still extract additional entities beyond GLiNER

### Part B: user prompt placeholder

The current prompt structure contains this block:

```text
---Data to be Processed---
<Entity_types>
[{entity_types}]

{recognized_entities_section}
<Input Text>
```

The continue/gleaning prompt also contains the same placeholder:

```text
---Data to be Processed---
<Entity_types>
[{entity_types}]

{recognized_entities_section}
<Input Text>
```

This placeholder is required. If the target version has different prompt builders, add an equivalent placeholder to both prompt templates.

## Code 6: GLiNER Sits Inside The Existing Extraction Pipeline

The target implementation must keep GLiNER inside the normal extraction path.

In this branch, there is a wrapper like:

```python
async def _process_extract_entities(
    self, chunk: dict[str, Any], pipeline_status=None, pipeline_status_lock=None
) -> list:
    chunk_results = await extract_entities(
        chunk,
        global_config=asdict(self),
        pipeline_status=pipeline_status,
        pipeline_status_lock=pipeline_status_lock,
        llm_response_cache=self.llm_response_cache,
        text_chunks_storage=self.text_chunks,
    )
    return chunk_results
```

And elsewhere the insertion flow calls this wrapper during ingestion.

Simple insert path pattern:

```python
tasks = [
    self.chunks_vdb.upsert(inserting_chunks),
    self._process_extract_entities(inserting_chunks),
    self.full_docs.upsert(new_docs),
    self.text_chunks.upsert(inserting_chunks),
]
await asyncio.gather(*tasks)
```

Queued/pipeline path pattern:

```python
await asyncio.gather(*first_stage_tasks)

entity_relation_task = asyncio.create_task(
    self._process_extract_entities(
        chunks, pipeline_status, pipeline_status_lock
    )
)
chunk_results = await entity_relation_task
```

Meaning:

- GLiNER is triggered automatically during ingestion
- no separate user action is needed
- any future version should attach GLiNER to the same logical stage: chunk-level entity extraction

## Code 7: Final Graph Data Still Comes From LLM Output Parsing

This is another important behavior constraint.

After the GLiNER-augmented prompt is sent to the LLM, the branch still parses the LLM response the normal way:

```python
maybe_nodes, maybe_edges = await _process_extraction_result(
    final_result,
    chunk_key,
    timestamp,
    file_path,
    tuple_delimiter=context_base["tuple_delimiter"],
    completion_delimiter=context_base["completion_delimiter"],
)
```

And the parser still expects normal `entity` / `relation` lines from the LLM.

So the persistence path is still:

- LLM output
- parser
- graph nodes/edges

Not:

- raw GLiNER entities
- direct graph insert

## Input And Output Contract

### Input to GLiNER

Input shape in this version:

```python
text: str
labels: list[str]
threshold: float
```

Where the runtime values are:

- `text = chunk content`
- `labels = configured entity types`
- `threshold = 0.3` at the integration call site

### Output from GLiNER helper

Output shape:

```python
tuple[str, list[dict]]
```

Meaning:

- first value: prompt-ready bullet list string
- second value: raw GLiNER entity objects

The current integration only uses the first value:

```python
recognized_entities_str, _ = await recognize_entities(...)
```

So if the future code wants exact parity, it can ignore the raw list too.

## Relevant Config Behavior

This branch has no explicit `GLINER_*` env vars.

The GLiNER-specific values are hardcoded in the NER module:

```python
NER_MODEL_CACHE_DIR = Path("data/ner_model")
NER_MODEL_NAME = "Ihor/gliner-biomed-base-v1.0"
```

But the surrounding extraction behavior depends on these environment-driven settings.

### 1. Entity types configuration

This is the most important config dependency for GLiNER behavior.

Why:

- those labels are passed into `recognize_entities(...)`
- the same labels are also used in the LLM extraction prompt

For the newer version, the intended entity type set to carry forward is:

```python
ENTITY_TYPES = [
    "Disease_disorder",
    "Pathogen",
    "Medication",
    "Anatomical_location",
    "Diagnostic_procedure",
    "Therapeutic_procedure",
    "Biological_structure",
    "Clinical_event",
    "Organism",
    "Sign_symptom",
    "Date",
    "Lab_test",
    "Lab_value",
    "Transmission_vector",
]
```

Important migration note:

- this does not have to live in `.env` in the newer version
- it can be implemented anywhere appropriate in the new architecture
- but the GLiNER call and the LLM extraction prompts should use the same exact type list

If the newer version changes entity type naming, GLiNER behavior will also change.

### 2. `ENABLE_LLM_CACHE_FOR_EXTRACT`

This does not switch GLiNER on or off.
It controls caching for the LLM extraction step around the GLiNER-augmented prompt.

Current cache behavior depends on logic like:

```python
if not hashing_kv.global_config.get("enable_llm_cache_for_entity_extract"):
    return None
```

and:

```python
if llm_response_cache.global_config.get("enable_llm_cache_for_entity_extract"):
    await save_to_cache(...)
```

This matters because prompt content now includes recognized NER entities.

### 3. `MAX_GLEANING`

This matters because the GLiNER block is also injected into the continue prompt.

If the newer version removes or changes gleaning, re-add the NER block anywhere the first extraction prompt is retried or continued.

### 4. `MAX_EXTRACT_INPUT_TOKENS`

This matters because the recognized entity block increases prompt size.

This branch guards the gleaning prompt context size before making the follow-up call.

### 5. `MAX_PARALLEL_INSERT`

This affects how many ingestion flows can trigger extraction concurrently.

Because the model object is cached in-process, the port should preserve safe reuse of the model under concurrent extraction.

## Dependency Behavior

This branch does not declare `gliner` directly in `pyproject.toml`.
Instead it relies on runtime installation:

```python
import pipmaster as pm

if not pm.is_installed("gliner"):
    pm.install("gliner")
```

For the newer version, the migration decision is:

- `gliner` should be a normal default dependency
- it should be installed by `uv sync`
- it should belong to the default package set, not an optional extra

So when reimplementing this in the newer version, prefer:

- declaring `gliner` directly in the main dependency list
- removing the need for runtime install as the primary installation path

This is an intentional improvement over the current branch.

## Minimal Reimplementation Recipe

If another agent needs to recreate this quickly in a new codebase, this is the minimum set:

1. Create a GLiNER helper module that:
   - lazy-loads `Ihor/gliner-biomed-base-v1.0`
   - caches it in memory
   - exposes `recognize_entities(text, labels, threshold)`
   - returns both formatted prompt text and raw entities

2. Ensure `gliner` is part of the default dependency set so a normal `uv sync` installs it.

3. Implement the target entity type list somewhere appropriate in the new version, even if it is no longer stored in `.env`:

```python
ENTITY_TYPES = [
    "Disease_disorder",
    "Pathogen",
    "Medication",
    "Anatomical_location",
    "Diagnostic_procedure",
    "Therapeutic_procedure",
    "Biological_structure",
    "Clinical_event",
    "Organism",
    "Sign_symptom",
    "Date",
    "Lab_test",
    "Lab_value",
    "Transmission_vector",
]
```

4. In the chunk-level LLM extraction function, before the first LLM call:

```python
recognized_entities_str, _ = await recognize_entities(
    content,
    entity_types,
    threshold=0.3,
)
```

5. Wrap the result as:

```python
if recognized_entities_str:
    recognized_entities_section = (
        f"<Recognized_Entities_from_NER>\n"
        f"{recognized_entities_str}\n"
        f"</Recognized_Entities_from_NER>\n"
    )
else:
    recognized_entities_section = ""
```

6. Pass `recognized_entities_section` into:
   - the initial entity extraction prompt
   - the continue / gleaning extraction prompt

7. Update the system prompt so the LLM knows these are hints to verify, not authoritative output.

8. Keep the existing parser and graph write path unchanged.

## If You Want Better Than This Version

If the next implementation is allowed to improve the design, the safest improvements are:

1. add `GLINER_MODEL_NAME`, `GLINER_CACHE_DIR`, `GLINER_THRESHOLD`, `ENABLE_GLINER_NER`
2. declare `gliner` in dependencies instead of only runtime install
3. log recognized labels and scores for debugging
4. deduplicate the recognized span list before injecting it into the prompt
5. add tests that verify the prompt contains the GLiNER block

## Bottom Line

To reproduce this branch faithfully, preserve these three code ideas together:

1. a lazy-loaded GLiNER helper
2. a per-chunk call to GLiNER before the LLM extraction call
3. prompt templates that include the recognized entity block in both initial and follow-up extraction prompts

That combination is the real integration. The exact file names can change.
