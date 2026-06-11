"""Tests for the revision-diff engine and REV selector grammar."""

import pytest

from gdoc.revdiff import (
    attach_comments,
    build_hunks,
    classify_block,
    clean_text,
    heading_level,
    load_blocks,
    parse_rev_range,
    resolve_at_timestamp,
    resolve_selector,
    strip_marker,
    word_diff_runs,
)
from gdoc.util import GdocError

# Sparse ids on purpose: selectors must count by list position,
# never by id arithmetic.
REVS = [
    {"id": "1", "modifiedTime": "2026-06-01T10:00:00.000Z"},
    {"id": "3", "modifiedTime": "2026-06-03T10:00:00.000Z"},
    {"id": "7", "modifiedTime": "2026-06-05T10:00:00.000Z"},
    {"id": "20", "modifiedTime": "2026-06-08T10:00:00.000Z"},
    {"id": "66", "modifiedTime": "2026-06-10T10:00:00.000Z"},
]


class TestSelectors:
    def test_bare_id(self):
        assert resolve_selector(REVS, "7")["id"] == "7"

    @pytest.mark.parametrize("sel", ["latest", "head", "HEAD", "Latest"])
    def test_latest_aliases(self, sel):
        assert resolve_selector(REVS, sel)["id"] == "66"

    def test_prev(self):
        assert resolve_selector(REVS, "prev")["id"] == "20"

    def test_head_n_counts_by_position_not_id(self):
        assert resolve_selector(REVS, "head~2")["id"] == "7"
        assert resolve_selector(REVS, "latest~4")["id"] == "1"

    def test_head_n_out_of_range(self):
        with pytest.raises(GdocError, match="out of range") as exc_info:
            resolve_selector(REVS, "head~5")
        assert exc_info.value.exit_code == 3

    def test_unknown_id_points_at_revisions(self):
        with pytest.raises(GdocError, match="gdoc revisions") as exc_info:
            resolve_selector(REVS, "999")
        assert exc_info.value.exit_code == 3

    def test_empty_revision_list(self):
        with pytest.raises(GdocError, match="no revisions") as exc_info:
            resolve_selector([], "latest")
        assert exc_info.value.exit_code == 3

    def test_at_timestamp_inclusive_boundary(self):
        assert resolve_selector(REVS, "@2026-06-05T10:00:00Z")["id"] == "7"

    def test_at_timestamp_between_revisions(self):
        assert resolve_selector(REVS, "@2026-06-06T00:00:00Z")["id"] == "7"

    def test_at_date_only(self):
        # Naive dates are local time; 2026-06-04 falls between revs 3
        # and 7 in every timezone.
        assert resolve_selector(REVS, "@2026-06-04")["id"] == "3"

    def test_at_before_earliest(self):
        with pytest.raises(GdocError, match="no revision at/before") as exc_info:
            resolve_selector(REVS, "@2026-05-01T00:00:00Z")
        assert exc_info.value.exit_code == 3

    def test_at_invalid_timestamp(self):
        with pytest.raises(GdocError, match="invalid timestamp") as exc_info:
            resolve_selector(REVS, "@yesterday")
        assert exc_info.value.exit_code == 3

    def test_since_uses_same_resolver(self):
        assert resolve_at_timestamp(REVS, "2026-06-09T00:00:00Z")["id"] == "20"


class TestRevRange:
    def test_range(self):
        assert parse_rev_range("1..3") == ("1", "3")

    def test_range_with_selectors(self):
        assert parse_rev_range("head~1..head") == ("head~1", "head")

    def test_single_defaults_to_latest(self):
        assert parse_rev_range("7") == ("7", "latest")

    @pytest.mark.parametrize("bad", ["..3", "1..", ".."])
    def test_half_open_range_rejected(self, bad):
        with pytest.raises(GdocError, match="invalid revision range") as exc_info:
            parse_rev_range(bad)
        assert exc_info.value.exit_code == 3


class TestCleanText:
    def test_de_escapes_punctuation(self):
        assert clean_text(r"\#5 and \~tilde\~ and \=eq") == "#5 and ~tilde~ and =eq"

    def test_de_escapes_double_escapes(self):
        assert clean_text(r"\\> nested") == "nested"

    def test_strips_leading_blockquote(self):
        assert clean_text("> quoted text") == "quoted text"

    def test_unescapes_html_entities(self):
        assert clean_text("a &amp; b") == "a & b"

    def test_replaces_image_refs_with_placeholder(self):
        assert clean_text("before ![][image3] after") == "before ⟦diagram⟧ after"

    def test_collapses_whitespace_and_nbsp(self):
        assert clean_text("a  b\t\tc") == "a b c"


class TestLoadBlocks:
    def test_drops_blanks_and_image_noise(self):
        text = (
            "# Title\n"
            "\n"
            "A paragraph.\n"
            "\n"
            "[image1]: <data:image/png;base64,AAAA>\n"
            "some line with data:image/png;base64,BBBB inline\n"
            "Last.\n"
        )
        assert load_blocks(text) == ["# Title", "A paragraph.", "Last."]


class TestBlockClassification:
    def test_heading(self):
        assert classify_block("## Head") == "heading"
        assert heading_level("### Deep") == 3
        assert strip_marker("## Head") == "Head"

    @pytest.mark.parametrize(
        "block", ["- item", "* item", r"\- item", "1. item", "2) item"],
    )
    def test_listitem(self, block):
        assert classify_block(block) == "listitem"
        assert strip_marker(block) == "item"

    def test_paragraph(self):
        assert classify_block("Just text.") == "paragraph"


OLD_SENTENCE = "We should ship the feature next week because the team is ready."
NEW_SENTENCE = (
    "We could possibly deliver the product next month since the group "
    "seems ready."
)


class TestWordDiffCoalescing:
    def test_rewritten_sentence_is_one_chunk_not_salad(self):
        runs = word_diff_runs(OLD_SENTENCE, NEW_SENTENCE, min_common=24)
        assert sum(1 for r in runs if r["op"] == "del") == 1
        assert sum(1 for r in runs if r["op"] == "ins") == 1

    def test_min_common_zero_keeps_shared_scraps(self):
        runs = word_diff_runs(OLD_SENTENCE, NEW_SENTENCE, min_common=0)
        assert sum(1 for r in runs if r["op"] == "del") > 1

    def test_flanking_equal_runs_survive(self):
        runs = word_diff_runs(OLD_SENTENCE, NEW_SENTENCE, min_common=24)
        assert runs[0] == {"op": "equal", "text": "We "}
        assert runs[-1]["op"] == "equal"
        assert "ready." in runs[-1]["text"]

    def test_identical_text_is_single_equal_run(self):
        runs = word_diff_runs("same text", "same text")
        assert runs == [{"op": "equal", "text": "same text"}]


class TestBuildHunks:
    def test_kinds(self):
        old = "# Title\n\nKeep me.\n\nDelete me entirely.\n"
        new = (
            "# Title\n\nKeep me.\n\n"
            "Brand new paragraph instead, fully different.\n\n"
            "Appended paragraph.\n"
        )
        hunks = build_hunks(old, new)
        assert [h["kind"] for h in hunks] == [
            "equal", "equal", "replace", "insert",
        ]

    def test_heading_level_recorded(self):
        hunks = build_hunks("## Old heading text here\n", "## New heading text here\n")
        assert hunks[0]["block_type"] == "heading"
        assert hunks[0]["level"] == 2

    def test_replace_extras_become_delete(self):
        old = "First old paragraph with words.\n\nSecond old paragraph with words.\n"
        new = "Completely rewritten single paragraph.\n"
        hunks = build_hunks(old, new)
        assert [h["kind"] for h in hunks] == ["replace", "delete"]

    def test_runs_use_cleaned_text(self):
        hunks = build_hunks("\\# not a heading\n", "")
        assert hunks[0]["kind"] == "delete"
        assert hunks[0]["block_type"] == "paragraph"
        assert hunks[0]["runs"][0]["text"] == "# not a heading"


def _comment(cid, quoted, content="note", author="Alice",
             created="2026-06-09T00:00:00Z"):
    return {
        "id": cid,
        "author": {"displayName": author},
        "createdTime": created,
        "resolved": False,
        "content": content,
        "quotedFileContent": {"value": quoted},
        "replies": [],
    }


class TestAttachComments:
    def test_prefers_changed_hunk_over_first_match(self):
        old = (
            "Mention of Conor in the stable intro paragraph here.\n\n"
            "Conor will handle the rollout plan.\n"
        )
        new = (
            "Mention of Conor in the stable intro paragraph here.\n\n"
            "Conor will handle the rollout plan and the budget.\n"
        )
        hunks = build_hunks(old, new)
        [comment] = attach_comments(hunks, [_comment("c1", "Conor")])
        assert comment["hunk"] == 1
        assert hunks[1]["kind"] == "replace"

    def test_first_match_when_nothing_changed_matches(self):
        text = "Alpha text.\n\nBeta text.\n"
        hunks = build_hunks(text, text)
        [comment] = attach_comments(hunks, [_comment("c1", "Beta")])
        assert comment["hunk"] == 1

    def test_unmatched_goes_to_appendix(self):
        hunks = build_hunks("Some text.\n", "Some text.\n")
        [comment] = attach_comments(hunks, [_comment("c1", "no such anchor")])
        assert comment["hunk"] is None

    def test_short_anchor_not_matched(self):
        hunks = build_hunks("It is ok here.\n", "It is ok here.\n")
        [comment] = attach_comments(hunks, [_comment("c1", "ok")])
        assert comment["hunk"] is None

    def test_stopword_anchor_not_matched(self):
        hunks = build_hunks("this is fine.\n", "this is fine.\n")
        [comment] = attach_comments(hunks, [_comment("c1", "this")])
        assert comment["hunk"] is None

    def test_action_only_replies_dropped(self):
        hunks = build_hunks("Anchor text here.\n", "Anchor text here.\n")
        raw = _comment("c1", "Anchor text")
        raw["replies"] = [
            {"author": {"displayName": "Bob"}, "content": "", "action": "resolve"},
            {"author": {"displayName": "Eve"}, "content": "real reply",
             "createdTime": "2026-06-09T01:00:00Z"},
        ]
        [comment] = attach_comments(hunks, [raw])
        assert [r["author"] for r in comment["replies"]] == ["Eve"]

    def test_sorted_by_created_time(self):
        hunks = build_hunks("Anchor text here.\n", "Anchor text here.\n")
        comments = attach_comments(hunks, [
            _comment("newer", "Anchor", created="2026-06-10T00:00:00Z"),
            _comment("older", "Anchor", created="2026-06-01T00:00:00Z"),
        ])
        assert [c["id"] for c in comments] == ["older", "newer"]
