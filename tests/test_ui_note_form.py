"""Final native note-editor presentation and original control parity."""

import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from bs4 import BeautifulSoup
from flask import Flask
from flask.testing import FlaskClient


def test_note_editor_fits_available_shell_space_without_clipping_status() -> None:
    """Electron chrome must reduce the scroll pane, not push its bottom off screen."""
    css = (Path(__file__).parents[1] / "static" / "css" / "ui-note-form.css").read_text("utf-8")
    rules = {
        ".sb-note-form-page #main-content": ["display:flex", "flex-direction:column"],
        ".sb-note-form-page #main-content > #noteFormRow": [
            "flex:1", "height:auto", "min-height:0",
        ],
        ".sb-note-form-page #noteFormLeftCol": ["min-height:0", "overflow:auto"],
        ".sb-note-form-page #noteFormRightCol": ["min-height:0", "overflow:auto"],
        ".sb-note-form-status": ["flex-shrink:0", "height:24px"],
    }
    for selector, declarations in rules.items():
        rule = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", css)
        assert rule is not None, selector
        for declaration in declarations:
            assert declaration in rule.group(1), (selector, declaration)
        if selector.endswith("#noteFormRow"):
            assert "100dvh" not in rule.group(1)


def test_nonmodal_flyouts_share_the_measured_navbar_offset_and_scroll_below_their_headers() -> None:
    """Desktop shell chrome shifts companion panes without changing modal positioning."""
    root = Path(__file__).parents[1]
    css = (root / "static" / "css" / "ui-note-form.css").read_text("utf-8")
    chat = (root / "static" / "css" / "ui-chat.css").read_text("utf-8")
    drawer = re.search(r"\.sb-nf-drawer\s*\{([^}]+)\}", css)
    assert drawer is not None
    assert "inset:var(--sb-ui-flyout-top) 0 0 auto" in drawer.group(1)
    assert "height:calc(100dvh - var(--sb-ui-flyout-top))" in drawer.group(1)
    assert "top: var(--sb-ui-flyout-top)" in chat
    assert ".sb-nf-drawer > .sb-nf-offcanvas-header {flex-shrink:0;}" in css
    assert ".sb-nf-drawer > .sb-nf-offcanvas-body {flex:1;min-height:0;}" in css
    template = (root / "templates" / "note_form.html").read_text("utf-8")
    assert "top: var(--sb-ui-header-height) !important" not in template
    modal = (root / "static" / "css" / "ui-dialog.css").read_text("utf-8")
    modal_rule = re.search(r"\.sb-dialog\s*\{([^}]+)\}", modal)
    assert modal_rule is not None
    assert "inset: 0" in modal_rule.group(1)
    assert "--sb-ui-flyout-top" not in modal_rule.group(1)


def test_autocomplete_rows_reset_native_button_borders_and_show_keyboard_selection() -> None:
    """Result rows must be full-width quiet options with a visible active match."""
    styles = Path(__file__).parents[1] / "static" / "css"
    native = (styles / "ui-note-form-native.css").read_text("utf-8")
    css = (styles / "ui-note-form.css").read_text("utf-8")
    row = re.search(r"\.sb-nf-list-group-item\s*\{([^}]+)\}", native)
    assert row
    for declaration in ["display:block", "width:100%", "border:0", "text-align:left", "font:inherit"]:
        assert declaration in row.group(1)
    selection = re.search(
        r"([^{}]*\.sb-nf-list-group-item-action\.active[^{}]*)\{([^}]+)\}", css,
    )
    assert selection
    assert ".enter-target" in selection.group(1)
    assert ":not(:focus-within)" in selection.group(1)
    assert ":focus-visible" in selection.group(1)
    assert "background:var(--sb-ui-selection)" in selection.group(2)


def test_small_modal_width_applies_to_outer_dialog_not_only_its_contents() -> None:
    """The contact photo dialog must not leave an empty half beside its contents."""
    styles = Path(__file__).parents[1] / "static" / "css"
    css = (styles / "ui-note-form.css").read_text("utf-8")
    rule = re.search(r"\.sb-nf-modal\.sb-nf-modal-sm\s*\{([^}]+)\}", css)
    assert rule is not None
    assert "width:min(400px,calc(100vw - 80px))" in rule.group(1)
    native = (styles / "ui-note-form-native.css").read_text("utf-8")
    assert ".sb-nf-modal-dialog{margin:0;width:100%}" in native
    assert ".sb-nf-modal-sm{" not in native


@pytest.mark.parametrize("kind", ["customer", "general", "edit"])
def test_note_form_retains_fields_commands_and_native_dialogs(
    client: FlaskClient, sample_data: dict[str, int], kind: str,
) -> None:
    """All three editor branches retain original IDs, forms and draft recovery."""
    customer_id = sample_data["customer1_id"]
    note_id = sample_data["call1_id"]
    route = {
        "customer": f"/note/new?customer_id={customer_id}",
        "general": "/note/new",
        "edit": f"/note/{note_id}/edit?next=/notes",
    }[kind]
    response = client.get(route)
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    assert soup.select_one("body.sb-page-frame.sb-note-form-page")
    styles = [urlsplit(link["href"]).path for link in soup.select("link[href]")]
    scripts = [urlsplit(script["src"]).path for script in soup.select("script[src]")]
    assert "/static/css/ui-page-frame.css" in styles
    main = soup.select_one("#main-content")
    assert not main.select(
        ".btn,.badge,.card,.row,.form-control,.form-select,.offcanvas,.modal,"
        "[data-bs-toggle],[data-bs-dismiss],[data-bs-target]"
    )
    form = main.select_one("form#noteForm")
    surface = main.select_one("#noteFormRow[data-sb-page-surface]")
    assert ("sb-directory-frame" in surface["class"]) is (kind == "general")
    assert "sb-directory-frame-wide" not in surface["class"]
    assert form["method"] == "POST"
    assert form.select_one('textarea[name="content"]#content')
    assert form.select_one('#call_date[name="call_date"][type="date"]')
    assert form.select_one('#call_time[name="call_time"][type="time"]')
    for field in ["partner_search", "attendee_search", "topic_search"]:
        assert form.find(id=field)
    assert form.select_one('button[type="submit"]')
    close = form.select_one('[data-note-close]')
    assert close.get_text(strip=True).endswith('Close')
    assert not close.has_attr('onclick')
    discard = form.select_one('button[onclick="discardDraft()"]')
    assert discard.get_text(strip=True).endswith(
        'Discard Changes' if kind == 'edit' else 'Discard Draft'
    )
    assert main.find(id='draftSaveIndicator')['role'] == 'status'
    assert main.find(id='noteDraftRecovery').has_attr('hidden')
    assert '/static/js/ui-note-drafts.js' in scripts
    assert form.find(id="noteSaveFeedback")["hidden"] == ""
    assert main.select_one(".sb-note-form-status")
    if kind == "general":
        assert form.select_one('#project_id[name="project_id"]')
        assert main.select_one('dialog#projectFlyout')
        for field in ["title", "description", "type", "due_date", "status"]:
            assert main.find(id=f"flyout_project_{field}")
        assert not main.find(id="noteFormRightCol")
        assert not main.find(id="refreshMilestonesBtn")
    else:
        assert form.select_one('#customer_id[name="customer_id"]')
        assert form.select_one('#engagement_ids[name="engagement_ids"][multiple]')
        for field in ["milestone_search", "task_subject", "task_category", "task_due_date"]:
            assert form.find(id=field)
        refresh = form.select_one('#refreshMilestonesBtn[type="button"]')
        assert refresh.has_attr("disabled")
        assert refresh["aria-label"] == "Refresh milestones from MSX"
        assert refresh["aria-describedby"] == "milestone_help_text"
        assert form.find(id="milestone_help_text")["role"] == "status"
        assert main.find(id="relatedNotesContainer")
        assert main.find(id="inlineEngContainer")
        for dialog_id in ["contactsFlyout", "customerInfoFlyout", "engagementFlyout"]:
            assert main.select_one(f"dialog.sb-dialog#{dialog_id}")
    if kind == "edit":
        assert main.select_one('[data-sb-dialog="deleteNoteModal"]')
        assert main.select_one('dialog#deleteNoteModal form[method="POST"]')
        assert form.select_one('input[name="next"]')["value"] == "/notes"
    for dialog_id in ["meetingImportModal", "templateApplyModal", "partnerFlyout",
                      "contactPhotoModal", "attendeeReviewModal"]:
        dialog = main.select_one(f"dialog.sb-dialog#{dialog_id}")
        assert dialog is not None and not dialog.has_attr("open")
    photo = main.select_one("#contactPhotoModal")
    assert "sb-nf-modal-sm" in photo["class"]
    assert "sb-nf-modal-sm" not in photo.select_one(".sb-nf-modal-dialog")["class"]
    assert "/static/js/ui-note-form-native.js" in scripts
    assert "/static/css/ui-note-form-native.css" in styles
    assert all(dialog.get("data-sb-nonmodal") == "true"
               for dialog in main.select("dialog.sb-nf-drawer"))
    assert all(dialog.has_attr("data-sb-note-drawer")
               for dialog in main.select("dialog.sb-nf-drawer"))
    assert all(main.find(id=dialog.get("aria-labelledby"))
               for dialog in main.select("dialog.sb-nf-modal,dialog.sb-nf-drawer"))


def test_project_attachment_guidance_is_not_repeated_per_note(app: Flask) -> None:
    """The editor's native project fields keep one explanation for the attachment list."""
    notes = [
        SimpleNamespace(id=1, call_date=date.today(), content="First general note"),
        SimpleNamespace(id=2, call_date=date.today(), content="Second general note"),
    ]
    with app.test_request_context("/"):
        rendered = app.jinja_env.get_template("partials/_project_form_fields.html").render(
            unattached_notes=notes, project_types=["general"],
            flyout_mode=True, project=None,
        )
    soup = BeautifulSoup(rendered, "html.parser")
    assert len(soup.select('input[name="note_ids"]')) == 2
    assert rendered.count("Only general notes not already attached") == 1
    assert any(urlsplit(link["href"]).path == "/static/css/ui-project-form.css"
               for link in soup.select("link[href]"))


def test_editor_keeps_rich_text_drafts_and_calendar_workflow_contracts() -> None:
    """Presentation migration keeps the existing editor implementation and storage contract."""
    source = (Path(__file__).parents[1] / "templates" / "note_form.html").read_text("utf-8")
    for contract in [
        "new Quill(", "quill.root.innerHTML", "function saveDraft(",
        "function restoreDraft(", "function debouncedAutoSave(",
        "formSubmittedSuccessfully", "from_meeting", "_fromMeetingWorkiqId",
        "function applyMeetingAttendees(", "function saveAllInlineEngagements(",
        "function importMeetingAttendees(", "function discardDraft(",
        'id="templateSelect"',
    ]:
        assert contract in source
    for dependency in [
        "bootstrap.", 'data-bs-toggle=', 'data-bs-dismiss=', "--bs-",
        "' .btn-primary'", "' .card-body'", "'bg-warning text-dark'",
        "'bi bi-star-fill fs-5", "'small text-muted", "'text-success-emphasis'",
    ]:
        assert dependency not in source


def test_note_detail_has_reading_frame_status_and_shell_free_fragment(
    client: FlaskClient, sample_data: dict[str, int],
) -> None:
    """The page status and Back context never leak into shared previews."""
    note_id = sample_data["call1_id"]
    page = BeautifulSoup(client.get(f"/note/{note_id}?next=/notes").data, "html.parser")
    fragment = BeautifulSoup(client.get(f"/api/note/{note_id}/detail").data, "html.parser")
    assert page.select_one("body.sb-page-frame.sb-note-reading-page")
    assert page.select_one(
        "#noteReadingSurface.sb-directory-frame.sb-directory-frame-wide[data-sb-page-surface]"
    )
    assert any(urlsplit(link["href"]).path == "/static/css/ui-page-frame.css"
               for link in page.select("link[href]"))
    assert page.select_one('.sb-note-commands a[href="/notes"]')
    assert page.select_one(".sb-note-status")
    assert not fragment.select(
        ".sb-note-status,.sb-note-page-header,body,footer.sb-page-status,[data-sb-page-surface]"
    )
    assert page.select_one(".sb-note-tag.sb-record-badge")
    assert not fragment.select(".badge,.card,.btn,[data-bs-toggle],[data-bs-dismiss]")


@pytest.mark.parametrize("destination", [
    "javascript:alert(1)", "https://example.com", "//example.com", "/\\example.com",
])
def test_note_back_command_only_accepts_local_context(
    client: FlaskClient, sample_data: dict[str, int], destination: str,
) -> None:
    """The new Back command does not turn an untrusted next parameter into a link."""
    response = client.get(
        f'/note/{sample_data["call1_id"]}', query_string={"next": destination},
    )
    soup = BeautifulSoup(response.data, "html.parser")
    assert not any(link.get_text(strip=True) == "Back"
                   for link in soup.select(".sb-note-commands a"))
