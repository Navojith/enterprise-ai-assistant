"""Structure-aware chunking: split one seed document into attributed, retrievable `Chunk`s.

"Structure-aware" means splitting on the document's own `##` section boundaries rather than a
fixed token window — a section is a coherent unit of meaning the author already delineated, so
splitting there (with a length-based fallback for sections that are themselves too long) keeps
each chunk topically coherent and gives every chunk a real section name to cite, which is what
makes citation attribution (`docs/ARCHITECTURE.md` "RAG design") and later citation
*verification* (Cycle 6) possible at all.

There is no YAML dependency here: the front matter this project's own seed generator writes is
five flat `key: value` pairs, so a general YAML parser would be a dependency pulled in to parse
a format simple enough to hand-roll correctly in a dozen lines — and hand-rolling it means a
malformed front matter block fails with an error naming this project's own expectations, not a
YAML library's generic syntax error.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from backend.app.core.errors import ValidationFailedError
from backend.app.retrieval.models import AccessLevel, Chunk, DocumentMetadata, DocumentType

_FRONT_MATTER_PATTERN = re.compile(r"\A---\n(.*?\n)---\n", re.DOTALL)
_SECTION_HEADING_PATTERN = re.compile(r"^##\s+(.+)$", re.MULTILINE)

# A section longer than this is split further so no single chunk is disproportionately large
# relative to the rest of the corpus — the seed documents rarely hit this, but real documents
# routinely have one long section among several short ones.
_MAX_CHUNK_WORDS = 220


def _parse_front_matter(raw: str, *, source: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            raise ValidationFailedError(
                f"Malformed front-matter line in {source}: {line!r} (expected 'key: value')"
            )
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip('"')
    return fields


def _parse_metadata(text: str, *, document_id: str, source: Path) -> tuple[DocumentMetadata, str]:
    match = _FRONT_MATTER_PATTERN.match(text)
    if match is None:
        raise ValidationFailedError(f"{source} is missing a --- front-matter block")
    fields = _parse_front_matter(match.group(1), source=source)
    body = text[match.end() :]

    required = {"title", "department", "document_type", "access_level", "created_date"}
    missing = required - fields.keys()
    if missing:
        raise ValidationFailedError(f"{source} front matter is missing fields: {sorted(missing)}")

    try:
        metadata = DocumentMetadata(
            document_id=document_id,
            title=fields["title"],
            department=fields["department"],
            document_type=DocumentType(fields["document_type"]),
            access_level=AccessLevel(fields["access_level"]),
            created_date=date.fromisoformat(fields["created_date"]),
        )
    except ValueError as exc:
        raise ValidationFailedError(f"{source} front matter has an invalid value: {exc}") from exc

    return metadata, body


def _split_long_section(heading: str, text: str) -> list[tuple[str, str]]:
    """Split one section's body into word-bounded parts if it exceeds `_MAX_CHUNK_WORDS`,
    breaking on paragraph boundaries so a split never lands mid-sentence when avoidable."""
    words = text.split()
    if len(words) <= _MAX_CHUNK_WORDS:
        return [(heading, text)]

    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    parts: list[tuple[str, str]] = []
    current: list[str] = []
    current_word_count = 0
    part_number = 1
    for paragraph in paragraphs:
        paragraph_words = len(paragraph.split())
        if current and current_word_count + paragraph_words > _MAX_CHUNK_WORDS:
            parts.append((f"{heading} (part {part_number})", "\n\n".join(current)))
            part_number += 1
            current, current_word_count = [], 0
        current.append(paragraph)
        current_word_count += paragraph_words
    if current:
        parts.append((f"{heading} (part {part_number})", "\n\n".join(current)))
    return parts


def parse_document(path: Path) -> tuple[DocumentMetadata, list[Chunk]]:
    """Parse one seed Markdown file into its metadata and a list of chunks, one per `##`
    section (further split by `_split_long_section` when a section is unusually long)."""
    text = path.read_text(encoding="utf-8")
    document_id = path.stem
    metadata, body = _parse_metadata(text, document_id=document_id, source=path)

    headings = list(_SECTION_HEADING_PATTERN.finditer(body))
    if not headings:
        raise ValidationFailedError(f"{path} has no ## sections to chunk")

    chunks: list[Chunk] = []
    for position, heading_match in enumerate(headings):
        heading = heading_match.group(1).strip()
        section_start = heading_match.end()
        section_end = headings[position + 1].start() if position + 1 < len(headings) else len(body)
        section_text = body[section_start:section_end].strip()
        if not section_text:
            continue
        for sub_heading, sub_text in _split_long_section(heading, section_text):
            chunks.append(
                Chunk.create(
                    document_id=document_id,
                    section=sub_heading,
                    text=sub_text,
                    metadata=metadata,
                )
            )
    return metadata, chunks


def parse_corpus(root: Path) -> list[Chunk]:
    """Parse every `.md` file under `root` (recursively) into chunks, in a stable (sorted)
    order — determinism here is what makes a re-ingestion run's "zero unchanged chunks
    re-embedded" outcome reproducible rather than order-dependent."""
    all_chunks: list[Chunk] = []
    for path in sorted(root.rglob("*.md")):
        _metadata, chunks = parse_document(path)
        all_chunks.extend(chunks)
    return all_chunks
