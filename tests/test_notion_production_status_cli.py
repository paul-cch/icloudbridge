from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import icloudbridge.cli.main as cli
from icloudbridge.cli.main import (
    _production_status_failure_row,
    _production_status_row,
    _production_status_targets,
    app,
)
from icloudbridge.utils.db import NotionRemindersDB


class _Plan:
    def __init__(self, counts):
        self.counts = counts


def test_production_status_targets_default_to_configured_mappings():
    config = SimpleNamespace(
        reminders=SimpleNamespace(
            notion_area_mappings={
                "Life": "Life",
                "Dissertation": "Dissertation",
                "Academic": "Academic",
            }
        )
    )

    assert _production_status_targets(config, None) == [
        "Life",
        "Dissertation",
        "Academic",
    ]


def test_production_status_targets_dedupe_explicit_filters():
    config = SimpleNamespace(reminders=SimpleNamespace(notion_area_mappings={}))

    assert _production_status_targets(config, ["Life", "Life", "Academic"]) == [
        "Life",
        "Academic",
    ]


def test_production_status_row_summarizes_all_plan_counts():
    context = {
        "area": "Life",
        "create_plan": _Plan(
            {"NOOP": 2, "CREATE_APPLE": 3, "CREATE_NOTION": 5}
        ),
        "update_plan": _Plan(
            {
                "NOOP": 7,
                "NEEDS_BASELINE": 11,
                "UPDATE_APPLE": 13,
                "UPDATE_NOTION": 17,
                "CONFLICT": 19,
            }
        ),
        "recovery_plan": _Plan(
            {"NOOP": 23, "RECOVER_APPLE_ID": 29, "UNRECOVERED": 31}
        ),
        "grace_plan": _Plan(
            {
                "NOOP": 37,
                "MISSING_APPLE_FIRST_SEEN": 41,
                "MISSING_APPLE_STILL_MISSING": 43,
                "MISSING_NOTION_FIRST_SEEN": 47,
                "MISSING_NOTION_STILL_MISSING": 53,
                "UNTRACKED": 59,
            }
        ),
    }

    row = _production_status_row("Life", context)

    assert row == {
        "apple_calendar": "Life",
        "notion_area": "Life",
        "status": "OK",
        "create_noop": 2,
        "create_apple": 3,
        "create_notion": 5,
        "needs_baseline": 11,
        "update_apple": 13,
        "update_notion": 17,
        "conflict": 19,
        "recover_apple_id": 29,
        "unrecovered": 31,
        "missing_apple_first_seen": 41,
        "missing_apple_still_missing": 43,
        "missing_notion_first_seen": 47,
        "missing_notion_still_missing": 53,
        "marker_writes": 88,
        "untracked": 59,
        "error": "",
    }


def test_production_status_failure_row_keeps_failed_list_visible():
    row = _production_status_failure_row("Academic", "not found")

    assert row["apple_calendar"] == "Academic"
    assert row["status"] == "FAILED"
    assert row["error"] == "not found"
    assert row["create_notion"] == ""


def _config(tmp_path):
    return SimpleNamespace(
        general=SimpleNamespace(config_file=None, log_level="INFO"),
        reminders_db_path=tmp_path / "reminders.sqlite",
        reminders=SimpleNamespace(
            notion_area_mappings={
                "Life": "Life",
                "Dissertation": "Dissertation",
                "Academic": "Academic",
            }
        ),
    )


def _status_context(area):
    return {
        "area": area,
        "create_plan": _Plan({"NOOP": 1, "CREATE_APPLE": 0, "CREATE_NOTION": 0}),
        "update_plan": _Plan(
            {
                "NOOP": 1,
                "NEEDS_BASELINE": 0,
                "UPDATE_APPLE": 0,
                "UPDATE_NOTION": 0,
                "CONFLICT": 0,
            }
        ),
        "recovery_plan": _Plan(
            {"NOOP": 1, "RECOVER_APPLE_ID": 0, "UNRECOVERED": 0}
        ),
        "grace_plan": _Plan(
            {
                "NOOP": 1,
                "MISSING_APPLE_FIRST_SEEN": 0,
                "MISSING_APPLE_STILL_MISSING": 0,
                "MISSING_NOTION_FIRST_SEEN": 0,
                "MISSING_NOTION_STILL_MISSING": 0,
                "UNTRACKED": 0,
            }
        ),
    }


@pytest.fixture
def cli_runner(monkeypatch, tmp_path):
    runner = CliRunner()
    monkeypatch.setattr(cli, "get_config_path", lambda: None)
    monkeypatch.setattr(cli, "set_config_path", lambda path: None)
    monkeypatch.setattr(cli, "setup_logging", lambda config, level_name: None)
    monkeypatch.setattr(cli, "load_config", lambda path=None: _config(tmp_path))
    return runner


def test_production_status_help_has_no_write_gates(cli_runner):
    result = cli_runner.invoke(app, ["reminders", "notion-production-status", "--help"])

    assert result.exit_code == 0
    assert "notion-production-status" in result.output
    assert "--apple-calendar" in result.output
    assert "--apply" not in result.output
    assert "--confirm-production" not in result.output


def test_production_status_defaults_to_all_mappings_and_readonly_receipts(
    cli_runner,
    monkeypatch,
):
    calls = []
    rows = []

    async def fake_read(
        data_source_id,
        apple_calendar,
        notion_token,
        api_version,
        notion_page_size,
        db_path,
        config,
        initialize_receipts=True,
    ):
        calls.append((apple_calendar, initialize_receipts))
        return _status_context(config.reminders.notion_area_mappings[apple_calendar])

    monkeypatch.setattr(cli, "_read_notion_production_plans", fake_read)
    monkeypatch.setattr(cli, "_print_production_status", rows.extend)

    result = cli_runner.invoke(app, ["reminders", "notion-production-status"])

    assert result.exit_code == 0
    assert calls == [
        ("Life", False),
        ("Dissertation", False),
        ("Academic", False),
    ]
    assert [row["apple_calendar"] for row in rows] == [
        "Life",
        "Dissertation",
        "Academic",
    ]
    assert "No writes attempted" in result.output


def test_production_status_filters_lists_and_dedupes(cli_runner, monkeypatch):
    calls = []

    async def fake_read(
        data_source_id,
        apple_calendar,
        notion_token,
        api_version,
        notion_page_size,
        db_path,
        config,
        initialize_receipts=True,
    ):
        calls.append(apple_calendar)
        return _status_context(config.reminders.notion_area_mappings[apple_calendar])

    monkeypatch.setattr(cli, "_read_notion_production_plans", fake_read)
    monkeypatch.setattr(cli, "_print_production_status", lambda rows: None)

    result = cli_runner.invoke(
        app,
        [
            "reminders",
            "notion-production-status",
            "--apple-calendar",
            "Life",
            "--apple-calendar",
            "Life",
            "--apple-calendar",
            "Academic",
        ],
    )

    assert result.exit_code == 0
    assert calls == ["Life", "Academic"]


def test_production_status_partial_failure_renders_success_and_exits_nonzero(
    cli_runner,
    monkeypatch,
):
    rows = []

    async def fake_read(
        data_source_id,
        apple_calendar,
        notion_token,
        api_version,
        notion_page_size,
        db_path,
        config,
        initialize_receipts=True,
    ):
        if apple_calendar == "Academic":
            return None
        return _status_context(config.reminders.notion_area_mappings[apple_calendar])

    monkeypatch.setattr(cli, "_read_notion_production_plans", fake_read)
    monkeypatch.setattr(cli, "_print_production_status", rows.extend)

    result = cli_runner.invoke(
        app,
        [
            "reminders",
            "notion-production-status",
            "--apple-calendar",
            "Life",
            "--apple-calendar",
            "Academic",
        ],
    )

    assert result.exit_code == 1
    assert [row["apple_calendar"] for row in rows] == ["Life", "Academic"]
    assert rows[0]["status"] == "OK"
    assert rows[1]["status"] == "FAILED"


@pytest.mark.asyncio
async def test_notion_reminders_readonly_mapping_read_does_not_create_missing_db(tmp_path):
    db_path = tmp_path / "missing" / "notion-reminders.sqlite"
    db = NotionRemindersDB(db_path)

    with pytest.raises(FileNotFoundError):
        await db.get_all_notion_reminder_mappings_readonly()

    assert not db_path.exists()
