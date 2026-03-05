"""
Tests for view routes (pages that display data).
These tests verify that pages load correctly and handle eager-loaded relationships.
"""
import os
from unittest.mock import patch

import pytest


def test_home_page_loads(client):
    """Test that home page loads successfully."""
    response = client.get('/')
    assert response.status_code == 200
    assert b'Welcome to NoteHelper' in response.data


def test_home_page_with_data(client, sample_data):
    """Test home page displays recent calls."""
    response = client.get('/')
    assert response.status_code == 200
    assert b'Calendar' in response.data


def test_calendar_api_returns_json(client, sample_data):
    """Test calendar API returns proper JSON with call log data."""
    response = client.get('/api/call-logs/calendar')
    assert response.status_code == 200
    
    data = response.get_json()
    assert 'year' in data
    assert 'month' in data
    assert 'month_name' in data
    assert 'days' in data
    assert 'first_weekday' in data
    assert 'days_in_month' in data
    assert 'prev_year' in data
    assert 'prev_month' in data
    assert 'next_year' in data
    assert 'next_month' in data


def test_calendar_api_with_params(client, sample_data):
    """Test calendar API accepts year and month parameters."""
    response = client.get('/api/call-logs/calendar?year=2025&month=6')
    assert response.status_code == 200
    
    data = response.get_json()
    assert data['year'] == 2025
    assert data['month'] == 6
    assert data['month_name'] == 'June'
    assert data['prev_month'] == 5
    assert data['next_month'] == 7


def test_calendar_api_month_boundaries(client, sample_data):
    """Test calendar API handles month boundary navigation."""
    # Test December -> January
    response = client.get('/api/call-logs/calendar?year=2025&month=12')
    data = response.get_json()
    assert data['next_year'] == 2026
    assert data['next_month'] == 1
    
    # Test January -> December
    response = client.get('/api/call-logs/calendar?year=2025&month=1')
    data = response.get_json()
    assert data['prev_year'] == 2024
    assert data['prev_month'] == 12


def test_customers_list_alphabetical(client, sample_data):
    """Test customers list in alphabetical view."""
    response = client.get('/customers')
    assert response.status_code == 200
    assert b'Acme Corp' in response.data
    assert b'Globex Inc' in response.data


def test_customers_list_grouped(client, sample_data):
    """Test customers list in grouped view."""
    # Set preference to grouped
    client.post('/api/preferences/customer-view', 
                json={'customer_view_grouped': True})
    
    response = client.get('/customers')
    assert response.status_code == 200
    assert b'Alice Smith' in response.data  # Seller name
    assert b'Acme Corp' in response.data


def test_customer_view_loads(client, sample_data):
    """Test individual customer page loads with eager-loaded data."""
    customer_id = sample_data['customer1_id']
    response = client.get(f'/customer/{customer_id}')
    assert response.status_code == 200
    assert b'Acme Corp' in response.data
    assert b'1001' in response.data
    assert b'Alice Smith' in response.data  # Seller badge
    assert b'West Region' in response.data  # Territory badge


def test_seller_view_loads(client, sample_data):
    """Test seller page loads with sorted customers."""
    seller_id = sample_data['seller1_id']
    response = client.get(f'/seller/{seller_id}')
    assert response.status_code == 200
    assert b'Alice Smith' in response.data
    assert b'Acme Corp' in response.data


def test_seller_view_with_territories(client, sample_data):
    """Test seller page displays territories correctly."""
    seller_id = sample_data['seller1_id']
    response = client.get(f'/seller/{seller_id}')
    assert response.status_code == 200
    assert b'West Region' in response.data


def test_territory_view_loads(client, sample_data):
    """Test territory page loads with sorted sellers (recent calls view)."""
    territory_id = sample_data['territory1_id']
    response = client.get(f'/territory/{territory_id}')
    assert response.status_code == 200
    assert b'West Region' in response.data
    assert b'Alice Smith' in response.data
    assert b'Recent Calls' in response.data


def test_territory_view_accounts(client, sample_data, app):
    """Test territory page loads with accounts view grouped by seller type."""
    from app.models import db, UserPreference
    
    with app.app_context():
        # Set preference to show accounts view
        pref = UserPreference.query.filter_by(user_id=1).first()
        pref.territory_view_accounts = True
        db.session.commit()
    
    territory_id = sample_data['territory1_id']
    response = client.get(f'/territory/{territory_id}')
    assert response.status_code == 200
    assert b'West Region' in response.data
    assert b'Accounts in Territory' in response.data
    assert b'Acme Corp' in response.data


def test_topic_view_loads(client, sample_data):
    """Test topic page loads with sorted call logs."""
    topic_id = sample_data['topic1_id']
    response = client.get(f'/topic/{topic_id}')
    assert response.status_code == 200
    assert b'Azure VM' in response.data
    assert b'Acme Corp' in response.data  # Customer name
    assert b'Discussed VM migration' in response.data


def test_topics_list_alphabetical(client, sample_data):
    """Test topics list sorted alphabetically."""
    response = client.get('/topics')
    assert response.status_code == 200
    assert b'Azure VM' in response.data
    assert b'Storage' in response.data


def test_topics_list_by_calls(client, sample_data):
    """Test topics list sorted by call count."""
    # Set preference to sort by calls
    client.post('/api/preferences/topic-sort',
                json={'topic_sort_by_calls': True})
    
    response = client.get('/topics')
    assert response.status_code == 200
    assert b'Azure VM' in response.data


def test_territories_list_loads(client, sample_data):
    """Test territories list page."""
    response = client.get('/territories')
    assert response.status_code == 200
    assert b'West Region' in response.data
    assert b'East Region' in response.data
    assert b'Alice Smith' in response.data  # Seller badge


def test_sellers_list_loads(client, sample_data):
    """Test sellers list page."""
    response = client.get('/sellers')
    assert response.status_code == 200
    assert b'Alice Smith' in response.data
    assert b'Bob Jones' in response.data


def test_call_logs_list_loads(client, sample_data):
    """Test call logs list page."""
    response = client.get('/call-logs')
    assert response.status_code == 200
    assert b'Acme Corp' in response.data
    assert b'Discussed VM migration' in response.data


def test_call_log_view_loads(client, sample_data):
    """Test individual call log page."""
    call_id = sample_data['call1_id']
    response = client.get(f'/call-log/{call_id}')
    assert response.status_code == 200
    assert b'Acme Corp' in response.data
    assert b'Azure VM' in response.data


def test_search_page_loads(client):
    """Test search page loads."""
    response = client.get('/search')
    assert response.status_code == 200
    assert b'Search Call Logs' in response.data


def test_search_with_query(client, sample_data):
    """Test search with query parameter."""
    response = client.get('/search?q=migration')
    assert response.status_code == 200
    assert b'Discussed VM migration' in response.data or b'Search Results' in response.data


def test_preferences_page_loads(client):
    """Test preferences page loads."""
    response = client.get('/preferences')
    assert response.status_code == 200
    assert b'Settings' in response.data


def test_customers_list_filters_without_calls(client, sample_data):
    """Test that customers without calls are filtered when preference is False."""
    from app.models import Customer, UserPreference, db

    # Create a customer without any call logs
    customer = Customer(
        name='Empty Customer',
        tpid=9999,
        user_id=1
    )
    db.session.add(customer)
    db.session.commit()

    # Default preference is True (show all customers), so customer should appear
    response = client.get('/customers')
    assert response.status_code == 200
    assert b'Empty Customer' in response.data
    
    # Set preference to False (hide customers without calls)
    client.post('/api/preferences/show-customers-without-calls',
                json={'show_customers_without_calls': False})
    
    # Now customer should be filtered out
    response = client.get('/customers')
    assert response.status_code == 200
    assert b'Empty Customer' not in response.data    # Enable showing customers without calls
    client.post('/api/preferences/show-customers-without-calls',
                json={'show_customers_without_calls': True})
    
    response = client.get('/customers')
    assert response.status_code == 200
    assert b'Empty Customer' in response.data
    assert b'Dark Mode' in response.data


def test_admin_shutdown_returns_success(client):
    """Test that the shutdown endpoint returns success and schedules a shutdown."""
    from unittest.mock import patch, MagicMock

    with patch('app.routes.admin.threading.Timer') as mock_timer:
        mock_instance = MagicMock()
        mock_timer.return_value = mock_instance

        response = client.post('/api/admin/shutdown')
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert 'shutting down' in data['message']

        # Verify a timer was started to kill the process
        mock_timer.assert_called_once()
        mock_instance.start.assert_called_once()


def test_admin_shutdown_rejects_get(client):
    """Test that GET requests to shutdown endpoint are rejected."""
    response = client.get('/api/admin/shutdown')
    assert response.status_code == 405


def test_admin_panel_has_shutdown_button(client):
    """Test that the admin panel includes the shutdown button."""
    response = client.get('/admin')
    assert response.status_code == 200
    assert b'shutdownServerBtn' in response.data
    assert b'Shut Down Server' in response.data


# =============================================================================
# Admin Panel AI Integration Card
# =============================================================================

def test_admin_ai_not_configured_shows_disabled_test(client):
    """Admin AI card shows red X for each setting when nothing is configured."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('AZURE_OPENAI_ENDPOINT', None)
        os.environ.pop('AZURE_OPENAI_DEPLOYMENT', None)
        os.environ.pop('AZURE_CLIENT_ID', None)
        os.environ.pop('AZURE_CLIENT_SECRET', None)
        os.environ.pop('AZURE_TENANT_ID', None)
        response = client.get('/admin')
        html = response.data.decode()

    # All 5 settings should show x-circle (not configured)
    assert html.count('x-circle-fill text-danger') >= 5
    assert html.count('check-circle-fill text-success') == 0 or 'AZURE_OPENAI' not in html.split('check-circle-fill')[0]
    # Setup guide should be visible
    assert 'Setup guide' in html
    # Test button should be disabled
    assert 'id="aiTestBtn"' not in html


def test_admin_ai_configured_shows_test_button(client):
    """Admin AI card shows green checks and enabled test button when fully configured."""
    env_vars = {
        'AZURE_OPENAI_ENDPOINT': 'https://test.openai.azure.com/',
        'AZURE_OPENAI_DEPLOYMENT': 'gpt-4o-mini',
        'AZURE_CLIENT_ID': '12345678-1234-1234-1234-123456789abc',
        'AZURE_CLIENT_SECRET': 'real-secret-value',
        'AZURE_TENANT_ID': '12345678-1234-1234-1234-123456789abc',
    }
    with patch.dict(os.environ, env_vars):
        response = client.get('/admin')
        html = response.data.decode()

    assert 'gpt-4o-mini' in html
    assert 'id="aiTestBtn"' in html
    # Should not show the setup guide link (all configured)
    assert 'Setup guide' not in html


def test_admin_ai_placeholder_values_detected(client):
    """Admin AI card detects placeholder values from .env.example and shows them as invalid."""
    env_vars = {
        'AZURE_OPENAI_ENDPOINT': 'https://your-resource.openai.azure.com/',
        'AZURE_OPENAI_DEPLOYMENT': 'gpt-4o-mini',
        'AZURE_CLIENT_ID': 'your-service-principal-client-id',
        'AZURE_CLIENT_SECRET': 'your-service-principal-secret',
        'AZURE_TENANT_ID': 'your-azure-tenant-id',
    }
    with patch.dict(os.environ, env_vars):
        response = client.get('/admin')
        html = response.data.decode()

    # Deployment is real (gpt-4o-mini), but everything else is a placeholder
    # Test button should be disabled because not all_configured
    assert 'id="aiTestBtn"' not in html
    assert 'Setup guide' in html


def test_admin_ai_missing_credentials_shows_red(client):
    """Admin AI card shows red X for credentials when endpoint is set but no service principal."""
    env_vars = {
        'AZURE_OPENAI_ENDPOINT': 'https://test.openai.azure.com/',
        'AZURE_OPENAI_DEPLOYMENT': 'gpt-4o-mini',
    }
    with patch.dict(os.environ, env_vars, clear=False):
        os.environ.pop('AZURE_CLIENT_ID', None)
        os.environ.pop('AZURE_CLIENT_SECRET', None)
        os.environ.pop('AZURE_TENANT_ID', None)
        response = client.get('/admin')
        html = response.data.decode()

    assert 'AZURE_CLIENT_ID' in html
    assert 'AZURE_CLIENT_SECRET' in html
    assert 'AZURE_TENANT_ID' in html
    assert 'id="aiTestBtn"' not in html


def test_admin_ai_missing_deployment_shows_red(client):
    """Admin AI card shows red X for deployment when it's not set."""
    env_vars = {
        'AZURE_OPENAI_ENDPOINT': 'https://test.openai.azure.com/',
        'AZURE_CLIENT_ID': '12345678-1234-1234-1234-123456789abc',
        'AZURE_CLIENT_SECRET': 'real-secret-value',
        'AZURE_TENANT_ID': '12345678-1234-1234-1234-123456789abc',
    }
    with patch.dict(os.environ, env_vars, clear=False):
        os.environ.pop('AZURE_OPENAI_DEPLOYMENT', None)
        response = client.get('/admin')
        html = response.data.decode()

    assert 'AZURE_OPENAI_DEPLOYMENT' in html
    assert 'id="aiTestBtn"' not in html


def test_admin_ai_non_guid_ids_detected(client):
    """Admin AI card detects non-GUID client/tenant IDs as invalid."""
    env_vars = {
        'AZURE_OPENAI_ENDPOINT': 'https://test.openai.azure.com/',
        'AZURE_OPENAI_DEPLOYMENT': 'gpt-4o-mini',
        'AZURE_CLIENT_ID': 'not-a-guid',
        'AZURE_CLIENT_SECRET': 'real-secret-value',
        'AZURE_TENANT_ID': 'also-not-a-guid',
    }
    with patch.dict(os.environ, env_vars):
        response = client.get('/admin')
        html = response.data.decode()

    # Non-GUID IDs should fail validation
    assert 'id="aiTestBtn"' not in html
    assert 'Setup guide' in html


# =============================================================================
# Admin Panel Update Check - Boot Commit Tracking
# =============================================================================

def test_update_check_includes_boot_commit(client, app):
    """Update check API includes boot_commit from app config."""
    app.config['BOOT_COMMIT'] = 'abc1234'
    mock_state = {
        'available': False,
        'local_commit': 'abc1234',
        'remote_commit': 'abc1234',
        'commits_behind': 0,
        'last_checked': '2026-03-04T12:00:00+00:00',
        'error': None,
    }
    with patch('app.services.update_checker.get_update_state', return_value=mock_state):
        response = client.get('/api/admin/update-check')
        data = response.get_json()

    assert data['boot_commit'] == 'abc1234'
    assert data['restart_needed'] is False


def test_update_check_restart_needed_when_commits_differ(client, app):
    """Update check API signals restart_needed when boot != disk commit."""
    app.config['BOOT_COMMIT'] = 'abc1234'
    mock_state = {
        'available': False,
        'local_commit': 'def5678',
        'remote_commit': 'def5678',
        'commits_behind': 0,
        'last_checked': '2026-03-04T12:00:00+00:00',
        'error': None,
    }
    with patch('app.services.update_checker.get_update_state', return_value=mock_state):
        response = client.get('/api/admin/update-check')
        data = response.get_json()

    assert data['boot_commit'] == 'abc1234'
    assert data['local_commit'] == 'def5678'
    assert data['restart_needed'] is True


def test_update_check_no_restart_when_boot_commit_none(client, app):
    """No restart_needed when boot commit could not be determined."""
    app.config['BOOT_COMMIT'] = None
    mock_state = {
        'available': False,
        'local_commit': 'abc1234',
        'remote_commit': 'abc1234',
        'commits_behind': 0,
        'last_checked': '2026-03-04T12:00:00+00:00',
        'error': None,
    }
    with patch('app.services.update_checker.get_update_state', return_value=mock_state):
        response = client.get('/api/admin/update-check')
        data = response.get_json()

    assert data['boot_commit'] is None
    assert data['restart_needed'] is False
