"""Tests for the unadvertised /metrics dashboard."""
from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
from bs4 import BeautifulSoup

from app.services.app_insights import AppInsightsError


def _all_cards_failed(payload: bytes, error_text: str) -> bool:
    """Heuristic: every card on the page should display the error."""
    return error_text.encode() in payload


class TestMetricsRoute:
    def test_renders_shell_without_calling_app_insights(self, client):
        """Page renders immediately as a shell - no App Insights queries on initial load."""
        with patch('app.services.usage_metrics.query_to_dicts') as mock_q:
            resp = client.get('/metrics')
        assert resp.status_code == 200
        assert b'Daily active installs' in resp.data
        # Shell must not block on App Insights
        mock_q.assert_not_called()
        # Cards have placeholders that JS will hydrate
        assert b'data-card="install_inventory"' in resp.data
        assert b'data-card="se_specialties"' in resp.data
        assert b'data-card="page_loads"' in resp.data
        assert b'data-card="dau"' in resp.data

    def test_invalid_days_falls_back_to_7(self, client):
        resp = client.get('/metrics?days=999')
        assert resp.status_code == 200
        assert b'7d' in resp.data

    def test_route_is_unadvertised(self, app):
        """No nav link should reference /metrics."""
        with app.test_client() as c:
            resp = c.get('/')
        assert resp.status_code in (200, 302)
        if resp.status_code == 200:
            assert b'href="/metrics"' not in resp.data


class TestMetricsCardApi:
    def test_version_card_enriches_boot_hashes_without_changing_install_counts(self, client):
        """Dashboard and SalesIQ share a dated version result, including unknown commits."""
        rows = [
            {'app_version': 'abcdef0', 'installs': 3},
            {'app_version': 'unknown', 'installs': 2},
        ]
        with patch('app.services.usage_metrics.query_to_dicts', return_value=rows), \
             patch('app.services.usage_metrics.get_commit_metadata', return_value={
                 'abcdef0': ('abcdef0', '2026-10-26'), 'unknown': ('unknown', None),
             }) as lookup:
            response = client.get('/api/metrics/card/app_versions?days=7')
        assert response.get_json()['rows'] == [
            {'app_version': 'abcdef0', 'commit_date': '2026-10-26', 'installs': 3},
            {'app_version': 'unknown', 'commit_date': None, 'installs': 2},
        ]
        lookup.assert_called_once_with(['abcdef0', 'unknown'])

    def test_version_card_groups_legacy_hashes_without_merging_collisions(self, client):
        """Resolve identity before summing, never group on seven-character display text."""
        first, second = '97d4b5a3' + '1' * 32, '97d4b5a4' + '2' * 32
        rows = [
            {'app_version': first, 'installs': 2},
            {'app_version': first[:8], 'installs': 1},
            {'app_version': first[:7], 'installs': 1},
            {'app_version': second, 'installs': 1},
            {'app_version': 'unknown', 'installs': 1},
        ]
        with patch('app.services.usage_metrics.query_to_dicts', return_value=rows), \
             patch('app.services.update_checker.subprocess.run', return_value=Mock(
                 stdout=f'{first}\t2026-10-24\n{second}\t2026-10-25\n',
             )):
            response = client.get('/api/metrics/card/app_versions?days=7')
        assert response.get_json()['rows'] == [
            {'app_version': first, 'commit_date': '2026-10-24', 'installs': 3},
            {'app_version': first[:7], 'commit_date': None, 'installs': 1},
            {'app_version': second, 'commit_date': '2026-10-25', 'installs': 1},
            {'app_version': 'unknown', 'commit_date': None, 'installs': 1},
        ]

    def test_card_returns_rows_as_json(self, client):
        with patch('app.services.usage_metrics.query_to_dicts', return_value=[{'active_installs': 5}]):
            resp = client.get('/api/metrics/card/activity_summary?days=7')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['card_id'] == 'activity_summary'
        assert body['rows'] == [{'active_installs': 5}]
        assert body['error'] is None

    def test_card_surfaces_app_insights_error_in_json(self, client):
        def deny(*args, **kwargs):
            raise AppInsightsError('App Insights HTTP 403: forbidden', status_code=403)

        with patch('app.services.usage_metrics.query_to_dicts', side_effect=deny):
            resp = client.get('/api/metrics/card/dau?days=7')
        assert resp.status_code == 200  # error returned in body, not status
        body = resp.get_json()
        assert body['error'] is not None
        assert '403' in body['error']
        assert body['status_code'] == 403
        assert body['rows'] == []

    def test_unknown_card_404s(self, client):
        resp = client.get('/api/metrics/card/does_not_exist?days=7')
        assert resp.status_code == 404

    def test_card_invalid_days_falls_back_to_7(self, client):
        with patch('app.services.usage_metrics.query_to_dicts', return_value=[]) as mock_q:
            resp = client.get('/api/metrics/card/dau?days=abc')
        assert resp.status_code == 200
        # Confirm the call happened (so the route accepted the request)
        assert mock_q.called

    def test_403_sets_auth_required_flag(self, client):
        """A 403 surfaces auth_required so the UI can show the sign-in button."""
        def deny(*args, **kwargs):
            raise AppInsightsError(
                'App Insights HTTP 403: insufficient access',
                status_code=403,
            )
        with patch('app.services.usage_metrics.query_to_dicts', side_effect=deny):
            resp = client.get('/api/metrics/card/dau?days=7')
        body = resp.get_json()
        assert body['auth_required'] is True

    def test_500_does_not_set_auth_required(self, client):
        """A non-auth error keeps auth_required False so we don't show the button."""
        def boom(*args, **kwargs):
            raise AppInsightsError('App Insights HTTP 500: server error', status_code=500)
        with patch('app.services.usage_metrics.query_to_dicts', side_effect=boom):
            resp = client.get('/api/metrics/card/dau?days=7')
        body = resp.get_json()
        assert body['auth_required'] is False


class TestMetricsSignIn:
    @pytest.fixture(autouse=True)
    def reset_login_process(self, monkeypatch):
        """Do not carry a mocked sign-in process between independent tests."""
        monkeypatch.setattr('app.routes.metrics._login_process', None)

    def test_sign_in_spawns_az_login_and_clears_cache(self, client):
        with patch('app.routes.metrics.subprocess.Popen') as mock_popen, \
             patch('app.routes.metrics.shutil.which', return_value='C:\\fake\\az.cmd'), \
             patch('app.routes.metrics.reset_token_cache') as mock_reset:
            resp = client.post('/api/metrics/sign-in')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['success'] is True
        # We spawned az login pinned to the BlaineCorp tenant
        called_args = mock_popen.call_args[0][0]
        assert 'login' in called_args
        assert '--tenant' in called_args
        assert '96d12531-723e-46c1-842b-0480739c7419' in called_args
        assert mock_popen.call_args.kwargs['env']['AZURE_CORE_ENABLE_BROKER_ON_WINDOWS'] == 'false'
        # And we dropped the in-process token cache
        mock_reset.assert_called_once()

    def test_repeated_sign_in_click_joins_running_attempt(self, client):
        """Repeated clicks do not launch competing MFA challenges."""
        with patch('app.routes.metrics.subprocess.Popen') as popen, \
             patch('app.routes.metrics.shutil.which', return_value=r'C:\fake\az.cmd'):
            popen.return_value.poll.return_value = None
            assert client.post('/api/metrics/sign-in').get_json()['success'] is True
            assert client.post('/api/metrics/sign-in').get_json()['success'] is True
        popen.assert_called_once()

    def test_sign_in_reports_missing_az(self, client):
        with patch('app.routes.metrics.shutil.which', return_value=None):
            resp = client.post('/api/metrics/sign-in')
        assert resp.status_code == 500
        assert 'not found' in resp.get_json()['error'].lower()


@pytest.mark.parametrize('days', [1, 7, 30, 90])
@pytest.mark.parametrize('card', [
    'activity_summary', 'dau', 'page_loads', 'install_inventory',
    'installs_by_role', 'se_specialties', 'app_versions', 'reports',
])
def test_usage_queries_respect_selected_calendar_window(client, card, days):
    """Both the backend timespan and KQL use the selected reporting range."""
    with patch('app.services.usage_metrics.query_to_dicts', return_value=[]) as query:
        response = client.get(f'/api/metrics/card/{card}?days={days}')
    assert response.status_code == 200
    assert query.call_args.kwargs['timespan'] == f'P{days}D'
    assert f'startofday(ago({days - 1}d))' in query.call_args.args[0]


def test_dau_assigns_one_role_per_install_per_day():
    """A role change cannot double-count an install or rewrite past days."""
    from app.services.usage_metrics import _q_dau

    query = _q_dau(7)
    assert 'SalesBuddy.PageView' in query
    assert 'SalesBuddy.FeatureUsage' not in query
    assert 'InstallProfile' not in query
    assert 'arg_max(timestamp, user_role)' in query
    assert 'by day = startofday(timestamp), instance_id' in query
    assert 'DAU = count()' in query
    assert 'range timestamp' in query
    assert 'coalesce(DAU, long(0))' in query


def test_inventory_uses_latest_metadata_and_distinguishes_background_activity():
    """Inventory is deduplicated per install and page activity stays a separate timestamp."""
    from app.services.usage_metrics import _q_install_inventory, _q_app_versions, _q_specialties

    query = _q_install_inventory(30)
    assert 'arg_max(timestamp, *) by instance_id' in query
    assert 'last_active_utc = max(timestamp)' in query
    assert 'last_seen_utc = timestamp' in query
    assert 'join kind=leftouter Activity' in query
    assert 'schema_version) == "2"' in query
    assert 'by app_version' in _q_app_versions(30)
    assert 'where user_role == "SE"' in _q_specialties(30)


def test_page_ranking_includes_all_pages_not_top_n():
    """Page load ranking excludes API requests and has no arbitrary page limit."""
    from app.services.usage_metrics import _q_page_loads, _q_reports

    query = _q_page_loads(7)
    assert 'SalesBuddy.PageView' in query
    assert 'page_loads = sum(loads), active_installs = count()' in query
    assert 'take ' not in query
    assert 'SalesBuddy.PageView' in _q_reports(7)

def test_legacy_role_enrichment_is_not_cut_off_by_short_activity_window(client):
    """The older power-feature card can still read a profile from before today's UTC midnight."""
    with patch('app.services.usage_metrics.query_to_dicts', return_value=[]) as query:
        assert client.get('/api/metrics/card/power_features?days=1').status_code == 200
    assert query.call_args.kwargs['timespan'] == 'P30D'
    assert '| where timestamp > ago(1d)' in query.call_args.args[0]


def test_metrics_template_matches_card_registry(client):
    """Every rendered card has a query and the first card summarizes activity."""
    from app.services.usage_metrics import _card_specs

    response = client.get('/metrics')
    cards = BeautifulSoup(response.data, 'html.parser').select('[data-card]')
    specs, _ = _card_specs(7)
    keys = [card['data-card'] for card in cards]
    assert keys[0] == 'activity_summary'
    assert len(keys) == len(set(keys))
    assert set(keys) == set(specs) - {'workiq_uptime_summary'}


def test_workbook_usage_tiles_match_page_based_contract():
    """The checked-in Azure workbook cannot revive request-based DAU or chart inventory as pie."""
    import json
    from pathlib import Path

    workbook = json.loads((
        Path(__file__).resolve().parents[1]
        / 'infra' / 'workbooks' / 'salesbuddy-usage-workbook.json'
    ).read_text(encoding='utf-8'))
    tiles = {
        item['name']: item['content']
        for item in workbook['variables']['workbookContent']['items']
    }
    for name in ('tile-dau-top', 'tile-page-loads', 'tile-activity-summary', 'tile-reports'):
        query = tiles[name]['query']
        assert 'SalesBuddy.PageView' in query
        assert 'SalesBuddy.FeatureUsage' not in query
        assert '{TimeRange:start}' in query
    for name in ('tile-page-loads', 'tile-install-inventory', 'tile-activity-summary'):
        assert tiles[name]['visualization'] == 'table'
    assert 'take ' not in tiles['tile-page-loads']['query']
    assert 'arg_max(timestamp, *) by instance_id' in tiles['tile-install-inventory']['query']
    assert 'where user_role == "SE"' in tiles['tile-se-specialties']['query']
    query = tiles['tile-dau-top']['query']
    assert query.index('arg_max(timestamp, user_role)') < query.index('where "{Role}"')
