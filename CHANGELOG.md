[← Back to README](README.md)

# Changelog

All notable **code changes** to this project — `server.py` and the test suite — in
version order, starting at 0.1. Administrative changes (documentation, README/SETUP.md,
CI/tooling config, LICENSE, dependency pins, changelog maintenance itself, etc.) are
tracked in git commit history only, not here.

## 2026-09-12

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
