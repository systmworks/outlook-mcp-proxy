# Committee Mailbox Cleanup Rules

Standing rules + reference for tidying folders in the Somerset Views Body
Corporate Committee mailbox (`svgccommittee@outlook.com`), accessed via the
`outlook-mcp-proxy` MCP connector. Read this top-to-bottom on pickup — rules
are ordered by how often you'll need them, not chronologically. The bottom of
the file is historical record (master log + session history), kept for audit
trail, not something you need to read to act correctly.

Mirrors the sibling project's
[`TREASURER_MAILBOX_LABEL_RULES.md`](../gmail-mcp-proxy-work/TREASURER_MAILBOX_LABEL_RULES.md),
adapted for Outlook's folder model (folders you file into) instead of Gmail's
label model (labels you stack on a message).

**Current status (2026-09-14): rogue-root cleanup is complete.** Mailbox root
holds only `Inbox` plus Outlook's own system folders (Archive, Conversation
History, Deleted Items, Drafts, Junk Email, Outbox, RSS Feeds, Sent Items,
Sync Issues) — no stray non-system folders remain outside `Inbox`. Any future
session should start by confirming that's still true (`list_folders` at root)
before assuming there's cleanup work to do.

---

## Rule 1 — Classify by topic, never by folder name, sender, or lot number

**What a message is actually about decides where it goes — not which rogue
folder it was sitting in, who sent it, or which lot number happens to appear
in it.** A folder called `lot 45` is not "communication from the Lot 45
owner" — it was created ad hoc because *some* message mentioned lot 45,
regardless of whether the real subject was a bylaw breach, an insurance
claim, a fence quote, or a plumbing issue. Confirmed the hard way: sampling
~20 of the mailbox's 49 rogue `lot NN` folders found **every single one**
mixed multiple unrelated topics together — never a single-topic "this lot
owner's mail" bucket. The same logic applies to sender-named or event-named
rogue folders (e.g. a caretaker's weekly digest covering repairs + insurance
+ trees in one message goes to whichever topic dominates, not to a generic
"caretaker" bucket just because of who sent it).

Multi-topic folders get split by `conversationId` (not subject text —
`Re:`/`Fwd:` prefixes break text matching), one destination per thread. Don't
bulk-move a folder's entire contents on the strength of its name alone —
sample with `list_messages` first, even for folders that look single-topic at
a glance. Reserve whole-folder `move_folder` for folders you've confirmed are
genuinely single-topic throughout.

## Rule 2 — Sort into the existing taxonomy; check for an existing `Lot NN` subfolder first

`Inbox`'s subfolders are the tidy, authoritative destination set (see the
[Folder Map](#folder-map-under-inbox) below for the full current list) —
don't invent a new top-level category without checking these first.

Several categories already maintain their own per-lot subfolder set:
`BYLAW Breaches`, `Emails from LOT OWNERS`, `LEGAL Advice`,
`FENCES & Patios`, `Parking/Speed`, `Insurance & Claims`, `PETS & Wildlife`.
When a rogue folder's content is genuinely lot-specific, **check whether that
category already has a `Lot NN` subfolder before creating one** — the same
lot number can legitimately have entries under more than one category (e.g.
`BYLAW Breaches/Lot 45` and `Insurance & Claims/Lot 45` can both exist for
different incidents).

## Rule 3 — Folder naming convention

- **Lot folders:** `Lot NN` — capital `Lot`, space, 2-digit number (`Lot 05`,
  `Lot 37`). Lots 100–120 stay 3-digit since that's their real number
  (`Lot 112`), not padding. Applies whenever a lot folder is touched (moved,
  renamed, recreated) — not a mandate to sweep untouched folders solely for
  case or padding.
- **All folder names generally:** Title-Case — capitalize every significant
  word, keep linking words lowercase (`from`/`of`/`for`/`in`/`on`/`at`/
  `to`/`with`/`&`), preserve existing acronyms in full caps
  (`CCTV`/`BYLAW`/`LOT`/`DOBRE`/`AMS`/`FIRE`/`OH&S`). Applies to any folder
  created or renamed from here on, mailbox-wide.
- Use `update_folder` to rename in place — `move_folder` relocates to a
  different parent instead, it does not rename.
- **Zero-padding removed (2026-09-14):** the zero-padded `Lot 0NN` folders
  under `Insurance & Claims` and `PETS & Wildlife` were previously flagged as
  untouched stragglers — now fixed. `Insurance & Claims`: `Lot 029`→`Lot 29`,
  `Lot 039 Kia`→`Lot 39 Kia`, `Lot 061`→`Lot 61`, `Lot 072`→`Lot 72`,
  `Lot 086`→`Lot 86` (`Lot 112` unchanged — already correct 3-digit).
  `PETS & Wildlife`: `Lot 002`→`Lot 02`, `Lot 006`→`Lot 06`, `Lot 007`→`Lot 07`,
  `Lot 032`→`Lot 32`, `Lot 036`→`Lot 36`, `Lot 039`→`Lot 39`, `Lot 046`→`Lot 46`,
  `Lot 051`→`Lot 51`, `Lot 092`→`Lot 92`, `Lot 099`→`Lot 99` (`Lot 109`/`Lot 113`
  unchanged — already correct 3-digit for the 100–120 range).
- **Missing-space stragglers fixed (2026-09-14):** `FENCES & Patios`'s
  `Lot117` renamed to `Lot 117` (no collision, straightforward rename).
  `PETS & Wildlife` had a `Lot113`/`Lot 113` collision (Rule 4) — `Lot113`
  held a 7-message pet-application thread, `Lot 113` held a 7-message patio-
  install thread; per Rule 4 the `Lot113` messages were moved into `Lot 113`
  and the empty `Lot113` duplicate deleted. Note the merged `Lot 113` folder
  now holds two different topics (pet application + patio install) under
  `PETS & Wildlife` — the patio-install thread is arguably a `FENCES & Patios`
  topic per Rule 1, but this merge was scoped as a naming fix only, not a
  re-classification; flag for a future topic-split pass if desired.
- **Known stragglers still deliberately left untouched** (flagged for
  awareness, not a to-do unless the user asks): misspelling `Surveyors
  Adivice` under `LEGAL Advice`; misspelling `Middle 29/28` under
  `FENCES & Patios`; trailing periods/whitespace on a few folder names.
  Don't "fix" these proactively — they were explicitly scoped out by the
  user.

## Rule 4 — Duplicate/name-collision handling: merge, never nest

When a rogue folder's name duplicates an existing Inbox category (a root
`CCTV` next to `Inbox/CCTV`, or two folders like `towing`/`towing info`
covering the same topic), **merge messages into the correct one and delete
the empty duplicate — never nest one inside the other** (avoids
`Inbox/FINANCIALS/Financials`-style redundancy). Still check each thread
individually before merging — a duplicate-named folder can contain one
off-topic thread that belongs elsewhere (e.g. a `CCTV` folder holding a
neighbor-privacy camera dispute that's actually a `BYLAW Breaches` matter).
If two `Lot NN` folders collide under the same parent (a zero-padded and a
2-digit version of the same number), move all messages into the surviving
one and delete the duplicate.

## Rule 5 — Max 3 folder levels deep

`Inbox` (1) → a Main Category (2) → a 3rd level only where genuinely needed,
e.g. individual `Lot NN` subfolders under `BYLAW Breaches`, or topic
subfolders under `Caretakers (DOBRE)`. Never nest a 4th level — flatten
instead.

## Rule 6 — Never touch messages sitting directly in `Inbox` itself

Only rogue folders outside `Inbox`, and `Inbox`'s existing subfolders as
destinations. Loose messages in `Inbox` are out of scope for this cleanup.

## Rule 7 — Vague subjects: defer, don't guess

If a subject line alone doesn't reveal the topic, don't force a
classification from the subject text. Leave it in place and flag it for a
follow-up pass that reads the message body — a wrong guess (misfiling a
bylaw breach as routine maintenance, say) is worse than leaving one message
unsorted for another session.

## Rule 8 — Don't trust a successful-looking response; re-verify

**A `move_message` call returning a clean JSON response is not proof the
move actually stuck.** Confirmed once: a call returned a correct new id and
correct destination `parentFolderId`, but a fresh `list_messages` on the
source folder afterward showed the same message still sitting there under
its original id. **Always re-verify a folder is genuinely empty with a fresh
`list_messages` before calling a cleanup pass done or before `delete_folder`
— even when every individual call in the batch reported success.** This is
stricter than just distrusting calls that errored; distrust silently-wrong
successes too.

## Rule 9 — Delete, don't file: automated notification folders

If a folder's content is an automated notification stream unrelated to
filing (e.g. Jira/Confluence task-notification digests, seen as `jira`,
`Jirra`, `Confluence / JIRA`), delete the folder and its contents outright —
don't sort its messages into a topic destination. Standing rule for any
future sweep that turns up the same pattern (new folder name, same
auto-notification source).

## Rule 10 — Content that looks unrelated to strata business

If a folder's content reads like personal correspondence or unrelated-work
notifications with no strata-business angle, don't guess a home for it —
flag it to the user and confirm whether it belongs in this mailbox at all
before filing it anywhere.

## Rule 11 — Efficiency: classify from subject lines where possible

Where the subject line alone makes the topic clear, classify on that basis
rather than reading the full message body — `move_message` and
`list_messages` results include full HTML bodies that are expensive to
process. When multiple messages in a folder share an identical (or
near-identical, ignoring `Re:`/`Fwd:`) subject, treat them as the same topic
and classify together rather than reading each individually. Still fall back
to a body read when the subject is genuinely ambiguous (Rule 7).

## Rule 12 — Processing order

Work the smallest-message-count rogue folders first, then progressively
larger ones. For large folders (dozens+ messages), sample the subject list
via `list_messages` (or a saved-large-result file parsed locally, e.g. with
PowerShell/jq, to avoid loading every HTML body into context) before
deciding whole-folder `move_folder` vs. per-message classification.

---

## Connector mechanics

- Tool prefix in this session: `mcp__223b2f5c-6fae-4ecb-a6e9-11046322110b__*`
  — the UUID is connector-instance-specific and **will differ** in future
  sessions. Identify the right connector by its tool shape: `list_folders`,
  `count_folders`, `list_messages(folder_id, max_results)`,
  `search_emails(query, max_results)`, `move_message`, `move_folder`,
  `read_conversation`, `update_folder`, `delete_folder`. Confirm with
  `get_profile` — it must return `svgccommittee@outlook.com`.
- `search_emails` uses Graph's `$search` and runs over the **whole
  mailbox** — it cannot be scoped to one folder. Use
  `list_messages(folder_id)` instead when the question is "what's actually
  inside this specific folder."
- `read_conversation` and `list_messages` both return `conversationId` —
  group by that, not subject text, when splitting a mixed folder (see
  Rule 1).
- **`move_message` returns a NEW message id** — don't reuse the old id in a
  follow-up call after moving it. **`move_folder` keeps its original id** —
  the opposite behavior. Confirmed by live test 2026-09-12.
- Known proxy quirk (retryable — just retry immediately, no backoff needed):
  intermittent `"This connector's server hostname doesn't resolve..."`
  error — a transient hiccup, not a real config problem. Seen recurring
  across large batches (e.g. 4 of 42 rename calls, or a 57-message move
  needing 3 retry rounds) — always resolves on straight retry.
- A service restart on the Proxmox host wipes the in-memory OAuth session
  store — if every call suddenly 401s, that's why; the user needs to
  redeploy/restart the service and the Claude connector needs re-adding, not
  just a retry.

## Standard workflow for auditing one rogue folder

1. `list_messages(folder_id)` → group results by `conversationId`.
2. Classify each thread by topic (Rule 1), checking for an existing `Lot NN`
   subfolder first (Rule 2) and the naming convention (Rule 3).
3. `move_message` each message to its destination folder — or `move_folder`
   the whole thing if genuinely single-topic (Rule 1).
4. Re-verify the source folder is empty with a fresh `list_messages` (Rule
   8), *then* `delete_folder`.
5. Update the [Master destination log](#master-destination-log) with what
   happened.

`count_folders()` is a quick before/after health check — the root total
should shrink as rogue folders are emptied and deleted.

---

## Folder Map under Inbox

`Inbox` has 25 top-level subfolders (childFolderCount shown where it has
children). Categories whose children are dominated by per-lot `Lot NN`
folders are marked accordingly rather than listing every lot number — see
Rule 2 for the check-both-locations principle.

```
Inbox/
├── AM Strata (AMS)                          [3 children — AGM 2024/2025/2026 archives, etc.]
├── Arborist List                            [1 child]
├── BYLAW Breaches                           [26 — per-lot Lot NN subfolders + `12/10/25`, `PDF Bylaws`]
├── Bylaw Review
├── Caretakers (DOBRE)                       [11 — see below, fully enumerated]
│   ├── Caretaker Breaches
│   ├── Caretaker Complaints
│   ├── Caretaker Contract
│   ├── Caretakers Corner
│   ├── Complaints
│   ├── Contractor Sign In
│   ├── Fair Work Commission
│   ├── General Emails.
│   ├── Newsletter
│   ├── OH&S Report
│   └── Quote for Work
├── CCTV                                     [3 children]
├── Committee Members                        [1 child]
├── Dobre-legal
├── Emails from LOT OWNERS                   [22 — per-lot Lot NN subfolders]
├── FENCES & Patios                          [35 — per-lot Lot NN subfolders + a few named
│                                              exceptions: `15 Glastonbury Drive`, `Behind 047`,
│                                              `Catchment`, `Middle 29/28`]
├── FINANCIALS                               [7 — see below, fully enumerated]
│   ├── 2025
│   ├── Accounts
│   ├── Budget 2026
│   ├── Financial Info
│   ├── Lot Owners
│   ├── Paid & Pending Invoices
│   └── Payment Plans                        [2 children]
├── Gardening Group
├── Insurance & Claims                       [7 — see below, fully enumerated]
│   ├── Cyclone 2025
│   ├── Lot 29
│   ├── Lot 39 Kia
│   ├── Lot 61
│   ├── Lot 72
│   ├── Lot 86
│   └── Lot 112  (100-120 range, correctly 3-digit)
├── LEGAL Advice                             [4 — see below, fully enumerated]
│   ├── Lot 08
│   ├── Lot 28
│   ├── Lot 88
│   └── Surveyors Adivice  (misspelled, 1 child — flagged, not fixed)
├── Maintenance & REPAIRS (Common)           [22 — see below, fully enumerated]
│   ├── Bathrooms                    ├── Irrigation
│   ├── BBQ Area                     ├── Pathways
│   ├── Boundary Gutters             ├── Plumbling, Leaks & Drainage  [5 children]
│   ├── Drains                       ├── Pool Pump Room
│   ├── Eastern Boundary             ├── Road Works
│   ├── Electrical and Power         ├── Rubbish Removal
│   ├── FIRE Equipment               ├── Signs
│   ├── Gardening - LAWN & Hedges    ├── Street Lights
│   ├── Gardening - TREES  [6 children]  ├── Swimming POOL  [4 children]
│   ├── Gardening - WEEDS and Clearing [1 child] ├── Workplace Health & Safety (WHS)
│   ├── Gate House                   └── GYM  [2 children]
├── Maintenance & REPAIRS (Private Lots)     [2 children]
├── MEETINGS                                 [9 — see below, fully enumerated]
│   ├── AGM 2024  [1 child]
│   ├── AGM 2025
│   ├── AGM 2026
│   ├── AM Strata  [2 children]
│   ├── Committee Meetings Inc. Unofficial
│   ├── Desk of Secretary
│   ├── Lot Owners Email                     [24 — per-lot Lot NN subfolders]
│   ├── Monthly Meetings  [6 children]
│   └── New Caretaking Agreement  [1 child]
├── OUTSTANDING TASKS                        [1 child]
├── Parking/Speed                            [18 — per-lot Lot NN subfolders + `Towing Info`]
├── PETS & Wildlife                          [15 — Cat/Dog Complaints, Dog Park, + per-lot
│                                              Lot NN subfolders (Lot 109/113 stay 3-digit,
│                                              100-120 range)]
├── Prior 8.1.24                             [13 children — pre-2024 archive, not actively sorted into]
├── Social Media                             [1 child]
├── Surveyor
├── Water Usage & Meters                     [1 child]
└── Window Tinting
```

---

## Master destination log

The authoritative record of what happened to every rogue root folder
processed during the 2026-09 cleanup. Update it every time a folder is
closed out (moved/merged/deleted) — this is what lets a session pick up
mid-cleanup without re-auditing folders already handled.

| Rogue folder (root) | Msgs | Destination | Method |
|---|---|---|---|
| `Caretaker breaches` | — | `Caretakers (DOBRE)/Caretaker Breaches` | `move_folder` |
| `Caretaker complaints` | — | `Caretakers (DOBRE)/Caretaker Complaints` | `move_folder` |
| `Caretaker contract` | — | `Caretakers (DOBRE)/Caretaker Contract` | `move_folder` |
| `Contractor Sign in` | — | `Caretakers (DOBRE)/Contractor Sign In` | `move_folder` |
| `Fair work Commission` | — | `Caretakers (DOBRE)/Fair Work Commission` | `move_folder` |
| `lot 47` (pre-rename) | 27 | Split: `Maintenance & REPAIRS (Common)` (drainage), `Parking/Speed` (trailer), `Caretakers (DOBRE)` (hedge/legal complaints) | per-message |
| `lot 51` (pre-rename) | 28 | Split: `Maintenance & REPAIRS (Common)` (retaining wall), `Insurance & Claims` (CIB/Crawford claim) | per-message |
| `lot 70` (pre-rename) | 31 | `BYLAW Breaches/Lot 70` (vandalism/police report/repair saga) | whole-folder `move_folder` + renamed |
| `pool pump room` | 29 | `Maintenance & REPAIRS (Common)/Pool Pump Room` | whole-folder `move_folder` |
| `paid & pending invoices` | 91 | `FINANCIALS/Paid & Pending Invoices` | whole-folder `move_folder` |
| `BC meetings` | 0 | *(deleted, was empty)* | `delete_folder` |
| `strata companies` | 0 | *(deleted, was empty)* | `delete_folder` |
| `gccc` | 1 | `Arborist List` | per-message |
| `incidents` | 1 | `BYLAW Breaches` | per-message |
| `incident reports` | 2 | `BYLAW Breaches` | per-message |
| `notices` | 1 | `Gardening Group` | per-message |
| `bushland inspections` | 2 | `Maintenance & REPAIRS (Common)` | per-message |
| `Greg/Dobre chats` | 2 | `Committee Members` | per-message |
| `garden edging` | 2 | `Maintenance & REPAIRS (Common)/Workplace Health & Safety (WHS)` | per-message |
| `IT Stuff` | 1 | `FINANCIALS` (Microsoft 365 subscription renewal — recurring cost) | per-message, then `delete_folder` |
| `committee stuff` (root, 1 msg) | 1 | `MEETINGS` (Committee Meeting Agenda + Financials/Aged Balance attachments, 8 July 2026 meeting) | per-message, then `delete_folder` |
| `committee stuff` (17-msg batch) | 17 | Split: `FINANCIALS` (Credit-Card-payment vote, 4), `Committee Members` (contact change 1 + Away notice 1 + Confidentiality agreement 1), `MEETINGS` (Meeting-dispute cluster 5 + AGM-minutes correction 1), `Caretakers (DOBRE)/Fair Work Commission` (3) | per-message |
| `Formal Complaints` | 3 | `Parking/Speed` (e-bike speeding, 1) + `Caretakers (DOBRE)` (contractor-governance + caretaker complaint, 2) | per-message |
| `capital improvements` | 6 | `Maintenance & REPAIRS (Common)` (sign-replacement, 4) + `FINANCIALS` (AB Concrete Designs invoice, 2) | per-message |
| `irrigation` | 8 | `Maintenance & REPAIRS (Common)/Irrigation` | whole-folder `move_folder` |
| `financial info` | 9 | `FINANCIALS/Financial Info` (Term Deposit maturity/rollover) | whole-folder `move_folder` |
| `common property inspections` | 9 | `MEETINGS` (walkaround reports, 5) + `BYLAW Breaches` (garden-maintenance compliance tracking, 4) | per-message |
| `Lot 58` (root) | 2 | `BYLAW Breaches/Lot 58` (unauthorised curb mount, retroactive approval request) | per-message, then `delete_folder` |
| `Lot 90` | 2 | `Emails from LOT OWNERS` (personal correspondence, resident Kay #90 ↔ Chairperson Rick) | per-message, then `delete_folder` |
| `Lot 33` (root) | 3 | `FENCES & Patios/Lot 33` (boulder/broken-fence incident) — later merged with a colliding `Lot 033` (9 msgs, tree-overhang + camera-complaint) | whole-folder `move_folder`, then merge |
| `Lot 54` (loose under Inbox) | 5 | Split: `BYLAW Breaches/Lot 54` (breach-letter/reminder-notice thread, 4) + `MEETINGS` (Facebook-post committee vote, 1) | per-message |
| `towing info` | 19 | `Parking/Speed/Towing Info` | whole-folder `move_folder` + renamed |
| `towing` (root, 57 msgs) | 57 | `Parking/Speed/Towing Info` (same topic, merged in) | per-message |
| `Committee meetings inc. unofficial` | 20 | `MEETINGS/Committee Meetings Inc. Unofficial` | whole-folder `move_folder` + renamed |
| `CCTV` (root) | 25 | `Inbox/CCTV` (23, footage/admin) + `BYLAW Breaches/Lot 17` (2, camera-privacy dispute) | per-message |
| `quotes` (root) | 73 | `Maintenance & REPAIRS (Common)` (44 total: 29+15 contractor quotes), `Arborist List` (11), `FENCES & Patios` (6: 5+1 fencing), `Parking/Speed` (4), `FINANCIALS` (1), `MEETINGS` (9: 8+1 MYBOS booking-system proposal) | per-message, then `delete_folder` |

**All rogue root folders from the original ~96-folder audit are now
processed.** Nothing is pending in this table.

---

## Session history (condensed)

Kept for audit trail and to explain *why* certain decisions were made — not
required reading to continue the work, since the Master destination log
above already captures the current state of every folder.

- **2026-09-12/13:** Initial audit — 391 total folders, ~96 non-system
  folders loose at root. Connector mechanics confirmed (`move_message` new-id
  vs `move_folder` same-id behavior; transient hostname-error retry pattern).
  Core classification rule (Rule 1) established after sampling ~20 of 49
  rogue `lot NN` folders and finding every one multi-topic.
- **2026-09-13:** Confirmed with user — Section G structural moves
  (Caretaker/Contractor/Fair-Work-Commission folders → subfolders under
  `Caretakers (DOBRE)`), Jira/Confluence delete-not-file rule, 3-level depth
  limit, subject-line-first classification for efficiency, 2-digit lot
  naming convention (with a dedicated rename pass), `LEGAL Advice`'s
  lot-specific-debt-recovery split corrected after an initial misfile.
- **2026-09-14 (day):** Digit-padding rename pass completed across `BYLAW
  Breaches` (19), `Emails from LOT OWNERS` (20), `LEGAL Advice` (3), then
  extended to `FENCES & Patios` (24, with a `Lot 033`/`Lot 33` collision
  resolved by merge) and `Parking/Speed` (15). Medium root folders (`pool
  pump room`, `paid & pending invoices`) swept whole-folder. Capitalization
  established as a standing convention; a full mailbox-wide Title-Case sweep
  renamed 23 folders. `PETS & Wildlife` and `Insurance & Claims`'s
  zero-padded lot folders explicitly scoped *out* of that pass — left as
  known stragglers (see Rule 3).
- **2026-09-14 (continued):** Remaining flagged root folders processed:
  `Lot 58`, `towing info`, `Committee meetings inc. unofficial`, root `CCTV`
  (split against the pre-existing `Inbox/CCTV`), root `towing` (57 msgs,
  merged into `Towing Info`). Rule 8 (don't trust a successful-looking
  response) established after a `move_message` call returned a clean
  response for a message that, on re-check, hadn't actually moved.
- **2026-09-14 (final):** `quotes` (73 msgs) processed in stages down to a
  final 2-message remainder (fencing quote + MYBOS booking-system proposal),
  then closed out. `committee stuff`, `IT Stuff`, `Lot 90` each reviewed and
  filed (previously left flagged/unfiled — see Rule 10 for why they'd been
  deferred: `IT Stuff` didn't cleanly match a category until read in full,
  `Lot 90` and part of `committee stuff` were personal/low-content messages
  needing a body read before a destination was obvious). Section G confirmed
  already complete from an earlier session (all 5 folders already subfolders
  under `Caretakers (DOBRE)`, just needed capitalization fixes). Root
  `Lot 33` and `Lot 58` (the loose, separate-from-category versions)
  confirmed no longer present — cleanup complete.
- **2026-09-14 (zero-padding + missing-space cleanup):** The previously
  flagged zero-padded stragglers under `Insurance & Claims` and
  `PETS & Wildlife` were renamed to drop the extra zero (e.g. `Lot 029`→
  `Lot 29`, `Lot 002`→`Lot 02`) per the padding convention (Rule 3). Missing-
  space stragglers also fixed: `FENCES & Patios`'s `Lot117`→`Lot 117`
  (straightforward rename); `PETS & Wildlife`'s `Lot113`/`Lot 113` name
  collision resolved per Rule 4 (7-message pet-application thread from
  `Lot113` moved into `Lot 113`, empty duplicate deleted) — see the note in
  Rule 3 about the resulting folder holding two topics.
- **Remaining known stray/misspelled folder never brought into a rename
  pass** (flagged, deliberately not actioned — see Rule 3): `LEGAL Advice`'s
  `Surveyors Adivice`; `FENCES & Patios`'s `Middle 29/28`.
