# Multimodal Case Input Transfer Note

This document describes the custom "text + corresponding images" input flow implemented in this branch, so it can be recreated in a newer LightRAG version even if the file structure is different.

The important behavior to preserve is:

`user enters or uploads one text case + linked image files -> custom API endpoint receives both -> server turns images into text descriptions -> server appends those descriptions to the original text -> enriched text is passed into the normal LightRAG ingestion pipeline`

## What This Feature Is For

This branch supports multimodal medical case ingestion by letting one text case carry a small set of corresponding images.

The key design choice is:

- LightRAG ingestion still receives text
- image understanding is performed before ingestion
- images are converted into textual descriptions
- those descriptions are merged into the original case text

This is intentionally a custom integration layer.

If the newer LightRAG version later has first-class image description support inside core ingestion, that future native mechanism can replace this custom path. Until then, preserve this custom behavior.

## Core Product Requirement

The new version should provide a UI input flow where one case consists of:

1. one main text input or text file
2. zero or more corresponding image attachments
3. one dedicated API call for this multimodal case upload

The frontend must not treat images as standalone documents.
The images belong to one text case and are only meaningful in relation to that case.

## High-Level Architecture

The current branch implements this in four layers:

1. frontend staged case composer
2. dedicated upload API that accepts one text case plus images
3. backend image-to-text enrichment helper
4. normal LightRAG ingestion using the enriched text

That separation should be preserved.

## Code 1: Frontend Must Model A Case As Text + Linked Images

Current UI file:

- [lightrag_webui/src/components/documents/UploadDocumentsDialog.tsx](/home/tin/LightRAG/lightrag_webui/src/components/documents/UploadDocumentsDialog.tsx)

The important state shape is:

```ts
type SelectedImage = {
  id: string
  file: File
  previewUrl: string
}

type SelectedTextCase = {
  id: string
  file: File
  images: SelectedImage[]
}
```

This is the correct mental model for the feature.

Do not model this as:

- one flat file list containing text and images mixed together
- independent image uploads
- generic document upload with no case-level grouping

The newer UI can look different, but it should preserve the same logical structure:

- select or enter one text case
- add images to that specific case
- submit that case as one unit

### Current image attachment behavior

In this branch, images are added to the currently active text case:

```ts
const addImagesToActiveCase = useCallback(
  (acceptedFiles: File[], rejectedFiles: FileRejection[]) => {
    if (!activeCaseId) {
      toast.error('Select a text file first.')
      return
    }

    setCases((currentCases) => {
      const nextCases = currentCases.map((selectedCase) => {
        if (selectedCase.id !== activeCaseId) {
          return selectedCase
        }

        const nextImages = acceptedFiles.map((file) => ({
          id: createId(),
          file,
          previewUrl: URL.createObjectURL(file)
        }))

        return {
          ...selectedCase,
          images: [...selectedCase.images, ...nextImages]
        }
      })

      return nextCases
    })
  },
  [activeCaseId]
)
```

What to preserve:

- images are attached to one selected case
- image preview URLs are frontend-only convenience state
- the UI must prevent attaching images before a text case exists

## Code 2: Frontend Must Call A Dedicated Multimodal Upload API

Current API client file:

- [lightrag_webui/src/api/lightrag.ts](/home/tin/LightRAG/lightrag_webui/src/api/lightrag.ts)

The current client builds a multipart request like this:

```ts
const buildCaseUploadFormData = (file: File, images: File[] = []) => {
  const formData = new FormData()
  formData.append('file', file)

  images.forEach((image) => {
    formData.append('images', image)
  })

  return formData
}

export const uploadCaseDocument = async (
  file: File,
  images: File[] = [],
  onUploadProgress?: (percentCompleted: number) => void
) => {
  const formData = buildCaseUploadFormData(file, images)

  const response = await axiosInstance.post('/documents/upload', formData, {
    headers: {
      'Content-Type': 'multipart/form-data'
    }
  })
  return response.data
}
```

This is the key API pattern to preserve:

- one request
- one main text payload
- repeated `images` form fields
- multipart form upload

### Target-state requirement for the newer version

Even if the exact route name changes, the new version should still have a dedicated API path specifically for:

- text case
- corresponding images

This should not be silently folded into a generic upload path unless that generic path still clearly preserves this exact behavior.

If the newer version has a cleaner API surface, a route like one of these is appropriate:

- `POST /documents/upload_multimodal_case`
- `POST /cases/upload`
- `POST /documents/upload_case_with_images`

What matters is behavior, not the route string.

## Code 3: Backend Route Must Accept One Text Upload Plus Optional Images

Current backend route file:

- [lightrag/api/routers/document_routes.py](/home/tin/LightRAG/lightrag/api/routers/document_routes.py)

The current helper function is:

```python
async def upload_case_to_input_dir(
    rag: LightRAG,
    doc_manager: "DocumentManager",
    background_tasks: BackgroundTasks,
    file: UploadFile,
    images: list[UploadFile] | None = None,
):
    safe_filename = sanitize_filename(file.filename, doc_manager.input_dir)

    if not doc_manager.is_supported_file(safe_filename):
        raise HTTPException(status_code=400, detail="Unsupported file type")

    existing_doc_data = await rag.doc_status.get_doc_by_file_path(safe_filename)
    if existing_doc_data:
        return InsertResponse(status="duplicated", ...)

    file_path = doc_manager.input_dir / safe_filename
    ...

    saved_image_paths: list[Path] = []
    case_image_dir = get_case_image_dir(doc_manager.input_dir, safe_filename)
    if images:
        saved_image_paths = await save_case_images(images, case_image_dir)

    track_id = generate_track_id("upload")
    background_tasks.add_task(
        pipeline_index_file,
        rag,
        file_path,
        track_id,
        saved_image_paths,
    )

    return InsertResponse(status="success", ...)
```

This is the server-side pattern to preserve:

1. save the main text case
2. save the corresponding images
3. keep the saved image paths associated with that case
4. schedule background ingestion with both the text file path and image paths

### Important design rule

The API should not require the frontend to generate image descriptions.

The frontend only uploads:

- text
- raw image files

The server is responsible for:

- storing images
- converting images into text descriptions
- combining descriptions with the main text

## Code 4: Images Are Stored Under A Case-Specific Folder

Current backend helper pattern:

```python
def get_case_image_dir(input_dir: Path, case_id: str) -> Path:
    return input_dir / "images" / case_id
```

and:

```python
async def save_case_images(
    images: list[UploadFile],
    case_image_dir: Path,
) -> list[Path]:
    if not images:
        return []

    case_image_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[Path] = []

    for image in images[:5]:
        if image.content_type and not image.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="Only image files are allowed.")

        safe_image_name = sanitize_filename(image.filename, case_image_dir)
        unique_image_path = case_image_dir / safe_image_name

        async with aiofiles.open(unique_image_path, "wb") as out_file:
            while True:
                chunk = await image.read(1024 * 1024)
                if not chunk:
                    break
                await out_file.write(chunk)

        saved_paths.append(unique_image_path)

    return saved_paths
```

What to preserve:

- images are persisted per case
- image files are validated as image content
- case-image association exists on disk before ingestion starts

The newer version can store images differently, but it must still preserve case-level association.

## Code 5: Image-To-Text Conversion Must Happen On The Server

Current multimodal helper file:

- [lightrag/multimodal.py](/home/tin/LightRAG/lightrag/multimodal.py)

The existing branch already contains a custom image description pipeline. The important behavior is:

- read image bytes
- base64 encode them
- send them to a vision-capable model
- get back one text description per image
- optionally refine the first-pass description

Current helper pattern:

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

and for multiple images:

```python
async def describe_images_with_refinement(image_data_list: list[str]) -> list[str]:
    descriptions: list[str] = []
    for image_data in image_data_list[:DEFAULT_IMAGES_UPLOAD_LIMIT]:
        description = await describe_image_with_refinement(image_data)
        if description:
            descriptions.append(description)
    return descriptions
```

### Target-state instruction

For the newer version, preserve this responsibility boundary:

- the custom multimodal case endpoint must trigger image description generation directly
- this happens before normal ingestion
- the ingestion layer still receives text

If the future LightRAG version has its own built-in image description pipeline, it can replace this helper, but the final result should still be:

- image descriptions become text
- that text is combined with the original case text
- the combined text is passed into ingestion

## Code 6: The Exact Text-Enrichment Step

This is the most important backend transformation.

Current helper:

```python
async def _augment_content_with_case_images(
    content: str,
    image_paths: list[Path] | None,
) -> str:
    if not image_paths:
        return content

    try:
        image_data_list: list[str] = []
        for image_path in image_paths[:5]:
            image_bytes = await asyncio.to_thread(image_path.read_bytes)
            image_data_list.append(base64.b64encode(image_bytes).decode("utf-8"))

        image_descriptions = await describe_images_with_refinement(image_data_list)
        if not image_descriptions:
            return content

        image_description_block = "\n".join(
            f"- {description}" for description in image_descriptions if description.strip()
        )
        if not image_description_block:
            return content

        return (
            f"{content.rstrip()}\n\nAttached image descriptions:\n{image_description_block}"
        )
    except Exception as exc:
        logger.warning("Failed to enrich content with image descriptions: %s", exc)
        return content
```

This is the exact behavior to preserve.

Important details:

1. if there are no images, return the original text unchanged
2. only a small bounded number of images are processed
3. each image is turned into one text description
4. descriptions are filtered for non-empty text
5. the final enrichment is appended to the original text
6. if image description fails, ingestion falls back to the original text instead of failing the entire upload

### Exact output shape

If the original case text is:

```text
Patient with fever, rash, and thrombocytopenia.
```

and the image descriptions are:

```text
- Maculopapular rash over both lower limbs
- Chest radiograph showing patchy bilateral opacities
```

then the final text passed into ingestion becomes:

```text
Patient with fever, rash, and thrombocytopenia.

Attached image descriptions:
- Maculopapular rash over both lower limbs
- Chest radiograph showing patchy bilateral opacities
```

That merged text is what enters the normal LightRAG backend pipeline.

## Code 7: Enrichment Must Happen Before Enqueueing Into LightRAG

Current enqueue helper:

```python
async def pipeline_enqueue_file(
    rag: LightRAG,
    file_path: Path,
    track_id: str = None,
    image_paths: list[Path] | None = None,
) -> tuple[bool, str]:
    ...
    content = ...  # extracted from text file
    content = await _augment_content_with_case_images(content, image_paths)

    if content:
        await rag.apipeline_enqueue_documents(
            content, file_paths=file_path.name, track_id=track_id
        )
```

This is the critical sequencing rule:

1. extract the base text content
2. enrich it with image-derived descriptions
3. enqueue the enriched text into LightRAG

Do not:

- enqueue the original text first and enrich later
- store images without using them during ingestion
- perform image description only at query time if the goal is multimodal ingestion

## Code 8: Background Processing Must Carry The Image Paths

Current scheduling pattern:

```python
background_tasks.add_task(
    pipeline_index_file,
    rag,
    file_path,
    track_id,
    saved_image_paths,
)
```

and:

```python
async def pipeline_index_file(
    rag: LightRAG,
    file_path: Path,
    track_id: str = None,
    image_paths: list[Path] | None = None,
):
    success, returned_track_id = await pipeline_enqueue_file(
        rag, file_path, track_id, image_paths
    )
```

This means the multimodal association is carried all the way into the ingestion worker.

The newer version must preserve this handoff somehow:

- the uploaded images must remain associated with the text case until enrichment is complete

## Code 9: Frontend UX Constraints Worth Preserving

The current UI also enforces a few useful behaviors:

1. user must select or stage a text case before attaching images
2. image count limit is fetched from backend runtime config
3. each staged case is uploaded independently and shows its own progress
4. successful cases are removed from staged state while failed cases remain visible

You do not need to preserve the exact visual layout, but these workflow rules are practical and should remain if possible.

## Suggested Target-State API Contract

For the newer version, a good explicit API contract is:

### Request

Multipart form-data with:

```text
file: <main text case file or text payload>
images: <repeated image file fields>
```

Optional future extension:

```text
case_id: <client or server generated identifier>
source_type: "text_with_images"
```

### Server behavior

1. validate and save the text payload
2. validate and save linked images
3. call the custom image description pipeline directly
4. append image descriptions to the original text
5. pass merged text into normal LightRAG ingestion

### Response

Return the same style of ingestion response as normal uploads:

```json
{
  "status": "success",
  "message": "File uploaded successfully with 2 attached image(s). Processing will continue in background.",
  "track_id": "upload_..."
}
```

## Minimal Reimplementation Recipe

If another agent needs to recreate this quickly in a new branch, the minimum required pieces are:

1. Build a frontend case composer that groups one text case with its corresponding images.
2. Send that grouped case to a dedicated multipart upload API.
3. Save the case text and image files server-side.
4. Add a backend helper that turns images into text descriptions.
5. Append those descriptions to the original text in a stable textual block.
6. Pass the enriched text into the normal LightRAG ingestion pipeline.
7. Keep failures in image description non-fatal so the text case can still ingest.

## Bottom Line

The feature to preserve is not "image upload" by itself.
The real feature is:

- one text case
- linked medical images
- custom API upload path
- server-side image description generation
- merged text sent into standard LightRAG ingestion

That is the behavior the newer LightRAG version should reproduce.
