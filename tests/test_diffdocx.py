"""Smoke tests for the .docx revision-diff renderer (optional dep)."""

import pytest

docx = pytest.importorskip("docx")

from gdoc.diffdocx import render_docx  # noqa: E402
from gdoc.revdiff import attach_comments, build_diff_model  # noqa: E402

OLD_REV = {"id": "69", "modifiedTime": "2026-06-01T10:00:00.000Z"}
NEW_REV = {"id": "190", "modifiedTime": "2026-06-10T19:23:00.000Z"}


def _model():
    old = (
        "# Plan\n\n"
        "We should ship the feature next week because the team is ready.\n\n"
        "Stable paragraph one.\n\nStable paragraph two.\n\n"
        "- a bullet that stays\n"
    )
    new = (
        "# Plan\n\n"
        "We could deliver the product next month since the group seems ready.\n\n"
        "Stable paragraph one.\n\nStable paragraph two.\n\n"
        "- a bullet that stays\n\n"
        "A freshly appended paragraph.\n"
    )
    model = build_diff_model("doc1", "Test Doc", OLD_REV, NEW_REV, old, new)
    comments = [{
        "id": "c1", "author": {"displayName": "Alice"},
        "createdTime": "2026-06-09T00:00:00Z", "resolved": False,
        "content": "Timing concern",
        "quotedFileContent": {"value": "deliver the product"},
        "replies": [],
    }]
    model["comments"] = attach_comments(model["hunks"], comments)
    return model


class TestRenderDocx:
    def test_writes_parseable_file_with_diff_content(self, tmp_path):
        out = tmp_path / "diff.docx"
        render_docx(_model(), str(out))

        assert out.exists()

        document = docx.Document(str(out))
        text = "\n".join(p.text for p in document.paragraphs)
        assert "Test Doc — revision diff" in text
        assert "deliver the product" in text
        assert "A freshly appended paragraph." in text

        # Comment threads render as single-cell tables
        cell_text = "\n".join(
            t.cell(0, 0).text for t in document.tables
        )
        assert "Alice" in cell_text
        assert "Timing concern" in cell_text
