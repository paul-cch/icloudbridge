from datetime import datetime, timezone
from types import SimpleNamespace

from icloudbridge.cli.main import _scope_test_slice_inputs
from icloudbridge.sources.reminders.notion_adapter import NotionTask


def _notion_task(title, sync_id="sync-id", reminder_id=None):
    return NotionTask(
        page_id=f"page-{title}",
        title=title,
        notes=None,
        completed=False,
        status="Not started",
        priority=None,
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
    return SimpleNamespace(
        uuid=uuid,
        title=title,
        notes=None,
        completed=False,
        priority=0,
        due_date=None,
        is_all_day=False,
    )


def test_notion_sync_test_scope_keeps_only_explicit_test_titles_and_mappings():
    notion_tasks = [
        _notion_task("[SYNC TEST] Apple to Notion", sync_id="test-sync"),
        _notion_task(
            "quoves add pics",
            sync_id="life-sync",
            reminder_id="life-apple-id",
        ),
    ]
    apple_reminders = [
        _apple_reminder("[SYNC TEST] Apple to Notion", "test-apple-id"),
        _apple_reminder("quoves add pics", "life-apple-id"),
    ]
    mappings = [
        {"apple_sync_id": "test-sync", "apple_calendar_name": "Notion Sync Test"},
        {"apple_sync_id": "life-sync", "apple_calendar_name": "Life"},
    ]

    scoped_tasks, scoped_reminders, scoped_mappings = _scope_test_slice_inputs(
        notion_tasks,
        apple_reminders,
        mappings,
        "Notion Sync Test",
    )

    assert [task.title for task in scoped_tasks] == ["[SYNC TEST] Apple to Notion"]
    assert [reminder.title for reminder in scoped_reminders] == [
        "[SYNC TEST] Apple to Notion"
    ]
    assert [mapping["apple_sync_id"] for mapping in scoped_mappings] == ["test-sync"]


def test_non_test_calendar_scope_is_passthrough():
    notion_tasks = [_notion_task("quoves add pics", sync_id="life-sync")]
    apple_reminders = [_apple_reminder("quoves add pics", "life-apple-id")]
    mappings = [{"apple_sync_id": "life-sync", "apple_calendar_name": "Life"}]

    scoped_tasks, scoped_reminders, scoped_mappings = _scope_test_slice_inputs(
        notion_tasks,
        apple_reminders,
        mappings,
        "Life",
    )

    assert scoped_tasks == notion_tasks
    assert scoped_reminders == apple_reminders
    assert scoped_mappings == mappings
