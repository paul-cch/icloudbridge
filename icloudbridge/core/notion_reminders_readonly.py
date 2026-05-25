"""Read-only matching for Notion Tasks and Apple Reminders."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from icloudbridge.sources.reminders.notion_adapter import NotionTask


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
            )
        )

    return ReadOnlySyncPlan(actions=actions)
