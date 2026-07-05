"""Per-tab write-conflict baselines (0.14.0).

The lenient effective-baseline rule: a `write --tab X` is allowed when
max(whole-doc last_read_version, tab_read_versions[X]) equals the current doc
version. These tests cover the two defects per-tab state fixes — the
cross-tab false negative (read tab A, write tab B) and the same-tab false
positive (write tab X twice) — plus the sibling-edit conservatism and the
--force escape, at both the end-to-end (real state) and decision layers.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gdoc.cli import cmd_cat, cmd_write
from gdoc.notify import ChangeInfo
from gdoc.state import DocState, load_state
from gdoc.util import GdocError

DOC_MIME = "application/vnd.google-apps.document"


def _tab_dict(tab_id, title):
    return {
        "id": tab_id, "title": title, "index": 0,
        "nesting_level": 0, "body": {"content": []},
    }


def _doc_with_tabs(*pairs):
    """A get_document_with_tabs() response for the given (id, title) pairs."""
    return {
        "revisionId": "rev1",
        "tabs": [
            {
                "tabProperties": {"tabId": tid, "title": title, "index": i},
                "documentTab": {"body": {"content": []}},
            }
            for i, (tid, title) in enumerate(pairs)
        ],
    }


def _cat_args(doc, tab):
    return SimpleNamespace(
        command="cat", doc=doc, plain=False, comments=False, all=False,
        tab=tab, all_tabs=False, no_images=False, json=False, verbose=False,
        quiet=False, revision=None, range=None, max_bytes=0,
    )


def _write_args(doc, tab, file, **over):
    d = dict(
        command="write", doc=doc, file=file, force=False, json=False,
        verbose=False, quiet=True, tab=tab, force_collapse_tabs=False,
    )
    d.update(over)
    return SimpleNamespace(**d)


def _run_cat_tab(doc, tab_title, tab_id, version):
    """Run `gdoc cat --tab TITLE` non-quiet; persists state under the caller's
    STATE_DIR patch. Stamps tab_read_versions[tab_id] = version."""
    change_info = ChangeInfo(current_version=version, mime_type=DOC_MIME)
    with patch("gdoc.notify.pre_flight", return_value=change_info), \
         patch("gdoc.api.docs.get_document_tabs",
               return_value=[_tab_dict(tab_id, tab_title)]), \
         patch("gdoc.api.docs.get_tab_text", return_value="body\n"), \
         patch("gdoc.api.docs.get_docs_service"):
        assert cmd_cat(_cat_args(doc, tab_title)) == 0


class TestTabBaselineWorkflow:
    """End-to-end read -> write cycles against real (tmp) state, so the tab id
    cat stamps is exactly the key write checks."""

    def test_write_same_tab_twice_both_succeed(self, tmp_path):
        """False-positive fix: the writer's own output advances the tab
        baseline, so a second write to the same tab isn't blocked."""
        f = tmp_path / "body.md"
        f.write_text("# new\n")
        with patch("gdoc.state.STATE_DIR", tmp_path):
            _run_cat_tab("abc123", "X", "t.x", 100)
            st = load_state("abc123")
            assert st.last_read_version is None  # tab read, not whole-doc
            assert st.tab_read_versions == {"t.x": 100}

            # Model the doc version advancing on each (mocked) write.
            doc_version = [100]

            def fake_ver(doc_id):
                return {"version": doc_version[0]}

            def fake_insert(*a, **k):
                doc_version[0] += 1
                return {"tab_id": "t.x", "tab_title": "X", "insert_index": 1}

            with patch("gdoc.api.docs.get_document_with_tabs",
                       return_value=_doc_with_tabs(("t.x", "X"))), \
                 patch("gdoc.api.docs.insert_markdown_into_tab",
                       side_effect=fake_insert), \
                 patch("gdoc.api.drive.get_file_version", side_effect=fake_ver):
                assert cmd_write(_write_args("abc123", "X", str(f))) == 0
                assert cmd_write(_write_args("abc123", "X", str(f))) == 0

            st = load_state("abc123")
            assert st.tab_read_versions["t.x"] == doc_version[0]
            assert st.last_read_version is None  # never became a whole-doc read

    def test_read_tab_a_then_write_tab_b_blocked(self, tmp_path):
        """False-negative fix: reading tab A gives no baseline for tab B."""
        f = tmp_path / "body.md"
        f.write_text("# new\n")
        with patch("gdoc.state.STATE_DIR", tmp_path):
            _run_cat_tab("abc123", "A", "t.a", 100)
            with patch("gdoc.api.docs.get_document_with_tabs",
                       return_value=_doc_with_tabs(("t.a", "A"), ("t.b", "B"))), \
                 patch("gdoc.api.docs.insert_markdown_into_tab") as mock_insert, \
                 patch("gdoc.api.drive.get_file_version",
                       return_value={"version": 100}):
                with pytest.raises(
                    GdocError, match="no read baseline for tab 'B'",
                ) as exc:
                    cmd_write(_write_args("abc123", "B", str(f)))
                assert exc.value.exit_code == 3
                mock_insert.assert_not_called()

    def test_plain_cat_then_write_tab_succeeds(self, tmp_path):
        """Lenient rule: a whole-doc cat is a genuine read of every tab, so
        its global baseline covers a subsequent tab write."""
        f = tmp_path / "body.md"
        f.write_text("# new\n")
        with patch("gdoc.state.STATE_DIR", tmp_path):
            change_info = ChangeInfo(current_version=100, mime_type=DOC_MIME)
            with patch("gdoc.notify.pre_flight", return_value=change_info), \
                 patch("gdoc.api.drive.export_doc", return_value="whole\n"):
                assert cmd_cat(_cat_args("abc123", None)) == 0
            assert load_state("abc123").last_read_version == 100

            with patch("gdoc.api.docs.get_document_with_tabs",
                       return_value=_doc_with_tabs(("t.b", "B"))), \
                 patch("gdoc.api.docs.insert_markdown_into_tab",
                       return_value={"tab_id": "t.b", "tab_title": "B",
                                     "insert_index": 1}), \
                 patch("gdoc.api.drive.get_file_version",
                       return_value={"version": 100}):
                assert cmd_write(_write_args("abc123", "B", str(f))) == 0


class TestTabBaselineDecision:
    """Focused conflict decisions with an injected state, pinning the sibling
    -edit conservatism, its message, and the --force escape."""

    @patch("gdoc.state.update_state_after_command")
    @patch("gdoc.api.drive.get_file_version", return_value={"version": 11})
    @patch("gdoc.api.docs.insert_markdown_into_tab")
    @patch("gdoc.api.docs.get_document_with_tabs")
    @patch("gdoc.state.load_state")
    @patch("gdoc.notify.pre_flight")
    def test_sibling_edit_blocks_tab_write(
        self, mock_pf, mock_load, mock_doc, mock_insert, _ver, _upd, tmp_path,
    ):
        f = tmp_path / "body.md"
        f.write_text("# new\n")
        # Tab X read at v100; the doc has since moved to v101 (a sibling edit).
        mock_load.return_value = DocState(
            last_read_version=None, tab_read_versions={"t.x": 100},
        )
        mock_pf.return_value = ChangeInfo(
            current_version=101, last_read_version=None,
        )
        mock_doc.return_value = _doc_with_tabs(("t.x", "X"))
        with pytest.raises(
            GdocError, match=r"tab 'X' may have changed .*v100 -> v101",
        ) as exc:
            cmd_write(_write_args("abc123", "X", str(f), quiet=False))
        assert exc.value.exit_code == 3
        mock_insert.assert_not_called()

    @patch("gdoc.state.update_state_after_command")
    @patch("gdoc.api.drive.get_file_version", return_value={"version": 100})
    @patch("gdoc.api.docs.insert_markdown_into_tab",
           return_value={"tab_id": "t.x", "tab_title": "X", "insert_index": 1})
    @patch("gdoc.api.docs.get_document_with_tabs")
    @patch("gdoc.state.load_state")
    @patch("gdoc.notify.pre_flight")
    def test_nonquiet_write_succeeds_on_own_tab_baseline(
        self, mock_pf, mock_load, mock_doc, mock_insert, _ver, _upd, tmp_path,
    ):
        """Non-quiet flagship happy path: with no whole-doc baseline, tab X
        read at v100 and the doc still at v100, `write --tab X` succeeds — the
        per-tab entry alone satisfies the guard. The non-quiet branch sources
        the global part from pre_flight (None here) and the per-tab part from
        state, so this locks that split, which the quiet success tests don't."""
        f = tmp_path / "body.md"
        f.write_text("# new\n")
        mock_load.return_value = DocState(
            last_read_version=None, tab_read_versions={"t.x": 100},
        )
        mock_pf.return_value = ChangeInfo(
            current_version=100, last_read_version=None,
        )
        mock_doc.return_value = _doc_with_tabs(("t.x", "X"))
        rc = cmd_write(_write_args("abc123", "X", str(f), quiet=False))
        assert rc == 0
        mock_insert.assert_called_once()

    @patch("gdoc.state.update_state_after_command")
    @patch("gdoc.api.drive.get_file_version", return_value={"version": 101})
    @patch("gdoc.api.docs.insert_markdown_into_tab",
           return_value={"tab_id": "t.x", "tab_title": "X", "insert_index": 1})
    @patch("gdoc.api.docs.get_document_with_tabs")
    @patch("gdoc.state.load_state")
    @patch("gdoc.notify.pre_flight")
    def test_force_bypasses_sibling_edit(
        self, mock_pf, mock_load, mock_doc, mock_insert, _ver, _upd, tmp_path,
    ):
        f = tmp_path / "body.md"
        f.write_text("# new\n")
        mock_load.return_value = DocState(
            last_read_version=None, tab_read_versions={"t.x": 100},
        )
        mock_pf.return_value = ChangeInfo(
            current_version=101, last_read_version=None,
        )
        mock_doc.return_value = _doc_with_tabs(("t.x", "X"))
        rc = cmd_write(_write_args("abc123", "X", str(f), quiet=False, force=True))
        assert rc == 0
        mock_insert.assert_called_once()

    @patch("gdoc.state.update_state_after_command")
    @patch("gdoc.api.drive.get_file_version", return_value={"version": 101})
    @patch("gdoc.api.docs.insert_markdown_into_tab")
    @patch("gdoc.api.docs.get_document_with_tabs")
    @patch("gdoc.state.load_state")
    def test_quiet_sibling_edit_blocks_tab_write(
        self, mock_load, mock_doc, mock_insert, _ver, _upd, tmp_path,
    ):
        """The --quiet path reads the same per-tab baseline from state."""
        f = tmp_path / "body.md"
        f.write_text("# new\n")
        mock_load.return_value = DocState(
            last_read_version=None, tab_read_versions={"t.x": 100},
        )
        mock_doc.return_value = _doc_with_tabs(("t.x", "X"))
        with pytest.raises(
            GdocError, match=r"tab 'X' may have changed .*v100 -> v101",
        ) as exc:
            cmd_write(_write_args("abc123", "X", str(f), quiet=True))
        assert exc.value.exit_code == 3
        mock_insert.assert_not_called()


class TestTabBaselineUrlParity:
    """A ?tab= URL drives the same per-tab conflict check as --tab."""

    @patch("gdoc.state.update_state_after_command")
    @patch("gdoc.api.drive.get_file_version", return_value={"version": 100})
    @patch("gdoc.api.docs.insert_markdown_into_tab")
    @patch("gdoc.api.docs.get_document_with_tabs")
    @patch("gdoc.state.load_state")
    def test_url_tab_blocked_without_baseline(
        self, mock_load, mock_doc, mock_insert, _ver, _upd, tmp_path,
    ):
        f = tmp_path / "body.md"
        f.write_text("# new\n")
        mock_load.return_value = None  # no prior read of anything
        mock_doc.return_value = _doc_with_tabs(("t.second", "Second"))
        args = _write_args(
            "https://docs.google.com/document/d/abc123/edit?tab=t.second",
            None, str(f), quiet=True,
        )
        with pytest.raises(
            GdocError, match="no read baseline for tab 't.second'",
        ) as exc:
            cmd_write(args)
        assert exc.value.exit_code == 3
        mock_insert.assert_not_called()
        # With no baseline the write is rejected regardless of version, so
        # the quiet path must not spend a get_file_version call to learn it.
        _ver.assert_not_called()
