"""Read-only cached milestone HVA context for new, restored, and edited notes."""

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.models import Customer, Job, Milestone, MsxTask, Note, db
from app.services.hva_inventory import get_milestone_hvas
from app.services.msx_api import TASK_CATEGORIES


@pytest.fixture
def milestone_hvas(client):
    """Create isolated local history across milestones, categories, and statuses."""
    category = next(item for item in TASK_CATEGORIES if item["is_hva"])
    customer = Customer(name="HVA context customer", tpid=998877)
    milestones = [
        Milestone(title=f"Milestone {index}", msx_milestone_id=str(uuid4()),
                  url="https://example.test/milestone", customer=customer)
        for index in range(3)
    ]
    db.session.add_all(milestones)
    db.session.flush()
    tasks = []
    for index, state in enumerate([0, 1, 2, None]):
        task = MsxTask(
            msx_task_id=str(uuid4()), subject=f"Logged activity {index}",
            description='<script>alert("not markup")</script>',
            task_category=category["value"], task_category_name=category["label"],
            is_hva=True, statecode=state, milestone_id=milestones[0].id,
            is_current_owner=index != 2, duration_minutes=45,
            msx_created_on=datetime(2026, 10, index + 1, 12, tzinfo=timezone.utc),
            due_date=datetime(2026, 10, 8, 12, tzinfo=timezone.utc),
            actual_end=datetime(2026, 10, 7, 12, tzinfo=timezone.utc) if state == 1 else None,
        )
        db.session.add(task)
        tasks.append(task)
    for milestone, is_hva, code in [
        (milestones[1], True, category["value"]),
        (milestones[0], False, category["value"]),
        (milestones[0], True, 0),
    ]:
        db.session.add(MsxTask(
            msx_task_id=str(uuid4()), subject="Other activity",
            task_category=code, is_hva=is_hva, milestone_id=milestone.id,
        ))
    db.session.commit()
    return milestones, tasks


def test_cached_history_is_complete_scoped_case_insensitive_and_read_only(
    client, milestone_hvas, monkeypatch,
):
    """History includes all statuses and past owners, but excludes non-HVA categories."""
    milestones, tasks = milestone_hvas
    remote = Mock(side_effect=AssertionError("History must not fetch MSX."))
    monkeypatch.setattr("app.routes.msx.get_milestones_by_account", remote)
    first = milestones[0].msx_milestone_id
    empty = milestones[2].msx_milestone_id
    unknown = str(uuid4())
    before = MsxTask.query.count(), Milestone.query.count(), Job.query.count()
    response = client.get("/api/msx/milestone-hvas", query_string=[
        ("milestone_id", first.upper()), ("milestone_id", empty), ("milestone_id", unknown),
    ])
    assert response.status_code == 200
    rows = response.json["milestones"][first]
    assert [row["subject"] for row in rows] == [task.subject for task in reversed(tasks)]
    assert {row["status"] for row in rows} == {"Open", "Completed", "Canceled", "Unknown"}
    assert rows[0]["duration_minutes"] == 45
    assert rows[0]["created"] == "2026-10-04T12:00:00+00:00"
    assert rows[0]["completed"] is None
    assert rows[0]["description"] == '<script>alert("not markup")</script>'
    assert response.json["milestones"][empty] == []
    assert response.json["milestones"][unknown] == []
    assert milestones[1].msx_milestone_id not in response.json["milestones"]
    assert (MsxTask.query.count(), Milestone.query.count(), Job.query.count()) == before
    assert get_milestone_hvas([]) == {}
    remote.assert_not_called()


def test_history_does_not_silently_truncate_large_milestone_lists(client, milestone_hvas):
    """Every cached HVA is available, independent of the report's page size."""
    milestones, tasks = milestone_hvas
    for index in range(105):
        db.session.add(MsxTask(
            msx_task_id=str(uuid4()), subject=f"Extra activity {index}",
            task_category=tasks[0].task_category, is_hva=True, milestone_id=milestones[0].id,
        ))
    db.session.commit()
    result = get_milestone_hvas([milestones[0].msx_milestone_id])
    assert len(result[milestones[0].msx_milestone_id]) == 109


def test_edit_form_exposes_existing_milestones_to_the_same_history_controller(
    client, sample_data, milestone_hvas,
):
    """Saved selections initialize history without needing to remove and reselect them."""
    from bs4 import BeautifulSoup

    milestones, _ = milestone_hvas
    milestone = milestones[0]
    milestone.customer_id = sample_data["customer1_id"]
    note = db.session.get(Note, sample_data["call1_id"])
    note.milestones.append(milestone)
    db.session.commit()
    response = client.get(f"/note/{note.id}/edit")
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    selected = soup.select_one("#selected_milestone [data-msx-milestone-id]")
    assert selected["data-msx-milestone-id"] == milestone.msx_milestone_id
    assert soup.select_one('script[src*="/ui-note-milestone-hvas.js"][defer]')


@pytest.mark.parametrize("ids", [[], [""], ["not-a-guid"], [str(uuid4())] * 101])
def test_invalid_history_requests_fail_explicitly(client, ids):
    """Malformed or unbounded requests never silently widen the lookup."""
    response = client.get("/api/msx/milestone-hvas", query_string=[
        ("milestone_id", value) for value in ids
    ])
    assert response.status_code == 400
    assert response.json["success"] is False
    assert response.json["error"]
    assert Job.query.count() == 0


def test_history_database_failure_is_logged_and_retryable(client, monkeypatch, caplog):
    """Read failures are visible rather than appearing as an empty task history."""
    monkeypatch.setattr("app.services.hva_inventory.get_milestone_hvas",
                        Mock(side_effect=SQLAlchemyError("database unavailable")))
    response = client.get("/api/msx/milestone-hvas",
                          query_string={"milestone_id": str(uuid4())})
    assert response.status_code == 500
    assert response.json["success"] is False
    assert "Could not read cached milestone HVA history" in caplog.text


def test_note_hva_browser_controller():
    """Run the actual UI controller with deterministic DOM and network fixtures."""
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for note HVA controller tests.")
    subprocess.run(
        ["node", "--test", r"tests\js\note_milestone_hvas.test.cjs"],
        cwd=Path(__file__).parents[1], check=True,
    )
