"""Read LightRAG's Postgres-backed storage into the same shapes the JSON-file
storage classes (JsonKVStorage / NetworkXStorage) write to disk, so the
next_step/*.py analysis scripts can run unmodified against either backend.

This module is read-only. It never writes to Postgres. Column-to-key mappings
below were verified against the live schema in lightrag/kg/postgres_impl.py
(NAMESPACE_TABLE_MAP / TABLES / SQL_TEMPLATES) and against each next_step
script's actual field accesses -- not assumed from the DDL alone. Two
mappings are non-obvious and worth remembering if this drifts from upstream:

  * full_docs record key is "file_path", sourced from the doc_name column
    (matches postgres_impl.py's own get_by_id_full_docs SQL alias).
  * llm_response_cache record key is "return" (not "return_value") --
    evaluate_gleaning_value.py reads record.get("return", ""), matching the
    JSON KV file's on-disk key, which differs from the Postgres column name.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any

import asyncpg


@dataclass(frozen=True, slots=True)
class PgConnectionParams:
    host: str
    port: int
    user: str
    password: str
    database: str
    workspace: str = ""

    @classmethod
    def from_env(cls, *, workspace: str | None = None) -> "PgConnectionParams":
        missing = [
            name
            for name in ("POSTGRES_USER", "POSTGRES_PASSWORD")
            if not os.environ.get(name)
        ]
        if missing:
            raise ValueError(
                "Missing required Postgres env var(s) for --storage-backend pg: "
                + ", ".join(missing)
                + " (set them, or source the project .env)"
            )
        return cls(
            host=os.environ.get("POSTGRES_HOST", "localhost"),
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            database=os.environ.get("POSTGRES_DATABASE", "rag"),
            workspace=(
                workspace
                if workspace is not None
                else os.environ.get("WORKSPACE", "default")
            ),
        )

    def identity(self) -> tuple[str, int, str, str]:
        """Identity tuple for "same source" checks -- the PG analog of a
        resolved directory path in JSON mode (see apply_entity_merge_plan.py's
        original-workspace guard)."""
        return (self.host, self.port, self.database, self.workspace)

    def describe(self) -> str:
        return f"postgresql://{self.user}@{self.host}:{self.port}/{self.database}?workspace={self.workspace!r}"


def _jsonb(value: Any) -> Any:
    """asyncpg returns JSONB as str unless a codec is registered; parse it."""
    if value is None:
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value


async def _connect(params: PgConnectionParams) -> asyncpg.Connection:
    return await asyncpg.connect(
        host=params.host,
        port=params.port,
        user=params.user,
        password=params.password,
        database=params.database,
    )


async def _fetch_kv(
    conn: asyncpg.Connection,
    query: str,
    params: PgConnectionParams,
    id_key: str = "id",
) -> dict[str, dict[str, Any]]:
    rows = await conn.fetch(query, params.workspace)
    return {row[id_key]: dict(row) for row in rows}


async def load_full_docs_async(
    params: PgConnectionParams,
    conn: asyncpg.Connection | None = None,
) -> dict[str, dict[str, Any]]:
    owns_conn = conn is None
    conn = conn or await _connect(params)
    try:
        records = await _fetch_kv(
            conn,
            """SELECT id, COALESCE(content, '') AS content,
                      COALESCE(doc_name, '') AS file_path,
                      sidecar_location, parse_format, content_hash,
                      process_options, chunk_options, parse_engine
               FROM LIGHTRAG_DOC_FULL WHERE workspace = $1""",
            params,
        )
        for record in records.values():
            record["chunk_options"] = _jsonb(record.get("chunk_options"))
            del record["id"]
        return records
    finally:
        if owns_conn:
            await conn.close()


async def load_text_chunks_async(
    params: PgConnectionParams,
    conn: asyncpg.Connection | None = None,
) -> dict[str, dict[str, Any]]:
    owns_conn = conn is None
    conn = conn or await _connect(params)
    try:
        records = await _fetch_kv(
            conn,
            """SELECT id, tokens, COALESCE(content, '') AS content,
                      chunk_order_index, full_doc_id, file_path,
                      llm_cache_list, heading, sidecar,
                      EXTRACT(EPOCH FROM create_time)::BIGINT AS create_time,
                      EXTRACT(EPOCH FROM update_time)::BIGINT AS update_time
               FROM LIGHTRAG_DOC_CHUNKS WHERE workspace = $1""",
            params,
        )
        for record in records.values():
            record["llm_cache_list"] = _jsonb(record.get("llm_cache_list")) or []
            record["heading"] = _jsonb(record.get("heading")) or {}
            record["sidecar"] = _jsonb(record.get("sidecar")) or {}
            del record["id"]
        return records
    finally:
        if owns_conn:
            await conn.close()


async def _load_chunk_index_table_async(
    table: str, params: PgConnectionParams, conn: asyncpg.Connection | None = None
) -> dict[str, dict[str, Any]]:
    """Shared loader for LIGHTRAG_ENTITY_CHUNKS / LIGHTRAG_RELATION_CHUNKS,
    which have an identical (id, chunk_ids, count) shape."""
    owns_conn = conn is None
    conn = conn or await _connect(params)
    try:
        records = await _fetch_kv(
            conn,
            f"SELECT id, chunk_ids, count FROM {table} WHERE workspace = $1",
            params,
        )
        for record in records.values():
            record["chunk_ids"] = _jsonb(record.get("chunk_ids")) or []
            del record["id"]
        return records
    finally:
        if owns_conn:
            await conn.close()


async def load_entity_chunks_async(
    params: PgConnectionParams, conn: asyncpg.Connection | None = None
) -> dict[str, dict[str, Any]]:
    return await _load_chunk_index_table_async(
        "LIGHTRAG_ENTITY_CHUNKS", params, conn
    )


async def load_relation_chunks_async(
    params: PgConnectionParams, conn: asyncpg.Connection | None = None
) -> dict[str, dict[str, Any]]:
    return await _load_chunk_index_table_async(
        "LIGHTRAG_RELATION_CHUNKS", params, conn
    )


async def load_full_entities_async(
    params: PgConnectionParams, conn: asyncpg.Connection | None = None
) -> dict[str, dict[str, Any]]:
    owns_conn = conn is None
    conn = conn or await _connect(params)
    try:
        records = await _fetch_kv(
            conn,
            "SELECT id, entity_names, count FROM LIGHTRAG_FULL_ENTITIES WHERE workspace = $1",
            params,
        )
        for record in records.values():
            record["entity_names"] = _jsonb(record.get("entity_names")) or []
            del record["id"]
        return records
    finally:
        if owns_conn:
            await conn.close()


async def load_full_relations_async(
    params: PgConnectionParams, conn: asyncpg.Connection | None = None
) -> dict[str, dict[str, Any]]:
    owns_conn = conn is None
    conn = conn or await _connect(params)
    try:
        records = await _fetch_kv(
            conn,
            "SELECT id, relation_pairs, count FROM LIGHTRAG_FULL_RELATIONS WHERE workspace = $1",
            params,
        )
        for record in records.values():
            record["relation_pairs"] = _jsonb(record.get("relation_pairs")) or []
            del record["id"]
        return records
    finally:
        if owns_conn:
            await conn.close()


async def load_doc_status_async(
    params: PgConnectionParams, conn: asyncpg.Connection | None = None
) -> dict[str, dict[str, Any]]:
    owns_conn = conn is None
    conn = conn or await _connect(params)
    try:
        records = await _fetch_kv(
            conn,
            """SELECT id, content_summary, content_length, chunks_count, status,
                      file_path, chunks_list, track_id, metadata, error_msg,
                      content_hash,
                      EXTRACT(EPOCH FROM created_at)::BIGINT AS created_at,
                      EXTRACT(EPOCH FROM updated_at)::BIGINT AS updated_at
               FROM LIGHTRAG_DOC_STATUS WHERE workspace = $1""",
            params,
        )
        for record in records.values():
            record["chunks_list"] = _jsonb(record.get("chunks_list")) or []
            record["metadata"] = _jsonb(record.get("metadata")) or {}
            del record["id"]
        return records
    finally:
        if owns_conn:
            await conn.close()


async def load_llm_response_cache_async(
    params: PgConnectionParams, conn: asyncpg.Connection | None = None
) -> dict[str, dict[str, Any]]:
    owns_conn = conn is None
    conn = conn or await _connect(params)
    try:
        rows = await conn.fetch(
            """SELECT id, original_prompt, return_value, chunk_id, cache_type,
                      queryparam,
                      EXTRACT(EPOCH FROM create_time)::BIGINT AS create_time,
                      EXTRACT(EPOCH FROM update_time)::BIGINT AS update_time
               FROM LIGHTRAG_LLM_CACHE WHERE workspace = $1""",
            params.workspace,
        )
        records: dict[str, dict[str, Any]] = {}
        for row in rows:
            record = dict(row)
            record_id = record.pop("id")
            # JSON KV file key is "return", not the Postgres column name
            # "return_value" -- see module docstring.
            record["return"] = record.pop("return_value")
            record["queryparam"] = _jsonb(record.get("queryparam"))
            records[record_id] = record
        return records
    finally:
        if owns_conn:
            await conn.close()


@dataclass(frozen=True, slots=True)
class EntityMetadata:
    """Mirrors build_entity_merge_plan.EntityMetadata so callers can use
    either interchangeably."""

    entity_type: str
    description: str


async def _graph_name_async(
    conn: asyncpg.Connection, params: PgConnectionParams
) -> str:
    """LightRAG's PGGraphStorage derives one AGE graph per (workspace,
    namespace); the namespace here is always chunk_entity_relation. Mirrors
    PGGraphStorage._get_workspace_graph_name()."""
    workspace = params.workspace.strip()
    if workspace and workspace.casefold() != "default":
        import re

        safe_workspace = re.sub(r"[^a-zA-Z0-9_]", "_", workspace)
        return f"{safe_workspace}_chunk_entity_relation"
    return "chunk_entity_relation"


async def load_graph_metadata_async(
    params: PgConnectionParams, conn: asyncpg.Connection | None = None
) -> dict[str, EntityMetadata]:
    """Load per-node (entity_type, description), matching
    build_entity_merge_plan.load_graph_metadata()'s GraphML-derived shape.

    Uses a typed Cypher RETURN (``n.entity_type AS entity_type``) rather than
    fetching the whole vertex as agtype, so results come back as plain text
    columns asyncpg already knows how to decode -- no agtype text-format
    parsing needed.
    """
    owns_conn = conn is None
    conn = conn or await _connect(params)
    try:
        graph_name = await _graph_name_async(conn, params)
        await conn.execute("LOAD 'age'")
        await conn.execute('SET search_path = ag_catalog, "$user", public')
        cypher = (
            "MATCH (n:base) RETURN n.entity_id AS entity_id, "
            "n.entity_type AS entity_type, n.description AS description"
        )
        query = (
            f"SELECT * FROM cypher('{graph_name}', $$ {cypher} $$) "
            "AS (entity_id text, entity_type text, description text)"
        )
        rows = await conn.fetch(query)
        return {
            row["entity_id"]: EntityMetadata(
                entity_type=row["entity_type"] or "",
                description=row["description"] or "",
            )
            for row in rows
            if row["entity_id"]
        }
    finally:
        if owns_conn:
            await conn.close()


async def active_document_ids_async(
    params: PgConnectionParams,
    active_statuses: frozenset[str],
    conn: asyncpg.Connection | None = None,
) -> list[str]:
    """PG-native equivalent of build_entity_merge_plan.active_document_ids,
    without pulling every doc_status column across the wire."""
    owns_conn = conn is None
    conn = conn or await _connect(params)
    try:
        rows = await conn.fetch(
            "SELECT id, status FROM LIGHTRAG_DOC_STATUS WHERE workspace = $1",
            params.workspace,
        )
        return sorted(
            row["id"]
            for row in rows
            if (row["status"] or "").casefold() in active_statuses
        )
    finally:
        if owns_conn:
            await conn.close()


@dataclass(frozen=True, slots=True)
class PgTableFingerprint:
    """Row count + latest update_time per table -- the PG analog of
    snapshot_file()'s (size, mtime, sha256) for a JSON KV file. Cheap
    (no full-table hash) at the cost of being a weaker drift signal than a
    content hash; acceptable here because the actual hard safety check
    (refuse_if_ingestion_active) is a separate, exact query."""

    table: str
    row_count: int
    max_update_epoch: float | None


_FINGERPRINT_TABLES: dict[str, tuple[str, str]] = {
    "full_docs": ("LIGHTRAG_DOC_FULL", "update_time"),
    "entity_chunks": ("LIGHTRAG_ENTITY_CHUNKS", "update_time"),
    "text_chunks": ("LIGHTRAG_DOC_CHUNKS", "update_time"),
    "doc_status": ("LIGHTRAG_DOC_STATUS", "updated_at"),
}


async def snapshot_tables_async(
    params: PgConnectionParams, conn: asyncpg.Connection | None = None
) -> dict[str, PgTableFingerprint]:
    owns_conn = conn is None
    conn = conn or await _connect(params)
    try:
        result: dict[str, PgTableFingerprint] = {}
        for key, (table, time_col) in _FINGERPRINT_TABLES.items():
            row = await conn.fetchrow(
                f"""SELECT count(*) AS row_count,
                           EXTRACT(EPOCH FROM max({time_col}))::DOUBLE PRECISION AS max_update
                    FROM {table} WHERE workspace = $1""",
                params.workspace,
            )
            result[key] = PgTableFingerprint(
                table=table,
                row_count=row["row_count"],
                max_update_epoch=row["max_update"],
            )
        return result
    finally:
        if owns_conn:
            await conn.close()


def snapshot_tables(params: PgConnectionParams) -> dict[str, PgTableFingerprint]:
    """Synchronous wrapper for snapshot_tables_async, for the sync next_step
    scripts (build_entity_merge_plan.py's before/after drift check)."""
    return asyncio.run(snapshot_tables_async(params))


def fingerprints_to_manifest(
    params: PgConnectionParams, fingerprints: dict[str, PgTableFingerprint]
) -> dict[str, Any]:
    return {
        "backend": "pg",
        "connection": {
            "host": params.host,
            "port": params.port,
            "database": params.database,
            "workspace": params.workspace,
        },
        "tables": {
            key: {
                "table": fp.table,
                "row_count": fp.row_count,
                "max_update_epoch": fp.max_update_epoch,
            }
            for key, fp in fingerprints.items()
        },
    }


def verify_fingerprints_unchanged(
    recorded: dict[str, Any], current: dict[str, PgTableFingerprint]
) -> list[str]:
    """Returns a list of human-readable mismatches (empty = unchanged)."""
    mismatches: list[str] = []
    for key, recorded_entry in recorded.get("tables", {}).items():
        now = current.get(key)
        if now is None:
            mismatches.append(f"{key}: no longer queryable")
            continue
        if now.row_count != recorded_entry.get("row_count"):
            mismatches.append(
                f"{key}: row_count changed ({recorded_entry.get('row_count')} -> {now.row_count})"
            )
        elif (now.max_update_epoch or 0) > (recorded_entry.get("max_update_epoch") or 0) + 1e-6:
            mismatches.append(
                f"{key}: rows updated since snapshot "
                f"(max_update_epoch {recorded_entry.get('max_update_epoch')} -> {now.max_update_epoch})"
            )
    return mismatches


# ---------------------------------------------------------------------------
# Sync wrappers for the 5 read-only, synchronous next_step scripts. Each
# opens one connection and loads everything that script needs in one
# asyncio.run() call, so callers don't need to touch asyncio at all.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PgWorkspaceData:
    """Bag of whichever tables a script asked for, matching JSON-mode's
    per-file dict-of-dicts shape one key per requested table."""

    full_docs: dict[str, dict[str, Any]] = field(default_factory=dict)
    text_chunks: dict[str, dict[str, Any]] = field(default_factory=dict)
    entity_chunks: dict[str, dict[str, Any]] = field(default_factory=dict)
    relation_chunks: dict[str, dict[str, Any]] = field(default_factory=dict)
    full_entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    full_relations: dict[str, dict[str, Any]] = field(default_factory=dict)
    doc_status: dict[str, dict[str, Any]] = field(default_factory=dict)
    llm_response_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    graph_metadata: dict[str, EntityMetadata] = field(default_factory=dict)
    fingerprints: dict[str, PgTableFingerprint] = field(default_factory=dict)


_LOADERS = {
    "full_docs": load_full_docs_async,
    "text_chunks": load_text_chunks_async,
    "entity_chunks": load_entity_chunks_async,
    "relation_chunks": load_relation_chunks_async,
    "full_entities": load_full_entities_async,
    "full_relations": load_full_relations_async,
    "doc_status": load_doc_status_async,
    "llm_response_cache": load_llm_response_cache_async,
}


async def _load_workspace_async(
    params: PgConnectionParams,
    *,
    want: set[str],
    want_graph: bool,
    want_fingerprints: bool,
) -> PgWorkspaceData:
    conn = await _connect(params)
    try:
        data = PgWorkspaceData()
        for key in want:
            setattr(data, key, await _LOADERS[key](params, conn=conn))
        if want_graph:
            data.graph_metadata = await load_graph_metadata_async(params, conn=conn)
        if want_fingerprints:
            data.fingerprints = await snapshot_tables_async(params, conn=conn)
        return data
    finally:
        await conn.close()


def load_workspace(
    params: PgConnectionParams,
    *,
    want: set[str] = frozenset(),
    want_graph: bool = False,
    want_fingerprints: bool = False,
) -> PgWorkspaceData:
    """Synchronous entry point for the 5 read-only next_step scripts."""
    return asyncio.run(
        _load_workspace_async(
            params, want=want, want_graph=want_graph, want_fingerprints=want_fingerprints
        )
    )
