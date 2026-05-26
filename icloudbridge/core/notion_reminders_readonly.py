"""Read-only matching for Notion Tasks and Apple Reminders."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from icloudbridge.sources.reminders.notion_adapter import (
    NotionTask,
    page_apple_sync_id,
    parse_notion_task,
)


@dataclass
class ReadOnlyMatch:
    """A read-only candidate match between Notion and Apple."""

    notion_task: NotionTask
    apple_reminder: Any
    reason: str


@dataclass
class ReadOnlyMatchReport:
    """Buckets for a read-only sync comparison."""

    matched: list[ReadOnlyMatch] = field(default_factory=list)
    notion_only: list[NotionTask] = field(default_factory=list)
    apple_only: list[Any] = field(default_factory=list)


@dataclass
class PlannedAction:
    """A proposed dry-run sync action."""

    kind: str
    direction: str
    title: str
    status: str
    due_date: Any
    source_id: str
    notion_task: NotionTask | None = None
    apple_reminder: Any | None = None


@dataclass
class ReadOnlySyncPlan:
    """Dry-run action plan for Notion and Apple reminders."""

    actions: list[PlannedAction] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        """Count planned actions by kind."""
        return {
            "NOOP": sum(1 for action in self.actions if action.kind == "NOOP"),
            "CREATE_APPLE": sum(
                1 for action in self.actions if action.kind == "CREATE_APPLE"
            ),
            "CREATE_NOTION": sum(
                1 for action in self.actions if action.kind == "CREATE_NOTION"
            ),
        }


@dataclass
class UpdatePlannedAction:
    """A proposed matched-row update action."""

    kind: str
    direction: str
    title: str
    status: str
    due_date: Any
    source_id: str
    changed_fields: list[str] = field(default_factory=list)
    notion_task: NotionTask | None = None
    apple_reminder: Any | None = None
    mapping: dict[str, Any] | None = None


@dataclass
class BidirectionalUpdatePlan:
    """Dry-run update plan for already matched Notion and Apple reminders."""

    actions: list[UpdatePlannedAction] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        """Count planned update actions by kind."""
        return {
            "NOOP": sum(1 for action in self.actions if action.kind == "NOOP"),
            "NEEDS_BASELINE": sum(
                1 for action in self.actions if action.kind == "NEEDS_BASELINE"
            ),
            "UPDATE_APPLE": sum(
                1 for action in self.actions if action.kind == "UPDATE_APPLE"
            ),
            "UPDATE_NOTION": sum(
                1 for action in self.actions if action.kind == "UPDATE_NOTION"
            ),
            "CONFLICT": sum(1 for action in self.actions if action.kind == "CONFLICT"),
        }


@dataclass
class IdentityRecoveryAction:
    """A proposed Apple reminder identity repair for a Notion row."""

    kind: str
    title: str
    apple_sync_id: str
    notion_page_id: str
    old_apple_reminder_id: str | None
    new_apple_reminder_id: str | None = None
    reason: str = ""
    notion_task: NotionTask | None = None
    apple_reminder: Any | None = None
    mapping: dict[str, Any] | None = None


@dataclass
class IdentityRecoveryPlan:
    """Dry-run plan for repairing stale Apple reminder identity receipts."""

    actions: list[IdentityRecoveryAction] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        """Count planned identity recovery actions by kind."""
        return {
            "NOOP": sum(1 for action in self.actions if action.kind == "NOOP"),
            "RECOVER_APPLE_ID": sum(
                1 for action in self.actions if action.kind == "RECOVER_APPLE_ID"
            ),
            "UNRECOVERED": sum(
                1 for action in self.actions if action.kind == "UNRECOVERED"
            ),
        }


@dataclass
class DeletionGraceAction:
    """A detection-only missing-side action for deletion/cancellation grace."""

    kind: str
    title: str
    apple_sync_id: str | None
    notion_page_id: str | None
    apple_reminder_id: str | None
    reason: str = ""
    notion_task: NotionTask | None = None
    apple_reminder: Any | None = None
    mapping: dict[str, Any] | None = None


@dataclass
class DeletionGracePlan:
    """Detection-only plan for two-run missing-side grace handling."""

    actions: list[DeletionGraceAction] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        """Count deletion grace actions by kind."""
        return {
            "NOOP": sum(1 for action in self.actions if action.kind == "NOOP"),
            "MISSING_APPLE_FIRST_SEEN": sum(
                1 for action in self.actions if action.kind == "MISSING_APPLE_FIRST_SEEN"
            ),
            "MISSING_APPLE_STILL_MISSING": sum(
                1 for action in self.actions if action.kind == "MISSING_APPLE_STILL_MISSING"
            ),
            "MISSING_NOTION_FIRST_SEEN": sum(
                1 for action in self.actions if action.kind == "MISSING_NOTION_FIRST_SEEN"
            ),
            "MISSING_NOTION_STILL_MISSING": sum(
                1 for action in self.actions if action.kind == "MISSING_NOTION_STILL_MISSING"
            ),
            "UNTRACKED": sum(1 for action in self.actions if action.kind == "UNTRACKED"),
        }


@dataclass
class CreateAppleExecutionResult:
    """Stats from applying CREATE_APPLE actions."""

    created_apple: int = 0
    skipped_existing_receipt: int = 0
    skipped_non_create_apple: int = 0


@dataclass
class CreateNotionExecutionResult:
    """Stats from applying CREATE_NOTION actions."""

    created_notion: int = 0
    skipped_non_create_notion: int = 0


@dataclass
class UpdateAppleExecutionResult:
    """Stats from applying UPDATE_APPLE actions."""

    updated_apple: int = 0
    skipped_non_update_apple: int = 0


@dataclass
class UpdateNotionExecutionResult:
    """Stats from applying UPDATE_NOTION actions."""

    updated_notion: int = 0
    skipped_non_update_notion: int = 0


@dataclass
class BaselineExecutionResult:
    """Stats from writing snapshot baselines for matched rows."""

    baselined: int = 0
    skipped_non_baseline: int = 0


@dataclass
class IdentityRecoveryExecutionResult:
    """Stats from applying stale Apple reminder ID repairs."""

    recovered: int = 0
    skipped_non_recovery: int = 0


@dataclass
class DeletionGraceExecutionResult:
    """Stats from recording missing-side grace markers."""

    marked_missing_apple: int = 0
    marked_missing_notion: int = 0
    cleared_markers: int = 0
    skipped: int = 0


@dataclass
class ReceiptCleanupExecutionResult:
    """Stats from manually cleaning local mapping receipts."""

    deleted_receipts: int = 0
    skipped: int = 0


@dataclass
class ProofMutation:
    """Deterministic source-side mutation for a Milestone 5D proof run."""

    field: str
    direction: str
    expected_action: str
    summary: str
    notion_updates: dict[str, Any] = field(default_factory=dict)
    apple_updates: dict[str, Any] = field(default_factory=dict)
    restore_title: bool = False


class CreateApplePartialFailure(RuntimeError):
    """Raised after Apple creation succeeds but identity persistence fails."""


class CreateNotionPartialFailure(RuntimeError):
    """Raised after Notion creation succeeds but SQLite identity persistence fails."""


def map_notion_priority_to_apple(priority: str | None) -> int:
    """Map Notion priority names to conservative Apple priority values."""
    if priority == "High":
        return 1
    if priority == "Medium":
        return 5
    if priority == "Low":
        return 9
    return 0


def map_apple_priority_to_notion(priority: int | None) -> str | None:
    """Map Apple priority values to existing Notion priority names."""
    if priority is None or priority == 0:
        return None
    if 1 <= priority <= 4:
        return "High"
    if priority == 5:
        return "Medium"
    if 6 <= priority <= 9:
        return "Low"
    return None


def build_proof_mutation(field: str, direction: str) -> ProofMutation:
    """Build deterministic test-slice proof mutation payloads."""
    if field not in {
        "title",
        "notes",
        "completion",
        "priority",
        "timed-due",
        "all-day-due",
        "clear-due",
    }:
        raise ValueError(f"Unsupported proof field: {field}")
    if direction not in {"notion-to-apple", "apple-to-notion"}:
        raise ValueError(f"Unsupported proof direction: {direction}")

    expected_action = "UPDATE_APPLE" if direction == "notion-to-apple" else "UPDATE_NOTION"
    notion_updates = {
        "title": None,
        "notes": None,
        "completed": None,
        "notion_priority": None,
        "due_date": None,
        "due_is_all_day": False,
    }
    apple_updates = {
        "title": None,
        "notes": None,
        "completed": None,
        "priority": None,
        "due_date": None,
        "is_all_day": False,
        "clear_due_date": False,
    }

    if field == "title":
        value = f"[SYNC TEST] {direction} title proof"
        notion_updates["title"] = value
        apple_updates["title"] = value
    elif field == "notes":
        value = f"Milestone 5D {direction} notes proof"
        notion_updates["notes"] = value
        apple_updates["notes"] = value
    elif field == "completion":
        notion_updates["completed"] = True
        apple_updates["completed"] = True
    elif field == "priority":
        notion_updates["notion_priority"] = "High"
        apple_updates["priority"] = 1
    elif field == "timed-due":
        value = datetime(2026, 6, 2, 9, 30, tzinfo=timezone(timedelta(hours=1)))
        notion_updates["due_date"] = value
        apple_updates["due_date"] = value
    elif field == "all-day-due":
        value = datetime(2026, 6, 3, tzinfo=timezone.utc)
        notion_updates["due_date"] = value
        notion_updates["due_is_all_day"] = True
        apple_updates["due_date"] = value
        apple_updates["is_all_day"] = True
    elif field == "clear-due":
        apple_updates["clear_due_date"] = True

    return ProofMutation(
        field=field,
        direction=direction,
        expected_action=expected_action,
        summary=f"{direction} {field} proof",
        notion_updates=notion_updates,
        apple_updates=apple_updates,
        restore_title=field == "title",
    )


def assert_proof_ready_plan(plan: BidirectionalUpdatePlan) -> None:
    """Require the proof slice to start from two unchanged matched rows."""
    if plan.counts != {
        "NOOP": 2,
        "NEEDS_BASELINE": 0,
        "UPDATE_APPLE": 0,
        "UPDATE_NOTION": 0,
        "CONFLICT": 0,
    }:
        raise ValueError(f"Proof requires exactly 2 NOOP rows, got {plan.counts}")


def assert_expected_proof_plan(
    plan: BidirectionalUpdatePlan,
    expected_action: str,
) -> UpdatePlannedAction:
    """Require exactly one expected update action against a sync-test row."""
    matching = [action for action in plan.actions if action.kind == expected_action]
    if len(matching) != 1:
        raise ValueError(f"Expected exactly one {expected_action}, got {plan.counts}")
    if plan.counts["NEEDS_BASELINE"] or plan.counts["CONFLICT"]:
        raise ValueError(f"Proof cannot continue with baseline/conflict actions: {plan.counts}")
    action = matching[0]
    title = action.title or ""
    apple_title = getattr(action.apple_reminder, "title", "") if action.apple_reminder else ""
    if not title.startswith("[SYNC TEST]") and not apple_title.startswith("[SYNC TEST]"):
        raise ValueError("Proof may only mutate [SYNC TEST] rows")
    return action


def _normalized_notes(value: str | None) -> str | None:
    return value if value else None


def _normalized_due(value: datetime | None, is_all_day: bool) -> str | None:
    if value is None:
        return None
    if is_all_day:
        return value.date().isoformat()
    return value.isoformat()


def build_notion_snapshot(task: NotionTask) -> dict[str, Any]:
    """Build the normalized Notion-side snapshot used for update detection."""
    return {
        "title": task.title,
        "notes": _normalized_notes(task.notes),
        "completed": task.completed,
        "status": task.status,
        "priority": task.priority,
        "apple_priority": map_notion_priority_to_apple(task.priority),
        "due_date": _normalized_due(task.due_date, task.due_is_all_day),
        "due_is_all_day": task.due_is_all_day,
    }


def build_apple_snapshot(reminder: Any) -> dict[str, Any]:
    """Build the normalized Apple-side snapshot used for update detection."""
    return {
        "title": getattr(reminder, "title", ""),
        "notes": _normalized_notes(getattr(reminder, "notes", None)),
        "completed": getattr(reminder, "completed", False),
        "priority": getattr(reminder, "priority", 0),
        "notion_priority": map_apple_priority_to_notion(
            getattr(reminder, "priority", 0)
        ),
        "due_date": _normalized_due(
            getattr(reminder, "due_date", None),
            getattr(reminder, "is_all_day", False),
        ),
        "due_is_all_day": getattr(reminder, "is_all_day", False),
    }


def snapshot_hash(snapshot: dict[str, Any]) -> str:
    """Return a stable hash for a normalized snapshot."""
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _snapshot_from_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _changed_fields(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
) -> list[str]:
    if previous is None:
        return []
    return [key for key in current if current.get(key) != previous.get(key)]


def build_bidirectional_update_plan(
    match_report: ReadOnlyMatchReport,
    mappings: list[dict[str, Any]],
) -> BidirectionalUpdatePlan:
    """Build a read-only update plan for matched Notion/Apple pairs."""
    by_apple_id = {mapping.get("apple_reminder_id"): mapping for mapping in mappings}
    by_sync_id = {mapping.get("apple_sync_id"): mapping for mapping in mappings}
    actions: list[UpdatePlannedAction] = []

    for match in match_report.matched:
        task = match.notion_task
        reminder = match.apple_reminder
        mapping = by_apple_id.get(getattr(reminder, "uuid", None)) or by_sync_id.get(
            task.apple_sync_id
        )
        notion_snapshot = build_notion_snapshot(task)
        apple_snapshot = build_apple_snapshot(reminder)
        source_id = task.apple_sync_id or getattr(reminder, "uuid", "")

        base_action = {
            "title": task.title,
            "status": task.status,
            "due_date": task.due_date,
            "source_id": source_id,
            "notion_task": task,
            "apple_reminder": reminder,
            "mapping": mapping,
        }

        if not mapping or not mapping.get("last_notion_snapshot_hash") or not mapping.get(
            "last_apple_snapshot_hash"
        ):
            actions.append(
                UpdatePlannedAction(
                    kind="NEEDS_BASELINE",
                    direction="baseline",
                    **base_action,
                )
            )
            continue

        notion_changed = snapshot_hash(notion_snapshot) != mapping.get(
            "last_notion_snapshot_hash"
        )
        apple_changed = snapshot_hash(apple_snapshot) != mapping.get(
            "last_apple_snapshot_hash"
        )
        previous_notion = _snapshot_from_mapping(mapping.get("last_notion_snapshot_json"))
        previous_apple = _snapshot_from_mapping(mapping.get("last_apple_snapshot_json"))
        changed_fields = sorted(
            set(_changed_fields(notion_snapshot, previous_notion))
            | set(_changed_fields(apple_snapshot, previous_apple))
        )

        if notion_changed and apple_changed:
            kind = "CONFLICT"
            direction = "both_changed"
        elif notion_changed:
            kind = "UPDATE_APPLE"
            direction = "notion_to_apple"
        elif apple_changed:
            kind = "UPDATE_NOTION"
            direction = "apple_to_notion"
        else:
            kind = "NOOP"
            direction = "both"
            changed_fields = []

        actions.append(
            UpdatePlannedAction(
                kind=kind,
                direction=direction,
                changed_fields=changed_fields,
                **base_action,
            )
        )

    order = {
        "NOOP": 0,
        "NEEDS_BASELINE": 1,
        "UPDATE_APPLE": 2,
        "UPDATE_NOTION": 3,
        "CONFLICT": 4,
    }
    actions.sort(key=lambda action: (order[action.kind], action.title, action.source_id))
    return BidirectionalUpdatePlan(actions=actions)


def _same_recovery_title(task: NotionTask, reminder: Any) -> bool:
    return task.title == getattr(reminder, "title", None)


def build_identity_recovery_plan(
    notion_tasks: list[NotionTask],
    apple_reminders: list[Any],
    mappings: list[dict[str, Any]],
) -> IdentityRecoveryPlan:
    """Find stale Apple Reminder ID receipts without proposing content changes."""
    by_sync_id = {mapping.get("apple_sync_id"): mapping for mapping in mappings}
    apple_by_id = {
        getattr(reminder, "uuid", None): reminder for reminder in apple_reminders
    }
    actions: list[IdentityRecoveryAction] = []

    for task in notion_tasks:
        apple_sync_id = task.apple_sync_id or task.page_id
        mapping = by_sync_id.get(apple_sync_id)
        old_id = task.apple_reminder_id or (mapping or {}).get("apple_reminder_id")
        if not old_id:
            actions.append(
                IdentityRecoveryAction(
                    kind="UNRECOVERED",
                    title=task.title,
                    apple_sync_id=apple_sync_id,
                    notion_page_id=task.page_id,
                    old_apple_reminder_id=None,
                    reason="missing_identity_receipt",
                    notion_task=task,
                    mapping=mapping,
                )
            )
            continue

        current = apple_by_id.get(old_id)
        if current is not None:
            actions.append(
                IdentityRecoveryAction(
                    kind="NOOP",
                    title=task.title,
                    apple_sync_id=apple_sync_id,
                    notion_page_id=task.page_id,
                    old_apple_reminder_id=old_id,
                    new_apple_reminder_id=old_id,
                    reason="apple_id_current",
                    notion_task=task,
                    apple_reminder=current,
                    mapping=mapping,
                )
            )
            continue

        candidates = [
            reminder
            for reminder in apple_reminders
            if _same_recovery_title(task, reminder)
        ]
        if len(candidates) == 1:
            recovered = candidates[0]
            actions.append(
                IdentityRecoveryAction(
                    kind="RECOVER_APPLE_ID",
                    title=task.title,
                    apple_sync_id=apple_sync_id,
                    notion_page_id=task.page_id,
                    old_apple_reminder_id=old_id,
                    new_apple_reminder_id=getattr(recovered, "uuid", None),
                    reason="exact_title_single_candidate",
                    notion_task=task,
                    apple_reminder=recovered,
                    mapping=mapping,
                )
            )
        else:
            actions.append(
                IdentityRecoveryAction(
                    kind="UNRECOVERED",
                    title=task.title,
                    apple_sync_id=apple_sync_id,
                    notion_page_id=task.page_id,
                    old_apple_reminder_id=old_id,
                    reason="ambiguous_title_candidates" if candidates else "no_candidate",
                    notion_task=task,
                    mapping=mapping,
                )
            )

    order = {"NOOP": 0, "RECOVER_APPLE_ID": 1, "UNRECOVERED": 2}
    actions.sort(key=lambda action: (order[action.kind], action.title, action.apple_sync_id))
    return IdentityRecoveryPlan(actions=actions)


def build_deletion_grace_plan(
    notion_tasks: list[NotionTask],
    apple_reminders: list[Any],
    mappings: list[dict[str, Any]],
) -> DeletionGracePlan:
    """Build a detection-only two-run grace plan for mapped missing items."""
    notion_by_page_id = {task.page_id: task for task in notion_tasks}
    notion_by_sync_id = {
        task.apple_sync_id: task for task in notion_tasks if task.apple_sync_id
    }
    apple_by_id = {
        getattr(reminder, "uuid", None): reminder for reminder in apple_reminders
    }
    actions: list[DeletionGraceAction] = []

    for mapping in mappings:
        apple_sync_id = mapping.get("apple_sync_id")
        notion_page_id = mapping.get("notion_page_id")
        apple_reminder_id = mapping.get("apple_reminder_id")
        if not apple_sync_id or not notion_page_id or not apple_reminder_id:
            actions.append(
                DeletionGraceAction(
                    kind="UNTRACKED",
                    title="",
                    apple_sync_id=apple_sync_id,
                    notion_page_id=notion_page_id,
                    apple_reminder_id=apple_reminder_id,
                    reason="missing_mapping_identity",
                    mapping=mapping,
                )
            )
            continue

        task = notion_by_page_id.get(notion_page_id) or notion_by_sync_id.get(apple_sync_id)
        reminder = apple_by_id.get(apple_reminder_id)
        title = (
            getattr(task, "title", None)
            or getattr(reminder, "title", None)
            or mapping.get("title")
            or apple_sync_id
        )

        if task is not None and reminder is not None:
            actions.append(
                DeletionGraceAction(
                    kind="NOOP",
                    title=title,
                    apple_sync_id=apple_sync_id,
                    notion_page_id=notion_page_id,
                    apple_reminder_id=apple_reminder_id,
                    reason="mapped_pair_present",
                    notion_task=task,
                    apple_reminder=reminder,
                    mapping=mapping,
                )
            )
        elif task is not None:
            kind = (
                "MISSING_APPLE_STILL_MISSING"
                if mapping.get("missing_apple_seen_at")
                else "MISSING_APPLE_FIRST_SEEN"
            )
            actions.append(
                DeletionGraceAction(
                    kind=kind,
                    title=title,
                    apple_sync_id=apple_sync_id,
                    notion_page_id=notion_page_id,
                    apple_reminder_id=apple_reminder_id,
                    reason="mapped_apple_absent",
                    notion_task=task,
                    mapping=mapping,
                )
            )
        elif reminder is not None:
            kind = (
                "MISSING_NOTION_STILL_MISSING"
                if mapping.get("missing_notion_seen_at")
                else "MISSING_NOTION_FIRST_SEEN"
            )
            actions.append(
                DeletionGraceAction(
                    kind=kind,
                    title=title,
                    apple_sync_id=apple_sync_id,
                    notion_page_id=notion_page_id,
                    apple_reminder_id=apple_reminder_id,
                    reason="mapped_notion_absent",
                    apple_reminder=reminder,
                    mapping=mapping,
                )
            )
        else:
            actions.append(
                DeletionGraceAction(
                    kind="UNTRACKED",
                    title=title,
                    apple_sync_id=apple_sync_id,
                    notion_page_id=notion_page_id,
                    apple_reminder_id=apple_reminder_id,
                    reason="both_sides_absent",
                    mapping=mapping,
                )
            )

    order = {
        "NOOP": 0,
        "MISSING_APPLE_FIRST_SEEN": 1,
        "MISSING_APPLE_STILL_MISSING": 2,
        "MISSING_NOTION_FIRST_SEEN": 3,
        "MISSING_NOTION_STILL_MISSING": 4,
        "UNTRACKED": 5,
    }
    actions.sort(
        key=lambda action: (
            order[action.kind],
            action.title,
            action.apple_sync_id or "",
        )
    )
    return DeletionGracePlan(actions=actions)


def build_readonly_match_report(
    notion_tasks: list[NotionTask],
    apple_reminders: list[Any],
) -> ReadOnlyMatchReport:
    """Match enrolled Notion rows to Apple reminders without mutating either side."""
    report = ReadOnlyMatchReport()
    remaining_apple = list(apple_reminders)

    for task in notion_tasks:
        match = None
        reason = ""

        if task.apple_reminder_id:
            match = next(
                (
                    reminder
                    for reminder in remaining_apple
                    if getattr(reminder, "uuid", None) == task.apple_reminder_id
                ),
                None,
            )
            if match is not None:
                reason = "apple_reminder_id"

        if match is None and task.title:
            matching_titles = [
                reminder
                for reminder in remaining_apple
                if getattr(reminder, "title", None) == task.title
            ]
            if len(matching_titles) == 1:
                match = matching_titles[0]
                reason = "title"

        if match is None:
            report.notion_only.append(task)
            continue

        report.matched.append(ReadOnlyMatch(task, match, reason))
        remaining_apple.remove(match)

    report.apple_only = remaining_apple
    return report


def build_readonly_sync_plan(match_report: ReadOnlyMatchReport) -> ReadOnlySyncPlan:
    """Build a dry-run plan from read-only match buckets."""
    actions: list[PlannedAction] = []

    for match in match_report.matched:
        actions.append(
            PlannedAction(
                kind="NOOP",
                direction="both",
                title=match.notion_task.title,
                status=match.notion_task.status,
                due_date=match.notion_task.due_date,
                source_id=match.notion_task.apple_reminder_id
                or getattr(match.apple_reminder, "uuid", ""),
                notion_task=match.notion_task,
                apple_reminder=match.apple_reminder,
            )
        )

    for task in match_report.notion_only:
        actions.append(
            PlannedAction(
                kind="CREATE_APPLE",
                direction="notion_to_apple",
                title=task.title,
                status=task.status,
                due_date=task.due_date,
                source_id=task.apple_sync_id or task.page_id,
                notion_task=task,
            )
        )

    for reminder in match_report.apple_only:
        actions.append(
            PlannedAction(
                kind="CREATE_NOTION",
                direction="apple_to_notion",
                title=getattr(reminder, "title", ""),
                status="completed" if getattr(reminder, "completed", False) else "open",
                due_date=getattr(reminder, "due_date", None),
                source_id=getattr(reminder, "uuid", ""),
                apple_reminder=reminder,
            )
        )

    return ReadOnlySyncPlan(actions=actions)


async def execute_create_apple_plan(
    plan: ReadOnlySyncPlan,
    apple_calendar_name: str,
    apple_calendar_id: str,
    reminders_adapter: Any,
    notion_adapter: Any,
    db: Any,
    allow_multiple_test_creates: bool = False,
) -> CreateAppleExecutionResult:
    """Execute only CREATE_APPLE actions for the dedicated Notion test slice."""
    if apple_calendar_name != "Notion Sync Test":
        raise ValueError("Milestone 3 can only write to the 'Notion Sync Test' list")

    create_actions = [action for action in plan.actions if action.kind == "CREATE_APPLE"]
    if len(create_actions) > 1 and not allow_multiple_test_creates:
        raise ValueError("Refusing multiple CREATE_APPLE actions without explicit opt-in")

    result = CreateAppleExecutionResult(
        skipped_non_create_apple=len(plan.actions) - len(create_actions)
    )

    for action in create_actions:
        task = action.notion_task
        if task is None:
            continue
        if task.apple_reminder_id:
            result.skipped_existing_receipt += 1
            continue

        created = await reminders_adapter.create_reminder(
            calendar_id=apple_calendar_id,
            title=task.title,
            notes=task.notes,
            completed=task.completed,
            priority=map_notion_priority_to_apple(task.priority),
            due_date=task.due_date,
            is_all_day=task.due_is_all_day,
        )

        try:
            await notion_adapter.update_page_apple_reminder_id(task.page_id, created.uuid)
            await db.upsert_notion_reminder_mapping(
                apple_sync_id=task.apple_sync_id or task.page_id,
                notion_page_id=task.page_id,
                apple_reminder_id=created.uuid,
                apple_calendar_name=apple_calendar_name,
                timestamp=datetime.now(timezone.utc),
            )
        except Exception as exc:
            raise CreateApplePartialFailure(
                "Apple reminder was created, but Notion/SQLite identity persistence failed. "
                "Rerun notion-plan before applying again."
            ) from exc

        result.created_apple += 1

    return result


async def execute_create_notion_plan(
    plan: ReadOnlySyncPlan,
    data_source_id: str,
    apple_calendar_name: str,
    notion_adapter: Any,
    db: Any,
    allow_multiple_test_creates: bool = False,
    sync_id_factory: Any | None = None,
) -> CreateNotionExecutionResult:
    """Execute only CREATE_NOTION actions for the dedicated Notion test slice."""
    if apple_calendar_name != "Notion Sync Test":
        raise ValueError("Milestone 4 can only create Notion rows from 'Notion Sync Test'")

    create_actions = [action for action in plan.actions if action.kind == "CREATE_NOTION"]
    if len(create_actions) > 1 and not allow_multiple_test_creates:
        raise ValueError("Refusing multiple CREATE_NOTION actions without explicit opt-in")

    result = CreateNotionExecutionResult(
        skipped_non_create_notion=len(plan.actions) - len(create_actions)
    )

    for action in create_actions:
        reminder = action.apple_reminder
        if reminder is None:
            continue

        apple_sync_id = (
            sync_id_factory() if sync_id_factory else f"apple-reminders:{uuid4()}"
        )
        created = await notion_adapter.create_apple_origin_task(
            data_source_id=data_source_id,
            title=getattr(reminder, "title", ""),
            notes=getattr(reminder, "notes", None),
            apple_sync_id=apple_sync_id,
            apple_reminder_id=getattr(reminder, "uuid", ""),
            completed=getattr(reminder, "completed", False),
            due_date=getattr(reminder, "due_date", None),
            due_is_all_day=getattr(reminder, "is_all_day", False),
        )

        try:
            await db.upsert_notion_reminder_mapping(
                apple_sync_id=page_apple_sync_id(created) or apple_sync_id,
                notion_page_id=created["id"],
                apple_reminder_id=getattr(reminder, "uuid", ""),
                apple_calendar_name=apple_calendar_name,
                timestamp=datetime.now(timezone.utc),
            )
        except Exception as exc:
            raise CreateNotionPartialFailure(
                "Notion row was created, but SQLite identity persistence failed. "
                "Rerun notion-plan before applying again."
            ) from exc

        result.created_notion += 1

    return result


async def execute_production_create_notion_plan(
    plan: ReadOnlySyncPlan,
    data_source_id: str,
    apple_calendar_name: str,
    notion_area: str,
    notion_adapter: Any,
    db: Any,
    max_creates: int,
    sync_id_factory: Any | None = None,
) -> CreateNotionExecutionResult:
    """Execute capped CREATE_NOTION actions for an allowlisted production list."""
    create_actions = [action for action in plan.actions if action.kind == "CREATE_NOTION"]
    if len(create_actions) > max_creates:
        raise ValueError(
            f"Refusing {len(create_actions)} CREATE_NOTION actions; cap is {max_creates}"
        )

    result = CreateNotionExecutionResult(
        skipped_non_create_notion=len(plan.actions) - len(create_actions)
    )

    for action in create_actions:
        reminder = action.apple_reminder
        if reminder is None:
            continue

        apple_sync_id = (
            sync_id_factory() if sync_id_factory else f"apple-reminders:{uuid4()}"
        )
        created = await notion_adapter.create_apple_origin_task(
            data_source_id=data_source_id,
            title=getattr(reminder, "title", ""),
            notes=getattr(reminder, "notes", None),
            apple_sync_id=apple_sync_id,
            apple_reminder_id=getattr(reminder, "uuid", ""),
            completed=getattr(reminder, "completed", False),
            due_date=getattr(reminder, "due_date", None),
            due_is_all_day=getattr(reminder, "is_all_day", False),
            area=notion_area,
        )

        try:
            await db.upsert_notion_reminder_mapping(
                apple_sync_id=page_apple_sync_id(created) or apple_sync_id,
                notion_page_id=created["id"],
                apple_reminder_id=getattr(reminder, "uuid", ""),
                apple_calendar_name=apple_calendar_name,
                timestamp=datetime.now(timezone.utc),
            )
        except Exception as exc:
            raise CreateNotionPartialFailure(
                "Notion row was created, but SQLite identity persistence failed. "
                "Rerun the production plan before applying again."
            ) from exc

        result.created_notion += 1

    return result


async def execute_update_apple_plan(
    plan: BidirectionalUpdatePlan,
    apple_calendar_name: str,
    reminders_adapter: Any,
    db: Any,
    allow_multiple_test_updates: bool = False,
) -> UpdateAppleExecutionResult:
    """Execute only UPDATE_APPLE actions for the dedicated Notion test slice."""
    if apple_calendar_name != "Notion Sync Test":
        raise ValueError("Milestone 5 can only update Apple from 'Notion Sync Test'")

    update_actions = [action for action in plan.actions if action.kind == "UPDATE_APPLE"]
    if len(update_actions) > 1 and not allow_multiple_test_updates:
        raise ValueError("Refusing multiple UPDATE_APPLE actions without explicit opt-in")

    result = UpdateAppleExecutionResult(
        skipped_non_update_apple=len(plan.actions) - len(update_actions)
    )

    for action in update_actions:
        task = action.notion_task
        reminder = action.apple_reminder
        if task is None or reminder is None:
            continue

        updated = await reminders_adapter.update_reminder(
            uuid=getattr(reminder, "uuid", ""),
            title=task.title,
            notes=task.notes if task.notes is not None else "",
            completed=task.completed,
            priority=map_notion_priority_to_apple(task.priority),
            due_date=task.due_date,
            is_all_day=task.due_is_all_day,
            clear_due_date=task.due_date is None,
        )
        await db.update_notion_reminder_snapshots(
            apple_sync_id=task.apple_sync_id
            or (action.mapping or {}).get("apple_sync_id")
            or task.page_id,
            notion_snapshot=build_notion_snapshot(task),
            apple_snapshot=build_apple_snapshot(updated),
            timestamp=datetime.now(timezone.utc),
        )
        result.updated_apple += 1

    return result


async def execute_production_update_apple_plan(
    plan: BidirectionalUpdatePlan,
    reminders_adapter: Any,
    db: Any,
    max_updates: int,
) -> UpdateAppleExecutionResult:
    """Execute capped UPDATE_APPLE actions for an allowlisted production list."""
    update_actions = [action for action in plan.actions if action.kind == "UPDATE_APPLE"]
    if len(update_actions) > max_updates:
        raise ValueError(
            f"Refusing {len(update_actions)} UPDATE_APPLE actions; cap is {max_updates}"
        )

    result = UpdateAppleExecutionResult(
        skipped_non_update_apple=len(plan.actions) - len(update_actions)
    )

    for action in update_actions:
        task = action.notion_task
        reminder = action.apple_reminder
        if task is None or reminder is None:
            continue

        updated = await reminders_adapter.update_reminder(
            uuid=getattr(reminder, "uuid", ""),
            title=task.title,
            notes=task.notes if task.notes is not None else "",
            completed=task.completed,
            priority=map_notion_priority_to_apple(task.priority),
            due_date=task.due_date,
            is_all_day=task.due_is_all_day,
            clear_due_date=task.due_date is None,
        )
        await db.update_notion_reminder_snapshots(
            apple_sync_id=task.apple_sync_id
            or (action.mapping or {}).get("apple_sync_id")
            or task.page_id,
            notion_snapshot=build_notion_snapshot(task),
            apple_snapshot=build_apple_snapshot(updated),
            timestamp=datetime.now(timezone.utc),
        )
        result.updated_apple += 1

    return result


async def execute_update_notion_plan(
    plan: BidirectionalUpdatePlan,
    apple_calendar_name: str,
    notion_adapter: Any,
    db: Any,
    allow_multiple_test_updates: bool = False,
) -> UpdateNotionExecutionResult:
    """Execute only UPDATE_NOTION actions for the dedicated Notion test slice."""
    if apple_calendar_name != "Notion Sync Test":
        raise ValueError("Milestone 5 can only update Notion from 'Notion Sync Test'")

    update_actions = [action for action in plan.actions if action.kind == "UPDATE_NOTION"]
    if len(update_actions) > 1 and not allow_multiple_test_updates:
        raise ValueError("Refusing multiple UPDATE_NOTION actions without explicit opt-in")

    result = UpdateNotionExecutionResult(
        skipped_non_update_notion=len(plan.actions) - len(update_actions)
    )

    for action in update_actions:
        task = action.notion_task
        reminder = action.apple_reminder
        if task is None or reminder is None:
            continue

        updated_page = await notion_adapter.update_task_from_apple(
            page_id=task.page_id,
            title=getattr(reminder, "title", ""),
            notes=getattr(reminder, "notes", None),
            completed=getattr(reminder, "completed", False),
            notion_priority=map_apple_priority_to_notion(getattr(reminder, "priority", 0)),
            due_date=getattr(reminder, "due_date", None),
            due_is_all_day=getattr(reminder, "is_all_day", False),
        )
        updated_task = parse_notion_task(updated_page)
        await db.update_notion_reminder_snapshots(
            apple_sync_id=updated_task.apple_sync_id
            or task.apple_sync_id
            or (action.mapping or {}).get("apple_sync_id")
            or task.page_id,
            notion_snapshot=build_notion_snapshot(updated_task),
            apple_snapshot=build_apple_snapshot(reminder),
            timestamp=datetime.now(timezone.utc),
        )
        result.updated_notion += 1

    return result


async def execute_production_update_notion_plan(
    plan: BidirectionalUpdatePlan,
    notion_adapter: Any,
    db: Any,
    max_updates: int,
) -> UpdateNotionExecutionResult:
    """Execute capped UPDATE_NOTION actions for an allowlisted production list."""
    update_actions = [action for action in plan.actions if action.kind == "UPDATE_NOTION"]
    if len(update_actions) > max_updates:
        raise ValueError(
            f"Refusing {len(update_actions)} UPDATE_NOTION actions; cap is {max_updates}"
        )

    result = UpdateNotionExecutionResult(
        skipped_non_update_notion=len(plan.actions) - len(update_actions)
    )

    for action in update_actions:
        task = action.notion_task
        reminder = action.apple_reminder
        if task is None or reminder is None:
            continue

        updated_page = await notion_adapter.update_task_from_apple(
            page_id=task.page_id,
            title=getattr(reminder, "title", ""),
            notes=getattr(reminder, "notes", None),
            completed=getattr(reminder, "completed", False),
            notion_priority=map_apple_priority_to_notion(getattr(reminder, "priority", 0)),
            due_date=getattr(reminder, "due_date", None),
            due_is_all_day=getattr(reminder, "is_all_day", False),
        )
        updated_task = parse_notion_task(updated_page)
        await db.update_notion_reminder_snapshots(
            apple_sync_id=updated_task.apple_sync_id
            or task.apple_sync_id
            or (action.mapping or {}).get("apple_sync_id")
            or task.page_id,
            notion_snapshot=build_notion_snapshot(updated_task),
            apple_snapshot=build_apple_snapshot(reminder),
            timestamp=datetime.now(timezone.utc),
        )
        result.updated_notion += 1

    return result


async def execute_production_baseline_plan(
    plan: BidirectionalUpdatePlan,
    db: Any,
) -> BaselineExecutionResult:
    """Write snapshot baselines for NEEDS_BASELINE production actions only."""
    baseline_actions = [
        action for action in plan.actions if action.kind == "NEEDS_BASELINE"
    ]
    result = BaselineExecutionResult(
        skipped_non_baseline=len(plan.actions) - len(baseline_actions)
    )

    timestamp = datetime.now(timezone.utc)
    for action in baseline_actions:
        task = action.notion_task
        reminder = action.apple_reminder
        if task is None or reminder is None:
            continue

        await db.update_notion_reminder_snapshots(
            apple_sync_id=task.apple_sync_id
            or (action.mapping or {}).get("apple_sync_id")
            or task.page_id,
            notion_snapshot=build_notion_snapshot(task),
            apple_snapshot=build_apple_snapshot(reminder),
            timestamp=timestamp,
        )
        result.baselined += 1

    return result


async def execute_identity_recovery_plan(
    plan: IdentityRecoveryPlan,
    apple_calendar_name: str,
    notion_adapter: Any,
    db: Any,
    allow_multiple_test_recoveries: bool = False,
) -> IdentityRecoveryExecutionResult:
    """Apply only Apple reminder ID receipt recovery for the test slice."""
    if apple_calendar_name != "Notion Sync Test":
        raise ValueError("Milestone 6 can only recover identities in 'Notion Sync Test'")

    recovery_actions = [
        action for action in plan.actions if action.kind == "RECOVER_APPLE_ID"
    ]
    if len(recovery_actions) > 1 and not allow_multiple_test_recoveries:
        raise ValueError("Refusing multiple identity recoveries without explicit opt-in")

    result = IdentityRecoveryExecutionResult(
        skipped_non_recovery=len(plan.actions) - len(recovery_actions)
    )
    for action in recovery_actions:
        if not action.title.startswith("[SYNC TEST]"):
            raise ValueError("Identity recovery may only mutate [SYNC TEST] rows")
        if action.notion_task is None or action.apple_reminder is None:
            continue
        if not action.new_apple_reminder_id:
            continue

        timestamp = datetime.now(timezone.utc)
        await notion_adapter.update_page_apple_reminder_id(
            action.notion_page_id,
            action.new_apple_reminder_id,
        )
        await db.replace_notion_mapping_apple_reminder_id(
            apple_sync_id=action.apple_sync_id,
            apple_reminder_id=action.new_apple_reminder_id,
            apple_calendar_name=apple_calendar_name,
            timestamp=timestamp,
        )
        await db.update_notion_reminder_snapshots(
            apple_sync_id=action.apple_sync_id,
            notion_snapshot=build_notion_snapshot(action.notion_task),
            apple_snapshot=build_apple_snapshot(action.apple_reminder),
            timestamp=timestamp,
        )
        result.recovered += 1
    return result


async def execute_production_identity_recovery_plan(
    plan: IdentityRecoveryPlan,
    apple_calendar_name: str,
    notion_adapter: Any,
    db: Any,
    max_recoveries: int,
) -> IdentityRecoveryExecutionResult:
    """Apply capped Apple reminder ID receipt recovery for production mappings."""
    recovery_actions = [
        action for action in plan.actions if action.kind == "RECOVER_APPLE_ID"
    ]
    if len(recovery_actions) > max_recoveries:
        raise ValueError(
            f"Refusing {len(recovery_actions)} identity recoveries; cap is {max_recoveries}"
        )

    result = IdentityRecoveryExecutionResult(
        skipped_non_recovery=len(plan.actions) - len(recovery_actions)
    )
    for action in recovery_actions:
        if action.notion_task is None or action.apple_reminder is None:
            continue
        if not action.new_apple_reminder_id:
            continue

        timestamp = datetime.now(timezone.utc)
        await notion_adapter.update_page_apple_reminder_id(
            action.notion_page_id,
            action.new_apple_reminder_id,
        )
        await db.replace_notion_mapping_apple_reminder_id(
            apple_sync_id=action.apple_sync_id,
            apple_reminder_id=action.new_apple_reminder_id,
            apple_calendar_name=apple_calendar_name,
            timestamp=timestamp,
        )
        await db.update_notion_reminder_snapshots(
            apple_sync_id=action.apple_sync_id,
            notion_snapshot=build_notion_snapshot(action.notion_task),
            apple_snapshot=build_apple_snapshot(action.apple_reminder),
            timestamp=timestamp,
        )
        result.recovered += 1
    return result


async def execute_deletion_grace_plan(
    plan: DeletionGracePlan,
    apple_calendar_name: str,
    db: Any,
) -> DeletionGraceExecutionResult:
    """Record only missing-side grace markers for the test slice."""
    if apple_calendar_name != "Notion Sync Test":
        raise ValueError("Gate J deletion grace recording is limited to 'Notion Sync Test'")

    result = DeletionGraceExecutionResult()
    timestamp = datetime.now(timezone.utc)
    for action in plan.actions:
        if not action.apple_sync_id:
            result.skipped += 1
            continue

        if action.kind == "NOOP":
            marker_present = bool(
                (action.mapping or {}).get("missing_apple_seen_at")
                or (action.mapping or {}).get("missing_notion_seen_at")
            )
            if marker_present:
                await db.clear_notion_reminder_missing_markers(
                    apple_sync_id=action.apple_sync_id,
                    timestamp=timestamp,
                )
                result.cleared_markers += 1
        elif action.kind == "MISSING_APPLE_FIRST_SEEN":
            await db.mark_notion_reminder_missing_apple(
                apple_sync_id=action.apple_sync_id,
                timestamp=timestamp,
            )
            result.marked_missing_apple += 1
        elif action.kind == "MISSING_NOTION_FIRST_SEEN":
            await db.mark_notion_reminder_missing_notion(
                apple_sync_id=action.apple_sync_id,
                timestamp=timestamp,
            )
            result.marked_missing_notion += 1
        else:
            result.skipped += 1
    return result


async def execute_production_deletion_grace_plan(
    plan: DeletionGracePlan,
    db: Any,
) -> DeletionGraceExecutionResult:
    """Record detection-only missing-side markers for production mappings."""
    result = DeletionGraceExecutionResult()
    timestamp = datetime.now(timezone.utc)
    for action in plan.actions:
        if not action.apple_sync_id:
            result.skipped += 1
            continue

        if action.kind == "NOOP":
            marker_present = bool(
                (action.mapping or {}).get("missing_apple_seen_at")
                or (action.mapping or {}).get("missing_notion_seen_at")
            )
            if marker_present:
                await db.clear_notion_reminder_missing_markers(
                    apple_sync_id=action.apple_sync_id,
                    timestamp=timestamp,
                )
                result.cleared_markers += 1
        elif action.kind == "MISSING_APPLE_FIRST_SEEN":
            await db.mark_notion_reminder_missing_apple(
                apple_sync_id=action.apple_sync_id,
                timestamp=timestamp,
            )
            result.marked_missing_apple += 1
        elif action.kind == "MISSING_NOTION_FIRST_SEEN":
            await db.mark_notion_reminder_missing_notion(
                apple_sync_id=action.apple_sync_id,
                timestamp=timestamp,
            )
            result.marked_missing_notion += 1
        else:
            result.skipped += 1
    return result


async def execute_production_receipt_cleanup_plan(
    plan: DeletionGracePlan,
    apple_calendar_name: str,
    db: Any,
    max_receipts: int,
) -> ReceiptCleanupExecutionResult:
    """Delete exact local receipts whose Notion and Apple sides are both absent."""
    cleanup_actions = [
        action
        for action in plan.actions
        if action.kind == "UNTRACKED" and action.reason == "both_sides_absent"
    ]
    if len(cleanup_actions) > max_receipts:
        raise ValueError(
            f"Refusing {len(cleanup_actions)} receipt cleanups; cap is {max_receipts}"
        )

    result = ReceiptCleanupExecutionResult(
        skipped=len(plan.actions) - len(cleanup_actions)
    )
    for action in cleanup_actions:
        if not action.apple_sync_id or not action.notion_page_id or not action.apple_reminder_id:
            result.skipped += 1
            continue

        deleted = await db.delete_notion_reminder_mapping(
            apple_sync_id=action.apple_sync_id,
            notion_page_id=action.notion_page_id,
            apple_reminder_id=action.apple_reminder_id,
            apple_calendar_name=apple_calendar_name,
        )
        result.deleted_receipts += deleted
        if not deleted:
            result.skipped += 1

    return result
