"""
Tests for MSX/revenue button availability.

The wizard now enforces account import before users can access the product,
so buttons are always enabled. Backend guards still reject requests when
no accounts have been synced.
"""
import pytest
from app.models import db, Customer, SyncStatus


class TestMilestoneTrackerButtons:
    """Test milestone tracker buttons are always enabled (wizard enforces import)."""

    def test_top_sync_button_always_enabled(self, client, app):
        """Top Sync from MSX button should always be active."""
        response = client.get('/reports/milestone-tracker')
        assert response.status_code == 200
        html = response.data.decode()
        assert 'onclick="syncMilestones()"' in html
        assert 'Import accounts</a> first' not in html

    def test_bottom_sync_button_always_enabled(self, client, app):
        """Bottom Sync from MSX button (empty state) should always be active."""
        response = client.get('/reports/milestone-tracker')
        assert response.status_code == 200
        html = response.data.decode()
        assert 'id="syncBtnEmpty"' in html


class TestRevenueDashboardButtons:
    """Test revenue dashboard buttons are always enabled (wizard enforces import)."""

    def test_top_buttons_always_enabled(self, client, app):
        """Top Import Data and Re-analyze buttons should always be active."""
        response = client.get('/reports/revenue')
        assert response.status_code == 200
        html = response.data.decode()
        assert 'Import accounts</a> first' not in html
        assert '/revenue/import' in html
        assert 'Re-analyze' in html

    def test_bottom_import_button_always_enabled(self, client, app):
        """Bottom Import Data button (empty state) should always link to import."""
        response = client.get('/reports/revenue')
        assert response.status_code == 200
        html = response.data.decode()
        assert '/revenue/import' in html


class TestRevenueImportButtons:
    """Test revenue import page buttons are always enabled (wizard enforces import)."""

    def test_import_button_always_enabled(self, client, app):
        """Import CSV button should always be active."""
        response = client.get('/revenue/import')
        assert response.status_code == 200
        html = response.data.decode()
        assert 'type="file"' in html
        assert 'Import accounts</a> first' not in html


class TestBackendGuardsNoCustomers:
    """Test that backend endpoints reject requests when no customers exist."""

    def test_milestone_sync_rejected_no_customers(self, client, app):
        """POST to milestone sync should return 400 when no customers."""
        response = client.post('/api/milestone-tracker/sync')
        assert response.status_code == 400
        data = response.get_json()
        assert data['error'] == 'Import accounts first'

    def test_revenue_import_rejected_no_customers(self, client, app):
        """POST to revenue import should return 400 when no customers."""
        from io import BytesIO
        data = {
            'file': (BytesIO(b'col1,col2\nval1,val2'), 'test.csv'),
        }
        response = client.post(
            '/api/revenue/import',
            data=data,
            content_type='multipart/form-data',
        )
        assert response.status_code == 400
        resp_data = response.get_json()
        assert resp_data['error'] == 'Import accounts first'

    def test_revenue_analyze_rejected_no_customers(self, client, app):
        """POST to revenue analyze should redirect with warning when no customers."""
        response = client.post('/revenue/analyze', follow_redirects=False)
        assert response.status_code == 302

    def test_milestone_sync_allowed_with_customers(self, client, app, sample_data):
        """POST to milestone sync should not return 400 when customers exist."""
        # Will fail for other reasons (no MSX token etc.) but NOT because of missing customers
        response = client.post('/api/milestone-tracker/sync')
        assert response.status_code != 400 or 'Import accounts first' not in response.get_json().get('error', '')
