from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from icloudbridge.core.notion_reminders_readonly import (
    ReadOnlyMatchReport,
    execute_create_apple_plan,
    build_readonly_match_report,
    build_readonly_sync_plan,
    map_notion_priority_to_apple,
)
from icloudbridge.sources.reminders.notion_adapter import NotionTask


def _notion_task(title, sync_id=None, reminder_id=None, status="Not started", priority=None):
    return NotionTask(
        page_id=f"page-{title}",
        title=title,
        notes=None,
        completed=status in {"Done", "Cancelled"},
        status=status,
        priority=priority,
        due_date=None,
        due_is_all_day=False,
        reminder_at=None,
        last_edited_time=datetime.now(timezone.utc),
        apple_sync_id=sync_id,
        apple_reminder_id=reminder_id,
        url=None,
        raw_properties={},
    )


def _apple_reminder(title, uuid):
    return SimpleNamespace(uuid=uuid, title=title, completed=False, due_date=None)


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


class FakeRemindersAdapter:
    def __init__(self):
        self.create_calls = []

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


class FakeNotionAdapter:
    def __init__(self):
        self.updated_receipts = []

    async def update_page_apple_reminder_id(self, page_id, apple_reminder_id):
        self.updated_receipts.append((page_id, apple_reminder_id))


class FakeNotionRemindersDB:
    def __init__(self):
        self.mappings = []

    async def upsert_notion_reminder_mapping(self, **kwargs):
        self.mappings.append(kwargs)


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
