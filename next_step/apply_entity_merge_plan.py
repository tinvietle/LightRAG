#!/usr/bin/env python3
"""Apply a reviewed entity merge plan to a LightRAG workspace.

This is the executor half of the entity-merge workflow described in
``next_step/entity_merge_implementation_and_evaluation_plan.md``. It consumes
the read-only plan produced by ``build_entity_merge_plan.py`` and, only when
explicitly told to, calls LightRAG's public ``amerge_entities`` API to collapse
each accepted component into its canonical entity.

Safety properties (do not weaken these without updating the design doc):

* Dry run by default. Nothing is written to the target workspace unless
  ``--apply`` is passed.
* Refuses to touch a workspace whose ``kv_store_doc_status.json`` shows any
  document still mid-ingestion.
* Verifies every input file recorded in the plan's ``manifest.json`` still has
  the same size and SHA-256 hash it had when the plan was built. If the source
  snapshot has drifted (re-ingestion, a different run, etc.), the plan may no
  longer describe this graph correctly, so the script refuses to proceed.
* Refuses to run against the same directory the plan's manifest says it was
  built from unless ``--allow-original-workspace`` is also passed. The
  intended usage is always: copy the finalized workspace, point ``--working-dir``
  at the copy, and leave the original untouched. This specific check is not
  from the design doc; it is added here as a safety rail because the design
  doc's own workflow assumes an isolated copy without a hard runtime enforcement.
* Only ever calls the public ``LightRAG.amerge_entities`` API - never touches
  storage files directly and never calls private merge helpers.
* Every component gets exactly one journal row per attempt, flushed
  immediately, so an interrupted run can be resumed without re-applying
  already-merged components or silently skipping ones that failed.
* A missing source (alias) entity is treated as a plan/snapshot mismatch
  (``rejected_precondition``), never as an implicit success. A component is
  only recorded as ``skipped_already_applied`` when the canonical entity
  exists AND every alias is already gone - i.e. this exact component was
  already merged by a previous run of this script.
* Stops the whole run on the first failure by default (correctness over
  throughput, per the design doc). Pass ``--continue-on-error`` to keep going
  and just record failures in the journal instead.

This script performs no evaluation of whether merging helps retrieval or
answer quality. That is a separate, later step.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# `build_entity_merge_plan.py` lives next to this file; running this script as
# `python next_step/apply_entity_merge_plan.py` puts `next_step/` on sys.path[0]
# so this import resolves without needing next_step to be an installed package.
from build_entity_merge_plan import ACTIVE_DOCUMENT_STATUSES  # noqa: E402

TERMINAL_SUCCESS_STATUSES = {"applied", "skipped_already_applied"}
DOC_STATUS_FILENAME = "kv_store_doc_status.json"


@dataclass(frozen=True, slots=True)
class MergeComponent:
    component_id: str
    canonical_entity: str
    source_entities: tuple[str, ...]
    entity_types: tuple[str, ...]
    risk_flags: tuple[str, ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--plan-dir",
        type=Path,
        default=Path("artifacts/entity_merge/workstation"),
        help="Directory containing merge_plan.jsonl and manifest.json.",
    )
    parser.add_argument(
        "--working-dir",
        type=Path,
        required=True,
        help=(
            "LightRAG working_dir to mutate. Must be an isolated copy of the "
            "workspace the plan was built from, never the live/original one."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually call amerge_entities. Without this flag, only a dry-run preview is produced and nothing is written.",
    )
    parser.add_argument(
        "--allow-original-workspace",
        action="store_true",
        help="Required in addition to --apply when --working-dir resolves to the same directory recorded as the plan's source storage.",
    )
    parser.add_argument(
        "--journal",
        type=Path,
        default=None,
        help="Defaults to <plan-dir>/apply_journal.jsonl",
    )
    parser.add_argument(
        "--component-ids",
        type=str,
        default=None,
        help="Comma-separated component_id allowlist. Default: every component in the plan.",
    )
    parser.add_argument(
        "--skip-risk-flags",
        type=str,
        default="",
        help="Comma-separated risk flags. Components carrying any of these are recorded as rejected_precondition and never applied.",
    )
    parser.add_argument(
        "--max-components",
        type=int,
        default=None,
        help="Process at most this many components (after filtering), useful for a small first batch.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Do not stop the run on the first failed component; record the failure and continue.",
    )
    parser.add_argument(
        "--flush-every",
        type=int,
        default=50,
        help=(
            "Batch this many merges before persisting to disk (requires "
            "amerge_entities(flush=False) support in lightrag.utils_graph). "
            "Each individual merge otherwise re-serializes the full entities/"
            "relationships vector stores, which dominates runtime for large "
            "batches. Set to 1 to flush after every merge (old behavior, slowest)."
        ),
    )
    args = parser.parse_args()
    if args.max_components is not None and args.max_components < 0:
        parser.error("--max-components must be non-negative")
    if args.flush_every < 1:
        parser.error("--flush-every must be at least 1")
    return args


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_snapshot(manifest_path: Path) -> str:
    """Verify every manifest input file is unchanged; return a snapshot id.

    The snapshot id is derived (not a field the current manifest.json stores)
    so every journal row can record which exact plan snapshot it was applied
    under, in case the plan is later regenerated from a different graph state.
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mismatches: list[str] = []
    hash_parts: list[str] = []
    for entry in manifest.get("input_files", []):
        path = Path(entry["path"])
        hash_parts.append(f"{entry['path']}:{entry['sha256']}")
        if not path.exists():
            mismatches.append(f"{path} no longer exists")
            continue
        actual_size = path.stat().st_size
        if actual_size != entry["size"]:
            mismatches.append(
                f"{path} size changed: expected {entry['size']}, found {actual_size}"
            )
            continue
        actual_hash = sha256_of(path)
        if actual_hash != entry["sha256"]:
            mismatches.append(f"{path} content changed (sha256 mismatch)")
    if mismatches:
        joined = "\n  - ".join(mismatches)
        raise SystemExit(
            "Refusing to apply: the source snapshot this plan was built from "
            f"has changed since planning:\n  - {joined}\n"
            "Regenerate the plan against the current workspace before applying."
        )
    return hashlib.sha256("\n".join(sorted(hash_parts)).encode("utf-8")).hexdigest()[:16]


def original_storage_dir(manifest_path: Path) -> Path | None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    input_files = manifest.get("input_files", [])
    if not input_files:
        return None
    return Path(input_files[0]["path"]).parent


def refuse_if_ingestion_active(working_dir: Path) -> None:
    doc_status_path = working_dir / DOC_STATUS_FILENAME
    if not doc_status_path.exists():
        return
    doc_status = json.loads(doc_status_path.read_text(encoding="utf-8"))
    active = sorted(
        doc_id
        for doc_id, record in doc_status.items()
        if str(record.get("status", "")).casefold() in ACTIVE_DOCUMENT_STATUSES
    )
    if active:
        raise SystemExit(
            f"Refusing to apply: {len(active)} document(s) in {doc_status_path} "
            f"are still mid-ingestion (e.g. {active[0]}). Wait for ingestion to "
            "finish, or confirm this working-dir is the intended target."
        )


def load_components(plan_path: Path) -> list[MergeComponent]:
    components: list[MergeComponent] = []
    with plan_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            components.append(
                MergeComponent(
                    component_id=record["component_id"],
                    canonical_entity=record["canonical_entity"],
                    source_entities=tuple(record["source_entities"]),
                    entity_types=tuple(record.get("entity_types", ())),
                    risk_flags=tuple(record.get("risk_flags", ())),
                )
            )
    return components


def load_journal(journal_path: Path) -> dict[str, str]:
    """Return the latest recorded status per component_id, if a journal exists.

    Skips batch-level rows (``"event": "batch_flush"``), which have no
    component_id of their own - they cover many components at once and are
    for audit/traceability, not per-component resume decisions.
    """
    if not journal_path.exists():
        return {}
    latest: dict[str, str] = {}
    with journal_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if "component_id" not in row:
                continue
            latest[row["component_id"]] = row["status"]
    return latest


def append_journal_row(journal_path: Path, row: dict[str, Any]) -> None:
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with journal_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()


def canonical_entity_type(component: MergeComponent) -> str | None:
    return component.entity_types[0] if component.entity_types else None


async def flush_all(rag) -> None:
    """Persist every storage amerge_entities(flush=False) left pending.

    The graph storage and the entity/relation chunk-tracking KV stores have
    no safety-net flush in finalize() - only the *_cache KV namespaces and
    the vector stores do. So this must be called explicitly, both
    periodically during a large batch and once more right before
    finalize_storages(), or graph/chunk-tracking mutations since the last
    flush are silently lost (they never existed anywhere but memory).
    """
    await asyncio.gather(
        rag.chunk_entity_relation_graph.index_done_callback(),
        rag.entities_vdb.index_done_callback(),
        rag.relationships_vdb.index_done_callback(),
        rag.entity_chunks.index_done_callback(),
        rag.relation_chunks.index_done_callback(),
    )


def build_lightrag_embedding_func():
    """Build the real embedding function from .env, matching the server.

    lightrag.api.config.global_args lazily parses sys.argv on first access,
    which would collide with this script's own argparse flags. Since every
    field that parser exposes has an environment-variable-backed default, we
    briefly present it with an empty argv so it parses pure env-var defaults,
    then restore argv for the rest of this process.
    """
    saved_argv = sys.argv
    try:
        sys.argv = [saved_argv[0] if saved_argv else "apply_entity_merge_plan.py"]
        from lightrag.api.config import global_args
        from lightrag.api.lightrag_server import create_embedding_function_from_args

        return create_embedding_function_from_args(global_args)
    finally:
        sys.argv = saved_argv


async def _unused_llm_model_func(*_args: Any, **_kwargs: Any) -> str:
    raise RuntimeError(
        "apply_entity_merge_plan.py never calls the LLM; this stub firing "
        "means something unexpected in LightRAG's init/merge path invoked it."
    )


async def run(args: argparse.Namespace) -> int:
    plan_path = args.plan_dir / "merge_plan.jsonl"
    manifest_path = args.plan_dir / "manifest.json"
    journal_path = args.journal or (args.plan_dir / "apply_journal.jsonl")
    skip_risk_flags = {f.strip() for f in args.skip_risk_flags.split(",") if f.strip()}
    allowed_ids = (
        {c.strip() for c in args.component_ids.split(",") if c.strip()}
        if args.component_ids
        else None
    )

    if not plan_path.exists():
        raise SystemExit(f"Merge plan not found: {plan_path}")
    if not manifest_path.exists():
        raise SystemExit(f"Manifest not found: {manifest_path}")

    working_dir = args.working_dir.resolve()
    original_dir = original_storage_dir(manifest_path)
    if original_dir is not None and working_dir == original_dir.resolve():
        if not args.allow_original_workspace:
            raise SystemExit(
                f"Refusing to apply: --working-dir ({working_dir}) is the same "
                f"directory the plan was built from ({original_dir}). Apply to "
                "an isolated copy instead, or pass --allow-original-workspace "
                "if you have deliberately decided to mutate the live workspace."
            )

    snapshot_id = verify_snapshot(manifest_path)
    refuse_if_ingestion_active(working_dir)

    components = load_components(plan_path)
    if allowed_ids is not None:
        components = [c for c in components if c.component_id in allowed_ids]
    if args.max_components is not None:
        components = components[: args.max_components]

    already = load_journal(journal_path)

    print(f"Snapshot id: {snapshot_id}")
    print(f"Plan: {plan_path} ({len(components)} component(s) selected)")
    print(f"Working dir: {working_dir}")
    print(f"Journal: {journal_path}")
    print(f"Mode: {'APPLY (will mutate storage)' if args.apply else 'DRY RUN (no mutation)'}")
    if args.apply:
        print(f"Flush every: {args.flush_every} merge(s)")
    print()

    if not args.apply:
        # Dry run: report what would happen, write nothing to the journal or
        # to storage, and exit. This is the default and safest invocation.
        would_apply = 0
        would_skip_risk = 0
        would_skip_done = 0
        for component in components:
            prior = already.get(component.component_id)
            if prior in TERMINAL_SUCCESS_STATUSES:
                would_skip_done += 1
                continue
            if set(component.risk_flags) & skip_risk_flags:
                would_skip_risk += 1
                continue
            would_apply += 1
        print(
            f"Would apply: {would_apply}\n"
            f"Would skip (already done per journal): {would_skip_done}\n"
            f"Would skip (matches --skip-risk-flags): {would_skip_risk}\n"
        )
        print("Re-run with --apply to actually mutate --working-dir.")
        return 0

    # --apply path below: real mutation happens from here on.
    from lightrag import LightRAG
    from lightrag.utils import VectorStorageConsistencyError

    embedding_func = build_lightrag_embedding_func()
    rag = LightRAG(
        working_dir=str(working_dir),
        embedding_func=embedding_func,
        llm_model_func=_unused_llm_model_func,
    )
    await rag.initialize_storages()

    applied = skipped_done = rejected = failed = 0
    since_last_flush: list[str] = []

    async def do_flush(reason: str) -> None:
        """Persist everything amerge_entities(flush=False) has deferred.

        Left uncaught on failure: a flush failure is a batch-level event, not
        a single component's failure, so it must not be journaled or handled
        as if it were one (see the periodic-flush call site below, which is
        deliberately outside the per-component try/except for this reason).
        """
        nonlocal since_last_flush
        if not since_last_flush:
            return
        covered = list(since_last_flush)
        try:
            await flush_all(rag)
        except Exception as error:
            append_journal_row(
                journal_path,
                {
                    "event": "batch_flush",
                    "status": "failed",
                    "reason": reason,
                    "covered_component_ids": covered,
                    "error": str(error),
                    "error_type": type(error).__name__,
                    "snapshot_id": snapshot_id,
                },
            )
            print(
                f"\nBATCH FLUSH FAILED (reason={reason}) covering components: "
                f"{covered}\n{error}\n"
                "These components' merges exist only in this process's memory "
                "and were not persisted. Whatever was flushed by an earlier, "
                "successful batch remains safely on disk. Recovery path: fix "
                "the underlying issue and re-run this script (already-flushed "
                "components will be skipped); if vector storage ever drifts "
                "from the graph, run `lightrag-rebuild-vdb`.\n"
            )
            raise
        append_journal_row(
            journal_path,
            {
                "event": "batch_flush",
                "status": "applied",
                "reason": reason,
                "covered_component_ids": covered,
                "snapshot_id": snapshot_id,
            },
        )
        since_last_flush = []

    try:
        for component in components:
            prior = already.get(component.component_id)
            if prior in TERMINAL_SUCCESS_STATUSES:
                skipped_done += 1
                continue

            if set(component.risk_flags) & skip_risk_flags:
                append_journal_row(
                    journal_path,
                    {
                        "component_id": component.component_id,
                        "status": "rejected_precondition",
                        "reason": "matched --skip-risk-flags",
                        "risk_flags": list(component.risk_flags),
                        "snapshot_id": snapshot_id,
                    },
                )
                rejected += 1
                continue

            target_exists = await rag.chunk_entity_relation_graph.has_node(
                component.canonical_entity
            )
            alias_exists = {
                name: await rag.chunk_entity_relation_graph.has_node(name)
                for name in component.source_entities
            }
            missing_aliases = [n for n, exists in alias_exists.items() if not exists]

            if not target_exists:
                append_journal_row(
                    journal_path,
                    {
                        "component_id": component.component_id,
                        "status": "rejected_precondition",
                        "reason": f"canonical entity '{component.canonical_entity}' not found in graph",
                        "snapshot_id": snapshot_id,
                    },
                )
                rejected += 1
                continue

            if missing_aliases and len(missing_aliases) == len(component.source_entities):
                # Every alias is already gone and the canonical exists: this
                # component's work was already done by a previous run.
                append_journal_row(
                    journal_path,
                    {
                        "component_id": component.component_id,
                        "status": "skipped_already_applied",
                        "snapshot_id": snapshot_id,
                    },
                )
                skipped_done += 1
                continue

            if missing_aliases:
                # Some but not all aliases are gone: an unexpected partial
                # state. Do not guess; flag for manual investigation.
                append_journal_row(
                    journal_path,
                    {
                        "component_id": component.component_id,
                        "status": "rejected_precondition",
                        "reason": "plan/snapshot mismatch: some but not all aliases missing",
                        "missing_aliases": missing_aliases,
                        "snapshot_id": snapshot_id,
                    },
                )
                rejected += 1
                continue

            append_journal_row(
                journal_path,
                {
                    "component_id": component.component_id,
                    "status": "pending",
                    "canonical_entity": component.canonical_entity,
                    "source_entities": list(component.source_entities),
                    "snapshot_id": snapshot_id,
                },
            )
            try:
                entity_type = canonical_entity_type(component)
                result = await rag.amerge_entities(
                    source_entities=list(component.source_entities),
                    target_entity=component.canonical_entity,
                    target_entity_data=(
                        {"entity_type": entity_type} if entity_type else None
                    ),
                    flush=False,
                )
                append_journal_row(
                    journal_path,
                    {
                        "component_id": component.component_id,
                        "status": "applied",
                        "canonical_entity": component.canonical_entity,
                        "source_entities": list(component.source_entities),
                        "merged_relation_count": result.get("relation_count"),
                        "snapshot_id": snapshot_id,
                    },
                )
                applied += 1
                since_last_flush.append(component.component_id)
            except VectorStorageConsistencyError as error:
                append_journal_row(
                    journal_path,
                    {
                        "component_id": component.component_id,
                        "status": "failed",
                        "error": str(error),
                        "error_type": "VectorStorageConsistencyError",
                        "snapshot_id": snapshot_id,
                    },
                )
                failed += 1
                print(
                    "\nVECTOR/GRAPH CONSISTENCY ERROR on component "
                    f"{component.component_id}: {error}\n"
                    "The knowledge graph already holds the merged state for this "
                    "component; the vector store may now lag behind it. Do not "
                    "retry this component blindly. Recovery path: run "
                    "`lightrag-rebuild-vdb` against this working-dir to rebuild "
                    "entities_vdb/relationships_vdb from the graph, then re-run "
                    "this script (already-applied components will be skipped).\n"
                )
                if not args.continue_on_error:
                    break
            except Exception as error:  # noqa: BLE001 - journal every failure mode
                append_journal_row(
                    journal_path,
                    {
                        "component_id": component.component_id,
                        "status": "failed",
                        "error": str(error),
                        "error_type": type(error).__name__,
                        "snapshot_id": snapshot_id,
                    },
                )
                failed += 1
                print(f"\nFAILED component {component.component_id}: {error}\n")
                if not args.continue_on_error:
                    break

            # Deliberately outside the try/except above: a flush covers many
            # components, so its failure must be journaled and reported as a
            # batch event (inside do_flush), never misattributed to whichever
            # single component happened to trigger the threshold.
            if len(since_last_flush) >= args.flush_every:
                await do_flush(reason="periodic")
    finally:
        # Persist anything left pending, however the loop above ended
        # (exhausted, broke on error, or an unexpected exception), before
        # finalize_storages() - the graph and chunk-tracking KV stores have
        # no safety-net flush of their own, unlike the vector stores.
        try:
            await do_flush(reason="final")
        except Exception:
            pass  # already journaled and printed inside do_flush
        await rag.finalize_storages()

    print(
        f"\nDone. applied={applied} skipped_already_applied={skipped_done} "
        f"rejected_precondition={rejected} failed={failed}"
    )
    return 1 if failed else 0


def main() -> int:
    args = parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
