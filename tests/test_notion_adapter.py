import pytest

from icloudbridge.sources.reminders.notion_adapter import (
    DEFAULT_NOTION_API_VERSION,
    DEFAULT_TASKS_DATA_SOURCE_ID,
    DISPOSABLE_APPLE_TO_NOTION_TITLE,
    DISPOSABLE_NOTION_TO_APPLE_TITLE,
    NotionTasksAdapter,
    build_apple_reminder_id_patch,
    build_disposable_task_properties,
    build_apple_sync_id_query,
    build_exact_title_query,
    parse_notion_task,
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


def test_build_disposable_task_properties_uses_safe_existing_options():
    properties = build_disposable_task_properties("sync-test-uuid", "Proof note")

    assert properties["Task Name"]["title"][0]["text"]["content"] == DISPOSABLE_NOTION_TO_APPLE_TITLE
    assert properties["Apple Sync ID"]["rich_text"][0]["text"]["content"] == "sync-test-uuid"
    assert properties["Source"]["select"]["name"] == "Manual"
    assert properties["Area"]["select"]["name"] == "Life"
    assert properties["Status"]["status"]["name"] == "Not started"
    assert properties["Notes"]["rich_text"][0]["text"]["content"] == "Proof note"


def test_build_exact_title_query_filters_only_the_disposable_title():
    query = build_exact_title_query(DISPOSABLE_APPLE_TO_NOTION_TITLE)

    assert query == {
        "filter": {
            "property": "Task Name",
            "title": {"equals": DISPOSABLE_APPLE_TO_NOTION_TITLE},
        },
        "page_size": 10,
    }


def test_parse_notion_task_extracts_safe_sync_fields():
    page = {
        "id": "page-id",
        "url": "https://notion.so/page-id",
        "last_edited_time": "2026-05-25T13:30:00.000Z",
        "properties": {
            "Task Name": {
                "type": "title",
                "title": [{"plain_text": "[SYNC TEST] Notion to Apple"}],
            },
            "Notes": {
                "type": "rich_text",
                "rich_text": [{"plain_text": "Proof note"}],
            },
            "Status": {"type": "status", "status": {"name": "Done"}},
            "Priority": {"type": "select", "select": {"name": "High"}},
            "Due Date": {"type": "date", "date": {"start": "2026-05-26"}},
            "Reminder At": {
                "type": "date",
                "date": {"start": "2026-05-26T09:30:00.000+01:00"},
            },
            "Apple Sync ID": {
                "type": "rich_text",
                "rich_text": [{"plain_text": "sync-id"}],
            },
            "Apple Reminder ID": {
                "type": "rich_text",
                "rich_text": [{"plain_text": "apple-id"}],
            },
        },
    }

    task = parse_notion_task(page)

    assert task.page_id == "page-id"
    assert task.title == "[SYNC TEST] Notion to Apple"
    assert task.notes == "Proof note"
    assert task.completed is True
    assert task.status == "Done"
    assert task.priority == "High"
    assert task.due_is_all_day is True
    assert task.apple_sync_id == "sync-id"
    assert task.apple_reminder_id == "apple-id"
    assert task.url == "https://notion.so/page-id"


def test_build_apple_sync_id_query_only_reads_enrolled_rows():
    query = build_apple_sync_id_query(page_size=25)

    assert query == {
        "filter": {
            "property": "Apple Sync ID",
            "rich_text": {"is_not_empty": True},
        },
        "page_size": 25,
    }


def test_build_apple_reminder_id_patch_updates_only_identity_receipt():
    patch = build_apple_reminder_id_patch("apple-id")

    assert patch == {
        "Apple Reminder ID": {
            "rich_text": [{"text": {"content": "apple-id"}}],
        }
    }
