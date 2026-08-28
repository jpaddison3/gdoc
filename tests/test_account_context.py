"""Per-request credential injection: the account is carried by the calling
context, and service caches are keyed by it — never process-global."""

import threading

import pytest

from gdoc import api, util


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Fresh caches and no machine-configured default account."""
    api.clear_service_caches()
    monkeypatch.setattr("gdoc.util.get_default_account", lambda: None)
    yield
    api.clear_service_caches()


@pytest.fixture()
def _fake_services(mocker):
    """Mock credentials + build so services record the account they carry."""
    mocker.patch(
        "gdoc.auth.get_credentials", side_effect=lambda account: f"creds:{account}"
    )
    built = {}

    def fake_build(api_name, version, credentials):
        service = object()
        built[service] = credentials
        return service

    mocker.patch("gdoc.api.build", side_effect=fake_build)
    mocker.patch("gdoc.api.docs.build", side_effect=fake_build)
    return built


def test_concurrent_accounts_see_only_their_own_credentials(_fake_services):
    """Two accounts used concurrently in one process must not see each
    other's credentials or cached services."""
    barrier = threading.Barrier(2, timeout=5)
    results = {}

    def use(account):
        with util.account_context(account):
            barrier.wait()  # both threads hold their contexts at once
            results[account] = api.get_drive_service()

    threads = [
        threading.Thread(target=use, args=(name,)) for name in ("alice", "bob")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert _fake_services[results["alice"]] == "creds:alice"
    assert _fake_services[results["bob"]] == "creds:bob"
    assert results["alice"] is not results["bob"]


def test_services_are_cached_per_account(_fake_services):
    with util.account_context("alice"):
        first = api.get_drive_service()
    with util.account_context("bob"):
        other = api.get_drive_service()
    with util.account_context("alice"):
        again = api.get_drive_service()

    assert first is again
    assert first is not other


def test_default_account_change_is_picked_up_unpinned(mocker, _fake_services):
    """An unpinned call resolves the configured default at call time."""
    default = mocker.patch("gdoc.util.get_default_account", return_value="a")

    first = api.get_drive_service()
    assert api.get_drive_service() is first  # unchanged default: cache kept

    default.return_value = "b"
    switched = api.get_drive_service()
    assert switched is not first
    assert _fake_services[switched] == "creds:b"


def test_every_factory_keys_on_the_account(mocker, _fake_services):
    """Docs service and the revisions session isolate accounts too."""
    from gdoc.api import docs, revisions

    sessions = {}

    def fake_session(credentials):
        session = object()
        sessions[session] = credentials
        return session

    mocker.patch(
        "google.auth.transport.requests.AuthorizedSession",
        side_effect=fake_session,
    )

    with util.account_context("alice"):
        docs_a = docs.get_docs_service()
        session_a = revisions._get_session()
    with util.account_context("bob"):
        docs_b = docs.get_docs_service()
        session_b = revisions._get_session()

    assert _fake_services[docs_a] == "creds:alice"
    assert _fake_services[docs_b] == "creds:bob"
    assert sessions[session_a] == "creds:alice"
    assert sessions[session_b] == "creds:bob"


def test_token_change_on_disk_drops_the_cached_service(
    tmp_path, monkeypatch, _fake_services
):
    """`gdoc auth` re-writing or removing a token in another terminal must
    not leave a long-lived server on the old cached credentials."""
    monkeypatch.setattr("gdoc.util.CONFIG_DIR", tmp_path)
    token = tmp_path / "accounts" / "work" / "token.json"
    token.parent.mkdir(parents=True)
    token.write_text("identity-one")

    with util.account_context("work"):
        first = api.get_drive_service()
        assert api.get_drive_service() is first  # untouched token: cache kept

        token.write_text("identity-two, different length")
        second = api.get_drive_service()
        assert second is not first  # re-auth: stale service dropped

        token.unlink()
        assert api.get_drive_service() is not second  # removal: dropped too


def test_account_context_restores_previous_value():
    """The pre-call account survives even a set_active_account inside the
    block (run_argv sets it when argv carries --account)."""
    assert util.get_active_account() is None
    with util.account_context("work"):
        assert util.get_active_account() == "work"
        util.set_active_account("other")
    assert util.get_active_account() is None


def test_account_context_validates_the_name():
    from gdoc.util import GdocError

    with pytest.raises(GdocError), util.account_context("../evil"):
        pass  # pragma: no cover


def test_token_path_for_resolved_accounts():
    assert util.token_path_for(None) == util.TOKEN_PATH
    assert (
        util.token_path_for("work")
        == util.CONFIG_DIR / "accounts" / "work" / "token.json"
    )


def test_run_argv_pins_one_account_across_the_command(mocker):
    """A `gdoc auth --set-default` from another process landing mid-command
    must not hand the command's later service accesses (the write) a
    different account than its earlier ones (the read)."""
    from gdoc import cli

    default = ["acct-a"]
    mocker.patch(
        "gdoc.util.get_default_account", side_effect=lambda: default[0],
    )
    seen = []

    def fake_cmd(args):
        seen.append(util.resolve_account())
        default[0] = "acct-b"  # the flip lands mid-command
        seen.append(util.resolve_account())
        return 0

    mocker.patch("gdoc.cli.cmd_tabs", side_effect=fake_cmd)
    assert cli.run_argv(["tabs", "doc123"], check_updates=False) == 0
    assert seen == ["acct-a", "acct-a"]
    assert util.get_active_account() is None  # the pin is scoped, not leaked


def test_run_argv_leaves_the_mcp_server_unpinned(mocker):
    """The MCP server pins per tool call (call_command), so the server
    process itself must keep floating resolution — a default changed
    mid-serve is picked up on the next call, not frozen at startup."""
    from gdoc import cli

    default = ["acct-a"]
    mocker.patch(
        "gdoc.util.get_default_account", side_effect=lambda: default[0],
    )
    seen = []

    def fake_mcp(args):
        seen.append(util.resolve_account())
        default[0] = "acct-b"
        seen.append(util.resolve_account())
        return 0

    mocker.patch("gdoc.cli.cmd_mcp", side_effect=fake_mcp)
    assert cli.run_argv(["mcp"], check_updates=False) == 0
    assert seen == ["acct-a", "acct-b"]
