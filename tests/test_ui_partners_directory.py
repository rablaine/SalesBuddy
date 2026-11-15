"""Native directory and existing sharing APIs, using only the isolated test database."""

import re
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from bs4 import BeautifulSoup

from app.models import Partner, PartnerContact, Specialty, Note, UserPreference, db


def directory(client):
    """Read the standalone directory without touching business-write actions."""
    response = client.get("/partners")
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    return soup.select_one("[data-partners-directory]"), soup


def test_native_empty_commands_and_shared_dialogs(client):
    """Creation and specialties keep original destinations, with no invented filter."""
    root, soup = directory(client)
    assert 'sb-directory-frame' in root['class']
    assert "No partners yet." in root.get_text()
    assert [a["href"] for a in root.select("a")] == [
        "/specialties", "/partners/new", "/partners/new",
    ]
    assert root.select_one("[data-share-directory]")
    assert not root.select(".card,.badge,.btn,.list-group,[onclick],[data-bs-toggle],form")
    assert not root.select("input,select")
    assert "0 Partners" in soup.select_one(".sb-page-status").get_text()
    assert "sb-page-frame" in soup.body["class"]
    for dialog_id in ("shareModal", "shareImportReviewModal"):
        dialog = soup.select_one(f"#{dialog_id}")
        assert dialog.name == "dialog"
        assert "sb-dialog" in dialog["class"]
        assert not dialog.select(".modal,.btn,[data-bs-dismiss]")
    scripts = [urlsplit(script["src"]).path for script in soup.select("script[src]")]
    for filename in ("ui-partners-directory.js", "share.js", "ui-table-sort.js", "ui-dialog.js"):
        assert scripts.count(f"/static/js/{filename}") == 1


def test_directory_reading_pane_overrides_shell_clipping(client):
    """The keyboard-accessible list must scroll inside the fixed page frame."""
    root, soup = directory(client)
    styles = [urlsplit(link["href"]).path for link in soup.select('link[rel="stylesheet"]')]
    shell = "/static/css/ui-page-frame.css"
    directory_css = "/static/css/ui-partners-directory.css"
    assert styles.index(shell) < styles.index(directory_css)
    reading = root.select_one(".partners-reading")
    assert reading.has_attr("data-sb-page-body")
    assert reading["tabindex"] == "0"
    source = client.get(directory_css).get_data(as_text=True)
    rule = re.search(r"\.sb-partners-directory \.partners-reading\s*\{([^}]+)\}", source)
    assert rule is not None
    assert "overflow: auto;" in rule.group(1)
    assert "scrollbar-gutter: stable;" in rule.group(1)


def test_complete_original_data_axes_and_numeric_keys(app, client):
    """All specialties remain linked, ratings distinguish zero from missing, counts are raw."""
    with app.app_context():
        first = Partner(name='Alpha "<partner>"', rating=0, favicon_b64="cached")
        last = Partner(name="Zulu", rating=None)
        first.specialties = [Specialty(name=f"Specialty {i}") for i in range(14)]
        first.contacts = [PartnerContact(name=f"Contact {i}") for i in range(12)]
        first.notes = [Note(content="Isolated") for _ in range(11)]
        db.session.add_all([last, first])
        db.session.commit()
        first_id, last_id = first.id, last.id
    root, _ = directory(client)
    rows = root.select("tbody tr")
    assert [int(row["data-partner-id"]) for row in rows] == [first_id, last_id]
    assert [th["data-export-label"] for th in root.select("th")] == [
        "Name", "Rating", "Specialties", "Contacts", "Notes",
    ]
    assert [th["data-sb-sort-type"] for th in root.select("th")] == [
        "text", "number", "text", "number", "number",
    ]
    first_row = rows[0]
    assert first_row["data-partner-href"] == f"/partners/{first_id}"
    assert first_row.select_one("a")["href"] == f"/partners/{first_id}"
    assert first_row.select_one("img")["src"] == "data:image/png;base64,cached"
    assert len(first_row.select(".partners-specialties a")) == 14
    assert all(link["href"].startswith("/specialties/")
               for link in first_row.select(".partners-specialties a"))
    cells = first_row.select("td")
    assert [cells[i]["data-sb-sort-value"] for i in (0, 1, 3, 4)] == [
        'Alpha "<partner>"', "0", "12", "11",
    ]
    assert cells[1].select_one("span")["aria-label"] == "0 out of 5 stars"
    assert rows[1].select("td")[1]["data-sb-sort-value"] == ""
    assert rows[1].select("td")[1].get_text() == ""
    assert rows[1].select("td")[3].get_text() == "0 contacts"
    assert not root.select(".card,.badge,.btn,.list-group,[onclick],[data-bs-toggle]")


@pytest.mark.parametrize("role", ["se", "dss", "seller_mode"])
def test_original_all_partner_all_note_scope(app, client, sample_data, role):
    """Neither role nor seller impersonation narrows the original directory population."""
    with app.app_context():
        partner = Partner(name="No notes")
        db.session.add(partner)
        pref = UserPreference.query.first()
        pref.user_role = "dss" if role == "dss" else "se"
        pref.my_seller_id = sample_data["seller1_id"]
        db.session.commit()
        expected = {str(p.id): len(p.notes) for p in Partner.query.all()}
    if role == "seller_mode":
        client.post(f'/api/seller-mode/activate/{sample_data["seller1_id"]}')
    root, _ = directory(client)
    assert {row["data-partner-id"]: int(row.select("td")[-1]["data-sb-sort-value"])
            for row in root.select("tbody tr")} == expected


def test_export_original_field_contract_excludes_private_note_customer_contact_fields(app, client):
    """Sharing exports original explicit fields, not expanded source data or linked notes."""
    with app.app_context():
        partner = Partner(name="Export", overview="Overview", rating=0, website="example.test")
        partner.contacts = [PartnerContact(name="Person", title="Local title", email="p@test",
                                          is_primary=True, photo_b64="Local photo")]
        partner.specialties = [Specialty(name="Specialty")]
        partner.notes = [Note(content="Never export this linked note")]
        db.session.add(partner)
        db.session.commit()
        partner_id = partner.id
    payload = client.get("/api/share/directory").json
    assert payload["success"] is True
    result = payload["partners"][0]
    assert set(result) == {
        "name", "overview", "rating", "website", "favicon_b64", "specialties", "contacts",
    }
    assert result["rating"] == 0
    assert result["contacts"] == [{"name": "Person", "email": "p@test", "is_primary": True}]
    assert client.get(f"/api/share/partner/{partner_id}").json["partner"] == result


def test_preview_cancel_equivalent_does_not_commit_and_matched_names_preserved(app, client):
    """Preview-only review is read-only, even when incoming names normalize to an existing name."""
    with app.app_context():
        db.session.add(Partner(name="Acme"))
        db.session.commit()
    result = client.post("/api/share/preview", json={
        "partners": [{"name": "Acme Inc.", "specialties": ["New"]}], "sender_name": "Sender",
    })
    assert result.status_code == 200
    assert result.json["previews"][0]["name"] == "Acme"
    assert result.json["previews"][0]["action"] == "update"
    with app.app_context():
        from app.services.partner_sharing import preview_partners
        # The request shell may commit telemetry; the domain provider must not commit.
        with patch("app.services.partner_sharing.db.session.commit") as commit:
            preview_partners([{"name": "Acme Inc.", "specialties": ["New"]}], "Sender")
            commit.assert_not_called()
        assert Partner.query.count() == 1
        assert Specialty.query.count() == 0
        assert Partner.query.first().overview is None


def test_import_failure_rolls_back_all_selected_data_and_retry_succeeds(app, client):
    """A failed multi-partner commit cannot leave a half-import; API keeps an honest error."""
    payload = {"sender_name": "Sender", "partners": [
        {"name": "First", "contacts": [], "specialties": ["One"]},
        {"name": "Second", "contacts": [], "specialties": []},
    ]}
    with patch("app.services.partner_sharing.db.session.commit",
               side_effect=RuntimeError("isolated commit failure")):
        response = client.post("/api/share/receive", json=payload)
    assert response.status_code == 503
    assert response.json == {"success": False, "error": "Could not import selected partners."}
    with app.app_context():
        assert Partner.query.count() == Specialty.query.count() == 0
    response = client.post("/api/share/receive", json=payload)
    assert response.status_code == 200
    assert response.json["created"] == 2
    with app.app_context():
        assert Partner.query.count() == 2


def test_zero_rating_update_is_visible_in_original_preview(app, client):
    """Zero is a real rating, not an absent sender review."""
    with app.app_context():
        db.session.add(Partner(name="Rated", rating=5))
        db.session.commit()
    response = client.post("/api/share/preview", json={
        "partners": [{"name": "Rated", "rating": 0}], "sender_name": "Sender",
    })
    assert response.status_code == 200
    assert response.json["previews"][0]["changes"]["rating"] == 0
    assert response.json["previews"][0]["has_changes"] is True
