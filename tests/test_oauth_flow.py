import asyncio
import time
import urllib.parse

import httpx
import jwt
import pytest
import respx

import server


@pytest.fixture(autouse=True)
async def http_client():
    server._http_client = httpx.AsyncClient(timeout=5)
    yield
    await server._http_client.aclose()
    server._http_client = None


@pytest.fixture
async def asgi_client():
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _state_query(location: str) -> dict:
    return urllib.parse.parse_qs(urllib.parse.urlparse(location).query)


def _make_jwt(jti: str, email: str = "a@example.com", read_only: bool = False) -> str:
    now = int(time.time())
    return jwt.encode({
        "jti": jti, "email": email, "read_only": read_only,
        "iat": now, "exp": now + 3600,
    }, server.JWT_SECRET, algorithm="HS256")


# ── _authorize ───────────────────────────────────────────────────────────────

async def test_authorize_rejects_unknown_redirect_uri(asgi_client):
    r = await asgi_client.get("/authorize", params={
        "redirect_uri": "https://evil.example.com/cb",
        "code_challenge": "test-challenge",
    })
    assert r.status_code == 400
    assert r.text == "Unknown redirect_uri"


async def test_authorize_rejects_missing_code_challenge(asgi_client):
    r = await asgi_client.get("/authorize", params={
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
    })
    assert r.status_code == 400
    assert r.text == "PKCE code_challenge is required"


async def test_authorize_happy_path_redirects_to_microsoft_consumers_tenant(asgi_client):
    r = await asgi_client.get("/authorize", params={
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "code_challenge": "test-challenge",
        "state": "client-state-xyz",
    })
    assert 300 <= r.status_code < 400
    location = r.headers["location"]
    # This is the single most important assertion in the whole suite: only the
    # consumers tenant may ever be used, or work/school accounts could sign in.
    assert location.startswith("https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?")
    assert "/common/" not in location
    assert "/organizations/" not in location
    our_state = _state_query(location)["state"][0]
    stored = server._state_store.pop(our_state)
    assert stored["client_redirect_uri"] == "https://claude.ai/api/mcp/auth_callback"
    assert stored["client_state"] == "client-state-xyz"
    assert stored["code_challenge"] == "test-challenge"
    assert stored["read_only"] is False
    assert "ms_code_verifier" in stored


async def test_authorize_includes_pkce_challenge_for_microsoft_leg(asgi_client):
    r = await asgi_client.get("/authorize", params={
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "code_challenge": "test-challenge",
    })
    location = r.headers["location"]
    q = _state_query(location)
    assert q["code_challenge_method"][0] == "S256"
    our_state = q["state"][0]
    stored = server._state_store.pop(our_state)
    assert server._pkce_ok(stored["ms_code_verifier"], q["code_challenge"][0])


async def test_authorize_sets_read_only_when_resource_names_restricted_alias(asgi_client):
    original = server.READ_ONLY_ALIASES
    server.READ_ONLY_ALIASES = frozenset({"work"})
    try:
        r = await asgi_client.get("/authorize", params={
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": "test-challenge",
            "resource": "http://test/work/mcp",
        })
        assert 300 <= r.status_code < 400
        our_state = _state_query(r.headers["location"])["state"][0]
        stored = server._state_store.pop(our_state)
        assert stored["read_only"] is True
        scope = _state_query(r.headers["location"])["scope"][0]
        assert "Mail.ReadWrite" not in scope
    finally:
        server.READ_ONLY_ALIASES = original


# ── _auth_callback ──────────────────────────────────────────────────────────

@pytest.fixture
def state():
    s = "test-state"
    server._state_store[s] = {
        "client_state": "client-xyz",
        "client_redirect_uri": "http://localhost/cb",
        "code_challenge": "test-challenge",
        "ms_code_verifier": "test-ms-verifier",
        "read_only": False,
        "created": time.time(),
    }
    yield s
    server._state_store.pop(s, None)


async def test_auth_callback_returns_400_on_microsoft_error(asgi_client):
    r = await asgi_client.get("/auth/callback", params={"error": "access_denied"})
    assert r.status_code == 400
    assert r.text == "Microsoft OAuth error: access_denied"


async def test_auth_callback_rejects_invalid_or_expired_state(asgi_client):
    r = await asgi_client.get("/auth/callback", params={"state": "does-not-exist", "code": "abc"})
    assert r.status_code == 400
    assert r.text == "Invalid or expired state"


@respx.mock
async def test_auth_callback_returns_400_on_token_exchange_failure(asgi_client, state):
    respx.post(server.MS_TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    r = await asgi_client.get("/auth/callback", params={"state": state, "code": "ms-code"})
    assert r.status_code == 400
    assert r.text == "Token exchange failed: invalid_grant"


@respx.mock
async def test_auth_callback_returns_502_on_profile_fetch_failure(asgi_client, state):
    respx.post(server.MS_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "mstok", "expires_in": 3600})
    )
    respx.get(server.ME).mock(return_value=httpx.Response(500, text="error"))
    r = await asgi_client.get("/auth/callback", params={"state": state, "code": "ms-code"})
    assert r.status_code == 502


@respx.mock
async def test_auth_callback_returns_502_when_no_email_resolvable(asgi_client, state):
    respx.post(server.MS_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "mstok", "expires_in": 3600})
    )
    respx.get(server.ME).mock(
        return_value=httpx.Response(200, json={"mail": None, "userPrincipalName": None})
    )
    r = await asgi_client.get("/auth/callback", params={"state": state, "code": "ms-code"})
    assert r.status_code == 502


@respx.mock
async def test_auth_callback_retries_transient_token_exchange_failure(asgi_client, state):
    # Regression test: _auth_callback previously used a raw httpx call with no
    # retry, inconsistent with every other outbound Graph/MS call in this file —
    # a single transient 5xx during the one-time code exchange failed the whole
    # login instead of recovering.
    respx.post(server.MS_TOKEN_URL).mock(side_effect=[
        httpx.Response(503),
        httpx.Response(200, json={"access_token": "mstok", "refresh_token": "rtok",
                                  "expires_in": 3600}),
    ])
    respx.get(server.ME).mock(
        return_value=httpx.Response(200, json={"mail": "a@example.com",
                                               "userPrincipalName": "a@example.com"})
    )
    r = await asgi_client.get("/auth/callback", params={"state": state, "code": "ms-code"})
    assert 300 <= r.status_code < 400
    parsed = _state_query(r.headers["location"])
    code_data = server._code_store.pop(parsed["code"][0])
    server._token_store.pop(code_data["jti"], None)


@respx.mock
async def test_auth_callback_happy_path_creates_session_and_redirects(asgi_client, state):
    respx.post(server.MS_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={
            "access_token": "mstok", "refresh_token": "rtok", "expires_in": 3600,
        })
    )
    respx.get(server.ME).mock(
        return_value=httpx.Response(200, json={"mail": "a@example.com",
                                               "userPrincipalName": "a@example.com"})
    )
    r = await asgi_client.get("/auth/callback", params={"state": state, "code": "ms-code"})
    assert 300 <= r.status_code < 400
    location = r.headers["location"]
    assert location.startswith("http://localhost/cb?")
    parsed = _state_query(location)
    assert parsed["state"][0] == "client-xyz"
    code_data = server._code_store.pop(parsed["code"][0])
    assert code_data["email"] == "a@example.com"
    token_data = server._token_store.pop(code_data["jti"])
    assert token_data["refresh_token"] == "rtok"
    assert token_data["email"] == "a@example.com"


@respx.mock
async def test_auth_callback_falls_back_to_user_principal_name_when_mail_is_null(asgi_client, state):
    # Real personal-MSA quirk: /me can return mail: null with the actual address
    # only present in userPrincipalName.
    respx.post(server.MS_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={
            "access_token": "mstok", "refresh_token": "rtok", "expires_in": 3600,
        })
    )
    respx.get(server.ME).mock(
        return_value=httpx.Response(200, json={"mail": None,
                                               "userPrincipalName": "personal@outlook.com"})
    )
    r = await asgi_client.get("/auth/callback", params={"state": state, "code": "ms-code"})
    assert 300 <= r.status_code < 400
    parsed = _state_query(r.headers["location"])
    code_data = server._code_store.pop(parsed["code"][0])
    assert code_data["email"] == "personal@outlook.com"
    server._token_store.pop(code_data["jti"], None)


# ── _refresh ────────────────────────────────────────────────────────────────

async def test_refresh_raises_reauth_required_for_unknown_jti():
    jti = "does-not-exist-jti"
    try:
        with pytest.raises(server.ReauthRequired):
            await server._refresh(jti)
    finally:
        server._refresh_locks.pop(jti, None)


@respx.mock
async def test_refresh_pops_session_and_raises_on_failed_refresh():
    jti = "jti-fail"
    server._token_store[jti] = {
        "access_token": "old", "refresh_token": "rtok",
        "expiry": time.time() - 100, "email": "a@example.com", "read_only": False,
        "jwt_exp": time.time() + 1000,
    }
    respx.post(server.MS_TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with pytest.raises(server.ReauthRequired):
        await server._refresh(jti)
    assert jti not in server._token_store
    assert jti not in server._refresh_locks


@respx.mock
async def test_refresh_updates_access_token_on_success():
    jti = "jti-ok"
    server._token_store[jti] = {
        "access_token": "old", "refresh_token": "rtok",
        "expiry": time.time() - 100, "email": "a@example.com", "read_only": False,
        "jwt_exp": time.time() + 1000,
    }
    respx.post(server.MS_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "new-token", "expires_in": 3600})
    )
    try:
        result = await server._refresh(jti)
        assert result == "new-token"
        assert server._token_store[jti]["access_token"] == "new-token"
    finally:
        server._token_store.pop(jti, None)
        server._refresh_locks.pop(jti, None)


@respx.mock
async def test_refresh_rotates_refresh_token_when_microsoft_returns_a_new_one():
    # Microsoft frequently rotates the refresh token on every use, unlike
    # Google's more static ones — the new one must always be stored, or the
    # *next* refresh fails with a reused/invalid refresh_token.
    jti = "jti-rotate"
    server._token_store[jti] = {
        "access_token": "old", "refresh_token": "rtok-old",
        "expiry": time.time() - 100, "email": "a@example.com", "read_only": False,
        "jwt_exp": time.time() + 1000,
    }
    respx.post(server.MS_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={
            "access_token": "new-token", "refresh_token": "rtok-new", "expires_in": 3600,
        })
    )
    try:
        await server._refresh(jti)
        assert server._token_store[jti]["refresh_token"] == "rtok-new"
    finally:
        server._token_store.pop(jti, None)
        server._refresh_locks.pop(jti, None)


@respx.mock
async def test_refresh_keeps_old_refresh_token_when_microsoft_omits_a_new_one():
    jti = "jti-keep"
    server._token_store[jti] = {
        "access_token": "old", "refresh_token": "rtok-old",
        "expiry": time.time() - 100, "email": "a@example.com", "read_only": False,
        "jwt_exp": time.time() + 1000,
    }
    respx.post(server.MS_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "new-token", "expires_in": 3600})
    )
    try:
        await server._refresh(jti)
        assert server._token_store[jti]["refresh_token"] == "rtok-old"
    finally:
        server._token_store.pop(jti, None)
        server._refresh_locks.pop(jti, None)


@respx.mock
async def test_refresh_concurrent_calls_for_same_session_issue_one_http_request():
    # Regression coverage for the per-jti asyncio.Lock in _refresh: two requests
    # racing an expired token should only ever fire one refresh_token grant.
    jti = "jti-concurrent"
    server._token_store[jti] = {
        "access_token": "old", "refresh_token": "rtok",
        "expiry": time.time() - 100, "email": "a@example.com", "read_only": False,
        "jwt_exp": time.time() + 1000,
    }
    route = respx.post(server.MS_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "new-token", "expires_in": 3600})
    )
    try:
        results = await asyncio.gather(server._refresh(jti), server._refresh(jti))
        assert results == ["new-token", "new-token"]
        assert route.call_count == 1
    finally:
        server._token_store.pop(jti, None)
        server._refresh_locks.pop(jti, None)


# ── _purge_expired_tokens ───────────────────────────────────────────────────

async def test_purge_expired_tokens_skips_session_with_in_flight_refresh():
    # Regression test: _purge_expired_tokens previously popped an expired
    # session's _token_store/_refresh_locks entry unconditionally, even while
    # _refresh was still awaiting Microsoft's response for that same session —
    # the refreshed token would then be written into a dict no longer in the
    # store, silently losing the session despite _refresh reporting success.
    jti = "jti-purge-race"
    server._token_store[jti] = {
        "access_token": "old", "refresh_token": "rtok",
        "expiry": time.time() + 3600, "email": "a@example.com", "read_only": False,
        "jwt_exp": time.time() - 1,  # already past the purge threshold
    }
    lock = asyncio.Lock()
    server._refresh_locks[jti] = lock
    await lock.acquire()  # simulate _refresh currently holding the lock
    try:
        server._purge_expired_tokens()
        assert jti in server._token_store
        assert jti in server._refresh_locks
    finally:
        lock.release()
        server._token_store.pop(jti, None)
        server._refresh_locks.pop(jti, None)


async def test_purge_expired_tokens_removes_session_once_lock_is_free():
    jti = "jti-purge-free"
    server._token_store[jti] = {
        "access_token": "old", "refresh_token": "rtok",
        "expiry": time.time() + 3600, "email": "a@example.com", "read_only": False,
        "jwt_exp": time.time() - 1,
    }
    server._refresh_locks[jti] = asyncio.Lock()  # present but not held
    server._purge_expired_tokens()
    assert jti not in server._token_store
    assert jti not in server._refresh_locks


# ── Bearer-auth middleware (/mcp) ───────────────────────────────────────────

async def test_mcp_endpoint_rejects_missing_authorization_header(asgi_client):
    r = await asgi_client.get("/mcp")
    assert r.status_code == 401
    assert "www-authenticate" in r.headers


async def test_mcp_endpoint_rejects_non_bearer_scheme(asgi_client):
    r = await asgi_client.get("/mcp", headers={"Authorization": "Basic abc123"})
    assert r.status_code == 401


async def test_mcp_endpoint_rejects_malformed_non_utf8_header_cleanly():
    # Regression test: non-UTF-8 bytes in the Authorization header must not raise
    # an uncaught UnicodeDecodeError — every other malformed-input case here gets
    # a clean 401. Drives the raw ASGI scope directly — httpx's own header
    # encoding won't reproduce invalid UTF-8 bytes.
    scope = {"type": "http", "path": "/mcp", "headers": [(b"authorization", b"Bearer \xff\xfe")]}
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await server.app(scope, receive, send)

    assert sent[0]["status"] == 401


async def test_mcp_endpoint_rejects_invalid_jwt(asgi_client):
    r = await asgi_client.get("/mcp", headers={"Authorization": "Bearer not-a-real-jwt"})
    assert r.status_code == 401


async def test_mcp_endpoint_rejects_valid_jwt_with_unknown_session(asgi_client):
    token = _make_jwt("unknown-jti")
    r = await asgi_client.get("/mcp", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


async def _stub_mcp(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def test_mcp_endpoint_accepts_valid_jwt_with_known_session(asgi_client):
    # FastMCP's real mounted app needs its session manager started via the ASGI
    # lifespan protocol, which httpx.ASGITransport doesn't drive — swap in a
    # trivial stub so this only exercises the bearer-auth branch in _App.__call__,
    # not FastMCP's internals.
    jti = "known-jti"
    server._token_store[jti] = {
        "access_token": "mstok", "refresh_token": "rtok",
        "expiry": time.time() + 3600, "email": "a@example.com", "read_only": False,
        "jwt_exp": time.time() + 86400,
    }
    token = _make_jwt(jti)
    original_mcp = server.app._mcp
    server.app._mcp = _stub_mcp
    try:
        r = await asgi_client.get("/mcp", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert "www-authenticate" not in r.headers
    finally:
        server.app._mcp = original_mcp
        server._token_store.pop(jti, None)
        server._refresh_locks.pop(jti, None)


async def test_mcp_endpoint_alias_routing_authenticates_through_split_alias(asgi_client):
    # End-to-end wiring check for _split_alias + bearer-auth working together on
    # an aliased path. Fine-grained read-only enforcement itself is covered by
    # _effective_read_only's unit tests (test_helpers.py) and
    # test_write_tools_reject_read_only_sessions (test_server.py).
    original_aliases = server.READ_ONLY_ALIASES
    server.READ_ONLY_ALIASES = frozenset({"work"})
    jti = "alias-jti"
    server._token_store[jti] = {
        "access_token": "mstok", "refresh_token": "rtok",
        "expiry": time.time() + 3600, "email": "a@example.com", "read_only": False,
        "jwt_exp": time.time() + 86400,
    }
    token = _make_jwt(jti, read_only=False)
    original_mcp = server.app._mcp
    server.app._mcp = _stub_mcp
    try:
        r = await asgi_client.get("/work/mcp", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
    finally:
        server.app._mcp = original_mcp
        server.READ_ONLY_ALIASES = original_aliases
        server._token_store.pop(jti, None)
        server._refresh_locks.pop(jti, None)
