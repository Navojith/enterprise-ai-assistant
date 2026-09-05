"""Tests for structure-aware chunking: section splitting, attribution, and error handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.core.errors import ValidationFailedError
from backend.app.retrieval.chunking import parse_corpus, parse_document

_VALID_DOC = """\
---
title: "Sample Incident"
department: payments
document_type: incident
access_level: internal
created_date: 2026-03-14
---

# Sample Incident

## Summary

A short summary section.

## Root Cause

A short root cause section.
"""


def test_parse_document_splits_on_section_headings(tmp_path: Path) -> None:
    path = tmp_path / "sample-incident.md"
    path.write_text(_VALID_DOC, encoding="utf-8")

    metadata, chunks = parse_document(path)

    assert metadata.document_id == "sample-incident"
    assert metadata.department == "payments"
    assert [c.section for c in chunks] == ["Summary", "Root Cause"]
    assert chunks[0].text == "A short summary section."
    assert all(c.document_id == "sample-incident" for c in chunks)


def test_parse_document_rejects_missing_front_matter(tmp_path: Path) -> None:
    path = tmp_path / "broken.md"
    path.write_text("# No front matter\n\n## Section\n\ntext\n", encoding="utf-8")

    with pytest.raises(ValidationFailedError, match="front-matter"):
        parse_document(path)


def test_parse_document_rejects_missing_required_field(tmp_path: Path) -> None:
    path = tmp_path / "incomplete.md"
    path.write_text(
        '---\ntitle: "X"\ndepartment: payments\n---\n\n# X\n\n## Section\n\ntext\n',
        encoding="utf-8",
    )

    with pytest.raises(ValidationFailedError, match="missing fields"):
        parse_document(path)


def test_parse_document_rejects_invalid_enum_value(tmp_path: Path) -> None:
    path = tmp_path / "bad-enum.md"
    path.write_text(
        _VALID_DOC.replace("document_type: incident", "document_type: not_a_real_type"),
        encoding="utf-8",
    )

    with pytest.raises(ValidationFailedError, match="invalid value"):
        parse_document(path)


def test_parse_document_rejects_no_sections(tmp_path: Path) -> None:
    path = tmp_path / "no-sections.md"
    path.write_text(
        '---\ntitle: "X"\ndepartment: payments\ndocument_type: incident\n'
        "access_level: internal\ncreated_date: 2026-01-01\n---\n\n# X\n\nJust prose, no headings.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationFailedError, match="no ## sections"):
        parse_document(path)


def test_long_section_is_split_by_paragraph_without_losing_content(tmp_path: Path) -> None:
    long_paragraph = " ".join(f"word{i}" for i in range(150))
    doc = _VALID_DOC.replace(
        "A short root cause section.",
        f"{long_paragraph}\n\n{long_paragraph}\n\n{long_paragraph}",
    )
    path = tmp_path / "long-section.md"
    path.write_text(doc, encoding="utf-8")

    _metadata, chunks = parse_document(path)
    root_cause_chunks = [c for c in chunks if c.section.startswith("Root Cause")]

    assert len(root_cause_chunks) > 1
    assert all(c.section.startswith("Root Cause (part") for c in root_cause_chunks)
    # No word count exceeds the soft cap by more than a single paragraph's worth.
    assert all(len(c.text.split()) <= 150 * 2 for c in root_cause_chunks)


def test_parse_corpus_is_order_independent_of_filesystem_listing(tmp_path: Path) -> None:
    (tmp_path / "b.md").write_text(_VALID_DOC, encoding="utf-8")
    (tmp_path / "a.md").write_text(
        _VALID_DOC.replace("Sample Incident", "Another Incident"), encoding="utf-8"
    )

    chunks = parse_corpus(tmp_path)

    assert [c.document_id for c in chunks[:2]] == ["a", "a"]
