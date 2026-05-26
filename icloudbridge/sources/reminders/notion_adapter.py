"""Read-only Notion Tasks preflight support for Reminders sync."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_NOTION_API_VERSION = "2025-09-03"
DEFAULT_TASKS_DATA_SOURCE_ID = "61ef2269-1dc6-4391-aaff-8013d2b857e3"
DISPOSABLE_NOTION_TO_APPLE_TITLE = "[SYNC TEST] Notion to Apple"
DISPOSABLE_APPLE_TO_NOTION_TITLE = "[SYNC TEST] Apple to Notion"

REQUIRED_TASK_PROPERTIES = {
    "Task Name": "title",
    "Status": "status",
    "Due Date": "date",
    "Notes": "rich_text",
    "Priority": "select",
}

REQUIRED_IDENTITY_PROPERTIES = {
    "Apple Sync ID": "rich_text",
}

OPTIONAL_TASK_PROPERTIES = {
    "Apple Reminder ID": "rich_text",
    "Source": "select",
    "Area": "select",
    "Tags": "multi_select",
    "Completed At": "date",
    "Reminder At": "date",
}


@dataclass
class PropertyCheck:
    """Result of checking one Notion data source property."""

    name: str
    expected_type: str
    actual_type: str | None
    present: bool
    ok: bool


@dataclass
class NotionSchemaReport:
    """Read-only report for a Notion Tasks data source."""

    ok: bool
    data_source_id: str
    database_id: str | None
    title: str
    url: str | None
    api_version: str
    required: list[PropertyCheck] = field(default_factory=list)
    identity: list[PropertyCheck] = field(default_factory=list)
    optional: list[PropertyCheck] = field(default_factory=list)
    status_options: list[str] = field(default_factory=list)
    source_options: list[str] = field(default_factory=list)
    area_options: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def missing_identity_properties(self) -> list[str]:
        """Identity properties that must be created before sync writes."""
        return [check.name for check in self.identity if not check.ok]


@dataclass
class NotionTask:
    """Normalized Notion Tasks row for read-only sync planning."""

    page_id: str
    title: str
    notes: str | None
    completed: bool
    status: str
    priority: str | None
    due_date: datetime | None
    due_is_all_day: bool
    reminder_at: datetime | None
    last_edited_time: datetime | None
    apple_sync_id: str | None
    apple_reminder_id: str | None
    url: str | None
    raw_properties: dict[str, Any]


class NotionAuthError(RuntimeError):
    """Raised when no usable Notion API token is available."""


def _find_token_in_json(value: Any) -> str | None:
    """Best-effort search for a Notion token in the CLI auth JSON structure."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("secret_", "ntn_", "notion_")):
            return stripped
        return None

    if isinstance(value, dict):
        preferred_keys = (
            "access_token",
            "api_token",
            "token",
            "notion_api_token",
            "NOTION_API_TOKEN",
        )
        for key in preferred_keys:
            found = _find_token_in_json(value.get(key))
            if found:
                return found
        for child in value.values():
            found = _find_token_in_json(child)
            if found:
                return found

    if isinstance(value, list):
        for child in value:
            found = _find_token_in_json(child)
            if found:
                return found

    return None


def load_notion_token(token: str | None = None) -> str:
    """
    Load a Notion API token without printing secret material.

    Priority:
    1. Explicit CLI argument.
    2. NOTION_API_TOKEN environment variable.
    3. ntn file auth at ~/.config/notion/auth.json.
    """
    if token:
        return token

    env_token = os.environ.get("NOTION_API_TOKEN")
    if env_token:
        return env_token

    auth_path = Path.home() / ".config" / "notion" / "auth.json"
    if auth_path.exists():
        try:
            data = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise NotionAuthError(f"Could not read ntn auth file: {exc}") from exc

        file_token = _find_token_in_json(data)
        if file_token:
            return file_token

    raise NotionAuthError(
        "No Notion token found. Set NOTION_API_TOKEN or authenticate ntn with file auth."
    )


def build_exact_title_query(title: str, page_size: int = 10) -> dict[str, Any]:
    """Build a narrow query for a Tasks row by exact title."""
    return {
        "filter": {
            "property": "Task Name",
            "title": {"equals": title},
        },
        "page_size": page_size,
    }


def build_apple_sync_id_query(page_size: int = 100, start_cursor: str | None = None) -> dict[str, Any]:
    """Build a safe query for only rows explicitly enrolled with Apple Sync ID."""
    query: dict[str, Any] = {
        "filter": {
            "property": "Apple Sync ID",
            "rich_text": {"is_not_empty": True},
        },
        "page_size": page_size,
    }
    if start_cursor:
        query["start_cursor"] = start_cursor
    return query


def _rich_text(content: str) -> dict[str, Any]:
    return {"rich_text": [{"text": {"content": content}}]}


def build_disposable_task_properties(
    apple_sync_id: str,
    notes: str,
    title: str = DISPOSABLE_NOTION_TO_APPLE_TITLE,
) -> dict[str, Any]:
    """Build the approved disposable Notion proof row properties."""
    return {
        "Task Name": {"title": [{"text": {"content": title}}]},
        "Apple Sync ID": _rich_text(apple_sync_id),
        "Source": {"select": {"name": "Manual"}},
        "Area": {"select": {"name": "Life"}},
        "Status": {"status": {"name": "Not started"}},
        "Notes": _rich_text(notes),
    }


def build_apple_origin_task_properties(
    title: str,
    notes: str | None,
    apple_sync_id: str,
    apple_reminder_id: str,
    completed: bool,
    due_date: datetime | None,
    due_is_all_day: bool,
    area: str = "Life",
) -> dict[str, Any]:
    """Build Notion properties for an Apple-originated reminder test row."""
    properties = {
        "Task Name": {"title": [{"text": {"content": title}}]},
        "Apple Sync ID": _rich_text(apple_sync_id),
        "Apple Reminder ID": _rich_text(apple_reminder_id),
        "Source": {"select": {"name": "Manual"}},
        "Area": {"select": {"name": area}},
        "Status": {"status": {"name": "Done" if completed else "Not started"}},
    }
    if notes:
        properties["Notes"] = _rich_text(notes)
    if due_date:
        properties["Due Date"] = {
            "date": {
                "start": due_date.date().isoformat()
                if due_is_all_day
                else due_date.isoformat()
            }
        }
    return properties


def build_task_update_from_apple_properties(
    title: str,
    notes: str | None,
    completed: bool,
    notion_priority: str | None,
    due_date: datetime | None,
    due_is_all_day: bool,
) -> dict[str, Any]:
    """Build a narrow Notion page patch from an Apple reminder."""
    properties: dict[str, Any] = {
        "Task Name": {"title": [{"text": {"content": title}}]},
        "Notes": _rich_text(notes) if notes else {"rich_text": []},
        "Status": {"status": {"name": "Done" if completed else "Not started"}},
    }
    if notion_priority:
        properties["Priority"] = {"select": {"name": notion_priority}}
    if due_date:
        properties["Due Date"] = {
            "date": {
                "start": due_date.date().isoformat()
                if due_is_all_day
                else due_date.isoformat()
            }
        }
    else:
        properties["Due Date"] = {"date": None}
    return properties


def build_task_proof_update_properties(
    title: str | None = None,
    notes: str | None = None,
    completed: bool | None = None,
    notion_priority: str | None = None,
    due_date: datetime | None = None,
    due_is_all_day: bool = False,
    clear_due_date: bool = False,
) -> dict[str, Any]:
    """Build a narrow proof-only Notion task patch."""
    properties: dict[str, Any] = {}
    if title is not None:
        properties["Task Name"] = {"title": [{"text": {"content": title}}]}
    if notes is not None:
        properties["Notes"] = _rich_text(notes) if notes else {"rich_text": []}
    if completed is not None:
        properties["Status"] = {"status": {"name": "Done" if completed else "Not started"}}
    if notion_priority is not None:
        properties["Priority"] = {"select": {"name": notion_priority}}
    if due_date is not None:
        properties["Due Date"] = {
            "date": {
                "start": due_date.date().isoformat()
                if due_is_all_day
                else due_date.isoformat()
            }
        }
    elif clear_due_date:
        properties["Due Date"] = {"date": None}
    return properties


def build_notes_patch(notes: str) -> dict[str, Any]:
    """Build a Notion page property patch for the Notes field."""
    return {"Notes": _rich_text(notes)}


def build_apple_reminder_id_patch(apple_reminder_id: str) -> dict[str, Any]:
    """Build a Notion page property patch for the Apple Reminder ID receipt."""
    return {"Apple Reminder ID": _rich_text(apple_reminder_id)}


def plain_text_from_property(prop: dict[str, Any] | None) -> str:
    """Extract plain text from a Notion title or rich_text property."""
    if not isinstance(prop, dict):
        return ""
    prop_type = prop.get("type")
    if prop_type not in {"title", "rich_text"}:
        return ""
    return "".join(part.get("plain_text", "") for part in prop.get(prop_type, []))


def page_title(page: dict[str, Any]) -> str:
    """Extract the Tasks title from a Notion page payload."""
    return plain_text_from_property(page.get("properties", {}).get("Task Name"))


def page_apple_sync_id(page: dict[str, Any]) -> str:
    """Extract the Apple Sync ID from a Notion page payload."""
    return plain_text_from_property(page.get("properties", {}).get("Apple Sync ID"))


def page_notes(page: dict[str, Any]) -> str:
    """Extract Notes from a Notion page payload."""
    return plain_text_from_property(page.get("properties", {}).get("Notes"))


def _select_name(prop: dict[str, Any] | None, prop_type: str) -> str | None:
    if not isinstance(prop, dict):
        return None
    value = prop.get(prop_type)
    if not isinstance(value, dict):
        return None
    return value.get("name")


def _parse_notion_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _parse_date_property(prop: dict[str, Any] | None) -> tuple[datetime | None, bool]:
    if not isinstance(prop, dict):
        return None, False
    date_value = prop.get("date")
    if not isinstance(date_value, dict):
        return None, False
    start = date_value.get("start")
    if not start:
        return None, False
    is_all_day = "T" not in start
    return _parse_notion_datetime(start), is_all_day


def parse_notion_task(page: dict[str, Any]) -> NotionTask:
    """Convert a Notion page payload into a normalized task."""
    properties = page.get("properties", {})
    due_date, due_is_all_day = _parse_date_property(properties.get("Due Date"))
    reminder_at, _ = _parse_date_property(properties.get("Reminder At"))
    status = _select_name(properties.get("Status"), "status") or ""

    return NotionTask(
        page_id=page.get("id", ""),
        title=page_title(page),
        notes=page_notes(page) or None,
        completed=status in {"Done", "Cancelled"},
        status=status,
        priority=_select_name(properties.get("Priority"), "select"),
        due_date=due_date,
        due_is_all_day=due_is_all_day,
        reminder_at=reminder_at,
        last_edited_time=_parse_notion_datetime(page.get("last_edited_time")),
        apple_sync_id=page_apple_sync_id(page) or None,
        apple_reminder_id=plain_text_from_property(properties.get("Apple Reminder ID")) or None,
        url=page.get("url"),
        raw_properties=properties,
    )


class NotionTasksAdapter:
    """Small Notion API client for preflight checks."""

    def __init__(
        self,
        token: str,
        api_version: str = DEFAULT_NOTION_API_VERSION,
        base_url: str = "https://api.notion.com",
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        max_rate_limit_retries: int = 3,
        default_rate_limit_delay: float = 1.0,
        max_rate_limit_delay: float = 10.0,
    ):
        self.token = token
        self.api_version = api_version
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport
        self.sleep = sleep
        self.max_rate_limit_retries = max_rate_limit_retries
        self.default_rate_limit_delay = default_rate_limit_delay
        self.max_rate_limit_delay = max_rate_limit_delay

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": self.api_version,
            "Content-Type": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            transport=self.transport,
        )

    def _rate_limit_delay(self, response: httpx.Response) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is None:
            return self.default_rate_limit_delay
        try:
            delay = float(retry_after)
        except ValueError:
            return self.default_rate_limit_delay
        return min(max(delay, 0.0), self.max_rate_limit_delay)

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        attempts = self.max_rate_limit_retries + 1
        for attempt in range(attempts):
            response = await client.request(
                method,
                url,
                headers=self._headers(),
                **kwargs,
            )
            if response.status_code != 429:
                response.raise_for_status()
                return response
            if attempt == attempts - 1:
                response.raise_for_status()
            await self.sleep(self._rate_limit_delay(response))

        raise RuntimeError("unreachable Notion rate-limit retry state")

    async def retrieve_data_source(self, data_source_id: str) -> dict[str, Any]:
        """Retrieve a Notion data source object."""
        async with self._client() as client:
            response = await self._request(
                client,
                "GET",
                f"/v1/data_sources/{data_source_id}",
            )
            return response.json()

    async def query_tasks_by_title(self, data_source_id: str, title: str) -> list[dict[str, Any]]:
        """Query Tasks rows by exact title."""
        async with self._client() as client:
            response = await self._request(
                client,
                "POST",
                f"/v1/data_sources/{data_source_id}/query",
                json=build_exact_title_query(title),
            )
            return response.json().get("results", [])

    async def query_tasks_with_apple_sync_id(
        self, data_source_id: str, page_size: int = 100
    ) -> list[NotionTask]:
        """Read only Notion rows explicitly enrolled with Apple Sync ID."""
        tasks: list[NotionTask] = []
        start_cursor: str | None = None

        async with self._client() as client:
            while True:
                response = await self._request(
                    client,
                    "POST",
                    f"/v1/data_sources/{data_source_id}/query",
                    json=build_apple_sync_id_query(
                        page_size=page_size,
                        start_cursor=start_cursor,
                    ),
                )
                payload = response.json()
                tasks.extend(parse_notion_task(page) for page in payload.get("results", []))
                if not payload.get("has_more"):
                    break
                start_cursor = payload.get("next_cursor")
                if not start_cursor:
                    break

        return tasks

    async def create_disposable_task(
        self,
        data_source_id: str,
        apple_sync_id: str,
        notes: str,
        title: str = DISPOSABLE_NOTION_TO_APPLE_TITLE,
    ) -> dict[str, Any]:
        """Create the approved disposable Notion proof row."""
        async with self._client() as client:
            response = await self._request(
                client,
                "POST",
                "/v1/pages",
                json={
                    "parent": {
                        "type": "data_source_id",
                        "data_source_id": data_source_id,
                    },
                    "properties": build_disposable_task_properties(
                        apple_sync_id=apple_sync_id,
                        notes=notes,
                        title=title,
                    ),
                },
            )
            return response.json()

    async def create_apple_origin_task(
        self,
        data_source_id: str,
        title: str,
        notes: str | None,
        apple_sync_id: str,
        apple_reminder_id: str,
        completed: bool,
        due_date: datetime | None,
        due_is_all_day: bool,
        area: str = "Life",
    ) -> dict[str, Any]:
        """Create a Notion Tasks row from an Apple reminder."""
        async with self._client() as client:
            response = await self._request(
                client,
                "POST",
                "/v1/pages",
                json={
                    "parent": {
                        "type": "data_source_id",
                        "data_source_id": data_source_id,
                    },
                    "properties": build_apple_origin_task_properties(
                        title=title,
                        notes=notes,
                        apple_sync_id=apple_sync_id,
                        apple_reminder_id=apple_reminder_id,
                        completed=completed,
                        due_date=due_date,
                        due_is_all_day=due_is_all_day,
                        area=area,
                    ),
                },
            )
            return response.json()

    async def retrieve_page(self, page_id: str) -> dict[str, Any]:
        """Retrieve a Notion page by ID."""
        async with self._client() as client:
            response = await self._request(
                client,
                "GET",
                f"/v1/pages/{page_id}",
            )
            return response.json()

    async def update_page_notes(self, page_id: str, notes: str) -> dict[str, Any]:
        """Update only the Notes property on a Notion page."""
        async with self._client() as client:
            response = await self._request(
                client,
                "PATCH",
                f"/v1/pages/{page_id}",
                json={"properties": build_notes_patch(notes)},
            )
            return response.json()

    async def update_page_apple_reminder_id(
        self, page_id: str, apple_reminder_id: str
    ) -> dict[str, Any]:
        """Update only the Apple Reminder ID property on a Notion page."""
        async with self._client() as client:
            response = await self._request(
                client,
                "PATCH",
                f"/v1/pages/{page_id}",
                json={"properties": build_apple_reminder_id_patch(apple_reminder_id)},
            )
            return response.json()

    async def update_task_from_apple(
        self,
        page_id: str,
        title: str,
        notes: str | None,
        completed: bool,
        notion_priority: str | None,
        due_date: datetime | None,
        due_is_all_day: bool,
    ) -> dict[str, Any]:
        """Update safe task fields on a Notion page from an Apple reminder."""
        async with self._client() as client:
            response = await self._request(
                client,
                "PATCH",
                f"/v1/pages/{page_id}",
                json={
                    "properties": build_task_update_from_apple_properties(
                        title=title,
                        notes=notes,
                        completed=completed,
                        notion_priority=notion_priority,
                        due_date=due_date,
                        due_is_all_day=due_is_all_day,
                    )
                },
            )
            return response.json()

    async def update_task_proof_fields(
        self,
        page_id: str,
        title: str | None = None,
        notes: str | None = None,
        completed: bool | None = None,
        notion_priority: str | None = None,
        due_date: datetime | None = None,
        due_is_all_day: bool = False,
        clear_due_date: bool = False,
    ) -> dict[str, Any]:
        """Update selected safe task fields for a test-slice proof run."""
        async with self._client() as client:
            response = await self._request(
                client,
                "PATCH",
                f"/v1/pages/{page_id}",
                json={
                    "properties": build_task_proof_update_properties(
                        title=title,
                        notes=notes,
                        completed=completed,
                        notion_priority=notion_priority,
                        due_date=due_date,
                        due_is_all_day=due_is_all_day,
                        clear_due_date=clear_due_date,
                    )
                },
            )
            return response.json()

    async def preflight_schema(self, data_source_id: str) -> NotionSchemaReport:
        """Run read-only schema checks against a Notion Tasks data source."""
        payload = await self.retrieve_data_source(data_source_id)
        properties = payload.get("properties", {})

        required = self._check_properties(properties, REQUIRED_TASK_PROPERTIES)
        identity = self._check_properties(properties, REQUIRED_IDENTITY_PROPERTIES)
        optional = self._check_properties(properties, OPTIONAL_TASK_PROPERTIES)

        errors = [
            f"Missing or mismatched required property: {check.name} "
            f"(expected {check.expected_type}, got {check.actual_type or 'missing'})"
            for check in required
            if not check.ok
        ]
        errors.extend(
            f"Missing required identity property for safe sync writes: {check.name}"
            for check in identity
            if not check.ok
        )

        warnings: list[str] = []
        if "Apple Reminders" not in self._select_options(properties, "Source"):
            warnings.append(
                "Source option 'Apple Reminders' is not present; use Source='Manual' "
                "or run an explicit schema migration before using that source value."
            )

        status_options = self._status_options(properties)
        for expected_status in ("Not started", "Done", "Cancelled"):
            if expected_status not in status_options:
                errors.append(f"Status option '{expected_status}' is missing")

        parent = payload.get("parent", {})
        title = "".join(part.get("plain_text", "") for part in payload.get("title", []))

        return NotionSchemaReport(
            ok=not errors,
            data_source_id=payload.get("id", data_source_id),
            database_id=parent.get("database_id"),
            title=title or "(untitled)",
            url=payload.get("url"),
            api_version=self.api_version,
            required=required,
            identity=identity,
            optional=optional,
            status_options=status_options,
            source_options=self._select_options(properties, "Source"),
            area_options=self._select_options(properties, "Area"),
            warnings=warnings,
            errors=errors,
        )

    def _check_properties(
        self, properties: dict[str, Any], expected: dict[str, str]
    ) -> list[PropertyCheck]:
        checks = []
        for name, expected_type in expected.items():
            prop = properties.get(name)
            actual_type = prop.get("type") if isinstance(prop, dict) else None
            checks.append(
                PropertyCheck(
                    name=name,
                    expected_type=expected_type,
                    actual_type=actual_type,
                    present=prop is not None,
                    ok=actual_type == expected_type,
                )
            )
        return checks

    def _status_options(self, properties: dict[str, Any]) -> list[str]:
        status = properties.get("Status", {}).get("status", {})
        return [option.get("name", "") for option in status.get("options", [])]

    def _select_options(self, properties: dict[str, Any], property_name: str) -> list[str]:
        prop = properties.get(property_name, {})
        prop_type = prop.get("type")
        if prop_type not in {"select", "multi_select"}:
            return []
        return [option.get("name", "") for option in prop.get(prop_type, {}).get("options", [])]
