import pytest

from icloudbridge.sources.reminders.notion_adapter import (
    DEFAULT_NOTION_API_VERSION,
    DEFAULT_TASKS_DATA_SOURCE_ID,
    NotionTasksAdapter,
    _find_token_in_json,
)


def _data_source_payload(properties):
    return {
        "id": DEFAULT_TASKS_DATA_SOURCE_ID,
        "url": "https://www.notion.so/example",
        "parent": {"type": "database_id", "database_id": "database-id"},
        "title": [{"plain_text": "Tasks"}],
        "properties": properties,
    }


def _property(prop_type, body=None):
    return {"type": prop_type, prop_type: body or {}}


class FakeNotionTasksAdapter(NotionTasksAdapter):
    def __init__(self, payload):
        super().__init__(token="secret_test", api_version=DEFAULT_NOTION_API_VERSION)
        self.payload = payload

    async def retrieve_data_source(self, data_source_id):
        return self.payload


@pytest.mark.asyncio
async def test_preflight_schema_passes_with_required_and_identity_properties():
    adapter = FakeNotionTasksAdapter(
        _data_source_payload(
            {
                "Task Name": _property("title"),
                "Status": _property(
                    "status",
                    {
                        "options": [
                            {"name": "Not started"},
                            {"name": "In progress"},
                            {"name": "Done"},
                            {"name": "Cancelled"},
                        ]
                    },
                ),
                "Due Date": _property("date"),
                "Notes": _property("rich_text"),
                "Priority": _property("select"),
                "Apple Sync ID": _property("rich_text"),
                "Source": _property("select", {"options": [{"name": "Manual"}]}),
                "Area": _property("select", {"options": [{"name": "Life"}]}),
            }
        )
    )

    report = await adapter.preflight_schema(DEFAULT_TASKS_DATA_SOURCE_ID)

    assert report.ok is True
    assert report.title == "Tasks"
    assert report.database_id == "database-id"
    assert report.missing_identity_properties == []
    assert any("Apple Reminders" in warning for warning in report.warnings)


@pytest.mark.asyncio
async def test_preflight_schema_fails_without_apple_sync_id():
    adapter = FakeNotionTasksAdapter(
        _data_source_payload(
            {
                "Task Name": _property("title"),
                "Status": _property(
                    "status",
                    {
                        "options": [
                            {"name": "Not started"},
                            {"name": "Done"},
                            {"name": "Cancelled"},
                        ]
                    },
                ),
                "Due Date": _property("date"),
                "Notes": _property("rich_text"),
                "Priority": _property("select"),
            }
        )
    )

    report = await adapter.preflight_schema(DEFAULT_TASKS_DATA_SOURCE_ID)

    assert report.ok is False
    assert report.missing_identity_properties == ["Apple Sync ID"]
    assert "Missing required identity property" in "\n".join(report.errors)


@pytest.mark.asyncio
async def test_preflight_schema_fails_on_property_type_mismatch():
    adapter = FakeNotionTasksAdapter(
        _data_source_payload(
            {
                "Task Name": _property("rich_text"),
                "Status": _property(
                    "status",
                    {
                        "options": [
                            {"name": "Not started"},
                            {"name": "Done"},
                            {"name": "Cancelled"},
                        ]
                    },
                ),
                "Due Date": _property("date"),
                "Notes": _property("rich_text"),
                "Priority": _property("select"),
                "Apple Sync ID": _property("rich_text"),
            }
        )
    )

    report = await adapter.preflight_schema(DEFAULT_TASKS_DATA_SOURCE_ID)

    assert report.ok is False
    assert "Task Name" in "\n".join(report.errors)


def test_find_token_in_nested_ntn_auth_json():
    token = _find_token_in_json(
        {
            "profiles": [
                {"workspace": "other", "access_token": ""},
                {"workspace": "Paul", "auth": {"token": "secret_test_token"}},
            ]
        }
    )

    assert token == "secret_test_token"
