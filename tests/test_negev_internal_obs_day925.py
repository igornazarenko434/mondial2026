"""Day-9.25: pin the source-level obs wrapping on negev_toto_mcp.

Pre-fix: raw `requests.get/post/patch` in `integrations/negev_toto_mcp.py`
relied on the CALL SITE (build_card, kickoff_cards, sync_negev_standings) to
wrap them in obs.external_call. Any NEW tool calling `toto_*` silently
bypassed rate-limiting + ledger recording. This test pins the new contract:
the wrap lives at the source (the `_fs` helper), so every call is observed
regardless of who initiated it.
"""
from __future__ import annotations
from unittest.mock import MagicMock, patch
import pytest


def _seed_token():
    """Bypass auth so the test can hit `_fs` without a refresh round-trip."""
    import integrations.negev_toto_mcp as ntm
    ntm._token.update(id="fake-id-token", refresh="fake-rt",
                       uid="test-uid", exp=9999999999.0)


def test_toto_get_document_records_api_call_at_source(monkeypatch):
    """One call to `toto_get_document` → one ledger row tagged provider=
    negev_toto endpoint=firestore:get_document. Even when no caller wraps."""
    _seed_token()
    import integrations.negev_toto_mcp as ntm

    fake_response = MagicMock()
    fake_response.ok = True
    fake_response.json.return_value = {
        "name": "projects/p/databases/(default)/documents/u/abc",
        "fields": {}}
    monkeypatch.setattr(ntm.requests, "get",
                         lambda url, **kw: fake_response)
    monkeypatch.setattr(ntm.requests, "post",
                         lambda url, **kw: fake_response)
    monkeypatch.setattr(ntm.requests, "patch",
                         lambda url, **kw: fake_response)
    monkeypatch.setenv("NEGEV_PROJECT_ID", "negev-toto")
    monkeypatch.setenv("NEGEV_FIREBASE_API_KEY", "AIza_test")

    # Reset cost ledger to a known state
    from core.obs.cost import ledger
    led = ledger()
    before = len(led.conn.execute(
        "SELECT 1 FROM api_calls WHERE provider='negev_toto'").fetchall())

    ntm.toto_get_document("u/abc")

    after = len(led.conn.execute(
        "SELECT 1 FROM api_calls WHERE provider='negev_toto'").fetchall())
    assert after == before + 1, \
        f"_fs wrap didn't record API call (delta={after - before})"

    last = led.conn.execute(
        "SELECT provider, endpoint FROM api_calls "
        "WHERE provider='negev_toto' ORDER BY id DESC LIMIT 1").fetchone()
    assert last[0] == "negev_toto"
    assert last[1] == "firestore:get_document"


def test_toto_read_collection_records_via_fs_helper(monkeypatch):
    """Same contract for read_collection — wraps in obs.external_call,
    records with the right endpoint label."""
    _seed_token()
    import integrations.negev_toto_mcp as ntm

    fake_response = MagicMock()
    fake_response.ok = True
    fake_response.json.return_value = {"documents": []}
    monkeypatch.setattr(ntm.requests, "get",
                         lambda url, **kw: fake_response)
    monkeypatch.setattr(ntm.requests, "post",
                         lambda url, **kw: fake_response)
    monkeypatch.setattr(ntm.requests, "patch",
                         lambda url, **kw: fake_response)
    monkeypatch.setenv("NEGEV_PROJECT_ID", "negev-toto")
    monkeypatch.setenv("NEGEV_FIREBASE_API_KEY", "AIza_test")

    from core.obs.cost import ledger
    led = ledger()
    before = len(led.conn.execute(
        "SELECT 1 FROM api_calls WHERE endpoint='firestore:read_collection'"
        ).fetchall())

    ntm.toto_read_collection("standings")

    after = len(led.conn.execute(
        "SELECT 1 FROM api_calls WHERE endpoint='firestore:read_collection'"
        ).fetchall())
    assert after == before + 1


def test_fs_helper_dispatches_to_correct_verb(monkeypatch):
    """The _fs helper must dispatch through the verb-specific requests.<verb>
    function so existing tests (which monkeypatch `requests.get`, etc.) keep
    intercepting calls without modification."""
    _seed_token()
    import integrations.negev_toto_mcp as ntm

    get_calls, post_calls, patch_calls = [], [], []

    def _g(url, **kw):
        get_calls.append(url)
        r = MagicMock(); r.ok = True; r.json.return_value = {}
        return r

    def _p(url, **kw):
        post_calls.append(url)
        r = MagicMock(); r.ok = True; r.json.return_value = {}
        return r

    def _pa(url, **kw):
        patch_calls.append(url)
        r = MagicMock(); r.ok = True; r.json.return_value = {}
        return r

    monkeypatch.setattr(ntm.requests, "get", _g)
    monkeypatch.setattr(ntm.requests, "post", _p)
    monkeypatch.setattr(ntm.requests, "patch", _pa)
    ntm._fs("GET", "http://x/a", endpoint="firestore:get_document",
            headers={}, timeout=20)
    ntm._fs("POST", "http://x/b", endpoint="firestore:runQuery",
            headers={}, json={}, timeout=20)
    ntm._fs("PATCH", "http://x/c", endpoint="firestore:patch_document",
            headers={}, json={}, timeout=20)
    assert get_calls == ["http://x/a"]
    assert post_calls == ["http://x/b"]
    assert patch_calls == ["http://x/c"]


def test_existing_call_sites_still_work_with_internal_wrapping(monkeypatch):
    """Regression: build_card / kickoff_cards / sync_negev_standings each
    wrap their toto_* calls in their own obs.external_call. Both layers
    record — outer = 'logical op', inner = 'physical HTTP'. Since negev_toto
    has no metered budget (PROVIDER_LIMITS), double-recording units is
    harmless (no false over-budget). Pin that the OUTER wrap STILL produces
    its 'get_match_details'-style endpoint row."""
    _seed_token()
    import integrations.negev_toto_mcp as ntm
    from core import obs

    fake_response = MagicMock()
    fake_response.ok = True
    fake_response.json.return_value = {"fields": {}}
    monkeypatch.setattr(ntm.requests, "get",
                         lambda url, **kw: fake_response)

    from core.obs.cost import ledger
    led = ledger()

    # Simulated outer wrap (as in build_card.py:71)
    with obs.external_call("negev_toto", "get_match_details"):
        ntm.toto_get_document("u/abc")

    # We should see BOTH endpoint labels in the ledger for this stretch
    outer = led.conn.execute(
        "SELECT 1 FROM api_calls WHERE endpoint='get_match_details' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    inner = led.conn.execute(
        "SELECT 1 FROM api_calls WHERE endpoint='firestore:get_document' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert outer is not None, "outer wrap (get_match_details) didn't record"
    assert inner is not None, "inner wrap (firestore:get_document) didn't record"
