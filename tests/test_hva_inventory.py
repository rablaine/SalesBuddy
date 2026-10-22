"""Owned-HVA browsing and one-click status changes without live MSX writes."""

import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup
from sqlalchemy.exc import SQLAlchemyError

from app.models import Customer, Job, Milestone, MsxTask, PrefetchedMeeting, SyncStatus, db
from app.services.hva_inventory import get_hva_inventory
from app.services.msx_api import TASK_CATEGORIES
from app.services.salesiq_tools import execute_tool


@pytest.fixture
def hva_rows(client):
    """Create linked and unlinked activities across all MSX states in the test DB."""
    category = next(c for c in TASK_CATEGORIES if c["is_hva"])
    customer = Customer(name="Inventory customer", tpid=987654)
    db.session.add(customer)
    db.session.flush()
    milestone = Milestone(
        customer_id=customer.id, title="Inventory milestone",
        msx_milestone_id="inventory-milestone", url="https://example.test/milestone",
    )
    db.session.add(milestone)
    db.session.flush()
    rows = []
    for name, state, linked, created in (
        ("Open session", 0, True, datetime(2026, 10, 1, 9)),
        ("Completed session", 1, True, datetime(2026, 10, 2, 23, 59, 59)),
        ("Canceled session", 2, False, datetime(2020, 1, 1)),
        ("Unknown session", None, False, None),
    ):
        row = MsxTask(
            msx_task_id=f"inventory-{state}", subject=name, task_category=category["value"],
            task_category_name=category["label"], is_hva=True, statecode=state,
            milestone_id=milestone.id if linked else None,
            msx_regarding_name=None if linked else "Outside local inventory",
            msx_created_on=created, due_date=datetime(2026, 10, 3, 23, 59, 59),
            actual_end=datetime(2026, 10, 2, 23, 59, 59) if state == 1 else None,
        )
        db.session.add(row)
        rows.append(row)
    for task_id, is_hva, code, owned in (
        ("not-owned", True, category["value"], False),
        ("not-hva", False, 0, True),
        ("old-category", True, 0, True),
    ):
        db.session.add(MsxTask(
            msx_task_id=task_id, subject=task_id, task_category=code, is_hva=is_hva,
            is_current_owner=owned, milestone_id=milestone.id,
        ))
    db.session.commit()
    return {"rows": rows, "category": category, "customer": customer, "milestone": milestone}


def test_inventory_counts_each_owned_hva_once_including_unlinked_and_old_tasks(hva_rows):
    """No meeting, note, current-FY, active-milestone or local-customer gate hides activities."""
    data = get_hva_inventory(per_page=1)
    assert data["summary"] == {"total": 4, "open": 1, "completed": 1,
                               "canceled": 1, "unknown": 1}
    assert data["pages"] == 4
    assert len(data["rows"]) == 1
    all_rows = get_hva_inventory()["rows"]
    assert len({row["msx_task_id"] for row in all_rows}) == 4
    unknown = next(row for row in all_rows if row["statecode"] is None)
    assert unknown["status"] == "Unknown"
    assert unknown["created"] is None
    assert unknown["milestone_id"] is None
    assert unknown["related"] == "Outside local inventory"
    assert data["complete_inventory"] is False
    SyncStatus.mark_completed("owned_tasks", success=True)
    assert get_hva_inventory()["complete_inventory"] is True


def test_status_customer_category_search_and_inclusive_utc_date_windows(hva_rows):
    """Filters apply to the activity itself and full matching totals, not to its parent date."""
    assert get_hva_inventory(status="completed")["summary"]["total"] == 1
    assert get_hva_inventory(customer_id=hva_rows["customer"].id)["summary"]["total"] == 2
    assert get_hva_inventory(category=hva_rows["category"]["value"])["summary"]["total"] == 4
    assert get_hva_inventory(search="Outside local")["summary"]["total"] == 2
    for field in ("created", "completed"):
        data = get_hva_inventory(date_field=field, date_from="2026-10-02", date_to="2026-10-02")
        assert [row["subject"] for row in data["rows"]] == ["Completed session"]
    assert get_hva_inventory(date_field="due", date_to="2026-10-03")["summary"]["total"] == 4
    assert get_hva_inventory(date_field="due", date_from="2026-10-04")["summary"]["total"] == 0


@pytest.mark.parametrize("sort", ["subject", "status", "category", "customer", "related",
                                 "created", "due", "completed"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_sorting_is_global_across_pages_with_stable_ties_and_unknowns_last(
    hva_rows, sort, direction,
):
    """Paging cannot sort only the currently visible records."""
    all_rows = get_hva_inventory(sort=sort, direction=direction)["rows"]
    by_id = sorted(all_rows, key=lambda row: row["id"], reverse=True)
    expected = sorted(
        (row for row in by_id if row[sort] is not None),
        key=lambda row: row[sort].lower(), reverse=direction == "desc",
    ) + [row for row in by_id if row[sort] is None]
    assert [row["id"] for row in all_rows] == [row["id"] for row in expected]
    paged = [
        get_hva_inventory(sort=sort, direction=direction, per_page=1, page=page)["rows"][0]
        for page in range(1, 5)
    ]
    assert [row["id"] for row in paged] == [row["id"] for row in all_rows]
    if sort in ("created", "completed", "customer", "related"):
        values = [row[sort] for row in all_rows]
        if None in values:
            assert values[-1] is None


@pytest.mark.parametrize("query", [
    "status=bogus", "category=0", "category=abc", "customer_id=-1",
    "date_field=call_date", "date_from=not-a-date",
    "date_from=2026-10-03&date_to=2026-10-01",
    "sort=bogus", "direction=random", "page=0", "per_page=101",
])
def test_invalid_inventory_filters_fail_explicitly_without_admitting_work(client, query):
    """Bad inputs never silently widen the report or trigger a sync."""
    response = client.get(f"/api/reports/hvas?{query}")
    assert response.status_code == 400
    assert response.json["success"] is False
    assert response.json["error"]
    assert Job.query.count() == 0


def test_view_discovery_shared_refresh_and_read_tool_match_the_inventory(client, hva_rows):
    """Browse, Reports, breadcrumbs and SalesIQ expose the same read-only activity view."""
    response = client.get("/reports/hvas?status=completed&per_page=25")
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    assert soup.select_one("body.sb-page-frame")
    assert soup.h1.get_text() == "HVAs"
    assert soup.select_one('#navBrowse a[href="/reports/hvas"]')
    assert soup.select_one('#navReports a[href="/reports/hvas"]')
    assert soup.select_one('.sb-context-bar a[href="/reports"]')
    assert len(soup.select("#hvaTable tbody tr")) == 1
    assert soup.select_one("#hvaTable[data-sb-sort-server]")
    assert len(soup.select("#hvaTable th[data-sb-sort-type]")) == 8
    assert soup.select_one('[data-sb-sync-progress="activity-reconciliation"]')
    assert "HVAs" in client.get("/reports").get_data(as_text=True)
    api = client.get("/api/reports/hvas?status=completed&per_page=25").json
    tool = execute_tool("report_hvas", {"status": "completed", "per_page": 25})
    assert api["summary"] == tool["summary"] == {"total": 1, "open": 0, "completed": 1,
                                               "canceled": 0, "unknown": 0}
    assert api["rows"] == tool["rows"]
    assert Job.query.count() == 0


def test_empty_inventory_teaches_refresh_without_sync_on_get(client):
    """An uninitialized report is honest about what has not been imported."""
    soup = BeautifulSoup(client.get("/reports/hvas").data, "html.parser")
    assert "No HVA tasks synced yet" in soup.get_text()
    assert Job.query.count() == 0


def test_custom_valid_page_size_remains_selected_in_the_browser(client, hva_rows):
    """A valid API/tool page size must not disagree with the browser's selected value."""
    soup = BeautifulSoup(client.get("/reports/hvas?per_page=1").data, "html.parser")
    assert soup.select_one("#hvaPageSize option[selected]")["value"] == "1"
    assert len(soup.select("#hvaTable tbody tr")) == 1


def test_status_actions_are_next_to_status_without_confirmation(client, hva_rows):
    """Owned tasks can complete or reopen beside their status, except canceled tasks."""
    soup = BeautifulSoup(client.get("/reports/hvas").data, "html.parser")
    actions = soup.select(".hva-task-action[data-hva-action]")
    assert len(actions) == 3
    for task in hva_rows["rows"]:
        row = soup.select_one(f'tr[data-task-id="{task.id}"]')
        button = row.select_one("[data-hva-toggle]")
        assert bool(button) == (task.statecode != 2)
        if button:
            action = button.parent
            verb = "reopen" if task.statecode == 1 else "complete"
            assert action["data-action"] == verb
            assert action["data-action-url"] == f"/api/reports/hvas/{task.id}/{verb}"
            assert button.find_parent("td") == row.select("td")[1]
            assert button["aria-label"] == f"{verb.title()} {task.subject}"
    assert not soup.select("[data-hva-confirmation]")


def test_category_dropdown_includes_every_category_and_stars_only_hvas(client, hva_rows):
    """Every task, including canceled tasks, has the correctly selected editable category."""
    soup = BeautifulSoup(client.get("/reports/hvas").data, "html.parser")
    controls = soup.select("[data-hva-category]")
    assert len(controls) == 4
    for control in controls:
        row = control.find_parent("tr")
        assert control["aria-label"].startswith("Category for ")
        assert control.find_parent("td") == row.select("td")[2]
        options = control.select("option")
        assert len(options) == len(TASK_CATEGORIES)
        assert control.select_one("option[selected]")["value"] == str(
            hva_rows["category"]["value"],
        )
        for option, category in zip(options, TASK_CATEGORIES):
            assert option["value"] == str(category["value"])
            assert option.get_text() == (
                ("\N{BLACK STAR} " if category["is_hva"] else "") + category["label"]
            )


@pytest.mark.parametrize("state", [0, 1, 2, None])
@pytest.mark.parametrize("is_hva", [True, False])
def test_category_change_updates_msx_and_cached_classification(
    client, hva_rows, monkeypatch, state, is_hva,
):
    """Any owned task can change category without losing state, dates or historical links."""
    task = hva_rows["rows"][0]
    task.statecode = state
    db.session.commit()
    before = (task.statecode, task.actual_end, task.due_date, task.milestone_id)
    category = next(c for c in TASK_CATEGORIES
                    if c["is_hva"] == is_hva and c["value"] != task.task_category)
    update = Mock(return_value={"success": True})
    monkeypatch.setattr("app.services.msx_api.update_task", update)
    response = client.post(f"/api/reports/hvas/{task.id}/category",
                           json={"category": category["value"]})
    assert response.status_code == 200
    assert response.json["category_name"] == category["label"]
    update.assert_called_once_with(task.msx_task_id, {"msp_taskcategory": category["value"]})
    db.session.refresh(task)
    assert task.task_category == category["value"]
    assert task.task_category_name == category["label"]
    assert task.is_hva == is_hva
    assert (task.statecode, task.actual_end, task.due_date, task.milestone_id) == before
    assert (task.id in {r["id"] for r in get_hva_inventory()["rows"]}) == is_hva
    assert client.post(f"/api/reports/hvas/{task.id}/category",
                       json={"category": category["value"]}).json["success"]
    assert update.call_count == 1


@pytest.mark.parametrize("body", [
    None, [], {}, {"category": True}, {"category": "861980004"},
    {"category": 861980004.0}, {"category": 123},
])
def test_invalid_category_is_rejected_without_msx_write(client, hva_rows, monkeypatch, body):
    """Malformed inputs cannot clear the category or silently select a different one."""
    update = Mock()
    monkeypatch.setattr("app.services.msx_api.update_task", update)
    task = hva_rows["rows"][0]
    response = client.post(f"/api/reports/hvas/{task.id}/category", json=body)
    assert response.status_code == 400
    assert response.json["error"]
    update.assert_not_called()


def test_category_change_requires_an_owned_task_and_post(client, hva_rows, monkeypatch):
    """Unowned tasks and unknown IDs cannot be changed through the inventory."""
    update = Mock()
    monkeypatch.setattr("app.services.msx_api.update_task", update)
    task = MsxTask.query.filter_by(subject="not-owned").one()
    for task_id in (task.id, 999999):
        url = f"/api/reports/hvas/{task_id}/category"
        assert client.get(url).status_code == 405
        assert client.post(url, json={"category": hva_rows["category"]["value"]}).status_code == 404
    update.assert_not_called()


@pytest.mark.parametrize("cache_failure", [False, True])
def test_category_failure_keeps_existing_cache_and_exposes_remote_change(
    client, hva_rows, monkeypatch, cache_failure,
):
    """Remote errors permit retry, while a successful remote write requires reconciliation."""
    task = hva_rows["rows"][0]
    original = (task.task_category, task.task_category_name, task.is_hva)
    category = next(c for c in TASK_CATEGORIES if not c["is_hva"])
    monkeypatch.setattr("app.services.msx_api.update_task", Mock(return_value={
        "success": cache_failure, "error": "Access denied",
    }))
    if cache_failure:
        monkeypatch.setattr(db.session, "commit", Mock(side_effect=SQLAlchemyError("Disk full")))
    response = client.post(f"/api/reports/hvas/{task.id}/category",
                           json={"category": category["value"]})
    assert response.status_code == (500 if cache_failure else 502)
    assert response.json.get("remote_changed", False) is cache_failure
    db.session.refresh(task)
    assert (task.task_category, task.task_category_name, task.is_hva) == original


def test_msx_category_update_payload_and_validation(monkeypatch):
    """The shared task updater sends the Dataverse field and rejects invalid option values."""
    from app.services import msx_api

    remote = Mock(return_value=Mock(status_code=204))
    monkeypatch.setattr(msx_api, "_msx_request", remote)
    monkeypatch.setattr(msx_api, "_is_writeback_disabled", lambda: False)
    fields = {"msp_taskcategory": TASK_CATEGORIES[0]["value"]}
    assert msx_api.update_task("task-guid", fields)["success"]
    assert remote.call_args.kwargs["json_data"] == fields
    remote.reset_mock()
    for invalid in (True, "861980004", 861980004.0, 123, None):
        assert not msx_api.update_task("task-guid", {"msp_taskcategory": invalid})["success"]
    remote.assert_not_called()


@pytest.mark.parametrize("state", [0, None, 99])
def test_completion_updates_msx_and_cache_without_deleting_activity_links(
    client, hva_rows, sample_data, monkeypatch, state,
):
    """The remote write uses the existing client and preserves all historical relationships."""
    task = hva_rows["rows"][0]
    now = datetime.now(timezone.utc)
    meeting = PrefetchedMeeting(
        workiq_id="hva-completion-meeting", subject="HVA completion meeting",
        meeting_date=date.today(), start_time=now, end_time=now + timedelta(minutes=30),
        expires_at=now + timedelta(days=1),
    )
    db.session.add(meeting)
    db.session.flush()
    task.statecode = state
    task.note_id = sample_data["call1_id"]
    task.meeting_id = meeting.id
    db.session.commit()
    before = (task.id, task.milestone_id, task.note_id, task.meeting_id)
    close = Mock(return_value={"success": True})
    monkeypatch.setattr("app.services.msx_api.close_task", close)
    response = client.post(f"/api/reports/hvas/{task.id}/complete")
    assert response.status_code == 200
    assert response.json["success"] is True
    close.assert_called_once_with(task.msx_task_id)
    db.session.refresh(task)
    assert (task.id, task.milestone_id, task.note_id, task.meeting_id) == before
    assert task.statecode == 1
    assert task.statuscode == 5
    assert now <= task.actual_end.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)
    assert MsxTask.query.count() == 7
    assert get_hva_inventory()["summary"]["completed"] == 2
    assert get_hva_inventory(status="open")["summary"]["total"] == 0
    completed = task.actual_end
    assert client.post(f"/api/reports/hvas/{task.id}/complete").json["already_completed"]
    assert close.call_count == 1
    db.session.refresh(task)
    assert task.actual_end == completed


def test_completion_supports_unknown_tasks_outside_local_milestones(client, hva_rows, monkeypatch):
    """Completing an unlinked owned HVA must not require inventing a local milestone."""
    task = hva_rows["rows"][3]
    close = Mock(return_value={"success": True})
    monkeypatch.setattr("app.services.msx_api.close_task", close)
    assert client.post(f"/api/reports/hvas/{task.id}/complete").json["success"] is True
    db.session.refresh(task)
    assert task.milestone_id is None
    assert task.statecode == 1


@pytest.mark.parametrize("task_name, expected_status", [
    ("Canceled session", 409), ("not-owned", 404), ("not-hva", 404), ("old-category", 404),
])
def test_ineligible_completion_is_rejected_without_an_msx_write(
    client, hva_rows, monkeypatch, task_name, expected_status,
):
    """The canceled policy and ownership/category gates are enforced server-side."""
    task = MsxTask.query.filter_by(subject=task_name).one()
    original_state = task.statecode
    close = Mock()
    monkeypatch.setattr("app.services.msx_api.close_task", close)
    response = client.post(f"/api/reports/hvas/{task.id}/complete")
    assert response.status_code == expected_status
    assert response.json["success"] is False
    assert response.json["error"]
    close.assert_not_called()
    db.session.refresh(task)
    assert task.statecode == original_state


def test_msx_completion_failure_keeps_cached_status_and_reports_error(client, hva_rows, monkeypatch):
    """Remote failures must never look like completed activities."""
    task = hva_rows["rows"][0]
    monkeypatch.setattr("app.services.msx_api.close_task", Mock(return_value={
        "success": False, "error": "Access denied in MSX",
    }))
    response = client.post(f"/api/reports/hvas/{task.id}/complete")
    assert response.status_code == 502
    assert response.json["error"] == "Access denied in MSX"
    db.session.refresh(task)
    assert task.statecode == 0
    assert task.actual_end is None


def test_completion_cache_failure_exposes_successful_remote_change(client, hva_rows, monkeypatch):
    """A failed cache commit explicitly advises reconciliation rather than repeating the write."""
    task = hva_rows["rows"][0]
    monkeypatch.setattr("app.services.msx_api.close_task", Mock(return_value={"success": True}))
    monkeypatch.setattr(db.session, "commit", Mock(side_effect=SQLAlchemyError("Disk full")))
    response = client.post(f"/api/reports/hvas/{task.id}/complete")
    assert response.status_code == 500
    assert response.json["remote_changed"] is True
    assert "Refresh MSX tasks" in response.json["error"]
    db.session.refresh(task)
    assert task.statecode == 0


def test_completion_is_post_only_and_unknown_id_is_explicit(client, monkeypatch):
    """Merely reading the report or following a URL cannot complete an activity."""
    close = Mock()
    monkeypatch.setattr("app.services.msx_api.close_task", close)
    assert client.get("/api/reports/hvas/12345/complete").status_code == 405
    response = client.post("/api/reports/hvas/12345/complete")
    assert response.status_code == 404
    assert response.json["error"]
    close.assert_not_called()


def test_reopening_preserves_links_clears_completion_and_is_idempotent(
    client, hva_rows, monkeypatch,
):
    """Reopening restores the MSX Not Started state without losing activity history."""
    task = hva_rows["rows"][1]
    before = (task.id, task.milestone_id, task.note_id, task.meeting_id, task.due_date)
    reopen = Mock(return_value={"success": True})
    monkeypatch.setattr("app.services.msx_api.reopen_task", reopen)
    response = client.post(f"/api/reports/hvas/{task.id}/reopen")
    assert response.status_code == 200
    assert response.json["statecode"] == 0
    reopen.assert_called_once_with(task.msx_task_id)
    db.session.refresh(task)
    assert task.statecode == 0
    assert task.statuscode == 2
    assert task.actual_end is None
    assert (task.id, task.milestone_id, task.note_id, task.meeting_id, task.due_date) == before
    assert get_hva_inventory()["summary"]["completed"] == 0
    assert get_hva_inventory()["summary"]["open"] == 2
    assert client.post(f"/api/reports/hvas/{task.id}/reopen").json["success"]
    assert reopen.call_count == 1


@pytest.mark.parametrize("task_name, expected_status", [
    ("Canceled session", 409), ("not-owned", 404), ("not-hva", 404), ("old-category", 404),
])
def test_ineligible_reopening_never_writes_msx(
    client, hva_rows, monkeypatch, task_name, expected_status,
):
    """Reopening has the same ownership, category and canceled-state protections."""
    reopen = Mock()
    monkeypatch.setattr("app.services.msx_api.reopen_task", reopen)
    task = MsxTask.query.filter_by(subject=task_name).one()
    assert client.post(f"/api/reports/hvas/{task.id}/reopen").status_code == expected_status
    reopen.assert_not_called()
    assert client.get(f"/api/reports/hvas/{task.id}/reopen").status_code == 405


@pytest.mark.parametrize("cache_failure", [False, True])
def test_reopening_failure_does_not_falsify_local_status(
    client, hva_rows, monkeypatch, cache_failure,
):
    """A failed remote write or cache commit leaves the cached completed record intact."""
    task = hva_rows["rows"][1]
    monkeypatch.setattr("app.services.msx_api.reopen_task", Mock(return_value={
        "success": cache_failure, "error": "Access denied",
    }))
    if cache_failure:
        monkeypatch.setattr(db.session, "commit", Mock(side_effect=SQLAlchemyError("Disk full")))
    response = client.post(f"/api/reports/hvas/{task.id}/reopen")
    assert response.status_code == (500 if cache_failure else 502)
    assert response.json.get("remote_changed", False) is cache_failure
    db.session.refresh(task)
    assert task.statecode == 1
    assert task.actual_end is not None


@pytest.mark.parametrize("completed", [True, False])
def test_msx_task_state_payload_and_writeback_gate(monkeypatch, completed):
    """MSX receives compatible state/reason pairs and reopening clears actualend."""
    from app.services import msx_api

    remote = Mock(return_value=Mock(status_code=204))
    monkeypatch.setattr(msx_api, "_msx_request", remote)
    monkeypatch.setattr(msx_api, "_is_writeback_disabled", lambda: False)
    change = msx_api.close_task if completed else msx_api.reopen_task
    assert change("task-guid")["success"]
    expected = {"statecode": 1, "statuscode": 5} if completed else {
        "statecode": 0, "statuscode": 2, "actualend": None,
    }
    assert remote.call_args.kwargs["json_data"] == expected
    monkeypatch.setattr(msx_api, "_is_writeback_disabled", lambda: True)
    remote.reset_mock()
    assert not change("task-guid")["success"]
    remote.assert_not_called()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for JS tests")
def test_hva_browser_controls():
    """Validate filtered navigation and the existing shared-sync completion contract."""
    test_file = Path(__file__).parent / "js" / "ui_hvas.test.cjs"
    result = subprocess.run(
        ["node", "--test", str(test_file)], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
