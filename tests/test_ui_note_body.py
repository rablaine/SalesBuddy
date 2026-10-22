"""Shared workbench note-body information, markup and command parity."""

from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytest
from bs4 import BeautifulSoup
from flask import Flask, render_template
from flask.testing import FlaskClient

from app.models import (
    CustomerContact, Engagement, InternalContact, Milestone, MsxTask, Note,
    NoteAttendee, Opportunity, Partner, PartnerContact, Project, SolutionEngineer, db,
)


RICH_CONTENT = (
    '<h2>Architecture</h2><p>Discuss <strong>Azure</strong> and <em>delivery</em>.</p>'
    '<ul><li>Design</li><li>Validate</li></ul><ol><li>Ship</li></ol>'
    '<blockquote>Customer context</blockquote><pre><code>SELECT 1;</code></pre>'
    '<table><thead><tr><th>Service</th></tr></thead><tbody><tr><td>SQL</td></tr></tbody>'
    '</table><p><a href="https://example.com/design">Design reference</a></p>'
    '<img src="/test-note-image.png" style="width:320px" alt="Architecture sketch">'
)


@pytest.fixture
def linked_body_note(app: Flask, sample_data: dict[str, int]) -> dict[str, int]:
    """Populate each optional metadata group in the isolated test database."""
    with app.app_context():
        note = db.session.get(Note, sample_data["call1_id"])
        assert note is not None
        note.content = RICH_CONTENT
        note.call_date = datetime(2026, 10, 3, 0, 0)
        note.updated_at = datetime(2026, 10, 3, 5, 0, tzinfo=timezone.utc)
        partner = Partner(name="Design Partner")
        project = Project(title="Architecture project")
        engagement = Engagement(title="Design engagement", customer_id=note.customer_id)
        opportunity = Opportunity(
            name="SQL architecture opportunity", msx_opportunity_id="note-body-opp",
            opportunity_number="7-ARCH", state="Open", estimated_value=12500,
            owner_name="Opportunity Owner", msx_url="https://example.com/opportunity",
        )
        milestone = Milestone(title="Architecture milestone", url="https://example.com/milestone")
        db.session.add_all([partner, project, engagement, opportunity, milestone])
        db.session.flush()
        note.partners.append(partner)
        note.projects.append(project)
        note.engagements.append(engagement)
        note.opportunities.append(opportunity)
        note.milestones.append(milestone)
        task = MsxTask(
            note_id=note.id, milestone_id=milestone.id, msx_task_id="note-body-task",
            msx_task_url="https://example.com/task", subject="Review architecture",
            task_category=1, task_category_name="Architecture Design Session", is_hva=True,
        )
        db.session.add(task)
        db.session.commit()
        return {
            "note": note.id, "partner": partner.id, "project": project.id,
            "engagement": engagement.id, "opportunity": opportunity.id, "milestone": milestone.id,
        }


@pytest.mark.parametrize("preview", [False, True])
def test_note_body_uses_native_panes_with_all_original_context(
    client: FlaskClient, linked_body_note: dict[str, int], preview: bool,
) -> None:
    """Full and fragment layouts retain every linked group without Bootstrap cards/grid."""
    note_id = linked_body_note["note"]
    path = f"/api/note/{note_id}/detail" if preview else f"/note/{note_id}"
    response = client.get(path)
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    root = soup.select_one(".sb-note-view[data-sb-note-detail]")
    assert root["data-sb-note-id"] == str(note_id)
    assert root.select_one(".sb-note-context")["aria-label"] == "Note details"
    assert root.select_one(".sb-note-main")["aria-label"] == "Note content"
    assert not root.select(".row, .card, .card-header, .card-body, .modal, [data-bs-toggle]")
    labels = {dt.get_text(strip=True) for dt in root.select(".sb-note-metadata dt")}
    assert labels == {
        "Date", "Customer", "Seller", "Territory", "Topics", "Partners",
        "Engagements", "Projects", "Milestones", "Opportunities", "MSX Tasks",
    }
    for entity, prefix in (
        ("partner", "partners"), ("project", "project"), ("engagement", "engagement"),
        ("milestone", "milestone"), ("opportunity", "opportunity"),
    ):
        assert root.select_one(f'a[href="/{prefix}/{linked_body_note[entity]}"]')
    for destination in ("opportunity", "task"):
        link = root.select_one(f'a[href="https://example.com/{destination}"]')
        assert link["target"] == "_blank"
    assert "7-ARCH" in root.get_text()
    assert "$12,500" in root.get_text()
    assert "Opportunity Owner" in root.get_text()
    assert "HVA" in root.get_text()
    assert "Architecture Design Session" in root.get_text()
    assert "October 03, 2026" in root.get_text()
    assert "at 12:00" not in root.get_text()
    assert len(root.select(".sb-note-timestamps .local-datetime")) == 2
    assert bool(root.select_one(".sb-note-page-header")) is not preview
    assert bool(soup.select_one("body.sb-note-reading-page")) is not preview
    groups = root.select(".sb-note-context > .sb-note-detail-group")
    assert [group["data-sb-note-group"] for group in groups] == ["context", "people", "work"]
    assert not root.select(".sb-note-detail-group h3, .sb-note-group-heading")
    assert {dt.get_text() for dt in groups[0].select("dt")} == {
        "Date", "Customer", "Seller", "Territory", "Topics",
    }
    assert {dt.get_text() for dt in groups[1].select("dt")} == {"Partners"}
    assert groups[2].select_one("[data-sb-note-actions]")
    assert not root.select("[data-sb-note-group][hidden]")


def test_rich_note_body_is_not_rewritten_or_reduced(
    client: FlaskClient, linked_body_note: dict[str, int],
) -> None:
    """Authored headings, lists, tables, code, formatting, links and image widths survive."""
    response = client.get(f'/api/note/{linked_body_note["note"]}/detail')
    soup = BeautifulSoup(response.data, "html.parser")
    content = soup.select_one(".call-content")
    assert content["tabindex"] == "0"
    assert RICH_CONTENT in response.text


@pytest.mark.parametrize("source", ["engagement", "project", "milestone", "opportunity", "task"])
def test_linked_work_group_keeps_each_independent_link_type(
    client: FlaskClient, app: Flask, linked_body_note: dict[str, int], source: str,
) -> None:
    """A work section still renders when just one kind of linked record is present."""
    with app.app_context():
        note = db.session.get(Note, linked_body_note["note"])
        assert note is not None
        relations = {
            "engagement": note.engagements, "project": note.projects,
            "milestone": note.milestones, "opportunity": note.opportunities,
        }
        for name, records in relations.items():
            if name != source:
                records.clear()
        if source != "task":
            for task in note.msx_tasks.all():
                db.session.delete(task)
        db.session.commit()
    response = client.get(f'/api/note/{linked_body_note["note"]}/detail')
    soup = BeautifulSoup(response.data, "html.parser")
    group = soup.select_one('[data-sb-note-group="work"]')
    assert group is not None
    labels = {
        "engagement": "Engagements", "project": "Projects", "milestone": "Milestones",
        "opportunity": "Opportunities", "task": "MSX Tasks",
    }
    assert [dt.get_text() for dt in group.select("dt")] == [labels[source]]
    assert bool(group.select_one("[data-sb-note-actions]")) is (source == "engagement")


def test_full_note_delete_is_native_and_keeps_existing_post(
    client: FlaskClient, linked_body_note: dict[str, int],
) -> None:
    """Delete stays explicit, cancellable and pointed at the unchanged write endpoint."""
    note_id = linked_body_note["note"]
    soup = BeautifulSoup(client.get(f"/note/{note_id}?next=/notes").data, "html.parser")
    assert soup.select_one(f'a[href="/note/{note_id}/edit?next=/notes"]')
    assert soup.select_one(".btn-share")["onclick"] == f"Share.openShareModal('note', {note_id})"
    assert soup.select_one(".share-online-count")
    assert soup.select_one('[data-sb-dialog="deleteModal"]')["type"] == "button"
    dialog = soup.find(id="deleteModal")
    assert dialog.name == "dialog"
    assert dialog["aria-labelledby"] == "deleteModalLabel"
    assert dialog["aria-describedby"] == "noteDeleteDescription"
    assert dialog.select_one("[data-sb-dialog-close][autofocus]").get_text() == "Cancel"
    form = dialog.select_one("form")
    assert form["method"] == "POST"
    assert form["action"] == f"/note/{note_id}/delete"
    assert form.select_one('button[type="submit"]').get_text() == "Delete Note"
    assert "/static/css/ui-note.css" in {
        urlsplit(link["href"]).path for link in soup.select('link[rel="stylesheet"][href]')
    }
    fragment = BeautifulSoup(client.get(f"/api/note/{note_id}/detail").data, "html.parser")
    assert not fragment.select("#deleteModal, .btn-share, .sb-note-page-header")


@pytest.mark.parametrize(
    "person_type", ["customer", "partner", "se", "seller", "internal", "external", "anonymous"],
)
def test_all_attendee_types_keep_destinations_and_tooltips(
    client: FlaskClient, app: Flask, sample_data: dict[str, int], person_type: str,
) -> None:
    """Polymorphic attendee navigation, names, email and role text stay intact."""
    with app.app_context():
        note = db.session.get(Note, sample_data["call1_id"])
        assert note is not None
        customer = CustomerContact(
            name="Customer Person", customer_id=note.customer_id,
            email="customer@example.com", title="Architect",
        )
        partner = Partner(name="Attendee Partner")
        contact = PartnerContact(
            name="Partner Person", partner=partner, email="partner@example.com", title="Lead",
        )
        se = SolutionEngineer(name="Engineer Person", alias="engineer", specialty="Azure Data")
        internal = InternalContact(name="Internal Person", alias="internal", role="DSS")
        db.session.add_all([customer, partner, contact, se, internal])
        db.session.flush()
        cases = {
            "customer": (
                {"customer_contact": customer}, f"/customer/{note.customer_id}", "Architect",
            ),
            "partner": ({"partner_contact": contact}, f"/partners/{partner.id}", "Lead"),
            "se": ({"solution_engineer": se}, f"/solution-engineer/{se.id}", "Azure Data"),
            "seller": (
                {"seller_id": sample_data["seller1_id"]},
                f'/seller/{sample_data["seller1_id"]}', "alices@microsoft.com",
            ),
            "internal": (
                {"internal_contact": internal}, f"/internal-contacts/{internal.id}/edit", "DSS",
            ),
            "external": (
                {"external_name": "External Person", "external_email": "guest@example.com"},
                "mailto:guest@example.com", "guest@example.com",
            ),
            "anonymous": ({"external_name": "Anonymous Person"}, None, None),
        }
        fields, destination, tooltip = cases[person_type]
        note.attendees.append(NoteAttendee(**fields))
        db.session.commit()
        note_id = note.id
    soup = BeautifulSoup(client.get(f"/api/note/{note_id}/detail").data, "html.parser")
    group = next(dt.parent for dt in soup.select("dt") if dt.get_text() == "Attendees")
    assert group.find_parent(class_="sb-note-detail-group")["data-sb-note-group"] == "people"
    badge = group.select_one(".sb-record-badge")
    assert badge.select_one(".bi")["aria-hidden"] == "true"
    if destination:
        assert badge.name == "a"
        assert badge["href"] == destination
        assert tooltip in badge["title"]
    else:
        assert badge.name == "span"
        assert "Anonymous Person" in badge.get_text()


def test_seller_mode_and_missing_metadata_keep_existing_conditions(
    client: FlaskClient, sample_data: dict[str, int],
) -> None:
    """Seller mode hides only Seller; empty general metadata does not invent links or actions."""
    with client:
        client.get(f'/api/note/{sample_data["call1_id"]}/detail')
        note = db.session.get(Note, sample_data["call1_id"])
        assert note is not None
        soup = BeautifulSoup(render_template(
            "partials/note_view_content.html", note=note, show_edit_delete=False,
            back_url="", seller_mode=True,
        ), "html.parser")
        assert "Seller" not in {dt.get_text() for dt in soup.select("dt")}
        timestamp = datetime.now(timezone.utc)
        general = Note(
            content="", call_date=datetime(2026, 10, 3),
            created_at=timestamp, updated_at=timestamp,
        )
        db.session.add(general)
        db.session.commit()
        soup = BeautifulSoup(render_template(
            "partials/note_view_content.html", note=general, show_edit_delete=False,
            back_url="", seller_mode=False,
        ), "html.parser")
        assert "General Note" in soup.get_text()
        assert "No topics tagged" in soup.get_text()
        assert soup.get_text().count("Not specified") == 2
        assert not soup.select("[data-sb-note-actions]")
        assert len(soup.select(".sb-note-timestamps .local-datetime")) == 1
        assert len(soup.select(".sb-note-detail-group")) == 1
        assert soup.select_one(".sb-note-detail-group")["data-sb-note-group"] == "context"
