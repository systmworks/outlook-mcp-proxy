[← Back to README](README.md)

# Changelog

All notable **code changes** to this project — `server.py` and the test suite — in
version order, starting at 0.1. Administrative changes (documentation, README/SETUP.md,
CI/tooling config, LICENSE, dependency pins, changelog maintenance itself, etc.) are
tracked in git commit history only, not here.

## 2026-09-12

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
