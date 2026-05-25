# Notion Tasks <-> Apple Reminders Sync Plan

## Goal

Adapt iCloudBridge's existing Apple Reminders sync foundation into a local, two-way sync between Apple Reminders and Paul's Notion `Tasks` data source.

The target Notion data source is:

- Data source ID: `61ef2269-1dc6-4391-aaff-8013d2b857e3`
- Database ID: `d2ca9bc1-3b49-4f3f-b93e-bd33c11d6f19`
- Title: `Tasks`

The first production target should sync exactly one Apple Reminders list, for example `Notion Sync`, rather than every list on the machine.

## Current iCloudBridge Foundation

iCloudBridge already has the hard Apple-side pieces:

- `icloudbridge/sources/reminders/eventkit.py` reads and writes Apple Reminders through EventKit.
- `icloudbridge/core/reminders_sync.py` implements bidirectional sync planning, SQLite mappings, deletion controls, dry-run behavior, and last-write-wins conflict handling.
- `icloudbridge/sources/reminders/caldav_adapter.py` provides the current remote task backend, using CalDAV VTODO.

The Notion implementation should reuse the EventKit adapter and the sync-engine shape, while replacing the CalDAV remote adapter with a Notion Tasks adapter.

## Non-Goals For V1

- Do not sync all Notion tasks by default.
- Do not hard-delete tasks on either side.
- Do not sync arbitrary Notion databases.
- Do not support every Notion property in the first version.
- Do not mutate existing GitHub/dissertation/Moodle mirror keys in `External ID`.

## Data Model

Add a local SQLite mapping table for Notion sync, separate from the existing CalDAV mapping table.

Suggested columns:

- `apple_uuid TEXT PRIMARY KEY`
- `notion_page_id TEXT UNIQUE NOT NULL`
- `last_sync_timestamp REAL NOT NULL`
- `last_apple_modified TEXT`
- `last_notion_edited TEXT`
- `last_synced_hash TEXT`
- `apple_calendar_name TEXT`
- `notion_data_source_id TEXT`
- `created_at TEXT`
- `updated_at TEXT`

Add a dedicated Notion property rather than overloading `External ID`:

- `Apple Reminder ID` as rich text, or
- `Apple Reminder URL` as URL/rich text if a stable URL is useful.

`External ID` is already used by GitHub, dissertation, Moodle, email, TickTick/imported tasks, and should stay reserved for those source systems.

## Field Mapping

V1 mapping:

| Apple Reminders | Notion Tasks |
| --- | --- |
| title | `Task Name` |
| notes | `Notes` |
| completed | `Status = Done` when true; `Not started` or `In progress` when false |
| priority | `Priority` |
| due date | `Due Date` |
| calendar/list | optional `Tags` or fixed sync config |
| reminder UUID | `Apple Reminder ID` plus SQLite ledger |

Recommended Notion defaults for Apple-originated tasks:

- `Source = Manual` or a new `Source = Apple Reminders`
- `Area = Life` unless configured otherwise
- `Status = Not started`

## Notion Adapter

Create `icloudbridge/sources/reminders/notion_adapter.py`.

Responsibilities:

- Authenticate with `NOTION_API_TOKEN` or existing ntn/file-auth config if explicitly supported.
- Query a configured Notion data source with pagination.
- Convert Notion pages into a normalized remote reminder/task dataclass.
- Create Notion pages under the configured data source.
- Update Notion page properties.
- Mark Notion rows as done/cancelled rather than deleting them.
- Respect Notion request limits and `Retry-After`.

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
    last_edited_time: datetime
    apple_reminder_id: str | None
    url: str | None
    raw_properties: dict[str, Any]
```

Core methods:

```python
async def get_tasks(self) -> list[NotionTask]
async def create_task(self, task: NotionTaskCreate) -> NotionTask
async def update_task(self, page_id: str, patch: NotionTaskPatch) -> NotionTask
async def mark_cancelled(self, page_id: str) -> bool
```

## Sync Engine

Create a new engine rather than heavily parameterizing the CalDAV one at first:

- `icloudbridge/core/notion_reminders_sync.py`

It can borrow the same structure as `RemindersSyncEngine`:

1. Fetch Apple reminders from the configured Apple list.
2. Fetch Notion tasks from the configured data source and optional filter.
3. Build mappings from SQLite.
4. Bootstrap-match by `Apple Reminder ID`, then by title plus due date when safe.
5. Plan creates, updates, completions, and cancellations.
6. Execute dry-run or real sync.
7. Update the ledger.

Use last-write-wins initially, based on:

- Apple `modification_date`
- Notion `last_edited_time`
- SQLite `last_sync_timestamp`

For conflicts where both sides changed title or notes, prefer latest edit but append a short sync note to Notion preserving the losing value.

## Deletion Policy

V1 should avoid hard deletion.

- Apple reminder completed -> Notion `Status = Done`.
- Notion `Status = Done` -> Apple reminder completed.
- Apple reminder deleted -> Notion `Status = Cancelled`, unless `skip_deletions` is enabled.
- Notion row cancelled/archived -> Apple reminder completed or moved to a configured archive list.

Hard delete can be a later opt-in feature behind a destructive confirmation.

## Filtering Policy

Do not sync the entire Notion database by default.

V1 should support one of these filters:

- only rows with `Apple Reminder ID` present;
- only rows tagged `apple-reminders`;
- only rows with `Source = Apple Reminders`;
- only rows in selected areas, such as `Life` and `Academic`.

The safest default is:

- Apple -> Notion: sync all reminders in the chosen Apple list.
- Notion -> Apple: sync rows where `Apple Reminder ID` is present or `Source = Apple Reminders`.

## Configuration

Add config fields for:

- Notion token source
- Notion data source ID
- Apple Reminders list name
- Notion filter mode
- default `Area`
- default `Source`
- deletion behavior
- dry-run default

Do not store Notion tokens in plaintext project files. Use the platform keychain where possible, or an environment variable for development.

## Implementation Milestones

### Milestone 1: Read-Only Proof

- Add Notion adapter with schema-aware parsing.
- Read tasks from Paul's `Tasks` data source.
- Read reminders from one Apple list.
- Print a dry-run comparison without creating or updating anything.

Done when: a command can show matched/unmatched Apple and Notion tasks without writes.

### Milestone 2: One-Way Notion To Apple

- Create Apple reminders for selected Notion tasks.
- Update the local SQLite ledger.
- Preserve due dates, title, notes, completion state, and priority.

Done when: a dry-run and then real sync can create Apple reminders from a small Notion test filter.

### Milestone 3: One-Way Apple To Notion

- Create Notion rows for reminders in the chosen Apple list.
- Set safe defaults for `Source`, `Area`, `Status`, and `Apple Reminder ID`.
- Avoid touching existing `External ID` values.

Done when: a new Apple reminder appears in Notion without duplicating on the next sync.

### Milestone 4: Bidirectional Updates

- Detect single-side updates.
- Update the opposite side.
- Implement last-write-wins for simple conflicts.
- Record sync stats and errors.

Done when: title, notes, due date, priority, and completion changes propagate both ways.

### Milestone 5: UI And Scheduling

- Add WebUI or menu-bar controls for Notion sync settings.
- Add dry-run/simulate button.
- Add manual sync button.
- Add scheduled sync support.

Done when: the sync can be configured and run without editing config files.

## Testing Plan

Unit tests:

- Notion property parsing.
- Notion property update payload generation.
- Priority mapping.
- Status/completion mapping.
- All-day due date handling.
- Conflict resolution.
- Duplicate bootstrap matching.

Integration tests with mocked APIs:

- Notion-only create.
- Apple-only create.
- Both unchanged.
- Apple changed since last sync.
- Notion changed since last sync.
- Both changed since last sync.
- Apple deleted.
- Notion cancelled.

Manual test protocol:

1. Create a separate Notion test data source or filtered test rows.
2. Create a separate Apple Reminders list called `Notion Sync Test`.
3. Run dry-run first.
4. Run one-way Notion -> Apple.
5. Run one-way Apple -> Notion.
6. Test completion in both directions.
7. Test due-date changes in both directions.
8. Test deletion/cancellation behavior.

## Risks

- Notion `last_edited_time` changes for any property update, so sync writes can create echo loops unless the SQLite ledger records source timestamps and content hashes.
- Apple reminder IDs may change in some migration/account scenarios; the ledger plus `Apple Reminder ID` property should reduce this risk.
- Recurrence does not map naturally into the current Notion schema. V1 should preserve recurrence only on the Apple side or store it in notes/tags.
- Notion API rate limits require throttling.
- GPL-3.0 applies to derivative distribution.

## First Concrete Next Step

Create `notion_adapter.py` and a read-only CLI command that prints a normalized task list from Notion plus a normalized reminder list from a selected Apple Reminders calendar. Do not write to either system until this read-only proof is solid.
