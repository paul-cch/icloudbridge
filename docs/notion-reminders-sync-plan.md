# Notion Tasks <-> Apple Reminders Sync Plan

## Confidence Statement

This plan is not treated as 100% proven until the verification gates near the end pass against live Apple Reminders and a safe Notion test slice.

The strategy is currently high-confidence as an implementation direction, but not factually complete enough for production writes. The reason is simple: Apple EventKit identifiers, Notion schema behavior, and bidirectional deletion semantics all have edge cases that cannot be resolved by design alone. They need executable tests.

## Goal

Adapt iCloudBridge's existing Apple Reminders sync foundation into a local, two-way sync between Apple Reminders and Paul's Notion `Tasks` data source.

The target Notion data source is:

- Data source ID: `61ef2269-1dc6-4391-aaff-8013d2b857e3`
- Database ID: `d2ca9bc1-3b49-4f3f-b93e-bd33c11d6f19`
- Title: `Tasks`

The first production target must sync exactly one Apple Reminders list, for example `Notion Sync`, rather than every list on the machine.

## Current iCloudBridge Foundation

iCloudBridge already has several useful pieces:

- `icloudbridge/sources/reminders/eventkit.py` reads and writes Apple Reminders through EventKit.
- `icloudbridge/core/reminders_sync.py` implements bidirectional sync planning, SQLite mappings, deletion controls, dry-run behavior, and last-write-wins conflict handling.
- `icloudbridge/sources/reminders/caldav_adapter.py` provides the current remote task backend, using CalDAV VTODO.
- `icloudbridge/utils/db.py` already has a reminders mapping table, but it is CalDAV-specific and not sufficient for Notion identity recovery.

The Notion implementation should reuse the EventKit adapter and sync-engine ideas, but it should not directly force Notion into the CalDAV adapter shape. Notion is a schemaful database API, not a task protocol.

## Hard Constraints

- V1 must be local-first on macOS, because Apple Reminders access depends on EventKit permissions.
- V1 must be dry-run first.
- V1 must never sync the whole Notion database by default.
- V1 must not use `External ID` for Apple identity. That property is already used by GitHub, dissertation, Moodle, email, TickTick/imported tasks, and legacy rows.
- V1 must not create new Notion select/status options implicitly unless the user explicitly approves a schema migration.
- V1 must not hard-delete tasks on either side.
- V1 must not rely on Apple `calendarItemIdentifier` alone.

## Adversarial Review: Loopholes And Fixes

### 1. Apple reminder identifiers can become invalid

Loophole: iCloudBridge's current reminder mapping uses the local EventKit `calendarItemIdentifier`. Apple documents that a full calendar sync can lose this identifier, so a stored local ID may stop resolving.

Fix:

- Store both `calendarItemIdentifier` and `calendarItemExternalIdentifier` when available.
- Store a stable generated sync UUID in the Notion row, such as `Apple Sync ID`.
- Cache fallback identity fields: normalized title, due date, completion state, calendar/list name, creation date, and last known Notion page ID.
- On lookup failure, run identity recovery before treating the item as deleted.
- Use deletion only after a grace period or two consecutive sync runs where recovery fails.

### 2. Notion writes can create echo loops

Loophole: Updating Notion changes `last_edited_time`, so the next sync may think Notion changed independently and write back to Apple.

Fix:

- Store content hashes for both sides after every successful write.
- Store source-side timestamps observed before and after writes.
- Treat a change as sync-authored when the remote hash equals the just-written hash, even if `last_edited_time` changed.
- Only declare a conflict when both side hashes differ from the last synced hash.

### 3. Notion schema may not contain the proposed properties/options

Loophole: The current plan mentions `Apple Reminder ID` and `Source = Apple Reminders`, but these may not exist. Writing unknown properties fails; writing unknown select/status options may mutate schema or fail depending on API behavior and permissions.

Fix:

- Add a schema preflight command.
- Required existing properties for V1: `Task Name`, `Status`, `Due Date`, `Notes`, `Priority`.
- Optional properties: `Apple Reminder ID`, `Apple Sync ID`, `Source`, `Area`, `Tags`, `Completed At`, `Reminder At`.
- If identity properties are missing, stop with a migration plan instead of syncing.
- Default to existing `Source = Manual` until a deliberate schema migration adds `Apple Reminders`.
- Treat schema migration as a separate explicit command.

### 4. Notion status mapping can lose intent

Loophole: Apple Reminders has a boolean completion state. Notion has statuses: `Not started`, `In progress`, `Blocked`, `Done`, `Cancelled`. Mapping all incomplete Apple reminders to `Not started` can erase `In progress` or `Blocked`.

Fix:

- Apple completion false must not overwrite a richer Notion status unless Apple is the winning side for a real conflict.
- Map Apple completed true to `Done`.
- Map Notion `Done` to Apple completed true.
- Map Notion `Cancelled` to Apple completed true plus a cancellation marker in notes or sync metadata.
- Preserve `Blocked` and `In progress` in Notion unless the user changes the Apple reminder in a way that clearly wins.

### 5. Deletion semantics are ambiguous

Loophole: A missing Apple reminder might mean deleted, identifier changed, list moved, permission changed, or iCloud has not synced yet. A missing Notion row might mean archived, filtered out, permission lost, or deleted.

Fix:

- V1 has no hard delete.
- Missing Apple item enters `missing_apple_seen_at` state, not immediate cancellation.
- Missing Notion item enters `missing_notion_seen_at` state, not immediate Apple completion.
- Require two consecutive runs or a minimum grace interval before marking `Cancelled`.
- Include a `skip_deletions` default of true until manual test gates pass.

### 6. Recurrence does not map cleanly to the current Notion schema

Loophole: iCloudBridge supports EventKit recurrence and CalDAV recurrence, but Paul's Notion Tasks schema only has a coarse `Repeat` select: Daily, Weekly, Monthly.

Fix:

- V1 reads recurrence but does not write full recurrence to Notion.
- If recurrence is present, preserve it in Apple and add a non-authoritative Notion note/tag only if explicitly configured.
- Do not round-trip complex recurrence until the schema gets dedicated recurrence fields.

### 7. Alarms and reminders are not the same as due dates

Loophole: Apple Reminders can have alarms separate from due dates. Notion has `Due Date` and `Reminder At`. Naively mapping only due dates can lose notification intent.

Fix:

- V1 maps due date only.
- Optional V1.1 maps first Apple alarm to Notion `Reminder At` when present.
- Never delete existing Apple alarms unless Notion explicitly wins and has a configured reminder field.

### 8. All-day due dates can shift across time zones

Loophole: Apple all-day reminders use floating date components, while Notion date properties can be date-only or date-time. Converting everything through UTC can shift dates.

Fix:

- Store `is_all_day` in the local ledger.
- Date-only Apple reminders become Notion date-only values.
- Date-time Apple reminders become Notion date-time values with timezone-preserving conversion.
- Unit-test Europe/London DST boundaries.

### 9. Notion API version and data source terminology changed

Loophole: Notion's API now distinguishes data sources from databases. Search filters use `data_source`, not `database`, under newer API versions.

Fix:

- Pin and document the Notion API version used by the adapter.
- Use data source query endpoints for row reads.
- Use page update endpoints for property writes.
- Keep database ID and data source ID separate in config and logs.

### 10. Webhooks do not remove the need for reconciliation

Loophole: Notion webhooks can signal changes, but they do not replace fetch-and-compare sync. Apple EventKit notifications are also invalidation signals rather than complete diffs.

Fix:

- V1 uses polling plus manual dry-run.
- EventKit notifications and Notion webhooks can later trigger a reconciliation run, not direct writes.
- Every run must re-fetch the relevant Apple list and Notion filtered slice.

### 11. Existing task mirrors can be corrupted by broad writes

Loophole: Paul's Notion Tasks data source already mirrors GitHub, dissertation, Moodle, email, TickTick/imported tasks, and manual tasks. A broad Notion -> Apple sync could copy far too much or overwrite canonical mirror rows.

Fix:

- Notion -> Apple only includes rows with `Apple Sync ID` or an explicit tag/filter chosen during setup.
- Apple -> Notion only writes rows from the configured Apple list.
- Existing mirrored rows are read-only unless explicitly enrolled.
- `External ID` is never edited.

### 12. The app may not have the right Notion capabilities

Loophole: The Notion integration may be able to read but not update pages, or may not have access to the data source.

Fix:

- Preflight checks must verify read, create, and update capability against a disposable test row or explicit test data source.
- If capabilities are insufficient, stop before touching Apple Reminders.

### 13. GPL affects downstream distribution

Loophole: iCloudBridge is GPL-3.0-or-later. A derivative distributed app likely needs GPL-compatible licensing.

Fix:

- Fine for a personal/local fork.
- If this becomes a separately distributed app, make licensing explicit before implementation work expands.

## Data Model

Add a local SQLite mapping table for Notion sync, separate from the existing CalDAV mapping table.

Suggested columns:

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `apple_calendar_item_id TEXT`
- `apple_external_id TEXT`
- `apple_sync_id TEXT NOT NULL UNIQUE`
- `notion_page_id TEXT UNIQUE NOT NULL`
- `notion_data_source_id TEXT NOT NULL`
- `apple_calendar_name TEXT NOT NULL`
- `last_sync_timestamp REAL NOT NULL`
- `last_apple_modified TEXT`
- `last_notion_edited TEXT`
- `last_synced_hash TEXT NOT NULL`
- `last_apple_hash TEXT`
- `last_notion_hash TEXT`
- `last_seen_apple_at TEXT`
- `last_seen_notion_at TEXT`
- `missing_apple_seen_at TEXT`
- `missing_notion_seen_at TEXT`
- `is_all_day BOOLEAN DEFAULT 0`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

Add these Notion properties through an explicit schema migration:

- `Apple Sync ID` as rich text. Required.
- `Apple Reminder ID` as rich text. Optional debug field.

Do not use `External ID` for Apple sync identity.

## Field Mapping

V1 mapping:

| Apple Reminders | Notion Tasks |
| --- | --- |
| title | `Task Name` |
| notes | `Notes` |
| completed true | `Status = Done` |
| completed false | preserve Notion status unless Apple-created row defaults to `Not started` |
| priority | `Priority` with conservative mapping |
| due date | `Due Date` with date-only preservation |
| first alarm | optional later `Reminder At` |
| calendar/list | fixed sync config; optional tag later |
| sync identity | `Apple Sync ID` plus SQLite ledger |

Default Notion values for Apple-originated tasks:

- `Source = Manual` unless the schema migration adds `Apple Reminders`.
- `Area = Life` unless configured otherwise.
- `Status = Not started`.

## Notion Adapter

Create `icloudbridge/sources/reminders/notion_adapter.py`.

Responsibilities:

- Authenticate with `NOTION_API_TOKEN` or a keyring-backed config value. Do not depend on shelling out to `ntn` for production sync.
- Query a configured Notion data source with pagination.
- Convert Notion pages into a normalized remote task dataclass.
- Create Notion pages under the configured data source.
- Update Notion page properties.
- Mark Notion rows as done/cancelled rather than deleting them.
- Respect Notion request limits and `Retry-After`.
- Pin the Notion API version and keep data source IDs distinct from database IDs.

Suggested dataclass:

```python
@dataclass
class NotionTask:
    page_id: str
    title: str
    notes: str | None
    completed: bool
    status: str
    priority: str | None
    due_date: datetime | None
    due_is_all_day: bool
    reminder_at: datetime | None
    last_edited_time: datetime
    apple_sync_id: str | None
    apple_reminder_id: str | None
    url: str | None
    raw_properties: dict[str, Any]
```

Core methods:

```python
async def preflight_schema(self) -> NotionSchemaReport
async def get_tasks(self, filter_mode: FilterMode) -> list[NotionTask]
async def create_task(self, task: NotionTaskCreate) -> NotionTask
async def update_task(self, page_id: str, patch: NotionTaskPatch) -> NotionTask
async def mark_cancelled(self, page_id: str) -> bool
```

## Sync Engine

Create a new engine rather than heavily parameterizing the CalDAV one at first:

- `icloudbridge/core/notion_reminders_sync.py`

It can borrow the same structure as `RemindersSyncEngine`:

1. Run schema and capability preflight.
2. Fetch Apple reminders from the configured Apple list.
3. Fetch Notion tasks from the configured data source and explicit filter.
4. Build mappings from SQLite.
5. Match by `Apple Sync ID` first.
6. Fall back to Apple external ID.
7. Fall back to title plus due date only during explicit bootstrap mode.
8. Recover missing Apple identifiers before treating anything as deleted.
9. Build a dry-run sync plan.
10. Execute only if dry-run output is accepted or auto-sync is already enabled after test gates pass.
11. Update the ledger with observed timestamps and hashes.

Conflict rule:

- If only one side hash changed since last sync, that side wins.
- If both side hashes changed, latest edit wins for V1.
- If both changed title or notes, preserve the losing value in a sync-conflict note or log entry.
- If a change was authored by the sync itself, suppress echo writes using the stored hashes.

## Deletion Policy

V1 should avoid hard deletion.

- Apple reminder completed -> Notion `Status = Done`.
- Notion `Status = Done` -> Apple reminder completed.
- Apple reminder missing -> mark as missing in the ledger; do not immediately cancel Notion.
- Notion row missing -> mark as missing in the ledger; do not immediately complete Apple.
- Apple deletion after grace period -> Notion `Status = Cancelled`, only if deletion handling is enabled.
- Notion `Cancelled` -> Apple reminder completed plus cancellation marker, only if enabled.

Hard delete can be a later opt-in feature behind a destructive confirmation.

## Filtering Policy

Do not sync the entire Notion database by default.

Safest default:

- Apple -> Notion: sync all reminders in the chosen Apple list.
- Notion -> Apple: sync only rows with `Apple Sync ID` or a setup-created test filter.

Allowed explicit filters:

- `Apple Sync ID` present.
- tag equals `apple-reminders`, if the tag exists.
- `Source = Apple Reminders`, only after schema migration.
- selected areas, only after a dry-run proves the exact row set.

Existing mirrored rows with `External ID` values are not enrolled automatically.

## Configuration

Add config fields for:

- Notion token source
- Notion data source ID
- Notion database ID for display/debug only
- Apple Reminders list name
- Notion filter mode
- default `Area`
- default `Source`
- deletion behavior
- dry-run default
- bootstrap mode enabled/disabled
- API version

Do not store Notion tokens in plaintext project files. Use the platform keychain where possible, or an environment variable for development.

## Implementation Milestones

### Milestone 0: Schema And Capability Preflight

- Read the Notion data source schema.
- Verify required properties.
- Verify identity properties exist, or produce a migration plan.
- Verify read/update/create permissions against a disposable test row or dedicated test data source.
- Verify Apple Reminders access and list discovery.

Done when: preflight produces a pass/fail report without writes to real tasks.

### Milestone 1: Read-Only Proof

- Add Notion adapter with schema-aware parsing.
- Read tasks from Paul's `Tasks` data source through an explicit safe filter.
- Read reminders from one Apple list.
- Print a dry-run comparison without creating or updating anything.

Done when: a command can show matched/unmatched Apple and Notion tasks without writes.

### Milestone 2: Dedicated Test Slice

- Create or use a Notion test data source/test rows.
- Create an Apple Reminders list called `Notion Sync Test`.
- Run all write tests only against the test slice.

Done when: no production Notion rows or normal Apple lists are touched.

### Milestone 3: One-Way Notion To Apple

- Create Apple reminders for selected Notion test tasks.
- Update the local SQLite ledger.
- Preserve due dates, title, notes, completion state, and priority.
- Verify repeated sync does not duplicate.

Done when: dry-run and real sync pass twice with zero unexpected changes.

### Milestone 4: One-Way Apple To Notion

- Create Notion rows for reminders in the chosen Apple test list.
- Set safe defaults for `Source`, `Area`, `Status`, and `Apple Sync ID`.
- Avoid touching existing `External ID` values.
- Verify repeated sync does not duplicate.

Done when: a new Apple reminder appears in Notion and a second sync is unchanged.

### Milestone 5: Bidirectional Updates

- Detect single-side updates.
- Update the opposite side.
- Suppress echo loops.
- Implement last-write-wins for simple conflicts.
- Record sync stats and errors.

Done when: title, notes, due date, priority, and completion changes propagate both ways across two repeated sync cycles.

### Milestone 6: Identity Recovery

- Simulate a lost Apple local identifier.
- Recover by external identifier or sync UUID plus fallback properties.
- Avoid false deletion.

Done when: identifier loss does not create duplicates or cancel the Notion row.

### Milestone 7: UI And Scheduling

- Add WebUI or menu-bar controls for Notion sync settings.
- Add dry-run/simulate button.
- Add manual sync button.
- Add scheduled sync support only after test gates pass.

Done when: the sync can be configured and run without editing config files.

## Testing Plan

Unit tests:

- Notion property parsing.
- Notion property update payload generation.
- Schema preflight failure modes.
- Priority mapping.
- Status/completion mapping.
- All-day due date handling.
- Europe/London DST date handling.
- Conflict resolution.
- Echo-loop suppression.
- Duplicate bootstrap matching.
- Apple identifier recovery.
- Deletion grace-period behavior.

Integration tests with mocked APIs:

- Notion-only create.
- Apple-only create.
- Both unchanged.
- Apple changed since last sync.
- Notion changed since last sync.
- Both changed since last sync.
- Apple missing for one run.
- Apple missing for two runs.
- Notion missing for one run.
- Notion cancelled.
- Notion schema missing identity property.
- Notion permission denied.
- Notion 429 with `Retry-After`.

Manual test protocol:

1. Create a separate Notion test data source or clearly filtered test rows.
2. Create a separate Apple Reminders list called `Notion Sync Test`.
3. Run preflight.
4. Run dry-run first.
5. Run one-way Notion -> Apple.
6. Run the same sync again and verify no duplicates.
7. Run one-way Apple -> Notion.
8. Run the same sync again and verify no duplicates.
9. Test completion in both directions.
10. Test due-date changes in both directions.
11. Test all-day dates around DST.
12. Test deletion/cancellation behavior with deletion handling disabled.
13. Test identity recovery.
14. Only then enable the real `Notion Sync` Apple list.

## Confidence Gates

The strategy graduates from design to production only when all gates pass:

- Gate A: schema preflight passes against the live Notion Tasks data source.
- Gate B: read-only proof lists the expected Notion rows and Apple reminders.
- Gate C: test-slice Notion -> Apple sync creates no duplicates across two runs.
- Gate D: test-slice Apple -> Notion sync creates no duplicates across two runs.
- Gate E: bidirectional edits converge to unchanged after a second sync.
- Gate F: completion status converges both ways.
- Gate G: all-day and timed due dates preserve the displayed date/time.
- Gate H: lost Apple identifier simulation does not duplicate or cancel a task.
- Gate I: Notion 429 retry handling is tested.
- Gate J: deletion/cancellation grace policy is tested with no hard deletes.

Until those pass, the app must default to dry-run or test-slice mode.

## First Concrete Next Step

Implement Milestone 0, not Milestone 1:

Create a schema/capability preflight command that checks the live Notion Tasks schema, verifies Apple Reminders access, and reports whether the required identity properties exist. Do not implement writes until preflight and a dedicated test slice are in place.
