"""
Tests for usage telemetry logging and API endpoints.
"""
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from app.models import db, UsageEvent


# =============================================================================
# Telemetry Service Unit Tests
# =============================================================================

class TestTelemetryHelpers:
    """Tests for telemetry service helper functions."""

    def test_derive_category_known_blueprint(self):
        """Should map known blueprints to friendly category names."""
        from app.services.telemetry import _derive_category
        assert _derive_category('call_logs', '/call-logs') == 'Call Logs'
        assert _derive_category('admin', '/admin') == 'Admin'
        assert _derive_category('ai', '/api/ai/suggest') == 'AI'
        assert _derive_category('msx', '/api/msx/status') == 'MSX Integration'
        assert _derive_category('revenue', '/revenue') == 'Revenue'

    def test_derive_category_unknown_blueprint(self):
        """Should title-case unknown blueprint names."""
        from app.services.telemetry import _derive_category
        result = _derive_category('some_new_thing', '/whatever')
        assert result == 'Some New Thing'

    def test_derive_category_no_blueprint_api(self):
        """Should return 'API' for un-blueprinted API routes."""
        from app.services.telemetry import _derive_category
        assert _derive_category(None, '/api/something') == 'API'

    def test_derive_category_no_blueprint_other(self):
        """Should return 'Other' for un-blueprinted non-API routes."""
        from app.services.telemetry import _derive_category
        assert _derive_category(None, '/some-page') == 'Other'

    def test_safe_referrer_path_full_url(self):
        """Should extract only the path from a full URL."""
        from app.services.telemetry import _safe_referrer_path

        class FakeReq:
            referrer = 'https://localhost:5000/call-logs?page=2&search=test'

        result = _safe_referrer_path(FakeReq())
        assert result == '/call-logs'

    def test_safe_referrer_path_none(self):
        """Should return None when no Referer header."""
        from app.services.telemetry import _safe_referrer_path

        class FakeReq:
            referrer = None

        assert _safe_referrer_path(FakeReq()) is None

    def test_safe_referrer_path_empty(self):
        """Should return None for empty referer."""
        from app.services.telemetry import _safe_referrer_path

        class FakeReq:
            referrer = ''

        assert _safe_referrer_path(FakeReq()) is None

    def test_should_log_normal_routes(self):
        """Should log normal page and API routes."""
        from app.services.telemetry import _should_log
        assert _should_log('/call-logs') is True
        assert _should_log('/api/admin/backup/status') is True
        assert _should_log('/customers') is True
        assert _should_log('/admin') is True

    def test_should_log_excludes_static(self):
        """Should exclude static assets and health checks."""
        from app.services.telemetry import _should_log
        assert _should_log('/static/css/style.css') is False
        assert _should_log('/static/js/app.js') is False
        assert _should_log('/health') is False
        assert _should_log('/sw.js') is False
        assert _should_log('/manifest.json') is False
        assert _should_log('/favicon.ico') is False


# =============================================================================
# Automatic Request Logging Integration Tests
# =============================================================================

class TestTelemetryCapture:
    """Tests that HTTP requests are automatically logged."""

    def test_page_view_logged(self, client, app):
        """A normal page view should create a UsageEvent."""
        response = client.get('/customers')
        assert response.status_code == 200

        with app.app_context():
            events = UsageEvent.query.filter_by(endpoint='/customers').all()
            assert len(events) >= 1
            event = events[-1]
            assert event.method == 'GET'
            assert event.status_code == 200
            assert event.is_api is False
            assert event.blueprint == 'customers'
            assert event.category == 'Customers'
            assert event.response_time_ms is not None
            assert event.response_time_ms >= 0

    def test_api_call_logged(self, client, app):
        """An API call should be logged with is_api=True."""
        response = client.get('/api/admin/backup/status')

        with app.app_context():
            events = UsageEvent.query.filter_by(
                endpoint='/api/admin/backup/status'
            ).all()
            assert len(events) >= 1
            event = events[-1]
            assert event.method == 'GET'
            assert event.is_api is True
            assert event.blueprint == 'admin'
            assert event.category == 'Admin'

    def test_post_request_logged(self, client, app, sample_data):
        """POST requests should be logged."""
        response = client.post(f'/call-log/{sample_data["call1_id"]}/delete')

        with app.app_context():
            events = UsageEvent.query.filter_by(method='POST').all()
            assert len(events) >= 1

    def test_404_logged_as_error(self, client, app):
        """404 responses should be logged with error info."""
        response = client.get('/nonexistent-page-xyz')
        assert response.status_code == 404

        with app.app_context():
            events = UsageEvent.query.filter_by(
                endpoint='/nonexistent-page-xyz'
            ).all()
            assert len(events) >= 1
            event = events[-1]
            assert event.status_code == 404
            assert event.error_type == 'HTTP 404'

    def test_static_excluded(self, client, app):
        """Static file requests should not be logged."""
        # Use a path that the _should_log filter excludes.
        # Note: Flask's static handler may 404 for missing files, but
        # the telemetry filter should still exclude /static/ paths.
        client.get('/static/js/app.js')  # May or may not exist

        with app.app_context():
            events = UsageEvent.query.filter(
                UsageEvent.endpoint.like('/static/%')
            ).all()
            assert len(events) == 0

    def test_health_check_excluded(self, client, app):
        """Health check endpoint should not be logged."""
        client.get('/health')

        with app.app_context():
            events = UsageEvent.query.filter_by(endpoint='/health').all()
            assert len(events) == 0

    def test_referrer_path_captured(self, client, app):
        """The Referer header should be captured as path-only."""
        client.get(
            '/api/customers',
            headers={'Referer': 'http://localhost:5000/call-log/new?customer=1'}
        )

        with app.app_context():
            events = UsageEvent.query.filter_by(endpoint='/api/customers').all()
            assert len(events) >= 1
            event = events[-1]
            assert event.referrer_path == '/call-log/new'

    def test_referrer_strips_query_string(self, client, app):
        """Query strings should be stripped from referrer to avoid PII."""
        client.get(
            '/api/customers/autocomplete',
            headers={'Referer': 'http://localhost:5000/search?q=sensitive+query&page=1'}
        )

        with app.app_context():
            events = UsageEvent.query.filter_by(
                endpoint='/api/customers/autocomplete'
            ).all()
            assert len(events) >= 1
            event = events[-1]
            # Should only have path, no query string
            assert event.referrer_path == '/search'
            assert 'sensitive' not in (event.referrer_path or '')

    def test_no_pii_in_events(self, client, app):
        """Events should never contain IP addresses, user-agents, or session data."""
        client.get(
            '/customers',
            headers={
                'User-Agent': 'SecretBrowser/1.0',
                'X-Forwarded-For': '192.168.1.100',
            }
        )

        with app.app_context():
            event = UsageEvent.query.filter_by(endpoint='/customers').first()
            assert event is not None
            # Check no PII fields exist on the model
            assert not hasattr(event, 'ip_address')
            assert not hasattr(event, 'user_agent')
            assert not hasattr(event, 'session_id')
            assert not hasattr(event, 'user_id')
            # Check string fields don't contain PII
            for field in [event.endpoint, event.referrer_path, event.error_message,
                          event.error_type, event.category, event.blueprint]:
                if field:
                    assert '192.168.1.100' not in field
                    assert 'SecretBrowser' not in field

    def test_response_time_measured(self, client, app):
        """Response time should be a positive number of milliseconds."""
        client.get('/customers')

        with app.app_context():
            event = UsageEvent.query.filter_by(endpoint='/customers').first()
            assert event is not None
            assert event.response_time_ms is not None
            assert event.response_time_ms >= 0


# =============================================================================
# Telemetry Stats API Tests
# =============================================================================

class TestTelemetryStatsAPI:
    """Tests for GET /api/admin/telemetry/stats."""

    def _seed_events(self, app, count=10):
        """Create some sample telemetry events."""
        with app.app_context():
            for i in range(count):
                event = UsageEvent(
                    method='GET' if i % 2 == 0 else 'POST',
                    endpoint=f'/api/test/{i}' if i % 3 == 0 else f'/page/{i}',
                    blueprint='admin' if i % 2 == 0 else 'call_logs',
                    view_function=f'test_func_{i}',
                    is_api=i % 3 == 0,
                    status_code=200 if i < 8 else 500,
                    response_time_ms=10.0 + i,
                    referrer_path=f'/ref/{i}' if i % 2 == 0 else None,
                    error_type='ServerError' if i >= 8 else None,
                    error_message='Something broke' if i >= 8 else None,
                    category='Admin' if i % 2 == 0 else 'Call Logs',
                    timestamp=datetime.now(timezone.utc) - timedelta(hours=i),
                )
                db.session.add(event)
            db.session.commit()

    def test_stats_empty(self, client, app):
        """Should return zeros with no telemetry data."""
        response = client.get('/api/admin/telemetry/stats')
        assert response.status_code == 200
        data = response.get_json()
        # The stats API itself generates events, so just check structure
        assert 'summary' in data
        assert 'by_category' in data
        assert 'top_endpoints' in data
        assert 'top_api_endpoints' in data
        assert 'recent_errors' in data
        assert 'daily_activity' in data
        assert 'feature_flows' in data

    def test_stats_with_data(self, client, app):
        """Should return aggregated stats after seeding events."""
        self._seed_events(app, count=10)
        response = client.get('/api/admin/telemetry/stats?days=30')
        assert response.status_code == 200
        data = response.get_json()

        # Summary should have reasonable counts  (seeded 10 + events from
        # the requests themselves, so just check > 0)
        assert data['summary']['total_events'] > 0
        assert isinstance(data['summary']['api_events'], int)
        assert isinstance(data['summary']['page_events'], int)

    def test_stats_category_breakdown(self, client, app):
        """Category breakdown should include seeded categories."""
        self._seed_events(app)
        response = client.get('/api/admin/telemetry/stats')
        data = response.get_json()
        categories = [c['category'] for c in data['by_category']]
        assert 'Admin' in categories

    def test_stats_days_param(self, client, app):
        """Days parameter should filter the time range."""
        with app.app_context():
            old = UsageEvent(
                method='GET', endpoint='/old', blueprint='main',
                view_function='old', is_api=False, status_code=200,
                response_time_ms=5.0, category='General',
                timestamp=datetime.now(timezone.utc) - timedelta(days=60),
            )
            db.session.add(old)
            db.session.commit()

        response = client.get('/api/admin/telemetry/stats?days=7')
        data = response.get_json()
        endpoints = [e['endpoint'] for e in data['top_endpoints']]
        assert '/old' not in endpoints

    def test_stats_errors_tracked(self, client, app):
        """Error events should appear in recent_errors."""
        with app.app_context():
            err = UsageEvent(
                method='POST', endpoint='/api/boom', blueprint='admin',
                view_function='boom', is_api=True, status_code=500,
                response_time_ms=100.0, category='Admin',
                error_type='ServerError', error_message='Kaboom',
            )
            db.session.add(err)
            db.session.commit()

        response = client.get('/api/admin/telemetry/stats')
        data = response.get_json()
        assert data['summary']['error_events'] > 0
        error_endpoints = [e['endpoint'] for e in data['recent_errors']]
        assert '/api/boom' in error_endpoints

    def test_stats_feature_flows(self, client, app):
        """Feature flows should show page->API relationships."""
        with app.app_context():
            event = UsageEvent(
                method='POST', endpoint='/api/ai/suggest-topics',
                blueprint='ai', view_function='api_ai_suggest_topics',
                is_api=True, status_code=200, response_time_ms=50.0,
                referrer_path='/call-log/new', category='AI',
            )
            db.session.add(event)
            db.session.commit()

        response = client.get('/api/admin/telemetry/stats')
        data = response.get_json()
        matching = [f for f in data['feature_flows']
                    if f['from_page'] == '/call-log/new'
                    and f['to_api'] == '/api/ai/suggest-topics']
        assert len(matching) >= 1

    def test_stats_avg_response_time(self, client, app):
        """Response time averages should be calculated per category."""
        with app.app_context():
            for ms in [10.0, 20.0, 30.0]:
                db.session.add(UsageEvent(
                    method='GET', endpoint='/test', blueprint='topics',
                    view_function='test', is_api=False, status_code=200,
                    response_time_ms=ms, category='Topics',
                ))
            db.session.commit()

        response = client.get('/api/admin/telemetry/stats')
        data = response.get_json()
        topics_cat = [c for c in data['by_category'] if c['category'] == 'Topics']
        assert len(topics_cat) == 1
        assert topics_cat[0]['avg_response_ms'] is not None
        # Average of 10, 20, 30 = 20
        assert 19.0 <= topics_cat[0]['avg_response_ms'] <= 21.0


# =============================================================================
# Telemetry Events API Tests
# =============================================================================

class TestTelemetryEventsAPI:
    """Tests for GET /api/admin/telemetry/events."""

    def test_events_empty(self, client, app):
        """Should return paginated structure even with no data."""
        response = client.get('/api/admin/telemetry/events')
        assert response.status_code == 200
        data = response.get_json()
        assert 'events' in data
        assert 'page' in data
        assert 'total' in data
        assert data['page'] == 1

    def test_events_pagination(self, client, app):
        """Should paginate events."""
        with app.app_context():
            for i in range(60):
                db.session.add(UsageEvent(
                    method='GET', endpoint=f'/page/{i}', blueprint='main',
                    view_function='test', is_api=False, status_code=200,
                    response_time_ms=5.0, category='General',
                ))
            db.session.commit()

        response = client.get('/api/admin/telemetry/events?per_page=10&page=1')
        data = response.get_json()
        assert len(data['events']) == 10
        assert data['total'] >= 60
        assert data['pages'] >= 6

    def test_events_category_filter(self, client, app):
        """Should filter events by category."""
        with app.app_context():
            db.session.add(UsageEvent(
                method='GET', endpoint='/admin', blueprint='admin',
                view_function='admin', is_api=False, status_code=200,
                response_time_ms=5.0, category='Admin',
            ))
            db.session.add(UsageEvent(
                method='GET', endpoint='/topics', blueprint='topics',
                view_function='topics', is_api=False, status_code=200,
                response_time_ms=5.0, category='Topics',
            ))
            db.session.commit()

        response = client.get('/api/admin/telemetry/events?category=Admin')
        data = response.get_json()
        for e in data['events']:
            assert e['category'] == 'Admin'

    def test_events_api_filter(self, client, app):
        """Should filter for API-only events."""
        with app.app_context():
            db.session.add(UsageEvent(
                method='GET', endpoint='/api/test', blueprint='main',
                view_function='api_test', is_api=True, status_code=200,
                response_time_ms=5.0, category='General',
            ))
            db.session.add(UsageEvent(
                method='GET', endpoint='/page', blueprint='main',
                view_function='page', is_api=False, status_code=200,
                response_time_ms=5.0, category='General',
            ))
            db.session.commit()

        response = client.get('/api/admin/telemetry/events?is_api=true')
        data = response.get_json()
        for e in data['events']:
            assert e['is_api'] is True

    def test_events_errors_only_filter(self, client, app):
        """Should filter for error events only."""
        with app.app_context():
            db.session.add(UsageEvent(
                method='GET', endpoint='/ok', blueprint='main',
                view_function='ok', is_api=False, status_code=200,
                response_time_ms=5.0, category='General',
            ))
            db.session.add(UsageEvent(
                method='GET', endpoint='/fail', blueprint='main',
                view_function='fail', is_api=False, status_code=500,
                response_time_ms=5.0, category='General',
                error_type='ServerError',
            ))
            db.session.commit()

        response = client.get('/api/admin/telemetry/events?errors_only=true')
        data = response.get_json()
        for e in data['events']:
            assert e['status_code'] >= 400

    def test_events_contain_expected_fields(self, client, app):
        """Each event should have all expected fields."""
        with app.app_context():
            db.session.add(UsageEvent(
                method='POST', endpoint='/api/test', blueprint='admin',
                view_function='test_fn', is_api=True, status_code=200,
                response_time_ms=42.5, referrer_path='/admin',
                category='Admin',
            ))
            db.session.commit()

        response = client.get('/api/admin/telemetry/events')
        data = response.get_json()
        # Find our seeded event
        evt = next((e for e in data['events'] if e['endpoint'] == '/api/test'), None)
        assert evt is not None
        assert evt['method'] == 'POST'
        assert evt['blueprint'] == 'admin'
        assert evt['view_function'] == 'test_fn'
        assert evt['is_api'] is True
        assert evt['status_code'] == 200
        assert evt['response_time_ms'] == 42.5
        assert evt['referrer_path'] == '/admin'
        assert evt['category'] == 'Admin'
        assert 'timestamp' in evt
        assert 'id' in evt


# =============================================================================
# Telemetry Clear API Tests
# =============================================================================

class TestTelemetryClearAPI:
    """Tests for POST /api/admin/telemetry/clear."""

    def test_clear_deletes_all_events(self, client, app):
        """Should delete all telemetry events."""
        with app.app_context():
            for i in range(5):
                db.session.add(UsageEvent(
                    method='GET', endpoint=f'/p/{i}', blueprint='main',
                    view_function='test', is_api=False, status_code=200,
                    response_time_ms=5.0, category='General',
                ))
            db.session.commit()
            assert UsageEvent.query.count() >= 5

        response = client.post('/api/admin/telemetry/clear')
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True

        with app.app_context():
            # May have a few events from the clear request itself, but the
            # 5 seeded ones should be gone.  The clear endpoint commits
            # before the after_request hook logs the clear request itself.
            remaining = UsageEvent.query.count()
            assert remaining <= 2  # at most the clear + stats requests


# =============================================================================
# UsageEvent Model Tests
# =============================================================================

class TestUsageEventModel:
    """Tests for the UsageEvent SQLAlchemy model."""

    def test_create_usage_event(self, app):
        """Should create a UsageEvent with all fields."""
        with app.app_context():
            event = UsageEvent(
                method='GET',
                endpoint='/test',
                blueprint='main',
                view_function='test_view',
                is_api=False,
                status_code=200,
                response_time_ms=15.5,
                referrer_path='/home',
                category='General',
            )
            db.session.add(event)
            db.session.commit()

            saved = UsageEvent.query.get(event.id)
            assert saved is not None
            assert saved.method == 'GET'
            assert saved.endpoint == '/test'
            assert saved.status_code == 200
            assert saved.response_time_ms == 15.5
            assert saved.timestamp is not None

    def test_usage_event_repr(self, app):
        """Should have a meaningful repr."""
        with app.app_context():
            event = UsageEvent(
                method='POST', endpoint='/api/test', is_api=True,
                status_code=201, blueprint='admin',
                view_function='test', category='Admin',
            )
            assert 'POST' in repr(event)
            assert '/api/test' in repr(event)
            assert '201' in repr(event)

    def test_usage_event_nullable_fields(self, app):
        """Optional fields should accept None."""
        with app.app_context():
            event = UsageEvent(
                method='GET', endpoint='/test', is_api=False,
                status_code=200,
            )
            db.session.add(event)
            db.session.commit()

            saved = UsageEvent.query.get(event.id)
            assert saved.blueprint is None
            assert saved.view_function is None
            assert saved.response_time_ms is None
            assert saved.referrer_path is None
            assert saved.error_type is None
            assert saved.error_message is None
            assert saved.category is None

    def test_usage_event_error_fields(self, app):
        """Should store error information."""
        with app.app_context():
            event = UsageEvent(
                method='GET', endpoint='/fail', is_api=False,
                status_code=500,
                error_type='ValueError',
                error_message='Something went wrong',
                category='General',
            )
            db.session.add(event)
            db.session.commit()

            saved = UsageEvent.query.get(event.id)
            assert saved.error_type == 'ValueError'
            assert saved.error_message == 'Something went wrong'

    def test_timestamp_auto_set(self, app):
        """Timestamp should be auto-set to UTC now."""
        with app.app_context():
            before = datetime.now(timezone.utc)
            event = UsageEvent(
                method='GET', endpoint='/test', is_api=False,
                status_code=200,
            )
            db.session.add(event)
            db.session.commit()
            after = datetime.now(timezone.utc)

            # Timestamp should be within the before/after window
            assert event.timestamp is not None
            # SQLite may not preserve timezone, so just check it's recent
            assert event.timestamp.year == before.year
            assert event.timestamp.month == before.month
            assert event.timestamp.day == before.day
