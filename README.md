# Outlook MCP Server

Self-hosted [MCP](https://modelcontextprotocol.io) server that exposes a **personal**
Outlook.com account (mail + calendar, via Microsoft Graph) to Claude.ai (or any MCP
client) via a standard OAuth 2.0 flow — no stored tokens, no pre-generated credentials.

**Personal accounts only.** This deliberately uses Microsoft's `consumers`-tenant
endpoint, which structurally excludes work/school (Azure AD) accounts — only
outlook.com / hotmail.com / live.com-style personal Microsoft accounts can ever sign in.

Sibling project to [gmail-mcp-proxy](https://github.com/systmworks/gmail-mcp-proxy),
which does the same thing for Gmail — see [Outlook vs Gmail](#outlook-vs-gmail) below
for where the two diverge.

## Quick Links

- [Setup](SETUP.md) — Azure app registration + deployment walkthrough
- [Changelog](CHANGELOG.md) — version history

## How it works

```
Claude.ai ──[OAuth]──► This server ──[OAuth]──► Microsoft
Claude.ai ──[MCP]────► This server ──[Graph API]──► Outlook mail / calendar
```

The server acts as an OAuth proxy: it presents itself as an OAuth 2.0 authorization
server to Claude, and internally delegates authentication to Microsoft. After the user
grants access, Microsoft Graph tokens are stored server-side (in memory) and injected
per-request. A short-lived JWT is issued to Claude as the bearer token.

**Multiple accounts** — add the server twice in Claude with different alias URLs
(`/personal/mcp`, `/family/mcp`). Each session is isolated; authenticate each with a
different Microsoft account.

## Tools

| Tool | Description |
|------|-------------|
| `get_profile` | Outlook account profile |
| `search_emails` | Search with Graph's `$search` syntax (`from:`, `subject:`, `body:`, `received:`, …) |
| `read_message` | Full message with decoded body and attachment metadata |
| `read_conversation` | All messages in a conversation (Outlook's equivalent of a Gmail thread), oldest first |
| `get_attachment` | Download an attachment's bytes (base64) by `message_id`/`attachment_id` — see `ATTACHMENT_MAX_MB` |
| `send_email` | Send, or reply within a conversation via `reply_to_message_id` |
| `create_draft` | Save a draft |
| `list_drafts` | List drafts |
| `send_draft` | Send an existing draft |
| `update_draft` | Replace the content of an existing draft |
| `delete_draft` | Permanently delete a draft |
| `list_folders` | Top-level folders by default; pass `parent_folder_id` (a folder id, or a well-known name like `"inbox"`) to drill into one, `recursive=True` to walk the whole subtree, or `name_contains` to search by name (capped at 200 results) |
| `create_folder` | Create a folder, optionally nested |
| `update_folder` | Rename a folder |
| `delete_folder` | Permanently delete a folder |
| `move_folder` | Re-parent a folder (and its contents/subfolders) under another folder |
| `list_categories` | The mailbox's master category list (color-coded tags) |
| `update_categories` | Add/remove categories on a message |
| `move_message` | Move a message to another folder |
| `mark_as_junk` | Move a message to Junk Email |
| `trash_message` | Move a message to Deleted Items |
| `list_calendars` | All calendars |
| `list_events` | Events in a time window (defaults to now–+30 days) |
| `search_events` | Search events by keyword |
| `get_event` | Single event by ID |

Calendar is **read-only** in this version, and there are no Contacts tools — both are
easy additions on Graph, deliberately deferred; see [Outlook vs Gmail](#outlook-vs-gmail).

## Prerequisites

- A GitHub account with this repo forked (or cloned) into it
- An Azure account (free) — used only to register an OAuth app, no billing needed
- A place to run the server with HTTPS — self-hosting (Python 3.12+ or Docker) behind
  Tailscale or your own reverse proxy is the documented path; see [Setup](SETUP.md)

## Configuration

| Variable | Description |
|----------|-------------|
| `MS_CLIENT_ID` | Azure App registration Application (client) ID |
| `MS_CLIENT_SECRET` | Azure App registration client secret — **expires within 24 months**, see [Notes](#notes) |
| `JWT_SECRET` | Secret for signing session JWTs (any random string) |
| `BASE_URL` | Public base URL, no trailing slash, e.g. `https://outlook-mcp-proxy.your-tailnet.ts.net` |
| `ALLOWED_REDIRECT_URIS` | Optional. Comma-separated allowlist of OAuth redirect URIs `/authorize` will accept. Defaults to Claude.ai's callback (`https://claude.ai/api/mcp/auth_callback`) — only change this if you're connecting a non-Claude.ai MCP client. |
| `LOG_LEVEL` | Optional. Python logging level (`INFO`, `WARNING`, `DEBUG`, etc.). Defaults to `INFO`. |
| `READ_ONLY_ALIASES` | Optional. Comma-separated list of connector aliases (e.g. `family`) that should be restricted to read-only access — no send, draft, folder/category changes, or move/trash. See below. |
| `API_RETRY_ATTEMPTS` | Optional. Total attempts (1–5) for an outbound Graph API call (read or write) before giving up on a retryable status (429/5xx). Defaults to `2`. Honors a `Retry-After` response header from Graph when present, in either delay-seconds or HTTP-date form. |
| `ATTACHMENT_MAX_MB` | Optional. Max attachment size (1–25MB, decoded) `get_attachment` will fetch. Defaults to `3`. Attachment bytes return as base64 text inside the MCP tool result — straight into the calling LLM's context, not just over the network. |

## Read-only accounts

To connect an account you want Claude to only ever read from — never send, delete, or
modify — add its alias to `READ_ONLY_ALIASES`, e.g. `READ_ONLY_ALIASES=family` for a
connector added at `/family/mcp`. That account's Microsoft OAuth grant will only ever
request `Mail.Read`/`Calendars.Read` — no write scope is ever issued for it, so even a
bug in this server can't make it send or delete anything; Graph rejects it regardless.
The server also refuses write tool calls itself with a clear error, as a second layer.

**Enforcement is server-side and unconditional** — which alias a request comes in
through is derived from the URL path on every single request, both at `/authorize`
(when deciding which Microsoft OAuth scopes to request) and on every `/mcp` call
afterward, not something the client asserts. A restricted alias stays restricted
even if its bearer token is ever presented to a different connector's endpoint.

## Development

```bash
pip install -r requirements-dev.txt
pytest          # test suite
ruff check .    # lint
mypy            # type check (config in pyproject.toml)
```

Tests mock all Graph API calls (via `respx`) and cover the pure-logic helpers (PKCE,
alias parsing, recipient parsing) plus tool behavior that's easy to get wrong — the
consumers-tenant-only OAuth requirement, refresh-token rotation, read-only enforcement,
and `move_message`'s new-id semantics. No live Microsoft credentials needed to run them.

## Outlook vs Gmail

Where this diverges from the [Gmail sibling project](https://github.com/systmworks/gmail-mcp-proxy):

**Harder / more work:**
- Categories need read-modify-write (no atomic add/remove on Graph), vs. Gmail's atomic label add/remove.
- Calendar listing needs two different endpoints (`calendarView` for time-range/recurrence-expansion vs. `events`/`$search`), vs. Gmail's single consistent surface.
- `move_message` returns a **new** message id — anything holding the old id afterward will fail. Gmail message ids never change.
- `move_folder`, by contrast, keeps the folder's **original** id — confirmed by live testing; only `parentFolderId` changes.
- Folder hierarchy is a real tree (`parentFolderId`), vs. Gmail's flat `/`-named labels.
- Two account-registration facts must be exactly right, with no forgiving fallback: the `consumers`-tenant-only endpoint, and "Personal Microsoft accounts only" in Azure Portal — get either wrong and work/school accounts could authenticate.
- The Azure client secret **expires within 24 months** (Google's doesn't) — needs a calendar reminder, not a code fix.
- Refresh tokens **roll on every use** and expire after ~90 days of account *inactivity* — a different failure mode than Google's, and the rotated token must always be re-stored.

**Easier / genuine advantages:**
- `read_message` is simpler — `body.content`/`body.contentType` arrive as ready JSON, no MIME-tree walking or base64url decoding.
- Attachment ids are **stable** across repeated reads of the same message — no Gmail-style partId/attachmentId mismatch bug to work around.
- Reply is a native Graph action (`/reply`) — no manual `In-Reply-To`/`References` MIME header construction.
- `$search` on messages returns full fields per hit already, with no separate enrichment round-trip needed (Gmail's `search_emails` needs one).
- Graph explicitly documents and honors `Retry-After` — a more reliable throttling signal than Gmail's docs provide.
- Calendar write, free/busy, `findMeetingTimes`, and a full Contacts API are all natively easy on Graph — deliberately deferred here, not technical gaps.

## Notes

- Sessions are stored in memory — a server restart requires re-authentication in Claude.ai
- Runs as a single process — don't scale to multiple replicas or `uvicorn --workers N`.
  Session/state stores are per-process in-memory, so a request landing on a different
  process than the one that authenticated it would fail as if unauthenticated.
- Microsoft access tokens are refreshed automatically using the stored refresh token,
  which Microsoft frequently rotates on every use — the server always stores whatever
  comes back.
- Personal Microsoft account refresh tokens roll on a ~90-day *inactivity* window —
  an account genuinely unused for that long needs the same re-auth as after a restart.
- The Azure app registration's client secret expires within 24 months. Rotate it in
  Azure Portal → your app → Certificates & secrets before then, update
  `MS_CLIENT_SECRET`, and restart the service.
- The server issues 30-day JWTs; Claude re-authenticates when they expire.
