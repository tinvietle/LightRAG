from __future__ import annotations

import mimetypes
import os
from pathlib import Path

import httpx
import argparse


# dataset_path = Path(__file__).resolve().parent / "../dataset/fold1/train"
base_url = os.getenv("LIGHTRAG_API_BASE_URL", "http://127.0.0.1:9621")
api_key = os.getenv("LIGHTRAG_API_KEY")
max_images = int(os.getenv("MAX_MULTIMODAL_CASE_IMAGES", "10"))
image_content_types = {
    ".webp": "image/webp",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
supported_image_suffixes = {
    ".webp",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
}


def iter_cases(root: Path):
    for json_path in sorted(root.rglob("*.json")):
        if json_path.is_file():
            image_paths = sorted(
                path
                for path in json_path.parent.iterdir()
                if path.is_file() and path.suffix.lower() in supported_image_suffixes
            )
            yield json_path, image_paths


def upload_case(client: httpx.Client, json_path: Path, image_paths: list[Path]) -> bool:
    files: list[tuple[str, tuple[str, object, str]]] = []
    open_files = []
    image_count = 0

    try:
        text_file = json_path.open("rb")
        open_files.append(text_file)
        files.append(("file", (json_path.name, text_file, "application/json")))

        for image_path in image_paths:
            if image_count >= max_images:
                break
            image_file = image_path.open("rb")
            open_files.append(image_file)
            mime_type = image_content_types.get(image_path.suffix.lower())
            if mime_type is None:
                mime_type, _ = mimetypes.guess_type(image_path.name)
            files.append(
                ("images", (image_path.name, image_file, mime_type or "application/octet-stream"))
            )
            image_count += 1

        response = client.post("/documents/upload_multimodal_case", files=files)

        try:
            payload = response.json()
        except ValueError:
            payload = {"detail": response.text}

        status = payload.get("status", "unknown")
        track_id = payload.get("track_id", "")

        if response.is_success:
            print(f"OK   {json_path} -> {status} track_id={track_id}")
            print(f"Sent {len(files)} files: {[f[1][0] for f in files]}")
            return True

        detail = payload.get("detail") or payload.get("message") or response.text
        print(f"FAIL {json_path} -> HTTP {response.status_code}: {detail}")
        return False
    finally:
        for handle in open_files:
            handle.close()
            
def upload_case_without_images(client: httpx.Client, json_path: Path) -> bool:
    files: list[tuple[str, tuple[str, object, str]]] = []
    open_files = []

    try:
        text_file = json_path.open("rb")
        open_files.append(text_file)
        files.append(("file", (json_path.name, text_file, "application/json")))

        response = client.post("/documents/upload_multimodal_case", files=files)

        try:
            payload = response.json()
        except ValueError:
            payload = {"detail": response.text}

        status = payload.get("status", "unknown")
        track_id = payload.get("track_id", "")

        if response.is_success:
            print(f"OK   {json_path} -> {status} track_id={track_id}")
            print(f"Sent {len(files)} files: {[f[1][0] for f in files]}")
            return True

        detail = payload.get("detail") or payload.get("message") or response.text
        print(f"FAIL {json_path} -> HTTP {response.status_code}: {detail}")
        return False
    finally:
        for handle in open_files:
            handle.close()


def parse_args() -> tuple[Path, bool]:
    parser = argparse.ArgumentParser(description="Upload multimodal cases to LightRAG.")
    parser.add_argument(
        "dataset_path",
        type=Path,
        help="Path to the dataset directory containing JSON and image files.",
    )
    parser.add_argument(
        "--use-without-images",
        action="store_true",
        help="Upload cases without images (only JSON files).",
    )
    args = parser.parse_args()
    return args.dataset_path, args.use_without_images

def main(root: Path, use_without_images: bool) -> None:
    if not root.exists():
        raise FileNotFoundError(f"Dataset path does not exist: {root}")

    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key

    with httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=120.0) as client:
        uploaded = 0
        skipped = 0

        for json_path, image_paths in iter_cases(root):
            if not image_paths:
                print(f"WARN {json_path} -> no linked image files found")

            try:
                if use_without_images:
                    success = upload_case_without_images(client, json_path)
                else:
                    success = upload_case(client, json_path, image_paths)

                if success:
                    uploaded += 1
                else:
                    skipped += 1
            except Exception as exc:
                skipped += 1
                print(f"ERR  {json_path} -> {exc}")

        print(f"Done. uploaded={uploaded} failed={skipped}")


if __name__ == "__main__":
    dataset_path, use_without_images = parse_args()
    main(dataset_path, use_without_images)
