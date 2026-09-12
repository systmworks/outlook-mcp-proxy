"""
Outlook MCP Server — FastMCP + Microsoft identity platform OAuth proxy

Personal (consumers-tenant) Microsoft accounts only — never /common or
/organizations, so work/school accounts structurally cannot authenticate here.

Flow:
  Claude.ai ──[OAuth]──► This server ──[OAuth]──► Microsoft
  Claude.ai ──[MCP]────► This server ──[Graph API]──► Outlook mail/calendar
"""
import asyncio
import base64
import hashlib
import hmac
import logging
import math
import os
import re
import secrets
import time
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlencode

import httpx
import jwt
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("outlook_mcp")

# ── Config ─────────────────────────────────────────────────────────────────────

MS_CLIENT_ID = os.environ["MS_CLIENT_ID"]
MS_CLIENT_SECRET = os.environ["MS_CLIENT_SECRET"]
BASE_URL = os.environ["BASE_URL"].rstrip("/")  # e.g. https://outlook-mcp-proxy.your-tailnet.ts.net
JWT_SECRET = os.environ["JWT_SECRET"]

HTTPX_TIMEOUT = 30.0
STATE_TTL = 600  # seconds; abandoned OAuth flows are purged after this

# Max attachment size (decoded bytes) get_attachment will fetch/return. Attachment
# bytes come back as base64 text inside the MCP tool result — i.e. straight into the
# calling LLM's context, not just over the network — so the default is kept small
# (base64 inflates ~33% and tokenizes poorly). Raise it if you need larger
# attachments and have the context budget.
ATTACHMENT_MAX_MB = max(1, min(25, int(os.environ.get("ATTACHMENT_MAX_MB", "3"))))
ATTACHMENT_MAX_BYTES = ATTACHMENT_MAX_MB * 1024 * 1024

DEFAULT_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"

# Redirect URIs /authorize is allowed to send the auth code to. Without this allowlist,
# an attacker can craft an /authorize?redirect_uri=<attacker-controlled> link and, once
# the victim completes Microsoft's consent screen, receive the resulting single-use code
# themselves — full account takeover if PKCE isn't also enforced (see _authorize below).
ALLOWED_REDIRECT_URIS = frozenset(
    u.strip() for u in os.environ.get(
        "ALLOWED_REDIRECT_URIS", DEFAULT_REDIRECT_URI
    ).split(",") if u.strip()
)

def _parse_read_only_aliases(value: str) -> frozenset[str]:
    """Comma-separated alias list -> a set of normalised (whitespace- and
    slash-stripped) aliases, dropping any token that's empty after stripping
    (e.g. a stray '/' entry) instead of letting it collapse to '' and silently
    matching the unaliased connector (whose alias is also '')."""
    return frozenset(
        stripped for a in value.split(",")
        if (stripped := a.strip().strip("/"))
    )


# Aliased connectors (e.g. /work/mcp) named here get Microsoft Graph scopes covering
# only read access — decided from the URL path alias at /authorize (see
# _ms_scopes() and _split_alias()), and re-enforced per-request regardless
# (see _effective_read_only()).
READ_ONLY_ALIASES = _parse_read_only_aliases(os.environ.get("READ_ONLY_ALIASES", ""))

# consumers-tenant-only — the single most important line in this file. Using /common
# or /organizations here would let work/school (Azure AD) accounts authenticate too;
# /consumers structurally restricts this server to personal Microsoft accounts.
MS_AUTHORIZE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
MS_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"

MS_SCOPES_BASE = [
    "openid",
    "offline_access",  # required to get a refresh token at all — easy to forget
    "User.Read",
    "Mail.Read",
    "Calendars.Read",
]
MS_SCOPES_WRITE = [
    "Mail.ReadWrite",
    "Mail.Send",
    "Calendars.ReadWrite",
]


def _ms_scopes(read_only: bool) -> str:
    scopes = MS_SCOPES_BASE if read_only else MS_SCOPES_BASE + MS_SCOPES_WRITE
    return " ".join(scopes)


GRAPH = "https://graph.microsoft.com/v1.0"
ME = f"{GRAPH}/me"

# Outbound Graph API calls retry on these — rate limiting and server errors are
# usually transient. Other 4xx (403/404, etc.) are permanent.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# Total attempts (including the first) for an outbound Graph API call before giving
# up. A bulk operation otherwise fails outright the moment Graph rate-limits a
# single call, with no chance to recover.
API_RETRY_ATTEMPTS = max(1, min(5, int(os.environ.get("API_RETRY_ATTEMPTS", "2"))))
_API_RETRY_DELAY = 0.3  # seconds between attempts, unless Retry-After says otherwise
_MAX_RETRY_DELAY = 60.0  # cap a Retry-After value — a bogus "inf"/huge value must not hang a call

# Shared connection-pooled client for all outbound Graph/Microsoft OAuth requests.
# Created/closed around the ASGI lifespan in _App.__call__ — avoids paying a fresh
# TCP+TLS handshake to graph.microsoft.com on every single tool call.
_http_client: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    if _http_client is None:
        raise RuntimeError("HTTP client not initialized — server lifespan hasn't started")
    return _http_client


# ── In-memory stores ───────────────────────────────────────────────────────────
# Fine for single-process personal use; restart clears sessions (re-auth needed).

_state_store: dict[str, dict] = {}   # our_state  → {..., "created": ts}
_code_store: dict[str, dict] = {}    # our_code   → {jti, email, code_challenge, ..., "created": ts}
_token_store: dict[str, dict] = {}   # jti        → {access_token, refresh_token, expiry, email, jwt_exp}
_refresh_locks: dict[str, asyncio.Lock] = {}  # jti → lock guarding concurrent token refreshes

# ── Per-request context ────────────────────────────────────────────────────────

_session_jti: ContextVar[str] = ContextVar("session_jti", default="")
_read_only: ContextVar[bool] = ContextVar("read_only", default=False)

# ── Helpers ────────────────────────────────────────────────────────────────────

class ReauthRequired(Exception):
    """Raised when a session is unknown or Microsoft has revoked/expired the refresh token."""


def _pkce_ok(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return hmac.compare_digest(computed, challenge)


def _new_pkce_pair() -> tuple[str, str]:
    """A fresh (verifier, challenge) pair for this server's own client role toward
    Microsoft — kept independent of whatever PKCE pair Claude used against us."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _resolve_email(profile: dict) -> str:
    """Personal Microsoft accounts can return `mail: null` from /me — the real
    address then only appears in `userPrincipalName`."""
    return profile.get("mail") or profile.get("userPrincipalName") or ""


def _purge_ttl(store: dict[str, dict]) -> None:
    now = time.time()
    expired = [k for k, v in store.items() if now - v.get("created", now) > STATE_TTL]
    for k in expired:
        store.pop(k, None)


def _purge_expired_states() -> None:
    _purge_ttl(_state_store)
    _purge_ttl(_code_store)


def _purge_expired_tokens() -> None:
    now = time.time()
    expired = [jti for jti, d in _token_store.items() if now >= d.get("jwt_exp", float("inf"))]
    for jti in expired:
        lock = _refresh_locks.get(jti)
        if lock is not None and lock.locked():
            # A refresh is in-flight for this session right now (e.g. this purge
            # was triggered by a completely unrelated concurrent request). Popping
            # the entry out from under it would let _refresh write a "successful"
            # refresh into a dict that's no longer in the store, silently losing
            # the session. Leave it — the next purge pass will catch it once the
            # refresh finishes and releases the lock, assuming jwt_exp is still past.
            continue
        _token_store.pop(jti, None)
        _refresh_locks.pop(jti, None)


async def _refresh(jti: str) -> str:
    # Lock per session so two concurrent requests hitting an expired token don't
    # both fire a refresh_token grant (Microsoft can reject the second as reused).
    lock = _refresh_locks.setdefault(jti, asyncio.Lock())
    async with lock:
        d = _token_store.get(jti)
        if not d:
            raise ReauthRequired("session not found")
        if time.time() < d["expiry"] - 60:
            # Another coroutine already refreshed while we waited on the lock.
            return d["access_token"]
        r = await _request_with_retry("POST", MS_TOKEN_URL, data={
            "client_id": MS_CLIENT_ID,
            "client_secret": MS_CLIENT_SECRET,
            "refresh_token": d["refresh_token"],
            "grant_type": "refresh_token",
            "scope": _ms_scopes(d.get("read_only", False)),
        })
        t = r.json()
        log.info("session %s: refresh response has_access_token=%s expires_in=%s",
                 jti, "access_token" in t, t.get("expires_in"))
        if "access_token" not in t:
            _token_store.pop(jti, None)
            _refresh_locks.pop(jti, None)
            reason = t.get("error_description", t.get("error", "refresh failed"))
            log.warning("token refresh failed, session needs re-auth: %s", reason)
            raise ReauthRequired(reason)
        d["access_token"] = t["access_token"]
        d["expiry"] = time.time() + t.get("expires_in", 3600)
        # Microsoft frequently rotates the refresh token on every use (unlike
        # Google's more static ones) — always store whatever comes back, or the
        # *next* refresh will fail with a reused/invalid refresh_token.
        new_refresh = t.get("refresh_token")
        if new_refresh:
            d["refresh_token"] = new_refresh
        log.info("session %s: refreshed, new expiry in %.0fs (now=%.0f, expiry=%.0f)",
                 jti, d["expiry"] - time.time(), time.time(), d["expiry"])
        return d["access_token"]


async def _ms_access_token(jti: str) -> str:
    d = _token_store.get(jti)
    if not d:
        raise ReauthRequired("session not found")
    remaining = d["expiry"] - time.time()
    if remaining <= 60:
        log.info("session %s: access token needs refresh (remaining=%.0fs, expiry=%.0f, now=%.0f)",
                 jti, remaining, d["expiry"], time.time())
        return await _refresh(jti)
    return d["access_token"]


async def _auth() -> dict:
    # Resolves the token from _token_store at the moment of use rather than trusting
    # a value captured earlier — see the Gmail sibling project's history for why a
    # cached/ContextVar-captured token can end up stale by the time a tool call
    # actually runs on a different asyncio Task than the one that refreshed it.
    jti = _session_jti.get()
    if not jti:
        raise RuntimeError("not authenticated")
    token = await _ms_access_token(jti)
    return {"Authorization": f"Bearer {token}"}


def _parse_retry_after(value: str) -> float | None:
    """Parse a Retry-After header value in either form RFC 7231 allows: a
    delay-seconds integer, or an HTTP-date. Returns None if neither parses, or
    if the value isn't a finite number (float() itself happily parses "inf"/
    "nan", which would otherwise hang a retry's sleep indefinitely)."""
    try:
        seconds = float(value)
    except ValueError:
        seconds = None
    if seconds is None:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    return seconds if math.isfinite(seconds) else None


async def _request_with_retry(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Graph API call with retry — up to API_RETRY_ATTEMPTS total tries on
    retryable statuses (429, 5xx) before giving up, honoring a Retry-After
    response header (delay-seconds or HTTP-date) when Graph sends one. Used for
    both read and write calls. A network-level error (timeout, connection reset)
    is NOT retried and propagates immediately — whether the request already
    landed server-side is ambiguous, and blindly retrying a non-idempotent write
    (e.g. sendMail) risks duplicating it. Callers keep calling r.raise_for_status()
    as before: a final retryable-status response is returned as-is (so that still
    raises)."""
    c = _client()
    r: httpx.Response | None = None
    for attempt in range(1, API_RETRY_ATTEMPTS + 1):
        r = await c.request(method, url, **kwargs)
        if r.status_code not in _RETRYABLE_STATUSES:
            return r
        if attempt < API_RETRY_ATTEMPTS:
            delay = _API_RETRY_DELAY
            retry_after = r.headers.get("Retry-After")
            if retry_after is not None:
                parsed = _parse_retry_after(retry_after)
                if parsed is not None:
                    delay = max(delay, parsed)
            await asyncio.sleep(min(delay, _MAX_RETRY_DELAY))
    assert r is not None
    return r


async def _call(method: str, url: str, *, headers: dict | None = None, **kwargs: Any) -> httpx.Response:
    """Authenticated Graph call: injects the bearer token (merged with any
    extra headers a caller needs, e.g. a Prefer header), goes through
    _request_with_retry, and raises on any error status. Shared by nearly
    every tool — this is the one place that shape is assembled."""
    merged_headers = {**await _auth(), **(headers or {})}
    r = await _request_with_retry(method, url, headers=merged_headers, **kwargs)
    r.raise_for_status()
    return r


async def _call_list(method: str, url: str, **kwargs: Any) -> list[dict]:
    """Like _call, for the common case of a Graph collection response — the
    actual items are under the "value" key."""
    r = await _call(method, url, **kwargs)
    return r.json().get("value", [])


_MAX_PAGINATION_PAGES = 50  # hard cap — mirrors depth<6 in list_folders' recursive walk;
                             # a self-referential or never-terminating @odata.nextLink
                             # (e.g. from a misbehaving proxy/cache) must not hang a call forever.


async def _call_list_all(method: str, url: str, *, headers: dict | None = None,
                         **kwargs: Any) -> list[dict]:
    """Like _call_list, but follows @odata.nextLink until exhausted (or
    _MAX_PAGINATION_PAGES pages, whichever comes first) instead of returning
    just the first page. Use this only where the caller needs the COMPLETE
    collection to behave correctly (e.g. enumerating every child folder) —
    everywhere else, a tool's own $top/max_results is a deliberate page size
    the caller chose, not an accidental truncation to fix. headers (e.g. a
    Prefer header) are resent on every page; params/other kwargs only on the
    first — nextLink already encodes the full query string for later pages."""
    items: list[dict] = []
    next_url: str | None = url
    first = True
    pages = 0
    while next_url and pages < _MAX_PAGINATION_PAGES:
        r = await _call(method, next_url, headers=headers, **(kwargs if first else {}))
        body = r.json()
        items.extend(body.get("value", []))
        next_url = body.get("@odata.nextLink")
        first = False
        pages += 1
    return items


def _require_write() -> None:
    if _read_only.get():
        raise PermissionError("this connection is authorized read-only; write actions are disabled")


def _effective_read_only(payload: dict, alias: str) -> bool:
    """A restricted alias stays restricted even if the JWT itself says
    read_only=False — e.g. because the OAuth client never echoed back the
    'resource' parameter that read_only was originally decided from. `alias`
    here comes from server-side path routing (_split_alias), not anything the
    client asserts, so this can't be bypassed by client behavior."""
    return payload.get("read_only", False) or alias in READ_ONLY_ALIASES


def _enc(value: str) -> str:
    """URL-encode a value for safe interpolation into a Graph REST path segment.
    Graph entity ids are documented to sometimes contain '/' (a reserved path
    delimiter) — without this, such an id would split the request onto an
    unintended path instead of addressing the resource it names."""
    return quote(value, safe="")


def _escape_search_phrase(value: str) -> str:
    """Graph's $search syntax delimits phrases with double quotes; strip any
    embedded quote so a crafted value can't break out of the phrase and be
    parsed as a separate, unintended search clause (e.g. 'foo\" OR \"bar')."""
    return value.replace('"', "")


def _escape_odata_literal(value: str) -> str:
    """OData string literals are single-quoted; the standard escape for an
    embedded quote is to double it, so a crafted value can't break out of the
    literal and alter the rest of the $filter expression."""
    return value.replace("'", "''")


def _parse_recipients(addresses: str) -> list[dict]:
    return [{"emailAddress": {"address": a.strip()}} for a in addresses.split(",") if a.strip()]


def _build_message(subject: str, body: str, to: str, cc: str = "") -> dict:
    message: dict = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": _parse_recipients(to),
    }
    if cc:
        message["ccRecipients"] = _parse_recipients(cc)
    return message


def _calendar_base(calendar_id: str) -> str:
    return ME if calendar_id == "primary" else f"{ME}/calendars/{_enc(calendar_id)}"


# ── FastMCP tools ──────────────────────────────────────────────────────────────

mcp = FastMCP("Outlook MCP")


@mcp.tool
async def get_profile() -> dict:
    """Get the authenticated Outlook account's profile."""
    r = await _call("GET", ME, params={"$select": "id,displayName,mail,userPrincipalName"})
    return r.json()


@mcp.tool
async def search_emails(query: str, max_results: int = 20) -> list[dict]:
    """Search mail using Microsoft Graph's $search syntax (from:, subject:, body:,
    participants:, received:, etc. — similar power to Gmail's operators, but not
    the same syntax). Each hit already includes from/subject/receivedDateTime/
    preview/categories/hasAttachments in this one call — no separate enrichment
    round-trip needed, unlike the Gmail equivalent."""
    return await _call_list("GET", f"{ME}/messages", params={
        "$search": f'"{_escape_search_phrase(query)}"',
        "$top": max_results,
        "$select": "id,conversationId,subject,from,toRecipients,receivedDateTime,"
                   "bodyPreview,hasAttachments,categories,parentFolderId",
    })


def _attachment_summary(a: dict) -> dict:
    return {
        "attachmentId": a.get("id", ""),
        "filename": a.get("name", ""),
        "mimeType": a.get("contentType", ""),
        "size": a.get("size", 0),
        "isInline": a.get("isInline", False),
    }


@mcp.tool
async def read_message(message_id: str) -> dict:
    """Read an Outlook message by ID. Returns headers, body (already-decoded
    content — Graph gives plain/HTML text directly, no MIME/base64 decoding
    needed), and attachment metadata (use get_attachment to download bytes)."""
    r = await _call("GET", f"{ME}/messages/{_enc(message_id)}")
    data = r.json()

    attachments: list[dict] = []
    if data.get("hasAttachments"):
        rows = await _call_list("GET", f"{ME}/messages/{_enc(message_id)}/attachments",
                                params={"$select": "id,name,contentType,size,isInline"})
        attachments = [_attachment_summary(a) for a in rows if not a.get("isInline")]

    body = data.get("body", {})
    return {
        "id": data["id"],
        "conversationId": data.get("conversationId", ""),
        "from": (data.get("from") or {}).get("emailAddress", {}).get("address", ""),
        "to": [r["emailAddress"]["address"] for r in data.get("toRecipients", [])],
        "cc": [r["emailAddress"]["address"] for r in data.get("ccRecipients", [])],
        "subject": data.get("subject", ""),
        "date": data.get("receivedDateTime", ""),
        "bodyPreview": data.get("bodyPreview", ""),
        "bodyType": body.get("contentType", ""),
        "body": body.get("content", ""),
        "categories": data.get("categories", []),
        "parentFolderId": data.get("parentFolderId", ""),
        "attachments": attachments,
    }


@mcp.tool
async def read_conversation(conversation_id: str) -> list[dict]:
    """Read all messages in a conversation (Outlook's equivalent of a Gmail
    thread), oldest first. Graph has no single "get whole thread" call, so this
    lists messages filtered by conversationId."""
    return await _call_list("GET", f"{ME}/messages", params={
        "$filter": f"conversationId eq '{_escape_odata_literal(conversation_id)}'",
        "$orderby": "receivedDateTime asc",
        "$select": "id,conversationId,subject,from,toRecipients,receivedDateTime,"
                   "bodyPreview,hasAttachments,categories,parentFolderId",
    })


@mcp.tool
async def get_attachment(message_id: str, attachment_id: str) -> dict:
    """Download an attachment's bytes (as standard base64) by message_id and
    attachment_id from read_message's attachments list. Unlike Gmail, Graph
    attachment ids are stable across repeated reads of the same message, so the
    id read_message returned earlier can always be reused here. Rejects
    attachments larger than ATTACHMENT_MAX_MB without downloading their bytes."""
    meta = await _call("GET", f"{ME}/messages/{_enc(message_id)}/attachments/{_enc(attachment_id)}",
                       params={"$select": "name,contentType,size"})
    m = meta.json()
    filename = m.get("name", "")
    mime_type = m.get("contentType", "")
    declared_size = m.get("size", 0)

    if declared_size > ATTACHMENT_MAX_BYTES:
        raise ValueError(
            f"attachment {filename!r} is {declared_size} bytes, exceeds "
            f"ATTACHMENT_MAX_MB ({ATTACHMENT_MAX_MB}MB) limit"
        )

    full = await _call("GET", f"{ME}/messages/{_enc(message_id)}/attachments/{_enc(attachment_id)}")
    content_bytes = full.json().get("contentBytes")
    if content_bytes is None:
        raise ValueError(
            f"attachment {filename!r} has no downloadable content — it may be a "
            f"reference (e.g. a OneDrive link) or item attachment rather than a file"
        )
    raw = base64.b64decode(content_bytes)
    if len(raw) > ATTACHMENT_MAX_BYTES:
        raise ValueError(
            f"attachment {filename!r} is {len(raw)} bytes, exceeds "
            f"ATTACHMENT_MAX_MB ({ATTACHMENT_MAX_MB}MB) limit"
        )

    return {
        "attachmentId": attachment_id,
        "messageId": message_id,
        "filename": filename,
        "mimeType": mime_type,
        "size": len(raw),
        "data": base64.b64encode(raw).decode(),
    }


@mcp.tool
async def send_email(to: str, subject: str, body: str, cc: str = "",
                     reply_to_message_id: str = "") -> dict:
    """Send an email, or reply within a conversation via reply_to_message_id.
    Replies use Graph's native reply action: subject is auto-generated ("RE: ...")
    and body is threaded above the quoted original — no manual In-Reply-To/
    References header construction needed, unlike Gmail."""
    _require_write()
    if reply_to_message_id:
        payload: dict = {"comment": body, "message": {"toRecipients": _parse_recipients(to)}}
        if cc:
            payload["message"]["ccRecipients"] = _parse_recipients(cc)
        await _call("POST", f"{ME}/messages/{_enc(reply_to_message_id)}/reply", json=payload)
        return {"sent": True, "replyTo": reply_to_message_id}

    message = _build_message(subject, body, to, cc)
    await _call("POST", f"{ME}/sendMail", json={"message": message, "saveToSentItems": True})
    return {"sent": True}


@mcp.tool
async def create_draft(to: str, subject: str, body: str, cc: str = "") -> dict:
    """Create a draft email."""
    _require_write()
    message = _build_message(subject, body, to, cc)
    r = await _call("POST", f"{ME}/messages", json=message)
    return r.json()


@mcp.tool
async def list_drafts(max_results: int = 10) -> list[dict]:
    """List draft emails."""
    return await _call_list("GET", f"{ME}/mailFolders/drafts/messages", params={"$top": max_results})


@mcp.tool
async def send_draft(draft_id: str) -> dict:
    """Send an existing draft."""
    _require_write()
    await _call("POST", f"{ME}/messages/{_enc(draft_id)}/send")
    return {"sent": draft_id}


@mcp.tool
async def update_draft(draft_id: str, to: str, subject: str, body: str,
                       cc: str = "") -> dict:
    """Replace the content of an existing draft."""
    _require_write()
    message = _build_message(subject, body, to, cc)
    r = await _call("PATCH", f"{ME}/messages/{_enc(draft_id)}", json=message)
    return r.json()


@mcp.tool
async def delete_draft(draft_id: str) -> dict:
    """Permanently delete a draft."""
    _require_write()
    await _call("DELETE", f"{ME}/messages/{_enc(draft_id)}")
    return {"deleted": draft_id}


async def _list_child_folders(folder_id: str | None, select: str | None = None) -> list[dict]:
    # Graph pages mailFolders results (observed: exactly 100 per page) — follow
    # @odata.nextLink for the full set, or a mailbox with more children than one
    # page holds (this feature exists specifically for large mailboxes) silently
    # loses the rest with no indication to the caller that more exist.
    url = f"{ME}/mailFolders" if folder_id is None else f"{ME}/mailFolders/{_enc(folder_id)}/childFolders"
    params: dict[str, Any] = {"$top": 100}
    if select:
        params["$select"] = select
    return await _call_list_all("GET", url, params=params)


_LIST_FOLDERS_MAX = 200  # hard safety cap — see docstring
_LIST_FOLDERS_MINIMAL_SELECT = "id,displayName,parentFolderId,childFolderCount"
_COUNT_FOLDERS_SELECT = "id,childFolderCount"


@mcp.tool
async def list_folders(parent_folder_id: str = "", recursive: bool = False,
                       name_contains: str = "", minimal: bool = False) -> list[dict]:
    """List mail folders (each item carries parentFolderId — unlike Gmail's flat
    "/"-named labels, Outlook folders have a real hierarchy).

    By default, returns only the immediate children of the root (top-level
    folders like Inbox/Drafts/Sent Items themselves, not what's inside them).
    parent_folder_id lists one specific folder's immediate children instead —
    it accepts a real folder id or a well-known name ("inbox", "archive",
    "junkemail", "deleteditems", etc.), so parent_folder_id="inbox" is a common
    starting point for a mailbox organized as subfolders directly under Inbox.
    recursive=True walks the whole subtree from there (or from the root, if
    parent_folder_id is also omitted) instead of just one level. name_contains
    filters the result by a case-insensitive substring match on displayName —
    combine it with recursive=True to search the whole tree by name in one
    call (e.g. a mailbox organized into hundreds of per-project subfolders)
    instead of walking down level by level.

    minimal=True drops unreadItemCount/totalItemCount from each returned
    folder, keeping only id/displayName/parentFolderId/childFolderCount — use
    this with recursive=True over a large tree when you only care about
    structure or names, not per-folder unread stats, to cut the result's
    token cost. name_contains still works unchanged under minimal=True,
    since displayName is always kept. If you don't need names or ids at all
    and just want a count, use count_folders instead — it's cheaper still.

    A mailbox can have far more folders than fit in one tool result — the
    result is capped at 200 folders regardless of the above; narrow with
    parent_folder_id/name_contains if you hit that cap."""
    select = _LIST_FOLDERS_MINIMAL_SELECT if minimal else None
    folders: list[dict] = []
    frontier = await _list_child_folders(parent_folder_id or None, select=select)
    folders.extend(frontier)
    if recursive:
        depth = 0
        while frontier and depth < 6:  # guards against pathological nesting
            depth += 1
            parents = [p for p in frontier if p.get("childFolderCount", 0) > 0]
            results = await asyncio.gather(
                *(_list_child_folders(p["id"], select=select) for p in parents)
            )
            frontier = [child for children in results for child in children]
            folders.extend(frontier)
    if name_contains:
        needle = name_contains.casefold()
        folders = [f for f in folders if needle in f.get("displayName", "").casefold()]
    return folders[:_LIST_FOLDERS_MAX]


@mcp.tool
async def count_folders(parent_folder_id: str = "") -> dict:
    """Count every folder in the mailbox (or one subtree), without returning
    the folders themselves — for when the actual question is "how many
    folders are there", not their names or ids. Cheaper than
    list_folders(recursive=True) in two ways: each Graph request asks only
    for id/childFolderCount (not name, parent, or unread stats), and the tool
    result itself is a handful of numbers instead of a potentially-large list
    of folder objects.

    parent_folder_id scopes the count to one subtree (a folder id, or a
    well-known name like "inbox"), same as list_folders; omit it to count the
    whole mailbox. The walk shares list_folders' depth<6 guard against
    pathological nesting — if that guard is hit, truncated is True and total
    is a lower bound (folders beyond depth 6 are not counted), never a
    silently-wrong exact-looking number."""
    total = 0
    frontier = await _list_child_folders(parent_folder_id or None, select=_COUNT_FOLDERS_SELECT)
    total += len(frontier)
    depth = 0
    while frontier and depth < 6:
        depth += 1
        parents = [p for p in frontier if p.get("childFolderCount", 0) > 0]
        results = await asyncio.gather(
            *(_list_child_folders(p["id"], select=_COUNT_FOLDERS_SELECT) for p in parents)
        )
        frontier = [child for children in results for child in children]
        total += len(frontier)
    truncated = bool(frontier) and depth >= 6
    return {"total": total, "depth_reached": depth, "truncated": truncated}


@mcp.tool
async def create_folder(display_name: str, parent_folder_id: str = "") -> dict:
    """Create a mail folder, optionally nested under parent_folder_id."""
    _require_write()
    url = (f"{ME}/mailFolders/{_enc(parent_folder_id)}/childFolders" if parent_folder_id
           else f"{ME}/mailFolders")
    r = await _call("POST", url, json={"displayName": display_name})
    return r.json()


@mcp.tool
async def update_folder(folder_id: str, display_name: str) -> dict:
    """Rename a mail folder. Graph folders have no visibility options — this is
    rename-only, unlike Gmail's update_label which could also change label/
    message-list visibility."""
    _require_write()
    r = await _call("PATCH", f"{ME}/mailFolders/{_enc(folder_id)}", json={"displayName": display_name})
    return r.json()


@mcp.tool
async def delete_folder(folder_id: str) -> dict:
    """Permanently delete a mail folder and everything in it."""
    _require_write()
    await _call("DELETE", f"{ME}/mailFolders/{_enc(folder_id)}")
    return {"deleted": folder_id}


@mcp.tool
async def move_folder(folder_id: str, destination_folder_id: str) -> dict:
    """Move a folder — and everything in it, including its own subfolders — to
    become a child of another folder (e.g. re-parenting a folder to live under
    Inbox). Accepts a real folder id or a well-known name ('inbox', 'archive',
    etc.) for either argument. Unlike move_message, a moved folder keeps its
    original id (confirmed by live testing) — the id in this result will match
    folder_id; only parentFolderId changes."""
    _require_write()
    r = await _call("POST", f"{ME}/mailFolders/{_enc(folder_id)}/move",
                    json={"destinationId": destination_folder_id})
    return r.json()


@mcp.tool
async def list_categories() -> list[dict]:
    """List the mailbox's master category list (name + color for each tag —
    the closest Outlook equivalent to a Gmail label, though categories have no
    per-tag visibility options)."""
    return await _call_list("GET", f"{ME}/outlook/masterCategories")


@mcp.tool
async def update_categories(message_id: str, add: list[str] | None = None,
                            remove: list[str] | None = None) -> dict:
    """Add/remove categories (color-coded tags) on a message. Graph has no atomic
    add/remove for categories, unlike Gmail's modify_labels — this reads the
    current list, merges in add/remove, and writes the full array back, so two
    concurrent updates to the same message could race."""
    _require_write()
    r = await _call("GET", f"{ME}/messages/{_enc(message_id)}", params={"$select": "categories"})
    current = set(r.json().get("categories", []))
    current |= set(add or [])
    current -= set(remove or [])
    r2 = await _call("PATCH", f"{ME}/messages/{_enc(message_id)}",
                     json={"categories": sorted(current)})
    return r2.json()


@mcp.tool
async def move_message(message_id: str, destination_folder_id: str) -> dict:
    """Move a message to another folder — accepts a folder id from list_folders,
    or a well-known name ('inbox', 'archive', 'junkemail', 'deleteditems', etc.).
    Graph assigns the moved message a NEW id: use the id in this result for any
    further operations, not the original message_id — Gmail message ids never
    change on a label change, but Outlook message ids do change on a move."""
    _require_write()
    r = await _call("POST", f"{ME}/messages/{_enc(message_id)}/move",
                    json={"destinationId": destination_folder_id})
    return r.json()


@mcp.tool
async def mark_as_junk(message_id: str) -> dict:
    """Move a message to the Junk Email folder — the closest Outlook equivalent
    to Gmail's report_phishing (there's no separate "report to Microsoft" API;
    this just relocates the message, same as Gmail's did)."""
    _require_write()
    r = await _call("POST", f"{ME}/messages/{_enc(message_id)}/move", json={"destinationId": "junkemail"})
    return r.json()


@mcp.tool
async def trash_message(message_id: str) -> dict:
    """Move a message to Deleted Items (soft delete, matching Gmail's trash
    semantics)."""
    _require_write()
    r = await _call("POST", f"{ME}/messages/{_enc(message_id)}/move",
                    json={"destinationId": "deleteditems"})
    return r.json()


@mcp.tool
async def list_calendars() -> list[dict]:
    """List all calendars on the account."""
    return await _call_list("GET", f"{ME}/calendars")


@mcp.tool
async def list_events(calendar_id: str = "primary", time_min: str = "",
                      time_max: str = "", max_results: int = 20) -> list[dict]:
    """List calendar events in a time window (RFC3339, e.g. 2026-05-20T00:00:00Z).
    Defaults to now through +30 days if omitted. Uses Graph's calendarView, which
    — unlike a plain events listing — correctly expands recurring events into
    their individual occurrences within the window."""
    now = datetime.now(UTC)
    start = time_min or now.isoformat()
    end = time_max or (now + timedelta(days=30)).isoformat()
    return await _call_list(
        "GET", f"{_calendar_base(calendar_id)}/calendarView",
        headers={"Prefer": 'outlook.timezone="UTC"'},
        params={"startDateTime": start, "endDateTime": end, "$top": max_results})


@mcp.tool
async def search_events(query: str, calendar_id: str = "primary",
                        max_results: int = 20) -> list[dict]:
    """Search events by keyword. Unlike list_events, this does not expand
    recurring events into individual occurrences — it matches the recurring
    series itself, not each future instance."""
    return await _call_list(
        "GET", f"{_calendar_base(calendar_id)}/events",
        params={"$search": f'"{_escape_search_phrase(query)}"', "$top": max_results})


@mcp.tool
async def get_event(event_id: str, calendar_id: str = "primary") -> dict:
    """Get a specific calendar event by ID."""
    r = await _call("GET", f"{_calendar_base(calendar_id)}/events/{_enc(event_id)}")
    return r.json()


# ── OAuth endpoints ────────────────────────────────────────────────────────────

def _base_oauth_metadata() -> dict:
    return {
        "issuer": BASE_URL,
        "authorization_endpoint": f"{BASE_URL}/authorize",
        "token_endpoint": f"{BASE_URL}/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
    }


async def _oauth_server_metadata(req: Request) -> JSONResponse:
    return JSONResponse({**_base_oauth_metadata(), "scopes_supported": ["outlook"]})


async def _openid_configuration(req: Request) -> JSONResponse:
    return JSONResponse({**_base_oauth_metadata(), "scopes_supported": ["openid", "outlook"]})


async def _protected_resource(req: Request) -> JSONResponse:
    # The alias this was reached through (if any) — stashed into scope["state"] by
    # _App.__call__ before the alias gets stripped for routing. Echoed back here so
    # Claude's OAuth client round-trips it as the 'resource' param on /authorize,
    # letting _authorize tell which aliased connector is authenticating.
    alias = getattr(req.state, "alias", "")
    resource = f"{BASE_URL}/{alias}/mcp" if alias else f"{BASE_URL}/mcp"
    return JSONResponse({
        "resource": resource,
        "authorization_servers": [BASE_URL],
    })


async def _authorize(req: Request):
    p = req.query_params
    redirect_uri = p.get("redirect_uri", DEFAULT_REDIRECT_URI)
    if redirect_uri not in ALLOWED_REDIRECT_URIS:
        return Response("Unknown redirect_uri", status_code=400)
    if not p.get("code_challenge"):
        return Response("PKCE code_challenge is required", status_code=400)
    if p.get("code_challenge_method", "S256") != "S256":
        # _pkce_ok (used at /token) always SHA-256-hashes the verifier — a
        # client negotiating the legal but unsupported 'plain' method would
        # otherwise fail later with an opaque invalid_grant instead of a clear
        # reason. This is also what code_challenge_methods_supported declares.
        return Response("Only the S256 code_challenge_method is supported", status_code=400)

    # Server-verified: _App.__call__ derives this from the URL path itself
    # (_split_alias), not from anything the client supplies — unlike the
    # OAuth 'resource' query parameter, which a client can omit or mismatch.
    # Deciding read_only from the client-echoed resource instead of this would
    # let a client that fails to echo it correctly get a read_only=False token
    # even when authenticating through a restricted alias; that token's
    # write-access would then depend on which endpoint it was later presented
    # to (_effective_read_only re-checks per-request) rather than being fixed
    # at mint time, so presenting it to the unaliased /mcp would bypass the
    # restriction entirely.
    alias = getattr(req.state, "alias", "")
    read_only = alias in READ_ONLY_ALIASES
    log.info("authorize: alias=%r -> %s", alias, "read-only" if read_only else "read-write")

    # A second, independent PKCE pair for this server's own client role toward
    # Microsoft — not required for a confidential (client-secret-bearing) client,
    # but adds defense in depth and costs nothing extra.
    ms_verifier, ms_challenge = _new_pkce_pair()

    our_state = secrets.token_urlsafe(16)
    _state_store[our_state] = {
        "client_state": p.get("state"),
        "client_redirect_uri": redirect_uri,
        "code_challenge": p.get("code_challenge"),
        "ms_code_verifier": ms_verifier,
        "read_only": read_only,
        "created": time.time(),
    }
    return RedirectResponse(
        f"{MS_AUTHORIZE_URL}?" + urlencode({
            "client_id": MS_CLIENT_ID,
            "redirect_uri": f"{BASE_URL}/auth/callback",
            "response_type": "code",
            "response_mode": "query",
            "scope": _ms_scopes(read_only),
            "state": our_state,
            "code_challenge": ms_challenge,
            "code_challenge_method": "S256",
        })
    )


async def _auth_callback(req: Request):
    error = req.query_params.get("error")
    if error:
        return Response(f"Microsoft OAuth error: {error}", status_code=400)

    state_data = _state_store.pop(req.query_params.get("state", ""), None)
    if not state_data:
        return Response("Invalid or expired state", status_code=400)

    r = await _request_with_retry("POST", MS_TOKEN_URL, data={
        "code": req.query_params.get("code"),
        "client_id": MS_CLIENT_ID,
        "client_secret": MS_CLIENT_SECRET,
        "redirect_uri": f"{BASE_URL}/auth/callback",
        "grant_type": "authorization_code",
        "code_verifier": state_data["ms_code_verifier"],
        "scope": _ms_scopes(state_data.get("read_only", False)),
    })
    tokens = r.json()

    if "error" in tokens:
        log.warning("Microsoft token exchange failed: %s", tokens.get("error"))
        return Response(f"Token exchange failed: {tokens['error']}", status_code=400)

    ui = await _request_with_retry(
        "GET", ME, headers={"Authorization": f"Bearer {tokens['access_token']}"},
        params={"$select": "id,displayName,mail,userPrincipalName"})
    if not ui.is_success:
        log.warning("Microsoft /me fetch failed (%s): %s", ui.status_code, ui.text[:200])
        return Response("Failed to fetch Microsoft account info", status_code=502)
    profile = ui.json()
    email = _resolve_email(profile)
    if not email:
        log.warning("Microsoft /me returned neither mail nor userPrincipalName: %s", profile)
        return Response("Could not determine account email address", status_code=502)

    jti = secrets.token_urlsafe(16)
    log.info("new session authenticated: %s (read_only=%s) jti=%s has_refresh_token=%s "
             "expires_in=%s",
             email, state_data.get("read_only", False), jti,
             tokens.get("refresh_token") is not None, tokens.get("expires_in"))
    _token_store[jti] = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "expiry": time.time() + tokens.get("expires_in", 3600),
        "email": email,
        "read_only": state_data.get("read_only", False),
        # Provisional; replaced with the real 30-day expiry once /token mints the client JWT.
        # Ensures flows abandoned between here and /token still get purged.
        "jwt_exp": time.time() + STATE_TTL,
    }

    our_code = secrets.token_urlsafe(16)
    _code_store[our_code] = {
        "jti": jti,
        "email": email,
        "code_challenge": state_data["code_challenge"],
        "client_redirect_uri": state_data["client_redirect_uri"],
        "client_state": state_data["client_state"],
        "read_only": state_data.get("read_only", False),
        "created": time.time(),
    }

    params: dict = {"code": our_code}
    if state_data["client_state"]:
        params["state"] = state_data["client_state"]
    return RedirectResponse(f"{state_data['client_redirect_uri']}?{urlencode(params)}")


async def _token(req: Request) -> JSONResponse:
    form = await req.form()
    # Starlette form values are `UploadFile | str`. A client posting multipart (or a
    # scanner doing so) would otherwise reach _pkce_ok with an UploadFile and crash it on
    # .encode(); treat any non-string value as absent so those requests get a clean 400.
    data = {k: v for k, v in form.multi_items() if isinstance(v, str)}
    code_data = _code_store.pop(data.get("code", ""), None)
    if not code_data:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    verifier = data.get("code_verifier")
    if not verifier or not _pkce_ok(verifier, code_data["code_challenge"]):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    now = int(time.time())
    exp = now + 86400 * 30
    token = jwt.encode({
        "jti": code_data["jti"],
        "email": code_data["email"],
        "read_only": code_data.get("read_only", False),
        "iat": now,
        "exp": exp,
    }, JWT_SECRET, algorithm="HS256")

    if code_data["jti"] in _token_store:
        _token_store[code_data["jti"]]["jwt_exp"] = exp

    return JSONResponse({"access_token": token, "token_type": "Bearer",
                         "expires_in": 86400 * 30})


# ── Bearer auth middleware (raw ASGI — preserves ContextVar across await) ──────

def _www_auth_header(alias: str) -> bytes:
    metadata_path = (f"/{alias}/.well-known/oauth-protected-resource" if alias
                     else "/.well-known/oauth-protected-resource")
    # alias comes from the request path (_split_alias) and can contain
    # surrogate-escaped bytes if the path wasn't valid UTF-8 — replace rather
    # than raise, or a single malformed request would crash with an unhandled
    # UnicodeEncodeError instead of getting the intended 401.
    return (
        f'Bearer realm="Outlook MCP", '
        f'resource_metadata="{BASE_URL}{metadata_path}"'
    ).encode("utf-8", errors="replace")

_OAUTH_PATHS = frozenset([
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
    "/.well-known/oauth-protected-resource",
    "/authorize",
    "/auth/callback",
    "/token",
])

_KNOWN_PATHS = _OAUTH_PATHS | {"/mcp"}

_SECURITY_HEADERS = [
    (b"strict-transport-security", b"max-age=63072000; includeSubDomains"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
]


def _with_security_headers(send):
    """Wraps an ASGI send() so every response — including ones from the mounted
    OAuth/FastMCP sub-apps — gets standard security headers. /authorize is the one
    point a real browser touches (the user's, round-tripping through Microsoft's
    consent screen), so this is worth doing even though most traffic is API calls."""
    async def wrapped(message):
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", [])) + _SECURITY_HEADERS
            message = {**message, "headers": headers}
        await send(message)
    return wrapped


_MULTI_SLASH = re.compile(r"/+")


def _normalize_path(path: str) -> str:
    """Collapse repeated slashes (e.g. '//mcp' -> '/mcp') before any routing or
    auth-gate matching runs. Without this, a non-canonical path form matches
    neither a known OAuth path nor the '/mcp'/'/mcp/' auth-gate check below —
    falling through to the mounted FastMCP app with no bearer-auth check
    performed at all, relying entirely on downstream routing behavior (which
    this file has no control over) to not also treat it as equivalent."""
    return _MULTI_SLASH.sub("/", path)


def _split_alias(path: str) -> tuple[str, str]:
    """Strip a leading /<alias> segment so /personal/mcp, /work/.well-known/... etc.
    resolve the same as their unaliased routes — lets two Claude connectors share one
    server. Returns (alias, normalised_path); alias is "" when there wasn't one."""
    if path in _KNOWN_PATHS or path.startswith("/mcp/"):
        return "", path
    segments = path.lstrip("/").split("/", 1)
    if len(segments) == 2:
        candidate = "/" + segments[1]
        if candidate in _KNOWN_PATHS or candidate.startswith("/mcp/"):
            return segments[0], candidate
    return "", path


class _App:
    """Dispatches OAuth paths to Starlette, everything else to FastMCP."""

    def __init__(self) -> None:
        self._oauth = Starlette(routes=[
            Route("/.well-known/oauth-authorization-server", _oauth_server_metadata),
            Route("/.well-known/openid-configuration", _openid_configuration),
            Route("/.well-known/oauth-protected-resource", _protected_resource),
            Route("/authorize", _authorize),
            Route("/auth/callback", _auth_callback),
            Route("/token", _token, methods=["POST"]),
        ])
        self._mcp = mcp.http_app()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            global _http_client
            _http_client = httpx.AsyncClient(timeout=HTTPX_TIMEOUT)
            try:
                await self._mcp(scope, receive, send)
            finally:
                await _http_client.aclose()
                _http_client = None
            return

        if scope["type"] == "http":
            send = _with_security_headers(send)

            try:
                _purge_expired_states()
                _purge_expired_tokens()
            except Exception:
                log.exception("periodic cleanup failed")

            alias, path = _split_alias(_normalize_path(scope["path"]))
            if path != scope["path"]:
                scope = {**scope, "path": path, "raw_path": path.encode()}
            # Starlette route handlers (e.g. _protected_resource) read this via
            # req.state.alias to echo the alias back into OAuth discovery responses.
            scope["state"] = {**(scope.get("state") or {}), "alias": alias}

            # Auth check for MCP endpoint only
            if path == "/mcp" or path.startswith("/mcp/"):
                headers = dict(scope.get("headers", []))
                auth = headers.get(b"authorization", b"").decode("utf-8", errors="replace")
                if not auth.startswith("Bearer "):
                    await self._send_401(send, alias)
                    return
                try:
                    payload = jwt.decode(auth[7:], JWT_SECRET, algorithms=["HS256"])
                    await _ms_access_token(payload["jti"])  # fail fast on a dead/revoked session
                except jwt.PyJWTError as e:
                    log.info("rejected MCP request: invalid/expired JWT (%s)", e)
                    await self._send_401(send, alias)
                    return
                except ReauthRequired as e:
                    log.warning("MCP request needs re-auth: %s", e)
                    await self._send_401(send, alias)
                    return
                except Exception:
                    log.exception("unexpected error validating MCP request")
                    await self._send_401(send, alias)
                    return
                _session_jti.set(payload["jti"])
                _read_only.set(_effective_read_only(payload, alias))

            if path in _OAUTH_PATHS:
                await self._oauth(scope, receive, send)
                return

        await self._mcp(scope, receive, send)

    @staticmethod
    async def _send_401(send, alias: str = "") -> None:
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"text/plain"),
                                (b"www-authenticate", _www_auth_header(alias))]})
        await send({"type": "http.response.body", "body": b"Unauthorized"})


app = _App()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), log_level="info")
