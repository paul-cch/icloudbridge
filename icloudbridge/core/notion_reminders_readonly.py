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
