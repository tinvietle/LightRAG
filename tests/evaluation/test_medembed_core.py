"""Coverage for next_step/medembed/medembed_core.py (chunking, vector mean, ranking)."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "next_step" / "medembed"))

from medembed_core import combine_vectors, pack_units, rank_neighbours, split_units  # noqa: E402


@pytest.mark.offline
def test_split_units_separates_sentences_and_field_lines():
    text = "Chief complaint: fever. Rash on trunk!\nLab findings: CRP 2.8; WBC 12\n\n"

    assert split_units(text) == [
        "Chief complaint: fever.",
        "Rash on trunk!",
        "Lab findings: CRP 2.8; WBC 12",
    ]
    assert split_units("   ") == []


@pytest.mark.offline
def test_pack_units_respects_the_token_limit_and_keeps_order():
    units = ["a", "b", "c", "d", "e"]
    lengths = [4, 4, 4, 4, 4]

    assert pack_units(units, lengths, 8) == ["a b", "c d", "e"]
    assert pack_units(units, lengths, 100) == ["a b c d e"]
    assert pack_units([], [], 10) == []


@pytest.mark.offline
def test_pack_units_gives_an_oversized_unit_its_own_chunk():
    assert pack_units(["x", "huge", "y"], [3, 900, 3], 10) == ["x", "huge", "y"]


@pytest.mark.offline
def test_combine_vectors_is_a_normalised_weighted_mean():
    vectors = np.array([[1.0, 0.0], [0.0, 1.0]])

    equal = combine_vectors(vectors)
    assert np.linalg.norm(equal) == pytest.approx(1.0)
    assert equal[0] == pytest.approx(equal[1])

    heavy = combine_vectors(vectors, [3.0, 1.0])
    assert heavy[0] > heavy[1] > 0
    with pytest.raises(ValueError):
        combine_vectors(np.empty((0, 2)))


@pytest.mark.offline
def test_rank_neighbours_orders_by_inner_product_and_can_exclude_self():
    matrix = np.array([[1.0, 0.0], [0.8, 0.6], [0.0, 1.0]], dtype=np.float32)
    query = np.array([1.0, 0.0], dtype=np.float32)

    assert [i for i, _ in rank_neighbours(query, matrix, 3)] == [0, 1, 2]
    assert [i for i, _ in rank_neighbours(query, matrix, 2, exclude=0)] == [1, 2]
    assert rank_neighbours(query, matrix, 1)[0][1] == pytest.approx(1.0)


# ---- helpers of medembed_rag.py that need no torch -------------------------------------
import hashlib  # noqa: E402
import sqlite3  # noqa: E402

from medembed_rag import cached_qwen, small_pool  # noqa: E402


def _case(case_id: str, disease: str, group: str | None = None) -> dict:
    return {
        "case_id": case_id,
        "split": "train",
        "specific": disease,
        "group": group or disease,
    }


@pytest.mark.offline
def test_small_pool_contains_the_chosen_diseases_and_fills_to_size():
    train = [_case(f"a{i}", "A") for i in range(12)] + [
        _case(f"b{i}", "B") for i in range(3)
    ]
    train += [_case(f"c{i}", f"C{i % 7}") for i in range(40)]
    chosen = [{"specific": "A"}, {"specific": "B"}]

    pool = small_pool(train, chosen, size=30, same=5, seed=1)
    ids = [c["case_id"] for c in pool]

    assert len(pool) == len(set(ids)) == 30
    assert (
        sum(c["specific"] == "A" for c in pool) >= 5
    )  # cap applies to the sibling pick
    assert sum(c["specific"] == "B" for c in pool) >= 3  # all three B cases exist
    assert [
        c["case_id"] for c in small_pool(train, chosen, 30, 5, 1)
    ] == ids  # deterministic


@pytest.mark.offline
def test_cached_qwen_returns_normalised_vectors_or_none_if_anything_is_missing(
    tmp_path,
):
    cache = tmp_path / "emb.sqlite"
    db = sqlite3.connect(str(cache))
    db.execute("CREATE TABLE vec (key TEXT PRIMARY KEY, blob BLOB NOT NULL)")

    def put(text: str, vector: list[float]) -> None:
        key = hashlib.sha256(f"m\n4096\n{text}".encode()).hexdigest()
        db.execute(
            "INSERT INTO vec VALUES (?, ?)",
            (key, np.asarray(vector, dtype=np.float32).tobytes()),
        )
        db.commit()

    put("one", [3.0, 4.0])
    result = cached_qwen(["one"], "m", cache)
    assert result is not None
    assert np.linalg.norm(result[0]) == pytest.approx(1.0)

    assert cached_qwen(["one", "two"], "m", cache) is None  # 'two' was never cached
    assert cached_qwen(["one"], "m", tmp_path / "missing.sqlite") is None
