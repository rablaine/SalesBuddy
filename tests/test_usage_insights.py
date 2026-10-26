"""Page-based activity and anonymous install telemetry regression tests."""
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

import pytest
from flask import Response, request

from app.models import UserPreference, db
from app.services import telemetry_shipper as shipper
from app.services.telemetry import _is_page_view


@pytest.fixture
def buffered_telemetry(monkeypatch):
    """Inspect queued envelopes without disk identity writes or remote transmission."""
    monkeypatch.setattr(shipper, 'is_telemetry_enabled', lambda: True)
    monkeypatch.setattr(shipper, '_instance_id', 'anonymous-install')
    monkeypatch.setattr(shipper, 'MAX_BUFFER_SIZE', 100000)
    monkeypatch.setattr(shipper, '_buffer', [])
    return shipper._buffer


@pytest.mark.parametrize('role,specialty,expected', [
    ('se', 'data', 'data'),
    ('se', 'infra', 'infra'),
    ('se', 'apps_ai', 'apps_ai'),
    ('se', 'other', 'other'),
    ('se', 'unrecognized', 'unknown'),
    ('dss', 'data', 'not_applicable'),
    ('dss', 'infra', 'not_applicable'),
    ('dss', 'apps_ai', 'not_applicable'),
])
def test_request_captures_install_role_specialty_and_full_boot_version(
    client, app, monkeypatch, buffered_telemetry, role, specialty, expected,
):
    """Saved role and specialty travel with each event, not a scoped seller selection."""
    monkeypatch.setitem(app.config, 'BOOT_COMMIT_FULL', 'a' * 40)
    monkeypatch.setitem(app.config, 'BOOT_COMMIT', 'aaaaaaa')
    with app.app_context():
        prefs = UserPreference.query.first()
        original = prefs.user_role, prefs.se_specialty
        try:
            prefs.user_role, prefs.se_specialty = role, specialty
            db.session.commit()
            with client.session_transaction() as session:
                session['seller_mode_seller_id'] = 123456
            assert client.get('/customers').status_code == 200
            events = [event['data']['baseData'] for event in buffered_telemetry]
            assert [event['name'] for event in events] == [
                'SalesBuddy.FeatureUsage', 'SalesBuddy.PageView',
            ]
            for event in events:
                props = event['properties']
                assert props['user_role'] == role
                assert props['se_specialty'] == expected
                assert props['app_version'] == 'a' * 40
                assert props['instance_id'] == 'anonymous-install'
                assert props['schema_version'] == '2'
        finally:
            prefs.user_role, prefs.se_specialty = original
            db.session.commit()


def test_page_view_uses_route_template_without_business_values(
    client, sample_data, buffered_telemetry,
):
    """Page rankings expose the page kind, never customer IDs, query values or names."""
    response = client.get(
        f'/customer/{sample_data["customer1_id"]}?search=private-customer-name'
    )
    assert response.status_code == 200
    events = [event['data']['baseData'] for event in buffered_telemetry]
    page = next(event for event in events if event['name'] == 'SalesBuddy.PageView')
    assert page['properties']['page'] == '/customer/<int:id>'
    assert 'private-customer-name' not in str(events)
    assert set(page['properties']) == {
        'schema_version', 'instance_id', 'app_version', 'user_role', 'se_specialty',
        'category', 'feature', 'method', 'is_api', 'app_mode', 'page',
    }


@pytest.mark.parametrize('method,path,status,body,mime,headers,expected', [
    ('GET', '/customers', 200, '<!DOCTYPE html><html></html>', 'text/html', {}, True),
    ('GET', '/customers', 200, '<html></html>', 'text/html', {}, True),
    ('GET', '/customers', 200, '<section>Fragment</section>', 'text/html', {}, False),
    ('GET', '/customers', 200, '{}', 'application/json', {}, False),
    ('GET', '/customers', 302, '<html></html>', 'text/html', {}, False),
    ('GET', '/customers', 404, '<html></html>', 'text/html', {}, False),
    ('GET', '/customers', 500, '<html></html>', 'text/html', {}, False),
    ('HEAD', '/customers', 200, '<html></html>', 'text/html', {}, False),
    ('POST', '/customers', 200, '<html></html>', 'text/html', {}, False),
    ('GET', '/api/admin/telemetry/stats', 200, '<html></html>', 'text/html', {}, False),
    ('GET', '/not-a-route', 200, '<html></html>', 'text/html', {}, False),
    ('GET', '/customers', 200, '<html></html>', 'text/html', {'Purpose': 'prefetch'}, False),
    ('GET', '/customers', 200, '<html></html>', 'text/html',
     {'Sec-Purpose': 'prefetch;prerender'}, False),
])
def test_only_successful_document_opens_count(
    app, method, path, status, body, mime, headers, expected,
):
    """Polling, fragments, errors, redirects, preload and non-GETs cannot inflate DAU."""
    with app.test_request_context(path, method=method, headers=headers):
        response = Response(body, status=status, mimetype=mime)
        assert _is_page_view(request, response) is expected


def test_background_api_and_missing_routes_do_not_emit_pages(client, buffered_telemetry):
    """Real API traffic remains request telemetry, not human activity."""
    client.get('/api/admin/telemetry/stats')
    client.get('/not-a-route')
    assert buffered_telemetry
    assert all(
        event['data']['baseData']['name'] == 'SalesBuddy.FeatureUsage'
        for event in buffered_telemetry
    )


def test_streaming_response_does_not_get_consumed(app):
    """Request telemetry must leave streaming response generators untouched."""
    with app.test_request_context('/customers'):
        response = Response(iter(['<html></html>']), mimetype='text/html')
        assert not _is_page_view(request, response)
        assert response.is_streamed


def test_opt_out_and_dev_do_not_emit_page_activity(client, monkeypatch):
    """The new event honors the same opt-out and development exclusion."""
    monkeypatch.setattr(shipper, '_buffer', [])
    monkeypatch.setenv('SALESBUDDY_TELEMETRY_OPT_OUT', 'true')
    client.get('/customers')
    assert shipper._buffer == []
    monkeypatch.delenv('SALESBUDDY_TELEMETRY_OPT_OUT')
    monkeypatch.setenv('FLASK_ENV', 'development')
    client.get('/customers')
    assert shipper._buffer == []


def test_profile_collection_contains_specialty_and_version(app, monkeypatch):
    """Daily profiles carry the same dimensions without generating activity events."""
    monkeypatch.setitem(app.config, 'BOOT_COMMIT_FULL', 'b' * 40)
    envelope = shipper._collect_install_profile(app)
    assert envelope['data']['baseData']['name'] == 'SalesBuddy.InstallProfile'
    props = envelope['data']['baseData']['properties']
    assert props['app_version'] == 'b' * 40
    assert props['schema_version'] == '2'
    assert 'se_specialty' in props


def test_profile_collection_failure_retries_next_flush(app, monkeypatch, buffered_telemetry):
    """A failed startup profile must not suppress retries for a full day."""
    monkeypatch.setattr(shipper, '_last_profile_sent', 0)
    with patch.object(shipper, '_collect_install_profile', return_value=None):
        assert shipper.queue_install_profile(app) is False
    assert shipper._last_profile_sent == 0
    assert shipper.queue_install_profile(app) is True
    assert shipper.queue_install_profile(app) is False
    assert len(buffered_telemetry) == 1


def test_concurrent_process_startup_keeps_one_install_identity(tmp_path, monkeypatch):
    """Web and worker initialization cannot invent two DAU identities for one install."""
    monkeypatch.setattr(shipper, '_get_data_dir', lambda: tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        identities = list(pool.map(lambda _: shipper.get_instance_id(), range(32)))
    assert len(set(identities)) == 1
    assert shipper.get_instance_id() == identities[0]
