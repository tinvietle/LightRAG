from __future__ import annotations

import os
import sys
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import BackgroundTasks

sys.argv = sys.argv[:1]
os.environ.setdefault("TOKEN_SECRET", "test-secret")
os.environ.setdefault("AUTH_ACCOUNTS", "")

from lightrag.api.routers.document_routes import (
    delete_case_image_dir,
    upload_case_to_input_dir,
)


class DummyDocStatus:
    async def get_doc_by_file_path(self, file_path: str):
        return None


class DummyRAG:
    def __init__(self):
        self.doc_status = DummyDocStatus()


class DummyDocManager:
    def __init__(self, input_dir: Path):
        self.input_dir = input_dir
        self.supported_extensions = (".txt", ".md", ".pdf")

    def is_supported_file(self, filename: str) -> bool:
        return filename.endswith(self.supported_extensions)


class FakeUploadFile:
    def __init__(self, filename: str, data: bytes, content_type: str):
        self.filename = filename
        self.content_type = content_type
        self.size = len(data)
        self._buffer = BytesIO(data)

    async def read(self, size: int = -1):
        return self._buffer.read(size)


@pytest.mark.asyncio
async def test_upload_case_to_input_dir_saves_images_and_schedules_ingestion(tmp_path):
    rag = DummyRAG()
    doc_manager = DummyDocManager(tmp_path)
    background_tasks = BackgroundTasks()
    text_file = FakeUploadFile("case-001.txt", b"Fever and cough", "text/plain")
    images = [
        FakeUploadFile("figure-1.png", b"image-1", "image/png"),
        FakeUploadFile("figure-2.jpg", b"image-2", "image/jpeg"),
    ]

    response = await upload_case_to_input_dir(
        rag,
        doc_manager,
        background_tasks,
        text_file,
        images,
    )

    assert response.status == "success"
    assert response.track_id.startswith("upload_")
    assert (tmp_path / "case-001.txt").exists()

    image_dir = tmp_path / "images" / "case-001.txt"
    assert image_dir.exists()
    assert sorted(path.name for path in image_dir.iterdir()) == [
        "figure-1.png",
        "figure-2.jpg",
    ]
    assert len(background_tasks.tasks) == 1
    assert background_tasks.tasks[0].func.__name__ == "pipeline_index_file"
    assert background_tasks.tasks[0].args[3] == [
        image_dir / "figure-1.png",
        image_dir / "figure-2.jpg",
    ]


def test_delete_case_image_dir_removes_case_folder(tmp_path):
    image_dir = tmp_path / "images" / "case-001.txt"
    image_dir.mkdir(parents=True)
    (image_dir / "figure-1.png").write_bytes(b"image-1")

    delete_case_image_dir(tmp_path, "case-001.txt")

    assert not image_dir.exists()
