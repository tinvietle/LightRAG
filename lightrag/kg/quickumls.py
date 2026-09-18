"""Optional QuickUMLS pre-recognition through an isolated Python worker."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from lightrag.kg.ner import _format_ner_entities, _select_ner_entities
from lightrag.utils import Tokenizer, logger


DEFAULT_QUICKUMLS_SEMTYPES = frozenset(
    {"T019", "T020", "T047", "T048", "T050", "T190", "T191"}
)
_WORKER_PATH = (
    Path(__file__).resolve().parents[2] / "integrations" / "quickumls" / "worker.py"
)


class QuickUMLSWorker:
    """A lazily started JSON-lines subprocess owning one QuickUMLS matcher."""

    def __init__(
        self,
        *,
        python: str,
        index_dir: str,
        nltk_data: str,
        threshold: float,
        window: int,
        semtypes: frozenset[str],
        timeout: float,
    ) -> None:
        # Do not resolve this symlink: venv Python executables commonly point
        # at the base interpreter, and dereferencing it loses venv packages.
        self.python = str(Path(python).expanduser().absolute())
        self.index_dir = str(Path(index_dir).expanduser().resolve())
        self.nltk_data = str(Path(nltk_data).expanduser().resolve())
        self.threshold = threshold
        self.window = window
        self.semtypes = semtypes
        self.timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_lines: list[str] = []
        self._lock = asyncio.Lock()
        self._request_id = 0

    async def _start(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return

        for path, description in (
            (self.python, "QuickUMLS Python executable"),
            (self.index_dir, "QuickUMLS index"),
            (self.nltk_data, "QuickUMLS NLTK data"),
        ):
            if not Path(path).exists():
                raise FileNotFoundError(f"{description} not found: {path}")

        worker_env = os.environ.copy()
        worker_env["NLTK_DATA"] = self.nltk_data
        self._process = await asyncio.create_subprocess_exec(
            self.python,
            str(_WORKER_PATH),
            "--index-dir",
            self.index_dir,
            "--threshold",
            str(self.threshold),
            "--window",
            str(self.window),
            "--semtypes",
            ",".join(sorted(self.semtypes)),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=worker_env,
        )
        self._stderr_lines.clear()
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        assert self._process.stdout is not None
        try:
            ready_line = await asyncio.wait_for(
                self._process.stdout.readline(), timeout=self.timeout
            )
        except Exception:
            await self.close()
            raise
        if not ready_line:
            error = await self._read_stderr()
            await self.close()
            raise RuntimeError(error or "QuickUMLS worker exited during startup")
        ready = json.loads(ready_line)
        if ready.get("status") != "ready":
            await self.close()
            raise RuntimeError(f"Invalid QuickUMLS worker handshake: {ready!r}")
        logger.info("QuickUMLS worker initialized")

    async def _read_stderr(self) -> str:
        return "\n".join(self._stderr_lines[-20:]).strip()

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while line := await process.stderr.readline():
            self._stderr_lines.append(line.decode(errors="replace").rstrip())

    async def match(self, text: str) -> list[dict[str, Any]]:
        async with self._lock:
            await self._start()
            assert self._process is not None
            assert self._process.stdin is not None
            assert self._process.stdout is not None
            self._request_id += 1
            request_id = self._request_id
            payload = json.dumps({"id": request_id, "text": text}) + "\n"
            self._process.stdin.write(payload.encode())
            await self._process.stdin.drain()
            try:
                line = await asyncio.wait_for(
                    self._process.stdout.readline(), timeout=self.timeout
                )
            except Exception:
                await self.close()
                raise
            if not line:
                error = await self._read_stderr()
                await self.close()
                raise RuntimeError(error or "QuickUMLS worker exited unexpectedly")
            response = json.loads(line)
            if response.get("id") != request_id:
                raise RuntimeError("QuickUMLS worker returned an unexpected request id")
            if response.get("error"):
                raise RuntimeError(str(response["error"]))
            return list(response.get("matches", []))

    async def close(self) -> None:
        process, self._process = self._process, None
        stderr_task, self._stderr_task = self._stderr_task, None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                process.terminate()
                await process.wait()
        if stderr_task is not None:
            await asyncio.gather(stderr_task, return_exceptions=True)


_workers: dict[tuple[Any, ...], QuickUMLSWorker] = {}


def _worker_key(**config: Any) -> tuple[Any, ...]:
    return (
        config["python"],
        config["index_dir"],
        config["nltk_data"],
        config["threshold"],
        config["window"],
        tuple(sorted(config["semtypes"])),
        config["timeout"],
    )


async def recognize_quickumls_entities(
    text: str,
    *,
    python: str,
    index_dir: str,
    nltk_data: str,
    threshold: float = 0.9,
    window: int = 10,
    semtypes: frozenset[str] = DEFAULT_QUICKUMLS_SEMTYPES,
    timeout: float = 30.0,
    max_entities: int = 50,
    max_tokens: int = 400,
    tokenizer: Tokenizer | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Return source-text hints; CUIs are metadata and never rename entities."""
    if not text:
        return "", []
    config = {
        "python": python,
        "index_dir": index_dir,
        "nltk_data": nltk_data,
        "threshold": threshold,
        "window": window,
        "semtypes": semtypes,
        "timeout": timeout,
    }
    key = _worker_key(**config)
    worker = _workers.setdefault(key, QuickUMLSWorker(**config))
    try:
        raw_entities = await worker.match(text)
        entities = _select_ner_entities(
            raw_entities,
            max_entities=max_entities,
            max_tokens=max_tokens,
            tokenizer=tokenizer,
        )
        logger.debug(f"QuickUMLS recognized {len(entities)} entities")
        return _format_ner_entities(entities), entities
    except Exception as error:
        logger.error(f"Error during QuickUMLS recognition: {error}")
        return "", []


async def close_quickumls_workers() -> None:
    """Stop all workers created by this process."""
    workers = list(_workers.values())
    _workers.clear()
    await asyncio.gather(*(worker.close() for worker in workers), return_exceptions=True)
