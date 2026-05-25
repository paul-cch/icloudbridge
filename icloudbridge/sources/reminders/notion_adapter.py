"""Read-only Notion Tasks preflight support for Reminders sync."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


DEFAULT_NOTION_API_VERSION = "2025-09-03"
DEFAULT_TASKS_DATA_SOURCE_ID = "61ef2269-1dc6-4391-aaff-8013d2b857e3"

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


class NotionTasksAdapter:
    """Small Notion API client for preflight checks."""

    def __init__(
        self,
        token: str,
        api_version: str = DEFAULT_NOTION_API_VERSION,
        base_url: str = "https://api.notion.com",
        timeout: float = 20.0,
    ):
        self.token = token
        self.api_version = api_version
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": self.api_version,
            "Content-Type": "application/json",
        }

    async def retrieve_data_source(self, data_source_id: str) -> dict[str, Any]:
        """Retrieve a Notion data source object."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.base_url}/v1/data_sources/{data_source_id}",
                headers=self._headers(),
            )
            response.raise_for_status()
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
