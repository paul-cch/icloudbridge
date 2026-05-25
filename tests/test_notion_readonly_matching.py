from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from icloudbridge.core.notion_reminders_readonly import (
    ReadOnlyMatchReport,
    build_apple_snapshot,
    build_bidirectional_update_plan,
    build_notion_snapshot,
    execute_create_apple_plan,
    execute_create_notion_plan,
    execute_update_apple_plan,
    execute_update_notion_plan,
    build_readonly_match_report,
    build_readonly_sync_plan,
    map_apple_priority_to_notion,
    map_notion_priority_to_apple,
    snapshot_hash,
)
from icloudbridge.sources.reminders.notion_adapter import NotionTask


def _notion_task(
    title,
    sync_id=None,
    reminder_id=None,
    status="Not started",
    priority=None,
    notes=None,
    due_date=None,
    due_is_all_day=False,
):
    return NotionTask(
        page_id=f"page-{title}",
        title=title,
        notes=notes,
        completed=status in {"Done", "Cancelled"},
        status=status,
        priority=priority,
        due_date=due_date,
        due_is_all_day=due_is_all_day,
        reminder_at=None,
        last_edited_time=datetime.now(timezone.utc),
        apple_sync_id=sync_id,
        apple_reminder_id=reminder_id,
        url=None,
        raw_properties={},
    )


def _apple_reminder(title, uuid, completed=False, notes=None, due_date=None, is_all_day=False):
    return SimpleNamespace(
        uuid=uuid,
        title=title,
        notes=notes,
        completed=completed,
        priority=0,
        due_date=due_date,
        is_all_day=is_all_day,
    )


def test_build_readonly_match_report_matches_by_apple_reminder_id_first():
    report = build_readonly_match_report(
        notion_tasks=[_notion_task("Different title", reminder_id="apple-1")],
        apple_reminders=[_apple_reminder("Apple title", "apple-1")],
    )

    assert len(report.matched) == 1
    assert report.matched[0].reason == "apple_reminder_id"
    assert report.notion_only == []
    assert report.apple_only == []


def test_build_readonly_match_report_falls_back_to_exact_title():
    report = build_readonly_match_report(
        notion_tasks=[_notion_task("Same title", sync_id="sync-1")],
        apple_reminders=[_apple_reminder("Same title", "apple-1")],
    )

    assert len(report.matched) == 1
    assert report.matched[0].reason == "title"
    assert report.notion_only == []
    assert report.apple_only == []


def test_build_readonly_match_report_reports_unmatched_rows():
    report = build_readonly_match_report(
        notion_tasks=[_notion_task("Only Notion", sync_id="sync-1")],
        apple_reminders=[_apple_reminder("Only Apple", "apple-1")],
    )

    assert [task.title for task in report.notion_only] == ["Only Notion"]
    assert [reminder.title for reminder in report.apple_only] == ["Only Apple"]
    assert report.matched == []


def test_build_readonly_sync_plan_turns_matched_rows_into_noop():
    report = build_readonly_match_report(
        notion_tasks=[_notion_task("Same title", sync_id="sync-1")],
        apple_reminders=[_apple_reminder("Same title", "apple-1")],
    )

    plan = build_readonly_sync_plan(report)

    assert [action.kind for action in plan.actions] == ["NOOP"]
    assert plan.actions[0].direction == "both"
    assert plan.actions[0].title == "Same title"
    assert plan.counts == {"NOOP": 1, "CREATE_APPLE": 0, "CREATE_NOTION": 0}


def test_build_readonly_sync_plan_turns_notion_only_rows_into_create_apple():
    report = build_readonly_match_report(
        notion_tasks=[_notion_task("Only Notion", sync_id="sync-1")],
        apple_reminders=[],
    )

    plan = build_readonly_sync_plan(report)

    assert [action.kind for action in plan.actions] == ["CREATE_APPLE"]
    assert plan.actions[0].direction == "notion_to_apple"
    assert plan.actions[0].title == "Only Notion"
    assert plan.actions[0].source_id == "sync-1"
    assert plan.counts == {"NOOP": 0, "CREATE_APPLE": 1, "CREATE_NOTION": 0}


def test_build_readonly_sync_plan_turns_apple_only_reminders_into_create_notion():
    report = build_readonly_match_report(
        notion_tasks=[],
        apple_reminders=[_apple_reminder("Only Apple", "apple-1")],
    )

    plan = build_readonly_sync_plan(report)

    assert [action.kind for action in plan.actions] == ["CREATE_NOTION"]
    assert plan.actions[0].direction == "apple_to_notion"
    assert plan.actions[0].title == "Only Apple"
    assert plan.actions[0].source_id == "apple-1"
    assert plan.counts == {"NOOP": 0, "CREATE_APPLE": 0, "CREATE_NOTION": 1}


def test_build_readonly_sync_plan_preserves_stable_action_order():
    report = build_readonly_match_report(
        notion_tasks=[
            _notion_task("Same title", sync_id="sync-1"),
            _notion_task("Only Notion", sync_id="sync-2"),
        ],
        apple_reminders=[
            _apple_reminder("Same title", "apple-1"),
            _apple_reminder("Only Apple", "apple-2"),
        ],
    )

    plan = build_readonly_sync_plan(report)

    assert [action.kind for action in plan.actions] == [
        "NOOP",
        "CREATE_APPLE",
        "CREATE_NOTION",
    ]
    assert plan.counts == {"NOOP": 1, "CREATE_APPLE": 1, "CREATE_NOTION": 1}


def test_map_notion_priority_to_apple_is_conservative():
    assert map_notion_priority_to_apple(None) == 0
    assert map_notion_priority_to_apple("High") == 1
    assert map_notion_priority_to_apple("Medium") == 5
    assert map_notion_priority_to_apple("Low") == 9
    assert map_notion_priority_to_apple("Urgent") == 0


def test_snapshot_normalization_is_stable_and_preserves_due_shape():
    due = datetime(2026, 5, 26, 9, 30, tzinfo=timezone.utc)
    task = _notion_task(
        "Same title",
        notes="",
        status="Not started",
        priority="High",
        due_date=due,
        due_is_all_day=False,
    )
    reminder = _apple_reminder(
        "Same title",
        "apple-1",
        notes="",
        due_date=due,
        is_all_day=False,
    )
    reminder.priority = 1

    notion_snapshot = build_notion_snapshot(task)
    apple_snapshot = build_apple_snapshot(reminder)

    assert notion_snapshot["notes"] is None
    assert apple_snapshot["notes"] is None
    assert notion_snapshot["due_date"] == "2026-05-26T09:30:00+00:00"
    assert apple_snapshot["due_date"] == "2026-05-26T09:30:00+00:00"
    assert notion_snapshot["due_is_all_day"] is False
    assert snapshot_hash(notion_snapshot) == snapshot_hash(dict(reversed(notion_snapshot.items())))


def test_map_apple_priority_to_notion_is_conservative():
    assert map_apple_priority_to_notion(0) is None
    assert map_apple_priority_to_notion(1) == "High"
    assert map_apple_priority_to_notion(4) == "High"
    assert map_apple_priority_to_notion(5) == "Medium"
    assert map_apple_priority_to_notion(9) == "Low"


def _mapping_for(task, reminder, notion_snapshot=None, apple_snapshot=None):
    notion_snapshot = notion_snapshot if notion_snapshot is not None else build_notion_snapshot(task)
    apple_snapshot = apple_snapshot if apple_snapshot is not None else build_apple_snapshot(reminder)
    return {
        "apple_sync_id": task.apple_sync_id,
        "notion_page_id": task.page_id,
        "apple_reminder_id": reminder.uuid,
        "last_notion_snapshot_hash": snapshot_hash(notion_snapshot),
        "last_apple_snapshot_hash": snapshot_hash(apple_snapshot),
        "last_notion_snapshot_json": notion_snapshot,
        "last_apple_snapshot_json": apple_snapshot,
    }


def test_build_bidirectional_update_plan_requires_baseline_without_snapshot():
    task = _notion_task("Same title", sync_id="sync-1", reminder_id="apple-1")
    reminder = _apple_reminder("Same title", "apple-1")
    report = build_readonly_match_report([task], [reminder])

    plan = build_bidirectional_update_plan(report, [])

    assert [action.kind for action in plan.actions] == ["NEEDS_BASELINE"]
    assert plan.counts["NEEDS_BASELINE"] == 1


def test_build_bidirectional_update_plan_detects_noop_and_single_side_changes():
    base_task = _notion_task("Same title", sync_id="sync-1", reminder_id="apple-1")
    base_reminder = _apple_reminder("Same title", "apple-1")
    mapping = _mapping_for(base_task, base_reminder)

    noop_plan = build_bidirectional_update_plan(
        build_readonly_match_report([base_task], [base_reminder]),
        [mapping],
    )
    assert [action.kind for action in noop_plan.actions] == ["NOOP"]

    notion_changed = _notion_task(
        "Notion changed",
        sync_id="sync-1",
        reminder_id="apple-1",
    )
    update_apple_plan = build_bidirectional_update_plan(
        build_readonly_match_report([notion_changed], [base_reminder]),
        [mapping],
    )
    assert [action.kind for action in update_apple_plan.actions] == ["UPDATE_APPLE"]

    apple_changed = _apple_reminder("Apple changed", "apple-1")
    update_notion_plan = build_bidirectional_update_plan(
        build_readonly_match_report([base_task], [apple_changed]),
        [mapping],
    )
    assert [action.kind for action in update_notion_plan.actions] == ["UPDATE_NOTION"]


def test_build_bidirectional_update_plan_detects_conflict_and_stable_order():
    base_task = _notion_task("Same title", sync_id="sync-1", reminder_id="apple-1")
    base_reminder = _apple_reminder("Same title", "apple-1")
    changed_both_task = _notion_task("Notion changed", sync_id="sync-1", reminder_id="apple-1")
    changed_both_reminder = _apple_reminder("Apple changed", "apple-1")
    needs_baseline_task = _notion_task("Needs baseline", sync_id="sync-2", reminder_id="apple-2")
    needs_baseline_reminder = _apple_reminder("Needs baseline", "apple-2")

    report = build_readonly_match_report(
        [base_task, changed_both_task, needs_baseline_task],
        [base_reminder, changed_both_reminder, needs_baseline_reminder],
    )
    plan = build_bidirectional_update_plan(report, [_mapping_for(base_task, base_reminder)])

    assert [action.kind for action in plan.actions] == [
        "NOOP",
        "NEEDS_BASELINE",
        "CONFLICT",
    ]
    assert plan.counts["CONFLICT"] == 1


class FakeRemindersAdapter:
    def __init__(self):
        self.create_calls = []
        self.update_calls = []

    async def create_reminder(self, **kwargs):
        self.create_calls.append(kwargs)
        return SimpleNamespace(
            uuid="apple-created",
            title=kwargs["title"],
            notes=kwargs.get("notes"),
            completed=kwargs.get("completed", False),
            priority=kwargs.get("priority", 0),
            due_date=kwargs.get("due_date"),
            is_all_day=kwargs.get("is_all_day", False),
            modification_date=datetime.now(timezone.utc),
        )

    async def update_reminder(self, **kwargs):
        self.update_calls.append(kwargs)
        return SimpleNamespace(
            uuid=kwargs["uuid"],
            title=kwargs.get("title"),
            notes=kwargs.get("notes"),
            completed=kwargs.get("completed", False),
            priority=kwargs.get("priority", 0),
            due_date=kwargs.get("due_date"),
            is_all_day=kwargs.get("is_all_day", False),
            modification_date=datetime.now(timezone.utc),
        )


class FakeNotionAdapter:
    def __init__(self):
        self.updated_receipts = []
        self.create_calls = []
        self.update_from_apple_calls = []

    async def update_page_apple_reminder_id(self, page_id, apple_reminder_id):
        self.updated_receipts.append((page_id, apple_reminder_id))

    async def create_apple_origin_task(self, **kwargs):
        self.create_calls.append(kwargs)
        return {
            "id": "notion-created",
            "properties": {
                "Apple Sync ID": {
                    "type": "rich_text",
                    "rich_text": [{"plain_text": kwargs["apple_sync_id"]}],
                },
                "Apple Reminder ID": {
                    "type": "rich_text",
                    "rich_text": [{"plain_text": kwargs["apple_reminder_id"]}],
                },
            },
        }

    async def update_task_from_apple(self, **kwargs):
        self.update_from_apple_calls.append(kwargs)
        return {
            "id": kwargs["page_id"],
            "last_edited_time": "2026-05-25T13:30:00.000Z",
            "properties": {
                "Task Name": {
                    "type": "title",
                    "title": [{"plain_text": kwargs["title"]}],
                },
                "Notes": {
                    "type": "rich_text",
                    "rich_text": [{"plain_text": kwargs.get("notes") or ""}],
                },
                "Status": {
                    "type": "status",
                    "status": {"name": "Done" if kwargs.get("completed") else "Not started"},
                },
                "Priority": {
                    "type": "select",
                    "select": {"name": kwargs.get("notion_priority")},
                },
                "Apple Sync ID": {
                    "type": "rich_text",
                    "rich_text": [{"plain_text": "sync-1"}],
                },
                "Apple Reminder ID": {
                    "type": "rich_text",
                    "rich_text": [{"plain_text": "apple-1"}],
                },
            },
        }


class FakeNotionRemindersDB:
    def __init__(self):
        self.mappings = []
        self.snapshots = []

    async def upsert_notion_reminder_mapping(self, **kwargs):
        self.mappings.append(kwargs)

    async def update_notion_reminder_snapshots(self, **kwargs):
        self.snapshots.append(kwargs)


@pytest.mark.asyncio
async def test_execute_create_apple_plan_creates_one_reminder_and_persists_identity():
    db = FakeNotionRemindersDB()
    reminders = FakeRemindersAdapter()
    notion = FakeNotionAdapter()
    report = build_readonly_match_report(
        notion_tasks=[_notion_task("Only Notion", sync_id="sync-1", priority="High")],
        apple_reminders=[],
    )
    plan = build_readonly_sync_plan(report)

    result = await execute_create_apple_plan(
        plan,
        apple_calendar_name="Notion Sync Test",
        apple_calendar_id="calendar-id",
        reminders_adapter=reminders,
        notion_adapter=notion,
        db=db,
    )

    assert result.created_apple == 1
    assert reminders.create_calls == [
        {
            "calendar_id": "calendar-id",
            "title": "Only Notion",
            "notes": None,
            "completed": False,
            "priority": 1,
            "due_date": None,
            "is_all_day": False,
        }
    ]
    assert notion.updated_receipts == [("page-Only Notion", "apple-created")]
    assert len(db.mappings) == 1
    mapping = db.mappings[0]
    assert mapping["apple_sync_id"] == "sync-1"
    assert mapping["notion_page_id"] == "page-Only Notion"
    assert mapping["apple_reminder_id"] == "apple-created"
    assert mapping["apple_calendar_name"] == "Notion Sync Test"


@pytest.mark.asyncio
async def test_execute_create_apple_plan_skips_noop_and_create_notion_actions():
    db = FakeNotionRemindersDB()
    reminders = FakeRemindersAdapter()
    notion = FakeNotionAdapter()
    report = build_readonly_match_report(
        notion_tasks=[_notion_task("Same title", sync_id="sync-1")],
        apple_reminders=[
            _apple_reminder("Same title", "apple-1"),
            _apple_reminder("Only Apple", "apple-2"),
        ],
    )
    plan = build_readonly_sync_plan(report)

    result = await execute_create_apple_plan(
        plan,
        apple_calendar_name="Notion Sync Test",
        apple_calendar_id="calendar-id",
        reminders_adapter=reminders,
        notion_adapter=notion,
        db=db,
    )

    assert result.created_apple == 0
    assert reminders.create_calls == []
    assert notion.updated_receipts == []


@pytest.mark.asyncio
async def test_execute_create_apple_plan_refuses_non_test_list():
    with pytest.raises(ValueError, match="Notion Sync Test"):
        await execute_create_apple_plan(
            build_readonly_sync_plan(ReadOnlyMatchReport()),
            apple_calendar_name="Life",
            apple_calendar_id="calendar-id",
            reminders_adapter=FakeRemindersAdapter(),
            notion_adapter=FakeNotionAdapter(),
            db=FakeNotionRemindersDB(),
        )


@pytest.mark.asyncio
async def test_execute_create_apple_plan_refuses_multiple_creates_by_default():
    report = build_readonly_match_report(
        notion_tasks=[
            _notion_task("One", sync_id="sync-1"),
            _notion_task("Two", sync_id="sync-2"),
        ],
        apple_reminders=[],
    )

    with pytest.raises(ValueError, match="multiple CREATE_APPLE"):
        await execute_create_apple_plan(
            build_readonly_sync_plan(report),
            apple_calendar_name="Notion Sync Test",
            apple_calendar_id="calendar-id",
            reminders_adapter=FakeRemindersAdapter(),
            notion_adapter=FakeNotionAdapter(),
            db=FakeNotionRemindersDB(),
        )


@pytest.mark.asyncio
async def test_execute_create_notion_plan_creates_one_row_and_persists_identity():
    db = FakeNotionRemindersDB()
    notion = FakeNotionAdapter()
    report = build_readonly_match_report(
        notion_tasks=[],
        apple_reminders=[_apple_reminder("Only Apple", "apple-1", notes="From Apple")],
    )
    plan = build_readonly_sync_plan(report)

    result = await execute_create_notion_plan(
        plan,
        data_source_id="data-source-id",
        apple_calendar_name="Notion Sync Test",
        notion_adapter=notion,
        db=db,
        sync_id_factory=lambda: "apple-reminders:fixed",
    )

    assert result.created_notion == 1
    assert notion.create_calls == [
        {
            "data_source_id": "data-source-id",
            "title": "Only Apple",
            "notes": "From Apple",
            "apple_sync_id": "apple-reminders:fixed",
            "apple_reminder_id": "apple-1",
            "completed": False,
            "due_date": None,
            "due_is_all_day": False,
        }
    ]
    assert len(db.mappings) == 1
    mapping = db.mappings[0]
    assert mapping["apple_sync_id"] == "apple-reminders:fixed"
    assert mapping["notion_page_id"] == "notion-created"
    assert mapping["apple_reminder_id"] == "apple-1"
    assert mapping["apple_calendar_name"] == "Notion Sync Test"


@pytest.mark.asyncio
async def test_execute_create_notion_plan_skips_noop_and_create_apple_actions():
    db = FakeNotionRemindersDB()
    notion = FakeNotionAdapter()
    report = build_readonly_match_report(
        notion_tasks=[
            _notion_task("Same title", sync_id="sync-1"),
            _notion_task("Only Notion", sync_id="sync-2"),
        ],
        apple_reminders=[_apple_reminder("Same title", "apple-1")],
    )
    plan = build_readonly_sync_plan(report)

    result = await execute_create_notion_plan(
        plan,
        data_source_id="data-source-id",
        apple_calendar_name="Notion Sync Test",
        notion_adapter=notion,
        db=db,
        sync_id_factory=lambda: "apple-reminders:fixed",
    )

    assert result.created_notion == 0
    assert notion.create_calls == []
    assert db.mappings == []


@pytest.mark.asyncio
async def test_execute_create_notion_plan_refuses_non_test_list():
    with pytest.raises(ValueError, match="Notion Sync Test"):
        await execute_create_notion_plan(
            build_readonly_sync_plan(ReadOnlyMatchReport()),
            data_source_id="data-source-id",
            apple_calendar_name="Life",
            notion_adapter=FakeNotionAdapter(),
            db=FakeNotionRemindersDB(),
        )


@pytest.mark.asyncio
async def test_execute_create_notion_plan_refuses_multiple_creates_by_default():
    report = build_readonly_match_report(
        notion_tasks=[],
        apple_reminders=[
            _apple_reminder("One", "apple-1"),
            _apple_reminder("Two", "apple-2"),
        ],
    )

    with pytest.raises(ValueError, match="multiple CREATE_NOTION"):
        await execute_create_notion_plan(
            build_readonly_sync_plan(report),
            data_source_id="data-source-id",
            apple_calendar_name="Notion Sync Test",
            notion_adapter=FakeNotionAdapter(),
            db=FakeNotionRemindersDB(),
        )


@pytest.mark.asyncio
async def test_execute_update_apple_plan_updates_one_reminder_and_refreshes_snapshots():
    db = FakeNotionRemindersDB()
    reminders = FakeRemindersAdapter()
    task = _notion_task(
        "Notion changed",
        sync_id="sync-1",
        reminder_id="apple-1",
        notes="New note",
        status="Done",
        priority="High",
    )
    apple = _apple_reminder("Same title", "apple-1")
    base_task = _notion_task("Same title", sync_id="sync-1", reminder_id="apple-1")
    plan = build_bidirectional_update_plan(
        build_readonly_match_report([task], [apple]),
        [_mapping_for(base_task, apple)],
    )

    result = await execute_update_apple_plan(
        plan,
        apple_calendar_name="Notion Sync Test",
        reminders_adapter=reminders,
        db=db,
    )

    assert result.updated_apple == 1
    assert reminders.update_calls == [
        {
            "uuid": "apple-1",
            "title": "Notion changed",
            "notes": "New note",
            "completed": True,
            "priority": 1,
            "due_date": None,
            "is_all_day": False,
            "clear_due_date": True,
        }
    ]
    assert len(db.snapshots) == 1
    assert db.snapshots[0]["apple_sync_id"] == "sync-1"


@pytest.mark.asyncio
async def test_execute_update_apple_plan_skips_other_actions_and_refuses_non_test_list():
    db = FakeNotionRemindersDB()
    reminders = FakeRemindersAdapter()
    task = _notion_task("Same title", sync_id="sync-1", reminder_id="apple-1")
    apple = _apple_reminder("Apple changed", "apple-1")
    plan = build_bidirectional_update_plan(
        build_readonly_match_report([task], [apple]),
        [_mapping_for(task, _apple_reminder("Same title", "apple-1"))],
    )

    result = await execute_update_apple_plan(
        plan,
        apple_calendar_name="Notion Sync Test",
        reminders_adapter=reminders,
        db=db,
    )

    assert result.updated_apple == 0
    assert reminders.update_calls == []
    with pytest.raises(ValueError, match="Notion Sync Test"):
        await execute_update_apple_plan(
            plan,
            apple_calendar_name="Life",
            reminders_adapter=reminders,
            db=db,
        )


@pytest.mark.asyncio
async def test_execute_update_apple_plan_refuses_multiple_updates_by_default():
    base_one = _notion_task("One", sync_id="sync-1", reminder_id="apple-1")
    base_two = _notion_task("Two", sync_id="sync-2", reminder_id="apple-2")
    changed_one = _notion_task("One changed", sync_id="sync-1", reminder_id="apple-1")
    changed_two = _notion_task("Two changed", sync_id="sync-2", reminder_id="apple-2")
    apple_one = _apple_reminder("One", "apple-1")
    apple_two = _apple_reminder("Two", "apple-2")
    plan = build_bidirectional_update_plan(
        build_readonly_match_report([changed_one, changed_two], [apple_one, apple_two]),
        [_mapping_for(base_one, apple_one), _mapping_for(base_two, apple_two)],
    )

    with pytest.raises(ValueError, match="multiple UPDATE_APPLE"):
        await execute_update_apple_plan(
            plan,
            apple_calendar_name="Notion Sync Test",
            reminders_adapter=FakeRemindersAdapter(),
            db=FakeNotionRemindersDB(),
        )


@pytest.mark.asyncio
async def test_execute_update_notion_plan_updates_one_row_and_refreshes_snapshots():
    db = FakeNotionRemindersDB()
    notion = FakeNotionAdapter()
    task = _notion_task("Same title", sync_id="sync-1", reminder_id="apple-1")
    base_apple = _apple_reminder("Same title", "apple-1")
    changed_apple = _apple_reminder("Apple changed", "apple-1", completed=True, notes="Apple note")
    changed_apple.priority = 5
    plan = build_bidirectional_update_plan(
        build_readonly_match_report([task], [changed_apple]),
        [_mapping_for(task, base_apple)],
    )

    result = await execute_update_notion_plan(
        plan,
        apple_calendar_name="Notion Sync Test",
        notion_adapter=notion,
        db=db,
    )

    assert result.updated_notion == 1
    assert notion.update_from_apple_calls == [
        {
            "page_id": "page-Same title",
            "title": "Apple changed",
            "notes": "Apple note",
            "completed": True,
            "notion_priority": "Medium",
            "due_date": None,
            "due_is_all_day": False,
        }
    ]
    assert len(db.snapshots) == 1
    assert db.snapshots[0]["apple_sync_id"] == "sync-1"


@pytest.mark.asyncio
async def test_execute_update_notion_plan_skips_other_actions_and_refuses_non_test_list():
    db = FakeNotionRemindersDB()
    notion = FakeNotionAdapter()
    base_task = _notion_task("Same title", sync_id="sync-1", reminder_id="apple-1")
    changed_task = _notion_task("Notion changed", sync_id="sync-1", reminder_id="apple-1")
    apple = _apple_reminder("Same title", "apple-1")
    plan = build_bidirectional_update_plan(
        build_readonly_match_report([changed_task], [apple]),
        [_mapping_for(base_task, apple)],
    )

    result = await execute_update_notion_plan(
        plan,
        apple_calendar_name="Notion Sync Test",
        notion_adapter=notion,
        db=db,
    )

    assert result.updated_notion == 0
    assert notion.update_from_apple_calls == []
    with pytest.raises(ValueError, match="Notion Sync Test"):
        await execute_update_notion_plan(
            plan,
            apple_calendar_name="Life",
            notion_adapter=notion,
            db=db,
        )
