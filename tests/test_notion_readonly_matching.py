from datetime import datetime, timezone
from types import SimpleNamespace

from icloudbridge.core.notion_reminders_readonly import build_readonly_match_report
from icloudbridge.sources.reminders.notion_adapter import NotionTask


def _notion_task(title, sync_id=None, reminder_id=None):
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
