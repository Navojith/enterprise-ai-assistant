"""Tests for the access-level policy and the Chunk identity/idempotency contract."""

from __future__ import annotations

from datetime import date

import pytest

from backend.app.retrieval.models import (
    AccessLevel,
    Chunk,
    DocumentMetadata,
    DocumentType,
    allowed_access_levels,
)


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("viewer", ["public", "internal"]),
        ("Analyst", ["public", "internal", "confidential"]),  # case-insensitive
        ("administrator", ["public", "internal", "confidential"]),
    ],
)
def test_allowed_access_levels_matches_role_ceiling(role: str, expected: list[str]) -> None:
    assert allowed_access_levels(role) == expected


def test_allowed_access_levels_rejects_unknown_role() -> None:
    with pytest.raises(ValueError, match="Unknown role"):
        allowed_access_levels("superuser")


def _metadata(**overrides: object) -> DocumentMetadata:
    defaults: dict[str, object] = {
        "document_id": "doc-1",
        "title": "Test Document",
        "department": "payments",
        "document_type": DocumentType.INCIDENT,
        "access_level": AccessLevel.INTERNAL,
        "created_date": date(2026, 1, 1),
    }
    defaults.update(overrides)
    return DocumentMetadata.model_validate(defaults)


def test_chunk_id_is_stable_across_content_changes() -> None:
    """Editing a section's text must update the same Pinecone record, not create a new one —
    otherwise re-ingestion after an edit leaves an orphaned duplicate under the old id."""
    metadata = _metadata()
    original = Chunk.create(document_id="doc-1", section="Summary", text="v1", metadata=metadata)
    edited = Chunk.create(document_id="doc-1", section="Summary", text="v2", metadata=metadata)

    assert original.chunk_id == edited.chunk_id
    assert original.content_hash != edited.content_hash


def test_content_hash_distinguishes_identical_text_in_different_sections() -> None:
    metadata = _metadata()
    a = Chunk.create(document_id="doc-1", section="Summary", text="same text", metadata=metadata)
    b = Chunk.create(document_id="doc-1", section="Impact", text="same text", metadata=metadata)

    assert a.content_hash != b.content_hash
    assert a.chunk_id != b.chunk_id


def test_to_pinecone_record_carries_the_required_metadata_schema() -> None:
    metadata = _metadata(department="security", access_level=AccessLevel.CONFIDENTIAL)
    chunk = Chunk.create(
        document_id="doc-1", section="Purpose", text="policy text", metadata=metadata
    )

    record = chunk.to_pinecone_record()

    assert record["_id"] == chunk.chunk_id
    assert record["department"] == "security"
    assert record["document_type"] == "incident"
    assert record["access_level"] == "confidential"
    assert record["created_date"] == "2026-01-01"
    assert isinstance(record["created_date_epoch"], int)


def test_to_pinecone_record_embeds_title_and_section_but_preserves_plain_text() -> None:
    """`chunk_text` is what the index actually embeds and must carry the document's title and
    section name (trade-off 26) — without it, a chunk whose body text is generic on its own
    (as several of this corpus's runbook sections deliberately are) is nearly unfindable by a
    query that names the document or section directly. `section_text` must stay the plain
    body, since `PineconeStore.search` reads it back as `RetrievedChunk.text` for display and
    citation."""
    metadata = _metadata(title="Test Document")
    chunk = Chunk.create(
        document_id="doc-1", section="Purpose", text="policy text", metadata=metadata
    )

    record = chunk.to_pinecone_record()

    assert record["chunk_text"] == "Test Document — Purpose\n\npolicy text"
    assert record["section_text"] == "policy text"


def test_content_hash_changes_when_only_the_title_changes() -> None:
    """The title is embedded as part of `chunk_text` (trade-off 26), so a title-only change
    must not be treated as "unchanged" by the idempotency check, or the re-embedded index
    would silently drift from what re-ingestion believes it already has."""
    text, section = "same text", "Purpose"
    a = Chunk.create(
        document_id="doc-1", section=section, text=text, metadata=_metadata(title="Title A")
    )
    b = Chunk.create(
        document_id="doc-1", section=section, text=text, metadata=_metadata(title="Title B")
    )

    assert a.content_hash != b.content_hash
