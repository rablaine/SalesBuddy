"""Native preferences, administration and insights retain their product contracts."""

from pathlib import Path
from urllib.parse import urlparse

import pytest
from bs4 import BeautifulSoup
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy.exc import SQLAlchemyError


ROOT = Path(__file__).resolve().parents[1]
SURFACES = (
    ("/preferences", "settings.html", 1152),
    ("/admin", "admin_panel.html", 1344),
    ("/analytics", "analytics.html", 1344),
    ("/metrics", "metrics.html", 1152),
    ("/connect-export", "connect_export.html", 1152),
    ("/admin/ai-logs", "admin_ai_logs.html", 1344),
    ("/admin/favicons", "admin_favicons.html", 1152),
)


def test_sync_console_belongs_to_admin_not_the_control_menu(client: FlaskClient) -> None:
    """Sync diagnostics remain discoverable without widening the shared Control menu."""
    soup = BeautifulSoup(client.get("/admin").data, "html.parser")
    link = soup.select_one('.sb-admin-page-heading a[href="/admin/sync-console"]')
    assert link.get_text(strip=True) == "Sync Status"
    assert not soup.select_one('#settingsMenu a[href="/admin/sync-console"]')


@pytest.mark.parametrize("route", ["/admin", "/admin/favicons"])
def test_admin_durable_sync_observation_contract(client: FlaskClient, route: str) -> None:
    """Admin callers render the shared JSON observer and retain progress callbacks."""
    response = client.get(route)
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    assert any(
        urlparse(script.get("src", "")).path.endswith("/js/ui-jobs.js")
        for script in soup.select("script[src]")
    )
    scripts = "\n".join(script.get_text() for script in soup.select("script:not([src])"))
    assert "onJobProgress:" in scripts
    assert "window.SBJobs.message(job)" in scripts
    if route == "/admin":
        assert "async function readAdminSync(" in scripts
        assert "The sync connection ended before completion." in scripts
        for operation in ("accounts", "milestones", "marketing"):
            panel = soup.select_one(f'[data-sb-sync-progress="{operation}"]')
            assert panel.select_one("progress")
            assert panel.select_one("[data-sync-message]")
        assert "window.SBSyncProgress.create(" in scripts


@pytest.mark.parametrize("route,template,width", SURFACES)
def test_native_administration_surface_contract(
    client: FlaskClient, route: str, template: str, width: int,
) -> None:
    """Connected bands, bounded native work regions and lifecycle hooks are real markup."""
    response = client.get(route)
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    root = soup.select_one("#main-content > [data-sb-page-surface]")
    assert root is not None
    assert "sb-directory-frame" in root["class"]
    assert ("sb-directory-frame-wide" in root["class"]) is (width == 1344)
    assert root.select_one("[data-sb-page-band]")
    assert root.select_one("[data-sb-page-body]")
    assert not root.select(".card, .row, .btn, .modal, .form-control, .form-select, .alert")
    assert "sb-page-frame" in soup.body["class"]
    assert soup.select_one(".sb-page-status")
    assert any(
        urlparse(script.get("src", "")).path.endswith("/js/ui-shortcuts.js")
        for script in soup.select("script[src]")
    )
    assert not soup.select_one('script[src*="bootstrap.bundle"], link[href*="bootstrap.min.css"]')
    source = (ROOT / "templates" / template).read_text(encoding="utf-8")
    assert "bootstrap." not in source
    assert 'data-bs-toggle=' not in source
    assert 'data-bs-dismiss=' not in source
    assert '.bs.' not in source
    assert "--bs-" not in source
    if width == 1344:
        assert "data-sb-page-pair=" in source
        assert "data-sb-page-pane" in source


def test_preferences_controls_keep_original_payload_and_native_delete(client: FlaskClient) -> None:
    """Every toggle, prompt field, defaults form and destructive dialog remains available."""
    from app.models import NoteTemplate, db

    template = NoteTemplate(name="Native template", content="Preserved content")
    db.session.add(template)
    db.session.commit()
    soup = BeautifulSoup(client.get("/preferences").data, "html.parser")
    for identity in (
        "settingsDarkModeSwitch", "copilotActionsSwitch", "staleMilestonesSwitch",
        "hygieneTasksSwitch", "connectImpactSwitch", "msxAutoWritebackSwitch",
        "workiqPrompt", "savePromptBtn", "resetPromptBtn", "refreshCopilotBtn",
        "templateSelect", "viewEditBtn", "deleteBtn", "defaultCustomer", "defaultNoncustomer",
    ):
        assert soup.select_one("#" + identity) is not None
    form = soup.select_one("#defaultTemplatesForm")
    assert form["method"] == "POST"
    assert form["action"] == "/api/preferences/default-templates"
    assert {field["name"] for field in form.select("select")} == {
        "default_template_customer_id", "default_template_noncustomer_id"}
    dialog = soup.select_one("dialog#deleteTemplateModal.sb-dialog")
    assert dialog is not None
    assert dialog.select_one("[data-sb-dialog-close]")
    assert dialog.select_one("#deleteTemplateForm")["method"] == "POST"
    assert str(template.id) in {option.get("value") for option in soup.select("#templateSelect option")}


@pytest.mark.parametrize("dark", [False, True])
def test_appearance_switch_is_shared_by_settings_and_setup(
    app: Flask, client: FlaskClient, dark: bool,
) -> None:
    """Both surfaces expose the same labelled native switch and retain saved checked state."""
    from app.models import UserPreference, db

    with app.app_context():
        preference = UserPreference.query.first()
        preference.dark_mode = dark
        db.session.commit()
    soup = BeautifulSoup(client.get("/preferences").data, "html.parser")
    for identity in ("settingsDarkModeSwitch", "onboardDarkModeToggle"):
        control = soup.select_one(f"#{identity}.sb-theme-switch-input")
        assert control["type"] == "checkbox"
        assert control["role"] == "switch"
        assert control["aria-label"]
        assert control.has_attr("checked") is dark
        label = control.find_parent("label", class_="sb-theme-switch")
        assert label["for"] == identity
        assert label.get_text(" ", strip=True) == "Light Dark"
        assert all(icon["aria-hidden"] == "true" for icon in label.select("i"))
    assert soup.select_one("#settingsDarkModeSwitch").find_parent(
        "div", class_="sb-admin-check",
    ) is not None
    css = (ROOT / "static" / "css" / "ui-base.css").read_text("utf-8")
    assert "appearance: none;" in css
    assert ".sb-theme-switch-input:checked::before { transform: translateX(24px); }" in css
    assert ".sb-theme-switch-input:focus-visible" in css
    assert "transition: transform 180ms cubic-bezier(.25, 1, .5, 1);" in css
    assert """@media (prefers-reduced-motion: reduce) {
    .sb-theme-switch-input::before { transition: none; }
}""" in css
    assert ".sb-setup-theme-choice" not in (
        ROOT / "static" / "css" / "ui-onboarding.css"
    ).read_text("utf-8")


def test_admin_top_columns_have_noninteractive_gutter_dividers(client: FlaskClient) -> None:
    """Statistics and the top administration pair share opt-in, layout-neutral dividers."""
    soup = BeautifulSoup(client.get("/admin").data, "html.parser")
    groups = soup.select(".sb-admin-admin-panel .sb-admin-column-dividers")
    assert len(groups) == 2
    assert len(groups[0].find_all("div", recursive=False)) == 9
    assert groups[1]["data-sb-page-pair"] == "administration"
    assert len(groups[1].select(":scope > [data-sb-page-pane]")) == 2
    css = (ROOT / "static" / "css" / "ui-administration.css").read_text("utf-8")
    rule = css.split(".sb-admin-column-dividers > * + *::before {", 1)[1].split("}", 1)[0]
    assert "position: absolute;" in rule
    assert "inset-inline-start: calc(var(--sb-admin-grid-gap) / -2);" in rule
    assert "border-inline-start: 1px solid var(--sb-ui-line);" in rule
    assert "pointer-events: none;" in rule
    pair_rule = css.split(
        '.sb-admin-admin-panel [data-sb-page-pair="administration"] {', 1,
    )[1].split("}", 1)[0]
    assert "--sb-admin-grid-gap: calc(var(--sb-ui-space-5) * 2);" in pair_rule
    pane_rule = css.split(
        '.sb-admin-admin-panel [data-sb-page-pair="administration"] > [data-sb-page-pane] {',
        1,
    )[1].split("}", 1)[0]
    assert "flex-basis: calc((100% - var(--sb-admin-grid-gap)) / 2);" in pane_rule


def test_default_template_failed_commit_retains_selections(
    app: Flask, client: FlaskClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed manual save rolls back, keeps its draft and never claims success."""
    from app.models import NoteTemplate, UserPreference, db

    with app.app_context():
        first = NoteTemplate(name="First", content="First")
        second = NoteTemplate(name="Second", content="Second")
        db.session.add_all([first, second])
        db.session.commit()
        first_id, second_id = first.id, second.id

    def fail_commit() -> None:
        """Simulate a storage failure only after fixture setup."""
        raise SQLAlchemyError("Storage unavailable")

    monkeypatch.setattr(db.session, "commit", fail_commit)
    response = client.post("/api/preferences/default-templates", data={
        "default_template_customer_id": str(first_id),
        "default_template_noncustomer_id": str(second_id),
    })
    assert response.status_code == 302 and response.location.endswith("/preferences")
    soup = BeautifulSoup(client.get("/preferences").data, "html.parser")
    assert soup.select_one("#defaultCustomer option[selected]")["value"] == str(first_id)
    assert soup.select_one("#defaultNoncustomer option[selected]")["value"] == str(second_id)
    assert "Your selections are retained" in soup.text
    assert "Default templates saved." not in soup.text
    with app.app_context():
        pref = UserPreference.query.first()
        assert not pref or pref.default_template_customer_id != first_id


def test_default_template_invalid_id_is_rejected(client: FlaskClient) -> None:
    """Unavailable IDs redirect back with a real validation failure, not an exception page."""
    response = client.post("/api/preferences/default-templates", data={
        "default_template_customer_id": "not-a-template",
        "default_template_noncustomer_id": "",
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"Could not save default templates." in response.data
    soup = BeautifulSoup(response.data, "html.parser")
    unavailable = soup.select_one("#defaultCustomer option[selected]")
    assert unavailable["value"] == "not-a-template"
    assert "Unavailable template" in unavailable.text


def test_ai_logs_preserve_failed_response_metadata_and_literal_text(client: FlaskClient) -> None:
    """Full text, token metadata and empty/failed responses survive native disclosure markup."""
    from app.models import AIQueryLog, db

    log = AIQueryLog(
        request_text="<script>literal request</script>",
        response_text=None, success=False, error_message="<b>Network failure</b>",
        model="test-model", prompt_tokens=12, completion_tokens=0, total_tokens=12,
    )
    db.session.add(log)
    db.session.commit()
    soup = BeautifulSoup(client.get("/admin/ai-logs").data, "html.parser")
    inspection = soup.select_one("[data-sb-log-id]")
    assert inspection["data-sb-log-id"] == str(log.id)
    assert "Failed" in inspection.text
    assert "test-model" in inspection.text and "(12+0)" in inspection.text
    assert "No response captured" in inspection.text
    assert inspection.select_one("pre").text == "<script>literal request</script>"
    assert "<b>Network failure</b>" in inspection.text
    assert not inspection.select("script, b")


def test_admin_native_disclosures_dialogs_and_danger_controls(client: FlaskClient) -> None:
    """FY, alignment, telemetry, health, backup and update workflows keep their identities."""
    soup = BeautifulSoup(client.get("/admin").data, "html.parser")
    for identity in (
        "fyManagement", "fyCollapse", "fyStartBtn", "fyStatus",
        "alignmentOverrideToggle", "alignmentConfigureBtn", "alignSaveBtn",
        "telemetryCollapse", "telemetryDays", "evtFromDt", "evtToDt",
        "evtEndpoint", "evtCategory", "evtType", "evtErrorsOnly",
        "updateStatus", "msxTestBtn", "importFromMsxBtn", "adminMilestoneSyncBtn",
        "adminMarketingSyncBtn", "shutdownServerBtn", "vacuumDatabaseBtn",
        "clearMilestonesBtn", "clearRevenueBtn",
    ):
        assert soup.select_one("#" + identity) is not None
    assert soup.select_one('[data-sb-admin-disclosure="dangerZoneCollapse"]')
    for identity in ("changelogModal", "archiveExplorerModal", "alignmentModal"):
        assert soup.select_one("dialog#" + identity + ".sb-dialog")
    source = (ROOT / "templates" / "admin_panel.html").read_text(encoding="utf-8")
    assert 'DELETE REVENUE' in source and 'DELETE MILESTONES' in source
    assert "FINAL WARNING" in source
    assert "sb:dialog-closed" in source and "pendingEnable" in source
    assert 'data-sb-sort-value="${b.size_bytes}"' in source
    assert 'data-sb-sort-value="${dt.getTime()}"' in source
    assert 'data-sb-sort-value="${t.getTime()}"' in source
    assert 'data-sb-sort-value="${e.response_time_ms ?? \'\'}"' in source
    for identity in (
        "milestoneSyncToggle", "autoStartToggle", "backupTaskToggle",
        "mergeSourceSearch", "mergeDestSearch", "restoreJson", "telemetryDays",
        "evtFromDt", "evtToDt", "evtEndpoint", "evtCategory", "evtType",
        "archiveTreeSearch", "alignTerrSearch",
    ):
        assert soup.select_one("#" + identity).get("aria-label")


def test_analytics_and_log_empty_states_are_native(client: FlaskClient) -> None:
    """Empty data remains informative and log text is never interpreted as HTML."""
    analytics = BeautifulSoup(client.get("/analytics").data, "html.parser")
    assert len(analytics.select(".sb-admin-stat-strip > div")) == 4
    assert len(analytics.select(".sb-admin-analytics-pane")) == 2
    logs = BeautifulSoup(client.get("/admin/ai-logs").data, "html.parser")
    assert "No AI queries logged yet." in logs.text


def test_completed_setup_offers_readonly_review_beside_unchanged_reset(app: Flask) -> None:
    """Completed setup exposes a non-destructive launcher without replacing the reset command."""
    source = (ROOT / "templates" / "admin_panel.html").read_text(encoding="utf-8")
    start = source.index("<!-- Setup Wizard (collapsed at bottom when complete) -->")
    end = source.index("<!-- Fiscal Year Management (default position) -->", start)
    fragment = app.jinja_env.from_string(source[start:end])
    soup = BeautifulSoup(fragment.render(setup_complete=True), "html.parser")
    review = soup.select_one("#adminReviewSetupStatusBtn")
    reset = soup.select_one("#adminRerunWizardBtn")
    assert review["type"] == "button"
    assert review["onclick"] == "window.openSetupWizard()"
    assert "sb-admin-command" in review["class"]
    assert review.parent is reset.parent
    assert "Re-run Setup Wizard" in reset.text
    assert not BeautifulSoup(fragment.render(setup_complete=False), "html.parser").select(
        "#adminReviewSetupStatusBtn, #adminRerunWizardBtn"
    )
    start = source.index("<!-- Setup Wizard (shown here when incomplete")
    end = source.index("<!-- FY Management (promoted position", start)
    incomplete = app.jinja_env.from_string(source[start:end]).render(
        accounts_synced=False, has_milestones=False, has_revenue=False
    )
    soup = BeautifulSoup(incomplete, "html.parser")
    assert soup.select_one("#adminReviewSetupStatusBtn")["onclick"] == "window.openSetupWizard()"
    assert "Launch Setup Wizard" in soup.select_one("#adminRerunWizardBtn").text


def test_admin_setup_review_entry_is_outside_collapsed_sections(client: FlaskClient) -> None:
    """Review remains discoverable without exposing or invoking the reset workflow."""
    soup = BeautifulSoup(client.get("/admin").data, "html.parser")
    entry = soup.select_one("[data-sb-page-band] #adminReviewSetupBtn")
    assert entry is not None
    assert entry["type"] == "button"
    assert entry["onclick"] == "window.openSetupWizard()"
    assert entry.find_parent(class_="sb-admin-disclosure") is None
    assert len(soup.select("#adminReviewSetupBtn")) == 1
    assert soup.select_one("#adminRerunWizardBtn") is not None


def test_metrics_generated_tables_use_raw_values_and_independent_keys() -> None:
    """Sorting is applied only to tabular data, not timelines or chart inputs."""
    source = (ROOT / "templates" / "metrics.html").read_text(encoding="utf-8")
    assert 'data-sb-sort-key="metrics-${escapeHtml(cardId)}"' in source
    assert 'data-sb-sort-value="${escapeHtml(r[c])}"' in source
    assert "renderTable(data.rows, cardId)" in source
    assert "renderDauLine(el, data.rows)" in source
    assert "renderActivitySummary(data.rows)" in source
    assert "renderWorkIQTimeline(data.rows)" in source


def test_connect_downloads_generation_and_history_remain_available(client: FlaskClient) -> None:
    """Export generation, retained drafts, cached AI, both download formats and delete survive."""
    soup = BeautifulSoup(client.get("/connect-export").data, "html.parser")
    assert soup.select_one("#exportForm")["onsubmit"] == "return generateExport(event)"
    for identity in (
        "exportName", "startDate", "endDate", "generateBtn", "summaryNotes", "summaryCustomers",
        "summaryTopics", "summaryRevenue", "textExportContent", "aiLoadingState", "aiErrorState",
        "aiContentState", "aiGenerateBtn", "aiProgressBar",
    ):
        assert soup.select_one("#" + identity)
    source = (ROOT / "templates" / "connect_export.html").read_text(encoding="utf-8")
    for behavior in ("downloadMarkdown()", "downloadJson()", "copyToClipboard()",
                     "copyAiSummary()", "regenerateAiSummary()", "sbConnectExport"):
        assert behavior in source
    assert "Delete this export record?" in source


def test_favicon_actions_have_a_real_lifecycle_block(client: FlaskClient) -> None:
    """Fetch/refresh/reload controls and async retry logic actually render in the base script slot."""
    soup = BeautifulSoup(client.get("/admin/favicons").data, "html.parser")
    for identity in ("fetchMissingBtn", "refreshAllBtn", "reloadPageBtn", "statusAlert"):
        assert soup.select_one("#" + identity)
    scripts = "\n".join(script.text for script in soup.select("script"))
    assert "faviconAction('/api/admin/fetch-favicons'" in scripts
    assert "faviconAction('/api/admin/refresh-favicons'" in scripts
    assert "resp.ok && data.success" in scripts
    assert "Re-fetch ALL favicons?" in scripts
