import base64
import hashlib

import httpx
import pytest

import server


def test_pkce_ok_matches_valid_verifier():
    verifier = "test-verifier-1234567890abcdefghijklmno"
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert server._pkce_ok(verifier, challenge) is True


def test_pkce_ok_rejects_wrong_verifier():
    assert server._pkce_ok("wrong-verifier", "some-unrelated-challenge") is False


def test_new_pkce_pair_round_trips():
    verifier, challenge = server._new_pkce_pair()
    assert server._pkce_ok(verifier, challenge) is True


def test_alias_from_resource_extracts_alias():
    assert server._alias_from_resource("https://host/work/mcp") == "work"


def test_alias_from_resource_returns_empty_for_unaliased():
    assert server._alias_from_resource("https://host/mcp") == ""


def test_alias_from_resource_returns_empty_for_none():
    assert server._alias_from_resource(None) == ""


def test_alias_from_resource_returns_empty_for_unparseable():
    assert server._alias_from_resource("https://host/a/b/c") == ""


def test_split_alias_strips_known_alias():
    assert server._split_alias("/work/mcp") == ("work", "/mcp")


def test_split_alias_leaves_unaliased_mcp_path():
    assert server._split_alias("/mcp") == ("", "/mcp")


def test_split_alias_strips_alias_from_oauth_path():
    assert server._split_alias("/personal/.well-known/oauth-protected-resource") == (
        "personal", "/.well-known/oauth-protected-resource",
    )


def test_split_alias_leaves_unrecognised_path_untouched():
    assert server._split_alias("/something/else") == ("", "/something/else")


def test_ms_scopes_read_only_excludes_write_scopes():
    scopes = server._ms_scopes(read_only=True).split()
    assert "Mail.ReadWrite" not in scopes
    assert "Mail.Send" not in scopes
    assert "Calendars.ReadWrite" not in scopes
    assert "Mail.Read" in scopes


def test_ms_scopes_read_write_includes_write_scopes():
    scopes = server._ms_scopes(read_only=False).split()
    assert "Mail.ReadWrite" in scopes
    assert "Mail.Send" in scopes
    assert "Calendars.ReadWrite" in scopes


def test_ms_scopes_always_requests_offline_access():
    # Required to get a refresh token at all on the Microsoft identity platform —
    # unlike Google, which grants one by default on first consent.
    assert "offline_access" in server._ms_scopes(read_only=True).split()
    assert "offline_access" in server._ms_scopes(read_only=False).split()


def test_resolve_email_prefers_mail_field():
    assert server._resolve_email({"mail": "a@example.com", "userPrincipalName": "b@x.com"}) == \
        "a@example.com"


def test_resolve_email_falls_back_to_user_principal_name():
    # Personal Microsoft accounts can return mail: null.
    assert server._resolve_email({"mail": None, "userPrincipalName": "b@example.com"}) == \
        "b@example.com"


def test_resolve_email_returns_empty_when_neither_present():
    assert server._resolve_email({}) == ""


def test_parse_recipients_splits_and_strips():
    assert server._parse_recipients("a@example.com, b@example.com") == [
        {"emailAddress": {"address": "a@example.com"}},
        {"emailAddress": {"address": "b@example.com"}},
    ]


def test_parse_recipients_ignores_empty_segments():
    assert server._parse_recipients("a@example.com, , ") == [
        {"emailAddress": {"address": "a@example.com"}},
    ]


def test_parse_recipients_empty_string_returns_empty_list():
    assert server._parse_recipients("") == []


def test_calendar_base_primary_uses_me():
    assert server._calendar_base("primary") == server.ME


def test_calendar_base_other_uses_calendar_id():
    assert server._calendar_base("abc123") == f"{server.ME}/calendars/abc123"


def test_client_raises_clear_error_before_lifespan_starts():
    original = server._http_client
    server._http_client = None
    try:
        with pytest.raises(RuntimeError, match="lifespan"):
            server._client()
    finally:
        server._http_client = original


def test_effective_read_only_true_when_jwt_says_so():
    assert server._effective_read_only({"read_only": True}, "") is True


def test_effective_read_only_true_when_alias_restricted_even_if_jwt_says_false():
    # Regression test: a READ_ONLY_ALIASES-restricted connector must stay
    # restricted even if the JWT was minted with read_only=False (e.g. the
    # OAuth client never echoed the 'resource' param during /authorize).
    original = server.READ_ONLY_ALIASES
    server.READ_ONLY_ALIASES = frozenset({"work"})
    try:
        assert server._effective_read_only({"read_only": False}, "work") is True
    finally:
        server.READ_ONLY_ALIASES = original


def test_effective_read_only_false_for_unrestricted_alias():
    original = server.READ_ONLY_ALIASES
    server.READ_ONLY_ALIASES = frozenset({"work"})
    try:
        assert server._effective_read_only({"read_only": False}, "personal") is False
    finally:
        server.READ_ONLY_ALIASES = original


@pytest.mark.asyncio
async def test_request_with_retry_honors_retry_after_header(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(server.asyncio, "sleep", fake_sleep)
    server._http_client = httpx.AsyncClient(timeout=1.0)
    try:
        calls = {"n": 0}

        async def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "7"}, json={})
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)
        server._http_client = httpx.AsyncClient(transport=transport, timeout=1.0)
        r = await server._request_with_retry("GET", "https://graph.microsoft.com/v1.0/me")
        assert r.status_code == 200
        assert sleeps and sleeps[0] == 7.0
    finally:
        await server._http_client.aclose()
        server._http_client = None
