# Query With Image Transfer Note

This document records how the `NER` branch of `tinvietle/LightRAG` implemented query-with-image support in both backend and frontend, so the same behavior can be recreated in a newer LightRAG version even if file layout and APIs have changed.

This note is based on the remote GitHub branch `NER`, not on the local new-version code.

## What This Feature Does

In the `NER` branch, a user can submit:

1. a normal text query
2. zero or more attached images

The system then uses the images in **two different ways**:

1. it turns the images into text descriptions and appends those descriptions to the query used for retrieval and keyword generation
2. it still passes the original raw images to the final LLM call when the provider supports multimodal input

That dual-path behavior is the key thing to preserve.

The real flow is:

`text query + base64 images -> server image description pipeline -> augmented retrieval query -> normal LightRAG retrieval -> final LLM call with augmented text + raw images`

## Core Design Rule

Do not reduce this feature to only one of these:

- "send images directly to the model"
- "convert images to text and discard the images"

The `NER` branch does both:

- images are converted to text for retrieval-side reasoning
- raw images are still passed through to the final model call

That is the most important architectural point.

## Code 1: Query Param Must Carry Images

In the `NER` branch, query images are represented as a list of base64 strings.

The core data shape in `lightrag/base.py` is:

```python
images: list[str] = field(default_factory=list)
"""Optional base64-encoded images attached to the query."""
```

What to preserve:

- image support lives in the query parameter object
- the images are part of the query contract, not a separate side channel
- the representation is text-safe and transport-safe

For the newer version, the exact type can change if needed, but the simplest parity path is still:

```python
images: list[str]
```

where each item is a base64 payload or a data URL.

## Code 2: API Request Must Explicitly Accept Images

In the `NER` branch, the API request schema in `lightrag/api/routers/query_routes.py` contains:

```python
images: list[str] = Field(
    default_factory=list,
    description="Base64-encoded images attached to the query. Up to 10 images are supported.",
)
```

Validation behavior:

```python
@field_validator("images", mode="after")
@classmethod
def images_limit_check(cls, images: list[str]) -> list[str]:
    limit = getattr(global_args, "image_upload_limit", 10)
    if limit is not None and len(images) > limit:
        raise ValueError(f"A maximum of {limit} images can be attached to a query.")
    return [image.strip() for image in images if isinstance(image, str) and image.strip()]
```

This is the API contract to preserve:

- query payload supports an `images` field
- it is optional
- empty strings are stripped out
- image count is limited by server config

### Target-state requirement

Even if the new version changes the route or schema style, the query API should still support something equivalent to:

```json
{
  "query": "What does this chest image suggest?",
  "mode": "mix",
  "images": ["<base64-image-1>", "<base64-image-2>"]
}
```

## Code 3: Frontend Must Convert Selected Images To Base64 Before Query

In the `NER` branch frontend, query images are read in the browser and converted to base64 strings before being sent to the API.

The key helper in `lightrag_webui/src/features/RetrievalTesting.tsx` is:

```ts
const readFileAsDataUrl = (file: File) =>
  new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = reader.result
      if (typeof result !== 'string') {
        reject(new Error('Failed to read image file'))
        return
      }
      resolve(result.split(',')[1] || '')
    }
    reader.onerror = () => reject(reader.error || new Error('Failed to read image file'))
    reader.readAsDataURL(file)
  })
```

And selected images are stored in frontend state like:

```ts
type SelectedImage = {
  name: string
  dataUrl: string
}
```

The query request is then built with:

```ts
const queryParams = {
  ...state.querySettings,
  query: actualQuery,
  response_type: 'Multiple Paragraphs',
  images: selectedImages.map((image) => image.dataUrl),
  conversation_history: ...,
}
```

Important behavior:

- the UI keeps image data in memory as base64
- the query request sends only the stripped base64 payload
- the backend does not need multipart upload for query-with-image

### What to preserve

The newer UI can be visually different, but it should still:

1. allow selecting query images directly in the retrieval UI
2. convert them client-side into a transport-safe string payload
3. include them in the query request body

## Code 4: Frontend Must Enforce Query Image Limits From Runtime Config

In the `NER` branch, the frontend fetches runtime config:

```ts
fetch('/api/config')
  .then((res) => res.json())
  .then((data) => {
    if (data?.image_upload_limit) setServerImageLimit(Number(data.image_upload_limit))
  })
```

and prevents excessive image attachments in the retrieval UI.

The backend exposes:

```python
@router.get("/config")
async def get_config():
    return {
        "image_upload_limit": getattr(global_args, "image_upload_limit", 10),
        "max_upload_size": getattr(global_args, "max_upload_size", None),
    }
```

and config is parsed in `lightrag/api/config.py` from:

```python
args.image_upload_limit = get_env_value(
    "MAX_IMAGES_UPLOAD",
    get_env_value("IMAGE_UPLOAD_LIMIT", 10, int),
    int,
    special_none=True,
)
```

This means the query-image feature is not hardcoded to 10 everywhere.

### Target-state requirement

The newer version should preserve:

- a server-side image limit for query images
- frontend awareness of that runtime limit
- backend validation even if the frontend fails to enforce it

## Code 5: Backend Normalizes Images Before Use

In `lightrag/multimodal.py`, the `NER` branch uses:

```python
def normalize_image_data(image_data: str) -> str:
    """Return raw base64 payload from either a data URL or plain base64 string."""
    if image_data.startswith("data:") and "," in image_data:
        return image_data.split(",", 1)[1].strip()
    return image_data.strip()
```

This is important because the system accepts:

- full data URLs
- plain base64 strings

The newer version should preserve this normalization step or an equivalent one.

Do not assume the frontend always sends one exact format forever.

## Code 6: Server Converts Images To Text Descriptions For Retrieval

This is the first half of the dual-path design.

The `NER` branch contains a dedicated image-description pipeline in `lightrag/multimodal.py`.

### Base image description call

```python
def _call_ollama_generate(
    system_prompt: str,
    user_prompt: str,
    image_b64: str,
    *,
    model: str = DEFAULT_IMAGE_DESCRIPTION_MODEL,
    url: str = DEFAULT_IMAGE_DESCRIPTION_URL,
    timeout: int = DEFAULT_IMAGE_DESCRIPTION_TIMEOUT,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "system": system_prompt,
        "prompt": user_prompt,
        "images": [image_b64],
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_ctx": 32768,
            "num_predict": 8192,
        },
    }
    response = requests.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()
```

### Two-stage description flow

```python
async def describe_image_with_refinement(image_data: str, ...) -> str:
    image_b64 = normalize_image_data(image_data)

    initial_response = await asyncio.to_thread(
        _call_ollama_generate,
        PROMPTS["generate_image_description"],
        PROMPTS["generate_image_description"],
        image_b64,
        ...
    )
    initial_output = initial_response.get("response", "").strip()

    refinement_prompt = PROMPTS["refine_image_description"].format(
        initial_output=initial_output
    )
    refined_response = await asyncio.to_thread(
        _call_ollama_generate,
        PROMPTS["refine_image_description"],
        refinement_prompt,
        image_b64,
        ...
    )
    refined_output = refined_response.get("response", "").strip()

    return refined_output or initial_output
```

### Multi-image flow

```python
async def describe_images_with_refinement(image_data_list: list[str]) -> list[str]:
    descriptions: list[str] = []
    for image_data in image_data_list[:DEFAULT_IMAGES_UPLOAD_LIMIT]:
        description = await describe_image_with_refinement(image_data)
        if description:
            descriptions.append(description)
    return descriptions
```

### What to preserve

For the newer version, the exact model or provider can change, but the behavior should remain:

1. normalize each query image
2. produce one text description per image
3. preserve image order
4. skip empty failed descriptions
5. return a list of descriptions

## Code 7: Query Text Is Augmented Before Retrieval

This is the second critical part of the retrieval-side logic.

In the `NER` branch:

```python
def build_image_augmented_query(query: str, image_descriptions: list[str]) -> str:
    if not image_descriptions:
        return query.strip()

    descriptions = "\n".join(
        f"- Image {index + 1}: {description}"
        for index, description in enumerate(image_descriptions)
        if description.strip()
    )
    if not descriptions:
        return query.strip()

    return f"{query.strip()}\n\nImage descriptions:\n{descriptions}".strip()
```

Example:

If the original query is:

```text
What is the most likely diagnosis?
```

and image descriptions are:

```text
- Image 1: Chest radiograph showing bilateral patchy airspace opacities.
- Image 2: Peripheral smear with ring forms inside erythrocytes.
```

then the retrieval-side query becomes:

```text
What is the most likely diagnosis?

Image descriptions:
- Image 1: Chest radiograph showing bilateral patchy airspace opacities.
- Image 2: Peripheral smear with ring forms inside erythrocytes.
```

This augmented text is what the retrieval pipeline should use.

## Code 8: Retrieval Uses The Augmented Query, Not Only The Raw Query

In `lightrag/operate.py`, the `NER` branch does:

```python
query_for_pipeline = query
if getattr(query_param, "images", None):
    image_descriptions = await describe_images_with_refinement(query_param.images)
    query_for_pipeline = build_image_augmented_query(query, image_descriptions)
```

Then keyword extraction uses:

```python
hl_keywords, ll_keywords = await get_keywords_from_query(
    query_for_pipeline, query_param, global_config, hashing_kv
)
```

And context building uses:

```python
context_result = await _build_query_context(
    query_for_pipeline,
    ll_keywords_str,
    hl_keywords_str,
    ...
)
```

This means the image descriptions influence:

- keyword extraction
- context retrieval
- downstream query caching keys

The newer version must preserve this sequencing:

1. turn images into descriptions first
2. build an augmented query
3. use the augmented query for retrieval-side logic

## Code 9: Final LLM Call Still Receives The Raw Images

This is the other half of the dual-path design.

Even though retrieval uses the augmented text query, the final model call still receives the original images:

```python
response = await use_model_func(
    user_query,
    system_prompt=sys_prompt,
    history_messages=query_param.conversation_history,
    enable_cot=True,
    stream=query_param.stream,
    images=query_param.images or None,
)
```

Important detail:

- `user_query` here is the augmented text query
- `images=query_param.images or None` still passes the original raw image list

So the final model sees both:

1. textual image descriptions embedded into the prompt/query text
2. the raw images as multimodal input

That is intentional and should be preserved unless there is a strong reason to simplify it.

## Code 10: Bypass Mode Also Supports Query Images

The `NER` branch also handles images in bypass mode inside `lightrag/lightrag.py`:

```python
bypass_query = query.strip()
if param.images:
    image_descriptions = await describe_images_with_refinement(param.images)
    bypass_query = build_image_augmented_query(
        bypass_query, image_descriptions
    )

response = await use_llm_func(
    bypass_query,
    system_prompt=system_prompt,
    history_messages=param.conversation_history,
    enable_cot=True,
    stream=param.stream,
    images=param.images or None,
)
```

So image support is not limited to KG retrieval modes.

The newer version should preserve image support for:

- retrieval modes like `mix`, `hybrid`, `local`, `global`, `naive`
- bypass/direct-LLM mode as well

## Code 11: Provider Adapters Must Know How To Send Multimodal Query Content

The `NER` branch has provider-specific helpers.

### OpenAI-style payload

In `lightrag/multimodal.py`:

```python
def build_openai_multimodal_user_content(prompt: str, images: list[str] | None) -> Any:
    if not images:
        return prompt

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image_data in images:
        normalized_image_data = normalize_image_data(image_data)
        image_url = (
            image_data
            if image_data.startswith("data:")
            else f"data:image/jpeg;base64,{normalized_image_data}"
        )
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": image_url},
            }
        )
    return content
```

and in `lightrag/llm/openai.py`:

```python
messages.append(
    {
        "role": "user",
        "content": build_openai_multimodal_user_content(prompt, images),
    }
)
```

### Ollama-style payload

In `lightrag/multimodal.py`:

```python
def build_ollama_multimodal_user_message(prompt: str, images: list[str] | None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "user", "content": prompt}
    if images:
        message["images"] = [normalize_image_data(image_data) for image_data in images]
    return message
```

and in `lightrag/llm/ollama.py`:

```python
messages.append(build_ollama_multimodal_user_message(prompt, images))
```

### What to preserve

The newer version should keep the multimodal provider adaptation layer separate from query logic.

Query logic should not hardcode OpenAI/Ollama message formats directly.

Instead:

1. query path passes `images=...`
2. provider adapter translates that into the provider-native multimodal payload

## Code 12: Query Cache Must Include Image Identity

The `NER` branch includes image data in cache identity with:

```python
compute_mdhash_id("|".join(query_param.images)) if query_param.images else ""
```

inside the query cache hash generation and metadata.

This matters because:

- same text query with different images must not hit the same cache entry

The newer version should preserve this behavior with some equivalent image signature.

It does not have to be the exact same hash function, but the cache key must incorporate image identity.

## Relevant Config Surface

The `NER` branch uses these config knobs for query-with-image behavior:

### 1. `MAX_IMAGES_UPLOAD`

Used as the image count limit for query images and other image-related flows.

The `NER` branch also supports legacy fallback `IMAGE_UPLOAD_LIMIT`.

### 2. `LIGHTRAG_IMAGE_DESCRIPTION_MODEL`

Controls which model generates the query image descriptions.

Default in `NER`:

```python
"gemma4:31b-cloud"
```

### 3. `LIGHTRAG_IMAGE_DESCRIPTION_URL`

Controls the HTTP endpoint used for image description generation.

Default in `NER`:

```python
"http://localhost:11434/api/generate"
```

### 4. `LIGHTRAG_IMAGE_DESCRIPTION_TIMEOUT`

Controls timeout for the image-description pipeline.

Default in `NER`:

```python
600
```

### Target-state note

The newer version does not need to keep these exact variable names, but it should still keep:

- query image count limit
- image description model config
- image description endpoint or provider config
- timeout control

## Minimal Reimplementation Recipe

If another agent needs to recreate this quickly in a newer branch, the minimum set is:

1. Extend query request schema to accept `images: list[str]`.
2. Add frontend query UI support for selecting images.
3. Convert selected images to base64 in the browser.
4. Send those images in the normal query request body.
5. Normalize images server-side.
6. Generate one text description per image.
7. Build an augmented retrieval query by appending an `Image descriptions:` block.
8. Use that augmented query for keyword extraction and retrieval context building.
9. Still pass the original images to the final provider call.
10. Make provider adapters responsible for OpenAI/Ollama-style multimodal payload shapes.
11. Include image identity in query cache keys.

## If The Newer LightRAG Already Has Native Multimodal Query Support

If the newer version already supports direct multimodal query inputs, do not assume that is enough.

You still need to check whether it preserves the `NER` branch behavior of:

1. retrieval-time image-to-text augmentation
2. final-call raw image passing

If it only has one of those, it is not behaviorally equivalent yet.

## Bottom Line

The feature to preserve from the `NER` branch is not merely "users can attach images to queries."

The actual feature is:

- frontend query image selection
- base64 image transport in the query request
- server-side image description generation
- retrieval-side query augmentation from those descriptions
- final LLM multimodal call that still receives the raw images

That full combination is the real implementation that should be reproduced in the newer LightRAG version.
