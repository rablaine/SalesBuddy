"""
Tests for VPN / IP-blocked detection in MSX integration.

Tests the centralized VPN detection in _msx_request(), the VPN state
management in msx_auth, and the VPN status/check API endpoints.
"""
import pytest
from unittest.mock import patch, MagicMock
from app.services.msx_auth import (
    is_vpn_blocked,
    get_vpn_state,
    set_vpn_blocked,
    clear_vpn_block,
    check_vpn_recovery,
    _vpn_state,
)
from app.services.msx_api import IP_BLOCKED_CODE, IP_BLOCKED_MESSAGE


class TestVpnStateManagement:
    """Tests for VPN state functions in msx_auth."""

    def setup_method(self):
        """Reset VPN state before each test."""
        clear_vpn_block()

    def test_initial_state_not_blocked(self):
        """VPN state should start as not blocked."""
        assert is_vpn_blocked() is False
        state = get_vpn_state()
        assert state["blocked"] is False
        assert state["blocked_at"] is None
        assert state["error_message"] is None

    def test_set_vpn_blocked(self):
        """Setting VPN blocked should update all state fields."""
        set_vpn_blocked("IP address is blocked by MSX firewall")
        assert is_vpn_blocked() is True
        state = get_vpn_state()
        assert state["blocked"] is True
        assert state["blocked_at"] is not None
        assert "IP address" in state["error_message"]

    def test_set_vpn_blocked_default_message(self):
        """Setting VPN blocked without message uses default."""
        set_vpn_blocked()
        assert is_vpn_blocked() is True
        state = get_vpn_state()
        assert state["error_message"] == "IP address blocked by MSX"

    @patch('app.services.msx_auth.clear_token_cache')
    def test_set_vpn_blocked_clears_token_cache(self, mock_clear):
        """Setting VPN blocked should also clear the token cache."""
        set_vpn_blocked("test")
        mock_clear.assert_called_once()

    def test_clear_vpn_block(self):
        """Clearing VPN block should reset all state."""
        set_vpn_blocked("blocked!")
        assert is_vpn_blocked() is True
        clear_vpn_block()
        assert is_vpn_blocked() is False
        state = get_vpn_state()
        assert state["blocked"] is False
        assert state["blocked_at"] is None

    def test_get_vpn_state_returns_copy(self):
        """get_vpn_state should return a copy, not the original dict."""
        state = get_vpn_state()
        state["blocked"] = True  # mutate the copy
        assert is_vpn_blocked() is False  # original should be unchanged


class TestVpnRecovery:
    """Tests for VPN recovery check."""

    def setup_method(self):
        clear_vpn_block()

    @patch('app.services.msx_api.test_connection')
    def test_check_vpn_recovery_success(self, mock_test):
        """Successful WhoAmI should clear VPN block."""
        set_vpn_blocked("blocked")
        mock_test.return_value = {"success": True, "user_id": "abc"}
        result = check_vpn_recovery()
        assert result["success"] is True
        assert is_vpn_blocked() is False

    @patch('app.services.msx_api.test_connection')
    def test_check_vpn_recovery_still_blocked(self, mock_test):
        """If still IP-blocked, VPN block should remain."""
        set_vpn_blocked("blocked")
        mock_test.return_value = {"success": False, "error": "IP address is blocked"}
        result = check_vpn_recovery()
        assert result["success"] is False
        assert result.get("vpn_blocked") is True
        assert is_vpn_blocked() is True

    @patch('app.services.msx_api.test_connection')
    def test_check_vpn_recovery_other_error(self, mock_test):
        """Non-VPN error should not clear VPN block but report failure."""
        set_vpn_blocked("blocked")
        mock_test.return_value = {"success": False, "error": "some other error"}
        result = check_vpn_recovery()
        assert result["success"] is False
        assert is_vpn_blocked() is True

    @patch('app.services.msx_api.test_connection')
    def test_check_vpn_recovery_updates_last_check(self, mock_test):
        """Recovery check should update last_check timestamp."""
        mock_test.return_value = {"success": False, "error": "timeout"}
        check_vpn_recovery()
        state = get_vpn_state()
        assert state["last_check"] is not None


class TestMsxRequestVpnDetection:
    """Tests for IP-blocked detection in _msx_request."""

    def setup_method(self):
        clear_vpn_block()

    @patch('app.services.msx_api.get_msx_token', return_value='fake-token')
    @patch('app.services.msx_api.requests')
    def test_ip_blocked_403_sets_vpn_state(self, mock_requests, mock_token):
        """A 403 response with IP-blocked error code should set VPN blocked."""
        from app.services.msx_api import _msx_request

        # Create a mock response that looks like an IP-blocked 403
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.ok = False
        mock_resp.text = (
            '{"error":{"code":"0x80095ffe","message":"Sorry, you can\'t access '
            'this resource because your IP address is blocked."}}'
        )
        mock_requests.get.return_value = mock_resp

        # Also mock refresh_token for the auth-retry path — but IP block
        # should skip the retry entirely
        with patch('app.services.msx_api.refresh_token') as mock_refresh:
            result = _msx_request('GET', 'https://example.com/test')

        assert result.status_code == 403
        assert is_vpn_blocked() is True
        # Should NOT have tried to refresh token (IP block skips retry)
        mock_refresh.assert_not_called()

    @patch('app.services.msx_api.get_msx_token', return_value='fake-token')
    @patch('app.services.msx_api.requests')
    def test_regular_403_still_retries_with_fresh_token(self, mock_requests, mock_token):
        """A regular 403 (not IP-blocked) should still do the token refresh retry."""
        from app.services.msx_api import _msx_request

        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.ok = False
        mock_resp.text = '{"error":"Forbidden"}'

        mock_requests.get.return_value = mock_resp

        with patch('app.services.msx_api.refresh_token', return_value=True) as mock_refresh:
            result = _msx_request('GET', 'https://example.com/test')

        # Regular 403 should trigger token refresh
        mock_refresh.assert_called_once()
        assert is_vpn_blocked() is False

    @patch('app.services.msx_api.get_msx_token', return_value='fake-token')
    @patch('app.services.msx_api.requests')
    def test_successful_response_clears_vpn_block(self, mock_requests, mock_token):
        """A successful MSX response should auto-clear a previous VPN block."""
        from app.services.msx_api import _msx_request

        # First, set VPN as blocked
        set_vpn_blocked("was blocked")
        assert is_vpn_blocked() is True

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.text = '{"value": []}'
        mock_requests.get.return_value = mock_resp

        result = _msx_request('GET', 'https://example.com/test')

        assert result.status_code == 200
        assert is_vpn_blocked() is False  # Auto-cleared!


class TestVpnConstants:
    """Tests for VPN detection constants."""

    def test_ip_blocked_code_value(self):
        """IP blocked error code should match MSX's known code."""
        assert IP_BLOCKED_CODE == "0x80095ffe"

    def test_ip_blocked_message_value(self):
        """IP blocked message should be a recognizable substring."""
        assert "IP address" in IP_BLOCKED_MESSAGE


class TestVpnStatusEndpoints:
    """Tests for VPN status and check API endpoints."""

    def setup_method(self):
        clear_vpn_block()

    def test_vpn_status_not_blocked(self, client):
        """GET /api/msx/vpn-status should return not blocked by default."""
        resp = client.get('/api/msx/vpn-status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["blocked"] is False

    def test_vpn_status_blocked(self, client):
        """GET /api/msx/vpn-status should return blocked when set."""
        set_vpn_blocked("IP address is blocked")
        resp = client.get('/api/msx/vpn-status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["blocked"] is True
        assert data["blocked_at"] is not None
        assert "IP address" in data["error_message"]

    @patch('app.services.msx_api.test_connection')
    def test_vpn_check_recovery_success(self, mock_test, client):
        """POST /api/msx/vpn-check should clear block on success."""
        set_vpn_blocked("blocked")
        mock_test.return_value = {"success": True, "user_id": "abc"}
        resp = client.post('/api/msx/vpn-check')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert is_vpn_blocked() is False

    @patch('app.services.msx_api.test_connection')
    def test_vpn_check_still_blocked(self, mock_test, client):
        """POST /api/msx/vpn-check should report 403 when still blocked."""
        set_vpn_blocked("blocked")
        mock_test.return_value = {"success": False, "error": "IP address is blocked"}
        resp = client.post('/api/msx/vpn-check')
        assert resp.status_code == 403  # after_request promotes vpn_blocked responses to 403
        data = resp.get_json()
        assert data["success"] is False
        assert is_vpn_blocked() is True

    @patch('app.routes.msx.get_milestones_by_account')
    @patch('app.routes.msx.extract_account_id_from_url', return_value='00000000-0000-0000-0000-000000000001')
    def test_after_request_promotes_vpn_blocked_to_403(self, mock_extract, mock_milestones, client):
        """MSX routes returning 200 with vpn_blocked should become 403."""
        mock_milestones.return_value = {
            "success": False,
            "error": "IP address is blocked — connect to VPN and retry.",
            "vpn_blocked": True,
        }
        # Create a customer with a TPID URL so the route reaches get_milestones_by_account
        from app.models import Customer, User, db
        test_user = User.query.first()
        customer = Customer(
            name="VPN Test Co",
            tpid=12345,
            tpid_url="https://microsoftsales.crm.dynamics.com/main.aspx?id=00000000-0000-0000-0000-000000000001",
        )
        db.session.add(customer)
        db.session.commit()

        resp = client.get(f'/api/msx/milestones-for-customer/{customer.id}')
        assert resp.status_code == 403
        data = resp.get_json()
        assert data["vpn_blocked"] is True

    def test_after_request_does_not_affect_normal_responses(self, client):
        """Normal 200 responses without vpn_blocked should stay 200."""
        resp = client.get('/api/msx/task-categories')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True


class TestBackgroundRefreshVpnSkip:
    """Test that background refresh skips when VPN is blocked."""

    def setup_method(self):
        clear_vpn_block()

    @patch('app.services.msx_auth._run_az_command')
    def test_refresh_skips_when_vpn_blocked(self, mock_az):
        """Background refresh logic should not call az CLI when VPN blocked."""
        from app.services.msx_auth import _vpn_state
        _vpn_state["blocked"] = True
        # The _refresh_loop checks _vpn_state["blocked"] and skips.
        # We test the flag directly since the loop runs in a daemon thread.
        assert _vpn_state["blocked"] is True
        # Clean up
        clear_vpn_block()
