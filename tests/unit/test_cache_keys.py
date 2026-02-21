"""
tests/unit/test_cache_keys.py — Cache key determinism (SPEC-01 file 25).

Acceptance criteria (SKILL-D R-D7, SPEC-01):
  - Each CacheKey builder is deterministic: same inputs → same key string.
  - Each key conforms to the pattern documented in api/cache/keys.py.
  - Different inputs produce different keys (no silent collision).
  - Known-good vectors from fixtures/cache_keys.json match the actual output.

No network, no LLM, no Neo4j — pure string computation.
"""

import json
import pathlib

import pytest

from api.cache.keys import CacheKey

# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------

_FIXTURES_DIR = pathlib.Path(__file__).parent.parent.parent / "fixtures"


@pytest.fixture(scope="module")
def cache_key_vectors() -> dict:
    """Load test vectors from fixtures/cache_keys.json."""
    path = _FIXTURES_DIR / "cache_keys.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Test: idempotency (same inputs → same key) for each builder
# ---------------------------------------------------------------------------

def test_schema_key_deterministic() -> None:
    """CacheKey.schema() is deterministic: two calls with the same run_id agree."""
    run_id = "abc123def456"
    assert CacheKey.schema(run_id) == CacheKey.schema(run_id)


def test_manifest_key_deterministic() -> None:
    """CacheKey.manifest() is deterministic."""
    run_id, doc_id = "run_abc", "doc_xyz"
    assert CacheKey.manifest(run_id, doc_id) == CacheKey.manifest(run_id, doc_id)


def test_graph_query_key_deterministic() -> None:
    """CacheKey.graph_query() is deterministic."""
    run_id, query_hash = "run_abc", "qhash_123"
    assert CacheKey.graph_query(run_id, query_hash) == CacheKey.graph_query(run_id, query_hash)


def test_chunk_key_deterministic() -> None:
    """CacheKey.chunk() is deterministic."""
    chunk_id = "chunk_001"
    assert CacheKey.chunk(chunk_id) == CacheKey.chunk(chunk_id)


def test_candidates_key_deterministic() -> None:
    """CacheKey.candidates() is deterministic."""
    run_id, detection_hash = "run_abc", "dhash_789"
    key = CacheKey.candidates(run_id, detection_hash)
    assert key == CacheKey.candidates(run_id, detection_hash)


def test_run_prefix_deterministic() -> None:
    """CacheKey.run_prefix() is deterministic."""
    run_id = "run_abc"
    assert CacheKey.run_prefix(run_id) == CacheKey.run_prefix(run_id)


# ---------------------------------------------------------------------------
# Test: known-good key formats from fixture vectors
# ---------------------------------------------------------------------------

def test_schema_key_fixture_vectors(cache_key_vectors: dict) -> None:
    """CacheKey.schema() matches every expected value from the fixture."""
    for case in cache_key_vectors["schema"]:
        actual = CacheKey.schema(run_id=case["run_id"])
        assert actual == case["expected"], (
            f"schema({case['run_id']!r}): got {actual!r}, expected {case['expected']!r}"
        )


def test_manifest_key_fixture_vectors(cache_key_vectors: dict) -> None:
    """CacheKey.manifest() matches every expected value from the fixture."""
    for case in cache_key_vectors["manifest"]:
        actual = CacheKey.manifest(run_id=case["run_id"], doc_id=case["doc_id"])
        assert actual == case["expected"]


def test_graph_query_key_fixture_vectors(cache_key_vectors: dict) -> None:
    """CacheKey.graph_query() matches every expected value from the fixture."""
    for case in cache_key_vectors["graph_query"]:
        actual = CacheKey.graph_query(run_id=case["run_id"], query_hash=case["query_hash"])
        assert actual == case["expected"]


def test_chunk_key_fixture_vectors(cache_key_vectors: dict) -> None:
    """CacheKey.chunk() matches every expected value from the fixture."""
    for case in cache_key_vectors["chunk"]:
        actual = CacheKey.chunk(chunk_id=case["chunk_id"])
        assert actual == case["expected"]


def test_candidates_key_fixture_vectors(cache_key_vectors: dict) -> None:
    """CacheKey.candidates() matches every expected value from the fixture."""
    for case in cache_key_vectors["candidates"]:
        actual = CacheKey.candidates(
            run_id=case["run_id"], detection_hash=case["detection_hash"]
        )
        assert actual == case["expected"]


def test_run_prefix_fixture_vectors(cache_key_vectors: dict) -> None:
    """CacheKey.run_prefix() matches every expected value from the fixture."""
    for case in cache_key_vectors["run_prefix"]:
        actual = CacheKey.run_prefix(run_id=case["run_id"])
        assert actual == case["expected"]


# ---------------------------------------------------------------------------
# Test: key format and prefix conventions
# ---------------------------------------------------------------------------

def test_schema_key_starts_with_schema_prefix() -> None:
    key = CacheKey.schema(run_id="abc123")
    assert key.startswith("schema:")


def test_manifest_key_starts_with_manifest_prefix() -> None:
    key = CacheKey.manifest(run_id="abc123", doc_id="doc456")
    assert key.startswith("manifest:")


def test_graph_query_key_starts_with_gq_prefix() -> None:
    key = CacheKey.graph_query(run_id="abc123", query_hash="qhash")
    assert key.startswith("gq:")


def test_chunk_key_starts_with_chunk_prefix() -> None:
    key = CacheKey.chunk(chunk_id="chunk001")
    assert key.startswith("chunk:")


def test_candidates_key_starts_with_candidates_prefix() -> None:
    key = CacheKey.candidates(run_id="abc123", detection_hash="dhash")
    assert key.startswith("candidates:")


def test_run_prefix_ends_with_colon() -> None:
    """run_prefix is used as a scan pattern; it must end with ':' for wildcard matching."""
    prefix = CacheKey.run_prefix(run_id="abc123")
    assert prefix.endswith(":")


def test_schema_key_embeds_run_id() -> None:
    """The run_id appears verbatim in the schema key."""
    run_id = "unique_run_id_xyz"
    key = CacheKey.schema(run_id=run_id)
    assert run_id in key


def test_manifest_key_embeds_both_components() -> None:
    """Both run_id and doc_id appear in the manifest key."""
    run_id, doc_id = "run_abc", "doc_xyz"
    key = CacheKey.manifest(run_id=run_id, doc_id=doc_id)
    assert run_id in key
    assert doc_id in key


# ---------------------------------------------------------------------------
# Test: collision resistance (different inputs → different keys)
# ---------------------------------------------------------------------------

def test_different_run_ids_produce_different_schema_keys() -> None:
    """Two different run_ids must not produce the same schema key."""
    key_a = CacheKey.schema(run_id="run_001")
    key_b = CacheKey.schema(run_id="run_002")
    assert key_a != key_b


def test_different_doc_ids_produce_different_manifest_keys() -> None:
    """The same run_id with different doc_ids produces different manifest keys."""
    run_id = "run_001"
    key_a = CacheKey.manifest(run_id=run_id, doc_id="doc_001")
    key_b = CacheKey.manifest(run_id=run_id, doc_id="doc_002")
    assert key_a != key_b


def test_schema_and_manifest_keys_do_not_collide() -> None:
    """schema:{run_id} and manifest:{run_id}:... must never be equal."""
    run_id = "abc123"
    schema_key = CacheKey.schema(run_id=run_id)
    manifest_key = CacheKey.manifest(run_id=run_id, doc_id="anything")
    assert schema_key != manifest_key


def test_chunk_and_candidates_keys_do_not_collide() -> None:
    """Keys from different namespaces must not accidentally collide."""
    some_id = "shared_value"
    chunk_key = CacheKey.chunk(chunk_id=some_id)
    # candidates key requires two args, but the prefix alone must differ
    assert not chunk_key.startswith("candidates:")


# ---------------------------------------------------------------------------
# Test: no-arg construction is forbidden (CacheKey is a namespace, not a factory)
# ---------------------------------------------------------------------------

def test_cache_key_all_methods_are_static() -> None:
    """All CacheKey builder methods are static — CacheKey is never instantiated."""
    import inspect
    for name, method in inspect.getmembers(CacheKey, predicate=inspect.isfunction):
        if not name.startswith("_"):
            assert isinstance(inspect.getattr_static(CacheKey, name), staticmethod), (
                f"CacheKey.{name} must be a @staticmethod"
            )
