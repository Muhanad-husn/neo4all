"""
tests/unit/test_run_id.py — Deterministic run_id derivation (SPEC-01 file 23).

Acceptance criteria (CLAUDE.md §10, SPEC-01):
  - Same inputs always produce the same run_id (idempotency).
  - Different inputs produce different run_ids (collision resistance).
  - Output is a 64-character lowercase hex digest (SHA-256 format).
  - The null-byte separator prevents boundary collisions between
    (user_namespace="ab", seed="c") and (user_namespace="a", seed="bc").
  - uuid4() is never used — output is fully deterministic.

No network, no LLM, no Neo4j — pure deterministic computation.
Fixture inputs loaded from fixtures/run_ids.json.
"""

import hashlib
import json
import pathlib

import pytest

from api.models.run import Phase, Run, derive_run_id

# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------

_FIXTURES_DIR = pathlib.Path(__file__).parent.parent.parent / "fixtures"


@pytest.fixture(scope="module")
def run_id_vectors() -> list[dict]:
    """Load test vectors from fixtures/run_ids.json."""
    path = _FIXTURES_DIR / "run_ids.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Reference implementation
#
# Mirrors the exact algorithm in derive_run_id() but written independently
# so the test can cross-validate the production code against a known formula.
# ---------------------------------------------------------------------------

def _reference_derive_run_id(user_namespace: str, timestamp_seed: str) -> str:
    """Canonical SHA-256 computation: sha256(user_namespace + NUL + timestamp_seed)."""
    raw = f"{user_namespace}\x00{timestamp_seed}".encode()
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Test: idempotency
# ---------------------------------------------------------------------------

def test_derive_run_id_is_deterministic() -> None:
    """Calling derive_run_id() twice with identical inputs returns the same string."""
    user_namespace = "alice"
    timestamp_seed = "2026-02-21T00:00:00+00:00"

    first = derive_run_id(user_namespace, timestamp_seed)
    second = derive_run_id(user_namespace, timestamp_seed)

    assert first == second


def test_derive_run_id_matches_fixture_vectors(run_id_vectors: list[dict]) -> None:
    """Every fixture vector: derive_run_id() output matches the reference computation."""
    for case in run_id_vectors:
        expected = _reference_derive_run_id(
            case["user_namespace"], case["timestamp_seed"]
        )
        actual = derive_run_id(case["user_namespace"], case["timestamp_seed"])
        assert actual == expected, (
            f"Mismatch for {case['description']!r}: "
            f"got {actual!r}, expected {expected!r}"
        )


# ---------------------------------------------------------------------------
# Test: output format
# ---------------------------------------------------------------------------

def test_derive_run_id_format() -> None:
    """Output is exactly 64 lowercase hex characters (SHA-256 digest)."""
    run_id = derive_run_id("alice", "2026-02-21T00:00:00+00:00")

    assert len(run_id) == 64, f"Expected 64 chars, got {len(run_id)}"
    assert run_id == run_id.lower(), "run_id must be lowercase hex"
    assert all(c in "0123456789abcdef" for c in run_id), (
        "run_id must contain only hex characters"
    )


# ---------------------------------------------------------------------------
# Test: collision resistance
# ---------------------------------------------------------------------------

def test_different_users_produce_different_run_ids() -> None:
    """Different user namespaces with the same seed produce different run_ids."""
    seed = "2026-02-21T00:00:00+00:00"
    assert derive_run_id("alice", seed) != derive_run_id("bob", seed)


def test_different_seeds_produce_different_run_ids() -> None:
    """The same user namespace with different seeds produces different run_ids."""
    user = "alice"
    assert (
        derive_run_id(user, "2026-02-21T00:00:00+00:00")
        != derive_run_id(user, "2026-02-21T00:00:01+00:00")
    )


def test_null_separator_prevents_boundary_collision() -> None:
    """(user='ab', seed='c') must differ from (user='a', seed='bc').

    Without the null-byte separator the raw bytes would be identical:
      'ab' + 'c'  ==  'a' + 'bc'  ==  b'abc'
    The separator makes these distinct inputs produce distinct digests.
    """
    id_ab_c = derive_run_id("ab", "c")
    id_a_bc = derive_run_id("a", "bc")

    assert id_ab_c != id_a_bc, (
        "Boundary collision detected: null-byte separator is not working"
    )


def test_boundary_collision_vectors_from_fixture(run_id_vectors: list[dict]) -> None:
    """The boundary-collision pair in the fixture produces different run_ids."""
    pair_ab_c = next(
        c for c in run_id_vectors if c["description"].startswith("boundary-collision pair A")
    )
    pair_a_bc = next(
        c for c in run_id_vectors if c["description"].startswith("boundary-collision pair B")
    )
    id_ab_c = derive_run_id(pair_ab_c["user_namespace"], pair_ab_c["timestamp_seed"])
    id_a_bc = derive_run_id(pair_a_bc["user_namespace"], pair_a_bc["timestamp_seed"])

    assert id_ab_c != id_a_bc


# ---------------------------------------------------------------------------
# Test: Run model
# ---------------------------------------------------------------------------

def test_run_create_is_deterministic() -> None:
    """Run.create() with the same arguments always produces the same run_id."""
    user_namespace = "alice"
    timestamp_seed = "2026-02-21T00:00:00+00:00"

    run_a = Run.create(user_namespace, timestamp_seed)
    run_b = Run.create(user_namespace, timestamp_seed)

    assert run_a.run_id == run_b.run_id


def test_run_create_matches_derive_run_id() -> None:
    """Run.create() embeds the same run_id as derive_run_id() would return."""
    user_namespace = "acme-corp/project-x"
    timestamp_seed = "2026-01-15T12:30:45+00:00"

    run = Run.create(user_namespace, timestamp_seed)
    expected_id = derive_run_id(user_namespace, timestamp_seed)

    assert run.run_id == expected_id


def test_run_create_starts_at_phase_init() -> None:
    """A freshly created Run is always in Phase.INIT."""
    run = Run.create("alice", "2026-02-21T00:00:00+00:00")
    assert run.phase == Phase.INIT


def test_run_create_schema_version_is_none() -> None:
    """A freshly created Run has no schema version (set during Phase 1)."""
    run = Run.create("alice", "2026-02-21T00:00:00+00:00")
    assert run.schema_version is None


def test_run_id_is_not_uuid4() -> None:
    """The run_id is a SHA-256 hex digest, not a UUID4 (which has dashes and version bits)."""
    run = Run.create("alice", "2026-02-21T00:00:00+00:00")
    # UUID4 format: 8-4-4-4-12 hex with dashes (e.g. "550e8400-e29b-41d4-a716-446655440000")
    assert "-" not in run.run_id, "run_id must not be a UUID4 (no dashes allowed)"
    assert len(run.run_id) == 64, "run_id must be a 64-char SHA-256 hex digest"
