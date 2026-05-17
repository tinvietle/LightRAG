# Document Upload With Images

The LightRAG server upload flow now supports a staged text-plus-image case upload.

## Multipart upload shape

Send `POST /documents/upload` as `multipart/form-data` with:

- `file`: required text document for the case
-- `images`: optional image files linked to that text file, up to 10 images per case

The text filename is used as the case key for the linked image directory.

## Storage layout

Uploaded images are stored under:

`<input-dir>/images/<case_id>`

where `<case_id>` is the uploaded text filename.

During ingestion, LightRAG enriches the extracted text with image descriptions before queueing the case for extraction.

## Deletion behavior

When a text case is deleted with file removal enabled, the linked image directory is removed as well.

## Client behavior

The WebUI upload dialog now stages text files and images separately and only starts uploading after the user confirms the selection.

Text files are mandatory. Images are optional, but they must be attached to a selected text case.
