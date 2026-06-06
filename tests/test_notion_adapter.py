import httpx
import pytest

from icloudbridge.sources.reminders.notion_adapter import (
    DEFAULT_NOTION_API_VERSION,
    DEFAULT_TASKS_DATA_SOURCE_ID,
    DISPOSABLE_APPLE_TO_NOTION_TITLE,
    DISPOSABLE_NOTION_TO_APPLE_TITLE,
    NotionTasksAdapter,
    _find_token_in_json,
    build_apple_identity_patch,
    build_apple_origin_task_properties,
    build_apple_reminder_id_patch,
    build_apple_sync_id_query,
    build_disposable_task_properties,
    build_exact_title_query,
    build_task_proof_update_properties,
    build_task_update_from_apple_properties,
    build_unenrolled_area_query,
    parse_notion_task,
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


def _retry_test_adapter(responses, sleep_calls=None, **kwargs):
    calls = []

    def handler(request):
        calls.append(request)
        response = responses[min(len(calls) - 1, len(responses) - 1)]
        return response

    async def fake_sleep(delay):
        if sleep_calls is not None:
            sleep_calls.append(delay)

    adapter = NotionTasksAdapter(
        token="secret_test",
        api_version=DEFAULT_NOTION_API_VERSION,
        base_url="https://api.notion.test",
        transport=httpx.MockTransport(handler),
        sleep=fake_sleep,
        **kwargs,
    )
    return adapter, calls


@pytest.mark.asyncio
async def test_notion_adapter_retries_429_with_retry_after_then_returns_payload():
    sleep_calls = []
    adapter, calls = _retry_test_adapter(
        [
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"ok": True}),
        ],
        sleep_calls=sleep_calls,
    )

    payload = await adapter.retrieve_data_source(DEFAULT_TASKS_DATA_SOURCE_ID)

    assert payload == {"ok": True}
    assert len(calls) == 2
    assert sleep_calls == [0.0]


@pytest.mark.asyncio
async def test_notion_adapter_uses_default_delay_for_missing_or_invalid_retry_after():
    missing_sleep_calls = []
    missing_adapter, _ = _retry_test_adapter(
        [
            httpx.Response(429),
            httpx.Response(200, json={"ok": True}),
        ],
        sleep_calls=missing_sleep_calls,
        default_rate_limit_delay=1.25,
    )

    await missing_adapter.retrieve_data_source(DEFAULT_TASKS_DATA_SOURCE_ID)

    invalid_sleep_calls = []
    invalid_adapter, _ = _retry_test_adapter(
        [
            httpx.Response(429, headers={"Retry-After": "later"}),
            httpx.Response(200, json={"ok": True}),
        ],
        sleep_calls=invalid_sleep_calls,
        default_rate_limit_delay=1.5,
    )

    await invalid_adapter.retrieve_data_source(DEFAULT_TASKS_DATA_SOURCE_ID)

    assert missing_sleep_calls == [1.25]
    assert invalid_sleep_calls == [1.5]


@pytest.mark.asyncio
async def test_notion_adapter_clamps_retry_after_to_max_delay():
    sleep_calls = []
    adapter, _ = _retry_test_adapter(
        [
            httpx.Response(429, headers={"Retry-After": "99.5"}),
            httpx.Response(200, json={"ok": True}),
        ],
        sleep_calls=sleep_calls,
        max_rate_limit_delay=10.0,
    )

    await adapter.retrieve_data_source(DEFAULT_TASKS_DATA_SOURCE_ID)

    assert sleep_calls == [10.0]


@pytest.mark.asyncio
async def test_notion_adapter_raises_after_rate_limit_retry_budget():
    sleep_calls = []
    adapter, calls = _retry_test_adapter(
        [httpx.Response(429, headers={"Retry-After": "0"})],
        sleep_calls=sleep_calls,
        max_rate_limit_retries=2,
    )

    with pytest.raises(httpx.HTTPStatusError):
        await adapter.retrieve_data_source(DEFAULT_TASKS_DATA_SOURCE_ID)

    assert len(calls) == 3
    assert sleep_calls == [0.0, 0.0]


@pytest.mark.asyncio
async def test_notion_adapter_does_not_retry_non_429_status_errors():
    sleep_calls = []
    adapter, calls = _retry_test_adapter(
        [httpx.Response(500)],
        sleep_calls=sleep_calls,
    )

    with pytest.raises(httpx.HTTPStatusError):
        await adapter.retrieve_data_source(DEFAULT_TASKS_DATA_SOURCE_ID)

    assert len(calls) == 1
    assert sleep_calls == []


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


def test_build_unenrolled_area_query_reads_only_unlinked_area_rows():
    query = build_unenrolled_area_query("Life", page_size=25, start_cursor="cursor")

    assert query == {
        "filter": {
            "and": [
                {"property": "Area", "select": {"equals": "Life"}},
                {"property": "Apple Sync ID", "rich_text": {"is_empty": True}},
                {"property": "Apple Reminder ID", "rich_text": {"is_empty": True}},
            ]
        },
        "page_size": 25,
        "start_cursor": "cursor",
    }


def test_build_apple_reminder_id_patch_updates_only_identity_receipt():
    patch = build_apple_reminder_id_patch("apple-id")

    assert patch == {
        "Apple Reminder ID": {
            "rich_text": [{"text": {"content": "apple-id"}}],
        }
    }


def test_build_apple_identity_patch_updates_both_identity_receipts():
    patch = build_apple_identity_patch("apple-reminders:sync", "apple-id")

    assert patch == {
        "Apple Sync ID": {
            "rich_text": [{"text": {"content": "apple-reminders:sync"}}],
        },
        "Apple Reminder ID": {
            "rich_text": [{"text": {"content": "apple-id"}}],
        },
    }


def test_build_apple_origin_task_properties_uses_safe_defaults_for_incomplete_reminder():
    properties = build_apple_origin_task_properties(
        title="[SYNC TEST] Apple to Notion",
        notes="From Apple",
        apple_sync_id="apple-reminders:uuid",
        apple_reminder_id="apple-id",
        completed=False,
        due_date=None,
        due_is_all_day=False,
    )

    assert properties["Task Name"]["title"][0]["text"]["content"] == "[SYNC TEST] Apple to Notion"
    assert properties["Notes"]["rich_text"][0]["text"]["content"] == "From Apple"
    assert properties["Apple Sync ID"]["rich_text"][0]["text"]["content"] == "apple-reminders:uuid"
    assert properties["Apple Reminder ID"]["rich_text"][0]["text"]["content"] == "apple-id"
    assert properties["Status"]["status"]["name"] == "Not started"
    assert properties["Source"]["select"]["name"] == "Manual"
    assert properties["Area"]["select"]["name"] == "Life"
    assert "Due Date" not in properties


def test_build_apple_origin_task_properties_accepts_configured_area():
    properties = build_apple_origin_task_properties(
        title="Academic task",
        notes=None,
        apple_sync_id="apple-reminders:uuid",
        apple_reminder_id="apple-id",
        completed=False,
        due_date=None,
        due_is_all_day=False,
        area="Academic",
    )

    assert properties["Area"]["select"]["name"] == "Academic"


def test_build_apple_origin_task_properties_maps_completed_and_due_date():
    from datetime import datetime, timezone

    due = datetime(2026, 5, 26, 9, 30, tzinfo=timezone.utc)

    properties = build_apple_origin_task_properties(
        title="Done Apple task",
        notes=None,
        apple_sync_id="apple-reminders:uuid",
        apple_reminder_id="apple-id",
        completed=True,
        due_date=due,
        due_is_all_day=False,
    )

    assert properties["Status"]["status"]["name"] == "Done"
    assert properties["Due Date"]["date"]["start"] == "2026-05-26T09:30:00+00:00"


def test_build_apple_origin_task_properties_preserves_date_only_due_date():
    from datetime import datetime

    properties = build_apple_origin_task_properties(
        title="All-day Apple task",
        notes=None,
        apple_sync_id="apple-reminders:uuid",
        apple_reminder_id="apple-id",
        completed=False,
        due_date=datetime(2026, 5, 26),
        due_is_all_day=True,
    )

    assert properties["Due Date"]["date"]["start"] == "2026-05-26"


def test_build_task_update_from_apple_properties_updates_safe_task_fields():
    from datetime import datetime, timezone

    due = datetime(2026, 5, 26, 9, 30, tzinfo=timezone.utc)

    properties = build_task_update_from_apple_properties(
        title="[SYNC TEST] Updated from Apple",
        notes="Updated note",
        completed=True,
        notion_priority="High",
        due_date=due,
        due_is_all_day=False,
    )

    assert properties["Task Name"]["title"][0]["text"]["content"] == "[SYNC TEST] Updated from Apple"
    assert properties["Notes"]["rich_text"][0]["text"]["content"] == "Updated note"
    assert properties["Status"]["status"]["name"] == "Done"
    assert properties["Priority"]["select"]["name"] == "High"
    assert properties["Due Date"]["date"]["start"] == "2026-05-26T09:30:00+00:00"


def test_build_task_update_from_apple_properties_omits_priority_when_none():
    properties = build_task_update_from_apple_properties(
        title="[SYNC TEST] Updated from Apple",
        notes=None,
        completed=False,
        notion_priority=None,
        due_date=None,
        due_is_all_day=False,
    )

    assert properties["Notes"]["rich_text"] == []
    assert properties["Status"]["status"]["name"] == "Not started"
    assert "Priority" not in properties
    assert properties["Due Date"]["date"] is None


def test_build_task_proof_update_properties_updates_only_requested_fields():
    from datetime import datetime, timedelta, timezone

    properties = build_task_proof_update_properties(
        title="[SYNC TEST] Proof title",
        notes="Proof note",
        completed=True,
        notion_priority="High",
        due_date=datetime(2026, 6, 2, 9, 30, tzinfo=timezone(timedelta(hours=1))),
        due_is_all_day=False,
    )

    assert properties["Task Name"]["title"][0]["text"]["content"] == "[SYNC TEST] Proof title"
    assert properties["Notes"]["rich_text"][0]["text"]["content"] == "Proof note"
    assert properties["Status"]["status"]["name"] == "Done"
    assert properties["Priority"]["select"]["name"] == "High"
    assert properties["Due Date"]["date"]["start"] == "2026-06-02T09:30:00+01:00"


def test_build_task_proof_update_properties_can_clear_due_date():
    properties = build_task_proof_update_properties(
        due_date=None,
        due_is_all_day=False,
        clear_due_date=True,
    )

    assert properties == {"Due Date": {"date": None}}
