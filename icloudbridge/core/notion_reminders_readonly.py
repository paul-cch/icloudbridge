"""Read-only matching for Notion Tasks and Apple Reminders."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from icloudbridge.sources.reminders.notion_adapter import NotionTask, page_apple_sync_id


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
