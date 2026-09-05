"""Shared retrieval domain types: document/chunk metadata and the access-level policy.

These are used across ingestion, the Pinecone store, hybrid fusion, and (in later cycles)
citation verification — centralizing them here means a chunk's shape and its access rule are
defined exactly once, instead of drifting between the code that writes chunks and the code
that filters them.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


# The six document types ASSESSMENT.md's "Business Scenario" names explicitly.
class DocumentType(StrEnum):
    INCIDENT = "incident"
    RUNBOOK = "runbook"
    ARCHITECTURE = "architecture"
    PRODUCT_SPEC = "product_spec"
    POLICY = "policy"
    MEETING_NOTES = "meeting_notes"


# Fictional commercial-bank departments (docs/DECISIONS.md's "~6 departments"). Each is also a
# Pinecone namespace, so this list is the single source of truth for both the seed corpus and
# the namespaces ingestion creates.
DEPARTMENTS: tuple[str, ...] = (
    "payments",
    "core_banking",
    "security",
    "human_resources",
    "product",
    "customer_support",
)


class AccessLevel(StrEnum):
    """A simple ordinal clearance — see docs/ASSUMPTIONS_AND_TRADEOFFS.md assumption 4.

    Declaration order is the clearance order: a role's ceiling is the highest level it may
    read, and every level at or below that ceiling is included in what its queries can return.
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"


_ACCESS_LEVEL_ORDER: tuple[AccessLevel, ...] = (
    AccessLevel.PUBLIC,
    AccessLevel.INTERNAL,
    AccessLevel.CONFIDENTIAL,
)

# Role -> the highest access_level that role's retrieval queries may return. Cycle 2 defines
# the formal Role/Principal types; this only needs role *names* to stay in sync with those.
# Kept here (not in core/security) because it is retrieval policy, not authentication.
_ROLE_CEILING: dict[str, AccessLevel] = {
    "viewer": AccessLevel.INTERNAL,
    "analyst": AccessLevel.CONFIDENTIAL,
    "administrator": AccessLevel.CONFIDENTIAL,
}


def allowed_access_levels(role: str) -> list[str]:
    """The `access_level` values a query on behalf of `role` may retrieve, most-open first.

    Returns plain strings (not `AccessLevel` members) because this feeds directly into a
    Pinecone metadata filter's `$in` clause. Raises `ValueError` for an unrecognized role
    rather than silently defaulting — a typo'd role name should fail loudly, not fall back to
    the most permissive or most restrictive clearance by accident.
    """
    ceiling = _ROLE_CEILING.get(role.lower())
    if ceiling is None:
        raise ValueError(f"Unknown role {role!r}; expected one of {sorted(_ROLE_CEILING)}")
    ceiling_index = _ACCESS_LEVEL_ORDER.index(ceiling)
    return [level.value for level in _ACCESS_LEVEL_ORDER[: ceiling_index + 1]]


class DocumentMetadata(BaseModel):
    """Metadata carried by every chunk of one source document — the fields ASSESSMENT.md's
    metadata example names, plus `title` for citation display."""

    document_id: str
    title: str
    department: str
    document_type: DocumentType
    access_level: AccessLevel
    created_date: date


def compute_content_hash(*, document_id: str, section: str, text: str) -> str:
    """A stable fingerprint of one chunk's content, used to skip re-embedding unchanged
    chunks on re-ingestion (docs/DECISIONS.md §4 cost guard). Keyed on `document_id` and
    `section` too, not just `text`, so two different sections that happen to contain identical
    text do not collide."""
    digest_input = f"{document_id}\x1f{section}\x1f{text}".encode()
    return hashlib.sha256(digest_input).hexdigest()


class Chunk(BaseModel):
    """One retrievable, independently-attributed unit of a document.

    `chunk_id` is a *stable identity* derived from (document_id, section) — not from the
    content hash — so editing a section's text updates the same Pinecone record in place
    instead of creating an orphaned duplicate under a new id every time the source changes.
    `content_hash` is what ingestion compares against its manifest to decide whether that
    record needs re-upserting at all.
    """

    chunk_id: str
    document_id: str
    section: str
    text: str
    content_hash: str
    metadata: DocumentMetadata

    @classmethod
    def create(
        cls, *, document_id: str, section: str, text: str, metadata: DocumentMetadata
    ) -> Chunk:
        chunk_id = f"{document_id}::{section}"
        return cls(
            chunk_id=chunk_id,
            document_id=document_id,
            section=section,
            text=text,
            content_hash=compute_content_hash(document_id=document_id, section=section, text=text),
            metadata=metadata,
        )

    def to_pinecone_record(self) -> dict[str, str | int]:
        """Flatten to the shape `upsert_records` expects for an integrated-inference index:
        `_id`, the text field named by the index's `field_map`, and metadata as plain top-level
        fields. `created_date` is stored both as an ISO string (for display/citation) and as a
        Unix-epoch integer (`created_date_epoch`) so a future numeric range filter — Pinecone's
        filter DSL does not support ordering on strings — has something to compare against.
        """
        created_at = datetime.combine(self.metadata.created_date, datetime.min.time(), tzinfo=UTC)
        return {
            "_id": self.chunk_id,
            "chunk_text": self.text,
            "document_id": self.document_id,
            "section": self.section,
            "content_hash": self.content_hash,
            "title": self.metadata.title,
            "department": self.metadata.department,
            "document_type": self.metadata.document_type.value,
            "access_level": self.metadata.access_level.value,
            "created_date": self.metadata.created_date.isoformat(),
            "created_date_epoch": int(created_at.timestamp()),
        }


class RetrievedChunk(BaseModel):
    """A chunk as it comes back from search: the record fields plus a relevance score."""

    chunk_id: str
    document_id: str
    section: str
    text: str
    title: str
    department: str
    document_type: str
    access_level: str
    created_date: str
    score: float = Field(
        description="Source-specific relevance score — not comparable across dense and sparse without fusion"
    )
