"""Read-only matching for Notion Tasks and Apple Reminders."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
