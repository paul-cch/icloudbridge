from datetime import datetime, timezone
from types import SimpleNamespace

from icloudbridge.core.notion_reminders_readonly import (
    build_readonly_match_report,
    build_readonly_sync_plan,
)
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
