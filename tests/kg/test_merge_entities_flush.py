from unittest.mock import AsyncMock

import pytest

from lightrag.utils_graph import _merge_entities_impl


def _make_node(name: str) -> dict:
    return {
        "entity_id": name,
        "description": f"description for {name}",
        "entity_type": "disease_disorder",
        "source_id": f"chunk-{name}",
        "file_path": f"{name}.json",
    }


class FakeGraph:
    """Minimal in-memory graph storage double, mirroring networkx_impl's
    deferred-persistence contract: mutations land in `self.nodes`
    immediately, but index_done_callback is a separately observable event.
    """

    def __init__(self, nodes: dict[str, dict]):
        self.nodes = dict(nodes)
        self.index_done_callback = AsyncMock(return_value=True)

    async def has_node(self, name: str) -> bool:
        return name in self.nodes

    async def get_node(self, name: str) -> dict | None:
        return self.nodes.get(name)

    async def get_node_edges(self, name: str):
        return []

    async def upsert_node(self, name: str, data: dict) -> None:
        self.nodes[name] = {**data, "entity_id": name}

    async def delete_node(self, name: str) -> None:
        self.nodes.pop(name, None)


class FakeVdb:
    def __init__(self):
        self.global_config: dict = {}
        self.upserted: list[dict] = []
        self.deleted_ids: list[str] = []
        self.index_done_callback = AsyncMock(return_value=True)

    async def upsert(self, data: dict) -> None:
        self.upserted.append(data)

    async def delete(self, ids: list[str]) -> None:
        self.deleted_ids.extend(ids)


@pytest.mark.offline
@pytest.mark.asyncio
async def test_merge_entities_flushes_by_default():
    """flush defaults to True: unchanged behavior from before this parameter existed."""
    graph = FakeGraph({"Alias": _make_node("Alias"), "Canonical": _make_node("Canonical")})
    entities_vdb = FakeVdb()
    relationships_vdb = FakeVdb()

    await _merge_entities_impl(
        graph,
        entities_vdb,
        relationships_vdb,
        source_entities=["Alias"],
        target_entity="Canonical",
    )

    graph.index_done_callback.assert_awaited()
    entities_vdb.index_done_callback.assert_awaited()
    relationships_vdb.index_done_callback.assert_awaited()
    assert "Alias" not in graph.nodes
    assert "Canonical" in graph.nodes


@pytest.mark.offline
@pytest.mark.asyncio
async def test_merge_entities_flush_false_defers_persistence():
    """flush=False must skip every index_done_callback while still merging in memory."""
    graph = FakeGraph({"Alias": _make_node("Alias"), "Canonical": _make_node("Canonical")})
    entities_vdb = FakeVdb()
    relationships_vdb = FakeVdb()

    await _merge_entities_impl(
        graph,
        entities_vdb,
        relationships_vdb,
        source_entities=["Alias"],
        target_entity="Canonical",
        flush=False,
    )

    graph.index_done_callback.assert_not_awaited()
    entities_vdb.index_done_callback.assert_not_awaited()
    relationships_vdb.index_done_callback.assert_not_awaited()
    # The merge itself must still have happened, entirely in memory.
    assert "Alias" not in graph.nodes
    assert "Canonical" in graph.nodes
    assert entities_vdb.upserted, "target entity should still be upserted into the vdb"
    assert entities_vdb.deleted_ids, "source alias should still be deleted from the vdb"
