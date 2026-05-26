from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from icloudbridge.core.notion_reminders_readonly import (
    ReadOnlyMatchReport,
    assert_expected_proof_plan,
    assert_proof_ready_plan,
    build_apple_snapshot,
    build_bidirectional_update_plan,
    build_deletion_grace_plan,
    build_identity_recovery_plan,
    build_notion_snapshot,
    build_proof_mutation,
    build_readonly_match_report,
    build_readonly_sync_plan,
    execute_create_apple_plan,
    execute_create_notion_plan,
    execute_deletion_grace_plan,
    execute_identity_recovery_plan,
    execute_production_baseline_plan,
    execute_production_create_notion_plan,
    execute_update_apple_plan,
    execute_update_notion_plan,
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


def test_build_proof_mutation_maps_each_field_for_notion_source():
    timed = build_proof_mutation("timed-due", "notion-to-apple")
    assert timed.expected_action == "UPDATE_APPLE"
    assert timed.notion_updates == {
        "title": None,
        "notes": None,
        "completed": None,
        "notion_priority": None,
        "due_date": datetime(2026, 6, 2, 9, 30, tzinfo=timezone(timedelta(hours=1))),
        "due_is_all_day": False,
    }

    all_day = build_proof_mutation("all-day-due", "notion-to-apple")
    assert all_day.notion_updates["due_date"] == datetime(2026, 6, 3, tzinfo=timezone.utc)
    assert all_day.notion_updates["due_is_all_day"] is True

    clear_due = build_proof_mutation("clear-due", "notion-to-apple")
    assert clear_due.notion_updates["due_date"] is None
    assert clear_due.notion_updates["due_is_all_day"] is False


def test_build_proof_mutation_maps_each_field_for_apple_source():
    priority = build_proof_mutation("priority", "apple-to-notion")
    assert priority.expected_action == "UPDATE_NOTION"
    assert priority.apple_updates == {
        "title": None,
        "notes": None,
        "completed": None,
        "priority": 1,
        "due_date": None,
        "is_all_day": False,
        "clear_due_date": False,
    }

    notes = build_proof_mutation("notes", "apple-to-notion")
    assert notes.apple_updates["notes"] == "Milestone 5D apple-to-notion notes proof"


def test_build_proof_mutation_rejects_unknown_field_and_direction():
    with pytest.raises(ValueError, match="Unsupported proof field"):
        build_proof_mutation("bad-field", "notion-to-apple")
    with pytest.raises(ValueError, match="Unsupported proof direction"):
        build_proof_mutation("notes", "sideways")


def test_assert_proof_ready_plan_requires_two_noops():
    task = _notion_task("Same title", sync_id="sync-1", reminder_id="apple-1")
    apple = _apple_reminder("Same title", "apple-1")
    plan = build_bidirectional_update_plan(
        build_readonly_match_report([task], [apple]),
        [_mapping_for(task, apple)],
    )

    with pytest.raises(ValueError, match="exactly 2 NOOP"):
        assert_proof_ready_plan(plan)

    second_task = _notion_task("Second", sync_id="sync-2", reminder_id="apple-2")
    second_apple = _apple_reminder("Second", "apple-2")
    ready_plan = build_bidirectional_update_plan(
        build_readonly_match_report([task, second_task], [apple, second_apple]),
        [_mapping_for(task, apple), _mapping_for(second_task, second_apple)],
    )
    assert_proof_ready_plan(ready_plan)


def test_assert_expected_proof_plan_requires_one_expected_update():
    base_task = _notion_task("[SYNC TEST] Same", sync_id="sync-1", reminder_id="apple-1")
    changed_task = _notion_task("[SYNC TEST] Changed", sync_id="sync-1", reminder_id="apple-1")
    apple = _apple_reminder("[SYNC TEST] Same", "apple-1")
    plan = build_bidirectional_update_plan(
        build_readonly_match_report([changed_task], [apple]),
        [_mapping_for(base_task, apple)],
    )

    action = assert_expected_proof_plan(plan, "UPDATE_APPLE")

    assert action.kind == "UPDATE_APPLE"
    with pytest.raises(ValueError, match="Expected exactly one UPDATE_NOTION"):
        assert_expected_proof_plan(plan, "UPDATE_NOTION")


def test_assert_expected_proof_plan_rejects_non_sync_test_rows():
    base_task = _notion_task("Same", sync_id="sync-1", reminder_id="apple-1")
    changed_task = _notion_task("Changed", sync_id="sync-1", reminder_id="apple-1")
    apple = _apple_reminder("Same", "apple-1")
    plan = build_bidirectional_update_plan(
        build_readonly_match_report([changed_task], [apple]),
        [_mapping_for(base_task, apple)],
    )

    with pytest.raises(ValueError, match=r"\[SYNC TEST\]"):
        assert_expected_proof_plan(plan, "UPDATE_APPLE")


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


def test_identity_recovery_plan_detects_stale_apple_reminder_id_by_title_and_snapshot():
    base_task = _notion_task(
        "[SYNC TEST] Notion to Apple",
        sync_id="sync-1",
        reminder_id="old-apple-id",
    )
    current_apple = _apple_reminder("[SYNC TEST] Notion to Apple", "new-apple-id")
    mapping = _mapping_for(base_task, current_apple)
    mapping["apple_reminder_id"] = "old-apple-id"

    plan = build_identity_recovery_plan([base_task], [current_apple], [mapping])

    assert plan.counts == {"NOOP": 0, "RECOVER_APPLE_ID": 1, "UNRECOVERED": 0}
    action = plan.actions[0]
    assert action.kind == "RECOVER_APPLE_ID"
    assert action.old_apple_reminder_id == "old-apple-id"
    assert action.new_apple_reminder_id == "new-apple-id"
    assert action.reason == "exact_title_single_candidate"


def test_identity_recovery_plan_refuses_ambiguous_title_candidates():
    task = _notion_task("[SYNC TEST] Duplicate", sync_id="sync-1", reminder_id="old")
    first = _apple_reminder("[SYNC TEST] Duplicate", "new-1")
    second = _apple_reminder("[SYNC TEST] Duplicate", "new-2")
    mapping = _mapping_for(task, first)
    mapping["apple_reminder_id"] = "old"

    plan = build_identity_recovery_plan([task], [first, second], [mapping])

    assert plan.counts == {"NOOP": 0, "RECOVER_APPLE_ID": 0, "UNRECOVERED": 1}
    assert plan.actions[0].kind == "UNRECOVERED"
    assert plan.actions[0].reason == "ambiguous_title_candidates"


def test_identity_recovery_plan_leaves_current_matches_as_noop():
    task = _notion_task("[SYNC TEST] Same", sync_id="sync-1", reminder_id="apple-1")
    apple = _apple_reminder("[SYNC TEST] Same", "apple-1")
    mapping = _mapping_for(task, apple)

    plan = build_identity_recovery_plan([task], [apple], [mapping])

    assert plan.counts == {"NOOP": 1, "RECOVER_APPLE_ID": 0, "UNRECOVERED": 0}


def test_deletion_grace_plan_reports_existing_mapped_rows_as_noop():
    task = _notion_task("[SYNC TEST] Same", sync_id="sync-1", reminder_id="apple-1")
    apple = _apple_reminder("[SYNC TEST] Same", "apple-1")

    plan = build_deletion_grace_plan([task], [apple], [_mapping_for(task, apple)])

    assert plan.counts["NOOP"] == 1
    assert plan.actions[0].kind == "NOOP"
    assert plan.actions[0].reason == "mapped_pair_present"


def test_deletion_grace_plan_detects_first_and_second_missing_apple_runs():
    task = _notion_task("[SYNC TEST] Same", sync_id="sync-1", reminder_id="apple-1")
    apple = _apple_reminder("[SYNC TEST] Same", "apple-1")
    mapping = _mapping_for(task, apple)

    first = build_deletion_grace_plan([task], [], [mapping])
    second = build_deletion_grace_plan(
        [task],
        [],
        [{**mapping, "missing_apple_seen_at": "2026-05-26T00:00:00+00:00"}],
    )

    assert first.actions[0].kind == "MISSING_APPLE_FIRST_SEEN"
    assert second.actions[0].kind == "MISSING_APPLE_STILL_MISSING"


def test_deletion_grace_plan_detects_first_and_second_missing_notion_runs():
    task = _notion_task("[SYNC TEST] Same", sync_id="sync-1", reminder_id="apple-1")
    apple = _apple_reminder("[SYNC TEST] Same", "apple-1")
    mapping = _mapping_for(task, apple)

    first = build_deletion_grace_plan([], [apple], [mapping])
    second = build_deletion_grace_plan(
        [],
        [apple],
        [{**mapping, "missing_notion_seen_at": "2026-05-26T00:00:00+00:00"}],
    )

    assert first.actions[0].kind == "MISSING_NOTION_FIRST_SEEN"
    assert second.actions[0].kind == "MISSING_NOTION_STILL_MISSING"


def test_deletion_grace_plan_resolved_mapping_returns_to_noop_with_marker():
    task = _notion_task("[SYNC TEST] Same", sync_id="sync-1", reminder_id="apple-1")
    apple = _apple_reminder("[SYNC TEST] Same", "apple-1")
    mapping = {
        **_mapping_for(task, apple),
        "missing_apple_seen_at": "2026-05-26T00:00:00+00:00",
    }

    plan = build_deletion_grace_plan([task], [apple], [mapping])

    assert plan.actions[0].kind == "NOOP"
    assert plan.actions[0].mapping["missing_apple_seen_at"] is not None


def test_deletion_grace_plan_reports_untracked_for_bad_or_absent_mappings():
    both_absent = {
        "apple_sync_id": "sync-1",
        "notion_page_id": "page-missing",
        "apple_reminder_id": "apple-missing",
    }
    missing_identity = {"apple_sync_id": "sync-2"}

    plan = build_deletion_grace_plan([], [], [both_absent, missing_identity])

    assert [action.kind for action in plan.actions] == ["UNTRACKED", "UNTRACKED"]
    assert {action.reason for action in plan.actions} == {
        "both_sides_absent",
        "missing_mapping_identity",
    }


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
        return {"id": page_id, "properties": {}}

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


class FakeRecoveryDB:
    def __init__(self):
        self.replacements = []
        self.snapshots = []

    async def replace_notion_mapping_apple_reminder_id(
        self,
        apple_sync_id,
        apple_reminder_id,
        apple_calendar_name,
        timestamp,
    ):
        self.replacements.append(
            (apple_sync_id, apple_reminder_id, apple_calendar_name, timestamp)
        )

    async def update_notion_reminder_snapshots(
        self,
        apple_sync_id,
        notion_snapshot,
        apple_snapshot,
        timestamp,
    ):
        self.snapshots.append((apple_sync_id, notion_snapshot, apple_snapshot, timestamp))


class FakeDeletionGraceDB:
    def __init__(self):
        self.missing_apple = []
        self.missing_notion = []
        self.cleared = []

    async def mark_notion_reminder_missing_apple(self, apple_sync_id, timestamp):
        self.missing_apple.append((apple_sync_id, timestamp))

    async def mark_notion_reminder_missing_notion(self, apple_sync_id, timestamp):
        self.missing_notion.append((apple_sync_id, timestamp))

    async def clear_notion_reminder_missing_markers(self, apple_sync_id, timestamp):
        self.cleared.append((apple_sync_id, timestamp))


class FakeRecoveryNotion:
    def __init__(self):
        self.updated_ids = []

    async def update_page_apple_reminder_id(self, page_id, apple_reminder_id):
        self.updated_ids.append((page_id, apple_reminder_id))
        return {"id": page_id, "properties": {}}


@pytest.mark.asyncio
async def test_execute_deletion_grace_plan_records_only_marker_updates():
    task = _notion_task("[SYNC TEST] Same", sync_id="sync-1", reminder_id="apple-1")
    apple = _apple_reminder("[SYNC TEST] Same", "apple-1")
    missing_apple_plan = build_deletion_grace_plan([task], [], [_mapping_for(task, apple)])
    missing_notion_plan = build_deletion_grace_plan([], [apple], [_mapping_for(task, apple)])
    resolved_plan = build_deletion_grace_plan(
        [task],
        [apple],
        [
            {
                **_mapping_for(task, apple),
                "missing_apple_seen_at": "2026-05-26T00:00:00+00:00",
            }
        ],
    )
    db = FakeDeletionGraceDB()

    missing_apple = await execute_deletion_grace_plan(
        missing_apple_plan,
        apple_calendar_name="Notion Sync Test",
        db=db,
    )
    missing_notion = await execute_deletion_grace_plan(
        missing_notion_plan,
        apple_calendar_name="Notion Sync Test",
        db=db,
    )
    resolved = await execute_deletion_grace_plan(
        resolved_plan,
        apple_calendar_name="Notion Sync Test",
        db=db,
    )

    assert missing_apple.marked_missing_apple == 1
    assert missing_notion.marked_missing_notion == 1
    assert resolved.cleared_markers == 1
    assert db.missing_apple[0][0] == "sync-1"
    assert db.missing_notion[0][0] == "sync-1"
    assert db.cleared[0][0] == "sync-1"


@pytest.mark.asyncio
async def test_execute_deletion_grace_plan_refuses_non_test_list_and_skips_second_runs():
    task = _notion_task("[SYNC TEST] Same", sync_id="sync-1", reminder_id="apple-1")
    apple = _apple_reminder("[SYNC TEST] Same", "apple-1")
    second_plan = build_deletion_grace_plan(
        [task],
        [],
        [
            {
                **_mapping_for(task, apple),
                "missing_apple_seen_at": "2026-05-26T00:00:00+00:00",
            }
        ],
    )
    db = FakeDeletionGraceDB()

    result = await execute_deletion_grace_plan(
        second_plan,
        apple_calendar_name="Notion Sync Test",
        db=db,
    )

    assert result.skipped == 1
    assert db.missing_apple == []
    with pytest.raises(ValueError, match="Notion Sync Test"):
        await execute_deletion_grace_plan(
            second_plan,
            apple_calendar_name="Real List",
            db=db,
        )


@pytest.mark.asyncio
async def test_execute_identity_recovery_plan_repairs_one_receipt_and_snapshots():
    task = _notion_task("[SYNC TEST] Notion to Apple", sync_id="sync-1", reminder_id="old")
    apple = _apple_reminder("[SYNC TEST] Notion to Apple", "new")
    mapping = _mapping_for(task, apple)
    mapping["apple_reminder_id"] = "old"
    plan = build_identity_recovery_plan([task], [apple], [mapping])
    notion = FakeRecoveryNotion()
    db = FakeRecoveryDB()

    result = await execute_identity_recovery_plan(
        plan,
        apple_calendar_name="Notion Sync Test",
        notion_adapter=notion,
        db=db,
    )

    assert result.recovered == 1
    assert result.skipped_non_recovery == 0
    assert notion.updated_ids == [(task.page_id, "new")]
    assert db.replacements[0][0:3] == ("sync-1", "new", "Notion Sync Test")
    assert db.snapshots[0][0] == "sync-1"


@pytest.mark.asyncio
async def test_execute_identity_recovery_plan_refuses_non_test_list_and_multiple_by_default():
    first = _notion_task("[SYNC TEST] One", sync_id="sync-1", reminder_id="old-1")
    second = _notion_task("[SYNC TEST] Two", sync_id="sync-2", reminder_id="old-2")
    first_apple = _apple_reminder("[SYNC TEST] One", "new-1")
    second_apple = _apple_reminder("[SYNC TEST] Two", "new-2")
    plan = build_identity_recovery_plan(
        [first, second],
        [first_apple, second_apple],
        [
            {**_mapping_for(first, first_apple), "apple_reminder_id": "old-1"},
            {**_mapping_for(second, second_apple), "apple_reminder_id": "old-2"},
        ],
    )

    with pytest.raises(ValueError, match="Notion Sync Test"):
        await execute_identity_recovery_plan(
            plan,
            apple_calendar_name="Real List",
            notion_adapter=FakeRecoveryNotion(),
            db=FakeRecoveryDB(),
        )
    with pytest.raises(ValueError, match="multiple"):
        await execute_identity_recovery_plan(
            plan,
            apple_calendar_name="Notion Sync Test",
            notion_adapter=FakeRecoveryNotion(),
            db=FakeRecoveryDB(),
        )


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
async def test_execute_production_create_notion_plan_uses_configured_area_and_cap():
    plan = build_readonly_sync_plan(
        build_readonly_match_report(
            notion_tasks=[],
            apple_reminders=[_apple_reminder("Academic task", "apple-1")],
        )
    )
    notion = FakeNotionAdapter()
    db = FakeNotionRemindersDB()

    result = await execute_production_create_notion_plan(
        plan,
        data_source_id="data-source-id",
        apple_calendar_name="Academic",
        notion_area="Academic",
        notion_adapter=notion,
        db=db,
        max_creates=5,
        sync_id_factory=lambda: "apple-reminders:fixed",
    )

    assert result.created_notion == 1
    assert notion.create_calls[0]["area"] == "Academic"
    assert db.mappings[0]["apple_calendar_name"] == "Academic"


@pytest.mark.asyncio
async def test_execute_production_create_notion_plan_refuses_over_cap():
    plan = build_readonly_sync_plan(
        build_readonly_match_report(
            notion_tasks=[],
            apple_reminders=[
                _apple_reminder("One", "apple-1"),
                _apple_reminder("Two", "apple-2"),
            ],
        )
    )

    with pytest.raises(ValueError, match="cap is 1"):
        await execute_production_create_notion_plan(
            plan,
            data_source_id="data-source-id",
            apple_calendar_name="Academic",
            notion_area="Academic",
            notion_adapter=FakeNotionAdapter(),
            db=FakeNotionRemindersDB(),
            max_creates=1,
        )


@pytest.mark.asyncio
async def test_execute_production_baseline_plan_writes_needs_baseline_snapshots():
    db = FakeNotionRemindersDB()
    task = _notion_task("Life task", sync_id="sync-1", reminder_id="apple-1")
    apple = _apple_reminder("Life task", "apple-1", notes="Apple note")
    noop_task = _notion_task("Stable", sync_id="sync-2", reminder_id="apple-2")
    noop_apple = _apple_reminder("Stable", "apple-2")
    plan = build_bidirectional_update_plan(
        build_readonly_match_report([task, noop_task], [apple, noop_apple]),
        [_mapping_for(noop_task, noop_apple)],
    )

    result = await execute_production_baseline_plan(plan, db=db)

    assert result.baselined == 1
    assert result.skipped_non_baseline == 1
    assert len(db.snapshots) == 1
    assert db.snapshots[0]["apple_sync_id"] == "sync-1"
    assert db.snapshots[0]["notion_snapshot"] == build_notion_snapshot(task)
    assert db.snapshots[0]["apple_snapshot"] == build_apple_snapshot(apple)


@pytest.mark.asyncio
async def test_execute_production_baseline_plan_ignores_non_baseline_actions():
    db = FakeNotionRemindersDB()
    task = _notion_task("Stable", sync_id="sync-1", reminder_id="apple-1")
    apple = _apple_reminder("Stable", "apple-1")
    plan = build_bidirectional_update_plan(
        build_readonly_match_report([task], [apple]),
        [_mapping_for(task, apple)],
    )

    result = await execute_production_baseline_plan(plan, db=db)

    assert result.baselined == 0
    assert result.skipped_non_baseline == 1
    assert db.snapshots == []


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
