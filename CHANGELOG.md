[← Back to README](README.md)

# Changelog

All notable **code changes** to this project — `server.py` and the test suite — in
version order, starting at 0.1. Administrative changes (documentation, README/SETUP.md,
CI/tooling config, LICENSE, dependency pins, changelog maintenance itself, etc.) are
tracked in git commit history only, not here.

## 2026-09-12

### 0.10 — Fourth review pass: bound pagination, fix attachment edge case

A fourth review pass, focused specifically on 0.9's brand-new pagination code
(the part of the codebase least scrutinized by the prior three rounds), found
it had introduced a new class of risk while fixing the old one.

**Fixed**
- `_call_list_all`'s pagination loop had no bound on page count — unlike
  every other loop in this file (`API_RETRY_ATTEMPTS`, `list_folders`'
  `depth < 6` guard). A self-referential or never-terminating
  `@odata.nextLink` (e.g. from a misbehaving proxy/cache) would have hung a
  tool call forever. Added `_MAX_PAGINATION_PAGES` (50) as a hard cap.
- `_call_list_all` silently dropped any caller-supplied `headers` (e.g. a
  `Prefer` header) from page 2 onward — harmless today since its only caller
  passes none, but a latent trap for the next one. `headers` are now resent
  on every page; `params`/other kwargs still only on the first, since
  `@odata.nextLink` already encodes the full query string.
- `get_attachment` treated a missing `contentBytes` (e.g. a
  `referenceAttachment` — a OneDrive link — or certain `itemAttachment`
  types, neither of which carry downloadable bytes at this endpoint) as a
  valid 0-byte download instead of a clear error.

### 0.9 — Fix silent folder-listing truncation beyond one Graph page

Asking "how many folders are in the mailbox" surfaced a real bug: `list_folders()`
(top-level) returned exactly 100 folders, and `list_folders(recursive=True)`
returned exactly 200 — both suspiciously round numbers that turned out to be
truncation artifacts, not real counts.

**Fixed**
- `_list_child_folders` requested Graph with `$top=100` but never followed
  `@odata.nextLink` for further pages, so any single folder (including the
  mailbox root) with more than 100 direct children silently lost the rest —
  with no indication to the caller that more existed. This directly
  undermined the large-mailbox support `list_folders` was built for (0.2):
  the outer 200-item safety cap assumes it's capping a *complete* walk, but
  the per-level fetch was losing folders before that cap even applied.
- Added `_call_list_all()` — follows `@odata.nextLink` until exhausted — and
  switched `_list_child_folders` to use it instead of `_call_list`. Every
  other tool's use of `_call_list` is left unchanged: their `$top`/
  `max_results` is a deliberate page size the caller (the LLM) chose, not an
  accidental truncation to fix.

### 0.8 — Third review pass: alias-binding fix + duplication cleanup

A third review pass (correctness + "does every line pull its weight?")
surfaced one real security gap and confirmed several cleanup opportunities
flagged repeatedly across earlier rounds. Findings were presented to the
user for review before fixing, per instruction.

**Fixed**
- `_authorize` decided `read_only` from the client-echoed OAuth `resource`
  query parameter instead of the server-verified URL path alias already
  available via `req.state.alias`. If a client ever failed to echo
  `resource` correctly, the resulting token got full write scope and a
  `read_only=False` JWT claim — and since that claim (not which alias the
  token was minted for) is what travels with it, presenting the same token
  to the unaliased `/mcp` instead of its own `/work/mcp` bypassed the
  restriction entirely, contradicting this project's documented read-only
  guarantee. `/authorize` now reads `req.state.alias` directly; the
  now-unreachable `_alias_from_resource` helper (and its dead tests) were
  removed.
- `_www_auth_header` crashed with an unhandled `UnicodeEncodeError` if the
  request path contained invalid UTF-8 bytes in the alias segment (a
  surrogate-escaped character from the ASGI server's path decoding). Now
  encodes with `errors="replace"` instead of the default strict mode;
  confirmed h11 still accepts the resulting header bytes.
- `_http_client` is now reset to `None` after `aclose()` in the lifespan
  handler, matching `_client()`'s own "not initialized" contract instead of
  silently handing out a closed client.
- `/authorize` now explicitly rejects a `code_challenge_method` other than
  `S256` with a clear 400, instead of deferring to an opaque `invalid_grant`
  at `/token` — `_pkce_ok` only ever supported S256 to begin with.

**Simplified**
- Added `_call()`/`_call_list()` — a shared "inject auth, retry, raise on
  error status" helper and its collection-returning variant — and rewrote
  every `@mcp.tool` function to use them. Removed the `if status != N:
  raise_for_status()` pattern from 5 write tools entirely: since
  `raise_for_status()` only ever raises on 4xx/5xx regardless of which
  specific success code was expected, and none of those 5 functions called
  `.json()` on the response afterward, the status comparison was pure
  no-op complexity — flagged as duplication in all three review rounds
  without being addressed until now. Net effect: ~20 tool functions
  shortened, `server.py` is 27 lines shorter despite the new fixes' added
  comments, with zero behavior change (all 113 tests pass unchanged).
- `_purge_expired_states` now calls a shared `_purge_ttl(store)` helper
  twice instead of duplicating the same filter/pop loop for `_state_store`
  and `_code_store`.

### 0.7 — Second review pass: four more confirmed fixes + regression tests

A follow-up full-file review (to check whether 0.5/0.6 missed anything) found
four more issues, each confirmed by direct execution before fixing. Also
closed the test-coverage gap 0.5/0.6 left behind — none of their new helpers
or behavior changes had a regression test until now.

**Fixed**
- `READ_ONLY_ALIASES` env parsing filtered tokens *before* stripping slashes,
  not after — a slash-only token (e.g. a stray `/`) passed the filter but
  collapsed to `""` once stripped, silently inserting the empty string into
  the set. Since `""` is also the alias of the *unaliased* connector, a
  malformed `READ_ONLY_ALIASES` value could force the main connector into
  read-only mode. Confirmed directly: `READ_ONLY_ALIASES=work,/` produced
  `frozenset({'', 'work'})` before the fix. Extracted into a standalone
  `_parse_read_only_aliases()` helper that filters on the final value.
- `_auth_callback`'s Microsoft token-exchange and profile-fetch calls now go
  through `_request_with_retry` like every other outbound call in this file
  (they were still using a raw `httpx` call) — a transient 5xx during the
  one-time authorization-code exchange no longer fails the whole login.
- `_parse_retry_after` now rejects non-finite values — `float("inf")` parses
  without error, which would otherwise hang a retry's `asyncio.sleep`
  indefinitely. `_request_with_retry` also caps any Retry-After-derived delay
  at `_MAX_RETRY_DELAY` (60s) as a second layer, since an absurdly large but
  finite value (`"99999999999"`) isn't caught by the finiteness check alone.
- A non-canonical request path like `//mcp` previously matched neither a
  known OAuth path nor the `/mcp`/`/mcp/` auth-gate check, skipping
  bearer-auth validation entirely and falling through to the mounted FastMCP
  app relying solely on downstream routing behavior to not also treat it as
  equivalent. Added `_normalize_path()` (collapses repeated slashes) and
  apply it before alias-splitting and auth-gate matching. Confirmed directly
  that `//mcp` bypassed the auth gate before the fix and is routed through it
  correctly after.

**Added**
- 23 new regression tests covering every fix in 0.5/0.6/0.7 that had none:
  `_enc`, `_escape_search_phrase`, `_escape_odata_literal`, `_build_message`,
  `_normalize_path`, `_parse_read_only_aliases`, `_parse_retry_after`'s
  non-finite rejection, `_request_with_retry` no longer retrying network
  errors, the Retry-After cap, the `_purge_expired_tokens` lock-skip
  behavior, `_auth_callback`'s new retry, and the `$search`/`$filter`
  escaping and id URL-encoding at the tool level.

### 0.6 — Fix confirmed injection, id-encoding, and token-purge race

The three lower-confidence findings from 0.5's review were tested/reproduced
directly (live search against the real mailbox, a Microsoft Learn/GitHub
check on Graph id formats, and a mocked-transport reproduction script) before
fixing, since each turned out to be confirmable rather than merely plausible.

**Fixed**
- `search_emails`, `search_events`, and `read_conversation` now escape their
  input before interpolating into Graph's `$search`/`$filter` parameters.
  Confirmed live: `search_emails("zzznonexistentqueryterm99887")` returned 0
  results, but appending `" OR "a` to that same garbage term returned 5
  unrelated messages — the embedded quote broke out of the intended phrase.
  `$search` values now have embedded `"` stripped; `$filter` string literals
  now double an embedded `'` per the OData escaping convention.
- Every id argument (`message_id`, `folder_id`, `draft_id`, `attachment_id`,
  `event_id`, `calendar_id`) is now URL-encoded (`urllib.parse.quote`) before
  being interpolated into a Graph REST path. Graph message ids are documented
  to sometimes contain `/` — a reserved path delimiter — which would
  otherwise split the request onto an unintended path.
- `_purge_expired_tokens` now skips a session whose refresh lock is currently
  held, instead of popping it regardless. Reproduced the bug directly: a
  concurrent purge (which runs on every incoming request, including
  unrelated ones) could previously pop a session's `_token_store` entry while
  `_refresh` was still awaiting Microsoft's response for it, so the
  refreshed token got written into a dict no longer in the store — the
  session was silently lost even though `_refresh` reported success.

### 0.5 — Code quality review fixes

A full-file code quality review of `server.py` (bugs, dead code, inefficient
loops) surfaced several issues; the ones confirmed with high confidence were
fixed directly.

**Fixed**
- `_request_with_retry` no longer retries on network-level errors (timeouts,
  connection resets) — only on a definite retryable HTTP status (429/5xx).
  A network error left it ambiguous whether a non-idempotent write (e.g.
  `send_email`'s `/sendMail` POST) had already landed server-side before the
  retry fired a second, possibly duplicate, request.
- `_refresh`'s call to Microsoft's token endpoint now goes through
  `_request_with_retry` too, instead of a bare `httpx` call — a single
  transient 5xx from Microsoft during a routine access-token refresh no
  longer forces the session into a full re-auth.
- `_request_with_retry` now parses `Retry-After` as either form RFC 7231
  allows (delay-seconds or an HTTP-date), instead of only the delay-seconds
  form — an HTTP-date value used to fail `float()` parsing and silently fall
  back to the much shorter default delay.
- Every read tool's Graph GET call now goes through `_request_with_retry` as
  well (previously only write tools did) — a transient 429/503 while listing
  folders or searching mail used to fail the call outright with no recovery,
  even though reads are idempotent and the safer half to retry.
- `list_folders(recursive=True)` now fetches each depth level's sibling
  folders concurrently via `asyncio.gather` instead of one at a time —
  matters most on the 385-folder mailbox that motivated this feature (0.2).
- `send_email`, `create_draft`, and `update_draft` now share one
  `_build_message()` helper instead of each rebuilding an identical
  subject/body/toRecipients/cc dict.
- Removed the `_user_email` ContextVar — it was set on every authenticated
  request but never read anywhere.

**Flagged, not fixed yet** (lower-confidence findings at the time — correctness
concerns that needed testing/reproduction before a direct fix was warranted):
unescaped input interpolated into Graph's `$filter`/`$search` OData
parameters; folder/message/draft/attachment ids interpolated unescaped into
REST URL paths; and a narrow-window race where `_purge_expired_tokens` can
pop a session's token entry while `_refresh` is still awaiting Microsoft's
response for it. All three were confirmed and fixed in 0.6 below.

### 0.4 — Confirm move_folder id behavior

Live-tested against the real mailbox: created a throwaway top-level folder,
moved it under Inbox, and compared ids. The returned `id` was identical
before and after the move — only `parentFolderId` changed.

**Changed**
- `move_folder` docstring now states as fact that a moved folder keeps its
  original id, rather than flagging it as unconfirmed.

### 0.3 — Add move_folder

Requested after live use surfaced the need to re-parent folders (e.g. moving
a "Lot NN" folder that had ended up at the mailbox root back under Inbox).
Graph supports this via the same `move` action shape as `move_message`
(`POST /me/mailFolders/{id}/move`, `{"destinationId": ...}`), using the same
`Mail.ReadWrite` scope already requested — no new OAuth consent needed.

**Added**
- `move_folder(folder_id, destination_folder_id)` — behind `_require_write()`,
  same as every other write tool. Unlike `move_message`, Graph's own docs
  don't state whether the folder keeps its id or gets a new one on move;
  flagged in the docstring as unconfirmed pending real-world verification.

### 0.2 — Fix list_folders for large mailboxes

Live testing against a real mailbox organized into hundreds of per-property
subfolders (385 folders total) found `list_folders()` exceeding the calling
session's output limit — it was the one list tool in this codebase with no
size cap at all, always walking the entire folder tree.

**Changed**
- `list_folders()` now defaults to top-level folders only, instead of
  recursively walking the whole tree.
- Added `parent_folder_id` (list one folder's immediate children — drill down
  one level at a time) and `recursive` (opt into the old whole-tree walk,
  from `parent_folder_id` or the root) parameters.
- Added `name_contains` (case-insensitive substring filter on `displayName`),
  most useful paired with `recursive=True` to search the whole tree by name in
  one call rather than walking down level by level.
- Added a hard 200-item cap on the result regardless of the above, as a
  last-resort guard against ever exceeding the calling session's output
  budget again.

### 0.1 — Initial implementation

Sibling project to [gmail-mcp-proxy](https://github.com/systmworks/gmail-mcp-proxy),
adapted to Microsoft Graph and the Microsoft identity platform for personal (consumers
tenant) Outlook.com accounts.

- OAuth: dual-role server (AS toward Claude, client toward Microsoft's
  `consumers`-tenant v2.0 endpoint only — never `/common` or `/organizations`), PKCE
  required on both legs, redirect_uri allowlist, in-memory session store, per-session
  refresh locking, refresh-token rotation handling (Microsoft rotates on every use,
  unlike Google's more static tokens).
- Mail tools: `get_profile`, `search_emails`, `read_message`, `read_conversation`,
  `get_attachment`, `send_email`, `create_draft`, `list_drafts`, `send_draft`,
  `update_draft`, `delete_draft`.
- Folder/category tools (Outlook's split analog to Gmail labels):
  `list_folders`, `create_folder`, `update_folder`, `delete_folder`,
  `list_categories`, `update_categories`, `move_message`, `mark_as_junk`,
  `trash_message`.
- Calendar tools (read-only in this version): `list_calendars`, `list_events`
  (via Graph's `calendarView` for correct recurrence expansion), `search_events`,
  `get_event`.
- Deliberately deferred: Contacts (Graph makes this easy; the Gmail project never had
  it either), Calendar write (create/update/delete events, accept/decline, free/busy,
  `findMeetingTimes` — all natively easy on Graph, out of scope for this version).
- Test suite: pytest + `respx`-mocked Graph API, no live credentials. Covers PKCE,
  alias parsing, the consumers-tenant-only OAuth requirement, refresh-token rotation,
  `Retry-After` handling, read-only enforcement, `move_message`'s new-id semantics, and
  `update_categories`'s read-modify-write merge.
