"""
Tests for the onboarding wizard (Issue #6).

Verifies that the multi-step onboarding modal appears when appropriate,
the dismiss endpoint works, and the modal is hidden after dismissal.
"""
import pytest
from unittest.mock import patch, MagicMock


class TestOnboardingWizardDisplay:
    """Tests for onboarding wizard visibility logic."""

    def test_onboarding_modal_shown_when_not_dismissed(self, client, app):
        """Onboarding wizard should appear when first_run_modal_dismissed is False."""
        response = client.get('/')
        assert response.status_code == 200
        # First-run setup is rendered and auto-opens.
        assert b'id="welcomeModal"' in response.data
        assert b'data-sb-auto-open="true"' in response.data
        assert b'onboardingStep1' in response.data
        assert b'onboardingStep2' in response.data
        # The wizard is two steps now - nothing past step 2 should exist.
        assert b'onboardingStep3' not in response.data

    def test_onboarding_modal_hidden_when_dismissed(self, client, app):
        """Completed users retain a closed, manually reviewable wizard without auto-opening."""
        from bs4 import BeautifulSoup

        with app.app_context():
            from app.models import db, UserPreference
            pref = UserPreference.query.first()
            pref.first_run_modal_dismissed = True
            db.session.commit()

        response = client.get('/')
        assert response.status_code == 200
        dialog = BeautifulSoup(response.data, 'html.parser').select_one('dialog#welcomeModal')
        assert dialog is not None
        assert dialog['data-sb-auto-open'] == 'false'
        assert not dialog.has_attr('open')

        # Clean up
        with app.app_context():
            from app.models import db, UserPreference
            pref = UserPreference.query.first()
            pref.first_run_modal_dismissed = False
            db.session.commit()

    def test_onboarding_shows_on_non_index_pages(self, client, app):
        """Onboarding wizard renders on all pages (it's in base.html)."""
        # The wizard is in base.html, so it should render on any page
        response = client.get('/customers')
        assert response.status_code == 200
        assert b'id="welcomeModal"' in response.data

    def test_onboarding_step_structure(self, client, app):
        """Verify both wizard steps have proper structure."""
        response = client.get('/')
        html = response.data.decode('utf-8')

        # Step 1: welcome, theme, and sign-in all on one screen
        assert "get you set up" in html.lower()
        assert 'onboardDarkModeToggle' in html
        assert 'onboardThemeFamily' in html
        assert 'Connect to MSX' in html
        assert 'startSignInBtn' in html
        assert 'Sign In to Azure' in html

        # Step 2: one button drives accounts, then milestones and revenue
        assert 'onboardStartImport' in html
        assert 'Start Import' in html
        for phase in ('Accounts', 'Milestones', 'Revenue'):
            assert 'id="phase%sBar"' % phase in html
            assert 'id="phase%sStatus"' % phase in html

    def test_onboarding_has_no_skip_button(self, client, app):
        """Verify the skip button has been removed (wizard is mandatory)."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'onboardSkipBtn' not in html
        assert "Skip setup" not in html

    def test_onboarding_has_navigation_buttons(self, client, app):
        """Verify Next/Back buttons exist."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'onboardNextBtn' in html
        assert 'onboardBackBtn' in html

    def test_onboarding_step_label_and_import_progress(self, client, app):
        """Step count stays at the header's right; only imports have progress bars."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(client.get('/').data, 'html.parser')
        header = soup.select_one('#welcomeModal .sb-dialog-header')
        assert header.select_one(':scope > #onboardingStepBadge').get_text() == 'Step 1 of 2'
        assert header.select_one('.sb-dialog-heading #setupWizardEyebrow')
        theme_row = soup.select_one('.sb-setup-theme')
        assert theme_row.select_one('.sb-setup-theme-copy h3 label[for="onboardThemeFamily"]')
        assert [
            child.get('class') for child in theme_row.find_all(recursive=False)
        ] == [
            ['sb-setup-theme-copy'],
            ['sb-record-field', 'sb-setup-theme-picker'],
            ['sb-theme-switch'],
        ]
        body = soup.select_one('#welcomeModal .sb-dialog-body')
        assert not body.select(':scope > .sb-setup-progress')
        assert not body.select_one('#onboardingStep1 [role="progressbar"]')
        for phase in ('Accounts', 'Milestones', 'Revenue'):
            assert body.select_one(f'#phase{phase}Bar')


class TestDismissWelcomeModalEndpoint:
    """Tests for the dismiss-welcome-modal API endpoint."""

    def test_dismiss_welcome_modal(self, client, app):
        """POST to dismiss endpoint should set first_run_modal_dismissed to True."""
        response = client.post('/api/preferences/dismiss-welcome-modal',
                               content_type='application/json')
        assert response.status_code == 200

        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            assert pref.first_run_modal_dismissed is True

        # Clean up
        with app.app_context():
            from app.models import db, UserPreference
            pref = UserPreference.query.first()
            pref.first_run_modal_dismissed = False
            db.session.commit()

    def test_dismiss_modal_then_page_hides_wizard(self, client, app):
        """Dismissing disables auto-opening while retaining read-only review availability."""
        # Dismiss
        response = client.post('/api/preferences/dismiss-welcome-modal',
                               content_type='application/json')
        assert response.status_code == 200

        # Load page
        response = client.get('/')
        assert response.status_code == 200
        assert b'id="welcomeModal"' in response.data
        assert b'data-sb-auto-open="false"' in response.data

        # Clean up
        with app.app_context():
            from app.models import db, UserPreference
            pref = UserPreference.query.first()
            pref.first_run_modal_dismissed = False
            db.session.commit()


class TestDarkModeToggleInOnboarding:
    """Tests for the dark mode toggle API used in the onboarding wizard."""

    def test_dark_mode_toggle_api(self, client, app):
        """POST to dark-mode endpoint should update preference."""
        response = client.post('/api/preferences/dark-mode',
                               json={'dark_mode': True},
                               content_type='application/json')
        assert response.status_code == 200

        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            assert pref.dark_mode is True

        # Clean up
        with app.app_context():
            from app.models import db, UserPreference
            pref = UserPreference.query.first()
            pref.dark_mode = False
            db.session.commit()

    def test_dark_mode_toggle_off(self, client, app):
        """POST dark_mode=false should disable dark mode."""
        # First enable
        client.post('/api/preferences/dark-mode',
                     json={'dark_mode': True},
                     content_type='application/json')

        # Then disable
        response = client.post('/api/preferences/dark-mode',
                               json={'dark_mode': False},
                               content_type='application/json')
        assert response.status_code == 200

        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            assert pref.dark_mode is False


class TestOldModalRemoval:
    """Tests verifying the old first-time modal was properly removed."""

    def test_no_old_first_time_modal_in_index(self, client, app):
        """The old firstTimeModal should not exist in index page."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'firstTimeModal' not in html
        assert 'firstTimeDarkModeSwitch' not in html

    def test_no_show_first_time_modal_in_session(self, client, app):
        """The show_first_time_modal session flag should not be used."""
        response = client.get('/')
        assert response.status_code == 200
        # Just verify the page loads — the session flag is no longer set

    def test_index_loads_without_show_first_time_modal_param(self, client):
        """Index should work without the old show_first_time_modal template var."""
        response = client.get('/')
        assert response.status_code == 200
        assert b'Sales Buddy' in response.data


class TestResetOnboarding:
    """Tests for the reset-onboarding API endpoint and re-run button."""

    def test_reset_onboarding_endpoint(self, client, app):
        """POST to reset-onboarding should set first_run_modal_dismissed to False."""
        # First dismiss
        client.post('/api/preferences/dismiss-welcome-modal',
                     content_type='application/json')

        # Then reset
        response = client.post('/api/preferences/reset-onboarding',
                               content_type='application/json')
        assert response.status_code == 200
        data = response.get_json()
        assert data['first_run_modal_dismissed'] is False

        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            assert pref.first_run_modal_dismissed is False

    def test_reset_then_wizard_shows_again(self, client, app):
        """The explicit reset command re-enables automatic first-run opening."""
        # Dismiss
        client.post('/api/preferences/dismiss-welcome-modal',
                     content_type='application/json')
        # Verify dismissed
        response = client.get('/')
        assert b'data-sb-auto-open="false"' in response.data

        # Reset
        client.post('/api/preferences/reset-onboarding',
                     content_type='application/json')
        # Verify wizard is back
        response = client.get('/')
        assert b'id="welcomeModal"' in response.data
        assert b'data-sb-auto-open="true"' in response.data

    def test_rerun_button_removed_from_navbar(self, client, app):
        """Navbar re-run button was removed (wizard is mandatory, can't be skipped)."""
        # Even when dismissed with no customers, the navbar button should not exist
        with app.app_context():
            from app.models import db, UserPreference
            pref = UserPreference.query.first()
            pref.first_run_modal_dismissed = True
            db.session.commit()

        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'id="rerunSetupBtn"' not in html

        # Clean up
        with app.app_context():
            from app.models import db, UserPreference
            pref = UserPreference.query.first()
            pref.first_run_modal_dismissed = False
            db.session.commit()


class TestAzLoginEndpoints:
    """Tests for the browser-based az login API endpoints."""

    @patch('app.routes.msx.get_az_cli_status')
    def test_az_status_endpoint(self, mock_status, client, app):
        """GET /api/msx/az-status should return az CLI status."""
        mock_status.return_value = {
            "az_installed": True,
            "logged_in": True,
            "user_email": "test@microsoft.com",
            "message": "Logged in as test@microsoft.com",
        }
        response = client.get('/api/msx/az-status')
        assert response.status_code == 200
        data = response.get_json()
        assert data['az_installed'] is True
        assert data['logged_in'] is True
        assert data['user_email'] == 'test@microsoft.com'

    @patch('app.routes.msx.get_az_cli_status')
    def test_az_status_not_installed(self, mock_status, client, app):
        """GET /api/msx/az-status should report when CLI not installed."""
        mock_status.return_value = {
            "az_installed": False,
            "logged_in": False,
            "user_email": None,
            "message": "Azure CLI not installed",
        }
        response = client.get('/api/msx/az-status')
        assert response.status_code == 200
        data = response.get_json()
        assert data['az_installed'] is False

    @patch('app.routes.msx.get_az_cli_status')
    @patch('app.routes.msx.start_az_login')
    def test_az_login_start_endpoint(self, mock_login, mock_status, client, app):
        """POST /api/msx/az-login/start should launch az login."""
        mock_login.return_value = {
            "success": True,
            "message": "Browser will open. Complete sign-in to continue.",
        }
        mock_status.return_value = {
            "az_installed": True,
            "logged_in": False,
            "user_email": None,
            "message": "Not logged in",
        }
        response = client.post('/api/msx/az-login/start')
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True

    @patch('app.routes.msx.start_az_login')
    def test_az_login_start_no_cli(self, mock_login, client, app):
        """POST /api/msx/az-login/start should report error when CLI missing."""
        mock_login.return_value = {
            "success": False,
            "error": "Azure CLI not installed. Install from https://aka.ms/installazurecli",
        }
        response = client.post('/api/msx/az-login/start')
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is False
        assert 'not installed' in data['error']

    @patch('app.routes.msx.get_msx_auth_status')
    @patch('app.routes.msx.refresh_token')
    @patch('app.routes.msx.set_subscription')
    @patch('app.routes.msx.get_az_cli_status')
    def test_az_login_complete_success(self, mock_status, mock_sub,
                                       mock_refresh, mock_auth, client, app):
        """POST /api/msx/az-login/complete should set subscription and refresh token."""
        mock_status.return_value = {
            "az_installed": True,
            "logged_in": True,
            "user_email": "test@microsoft.com",
            "message": "Logged in as test@microsoft.com",
        }
        mock_sub.return_value = True
        mock_refresh.return_value = True
        mock_auth.return_value = {
            "authenticated": True,
            "user": "test@microsoft.com",
            "expires_on": None,
            "last_refresh": None,
            "error": None,
        }
        response = client.post('/api/msx/az-login/complete')
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['user_email'] == 'test@microsoft.com'

    @patch('app.routes.msx.get_az_cli_status')
    def test_az_login_complete_not_logged_in(self, mock_status, client, app):
        """POST /api/msx/az-login/complete should 400 when not logged in."""
        mock_status.return_value = {
            "az_installed": True,
            "logged_in": False,
            "user_email": None,
            "message": "Not logged in",
        }
        response = client.post('/api/msx/az-login/complete')
        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False

    @patch('app.routes.msx.get_az_login_process_status')
    def test_az_login_status_running(self, mock_proc, client, app):
        """GET /api/msx/az-login/status should report running process."""
        mock_proc.return_value = {
            "active": True,
            "running": True,
            "exit_code": None,
            "elapsed_seconds": 5.2,
        }
        response = client.get('/api/msx/az-login/status')
        assert response.status_code == 200
        data = response.get_json()
        assert data['running'] is True
        assert data['exit_code'] is None

    @patch('app.routes.msx.get_az_login_process_status')
    def test_az_login_status_success(self, mock_proc, client, app):
        """GET /api/msx/az-login/status should report exit code 0 on success."""
        mock_proc.return_value = {
            "active": True,
            "running": False,
            "exit_code": 0,
            "elapsed_seconds": 12.3,
        }
        response = client.get('/api/msx/az-login/status')
        assert response.status_code == 200
        data = response.get_json()
        assert data['running'] is False
        assert data['exit_code'] == 0

    @patch('app.routes.msx.get_az_login_process_status')
    def test_az_login_status_failed(self, mock_proc, client, app):
        """GET /api/msx/az-login/status should report non-zero exit on failure."""
        mock_proc.return_value = {
            "active": True,
            "running": False,
            "exit_code": 1,
            "elapsed_seconds": 300.0,
        }
        response = client.get('/api/msx/az-login/status')
        assert response.status_code == 200
        data = response.get_json()
        assert data['running'] is False
        assert data['exit_code'] == 1

    @patch('app.routes.msx.get_az_login_process_status')
    def test_az_login_status_no_process(self, mock_proc, client, app):
        """GET /api/msx/az-login/status should handle no active process."""
        mock_proc.return_value = {
            "active": False,
            "running": False,
            "exit_code": None,
            "elapsed_seconds": 0,
        }
        response = client.get('/api/msx/az-login/status')
        assert response.status_code == 200
        data = response.get_json()
        assert data['active'] is False


class TestOnboardingAuthUiElements:
    """Tests that the wizard Step 2 has the correct az-login UI elements."""

    def test_step2_has_sign_in_button(self, client, app):
        """Step 2 should show the 'Sign In to Azure' button."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'Sign In to Azure' in html
        assert 'startSignInBtn' in html

    def test_step2_has_auth_states(self, client, app):
        """Step 2 should have all sign-in state divs."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'id="signInInitial"' in html
        assert 'id="signInWaiting"' in html
        assert 'id="signInSuccess"' in html
        assert 'id="signInError"' in html
        assert 'id="signInNoCli"' in html
        assert 'id="signInVpnBlocked"' in html

    def test_step2_no_device_code_elements(self, client, app):
        """Step 2 should NOT have old device code elements."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'authDeviceCode' not in html
        assert 'authCopyCode' not in html
        assert 'devicelogin' not in html

    def test_step2_has_retry_buttons(self, client, app):
        """Step 2 should have retry, cancel buttons."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'id="signInRetryBtn"' in html
        assert 'id="signInNoCliRetryBtn"' in html
        assert 'id="signInCancelBtn"' in html

    def test_step2_no_wrong_tenant_state(self, client, app):
        """Step 2 should NOT have the authWrongTenant state (simplified auth)."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'id="authWrongTenant"' not in html
        assert 'id="authRetryWrongTenant"' not in html

    def test_step2_has_corporate_account_hint(self, client, app):
        """Step 2 should remind users to select their Microsoft corporate account."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'Microsoft corporate account' in html

    def test_step2_has_120s_timeout(self, client, app):
        """Step 2 JS should use a 120-second sign-in timeout."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'SIGN_IN_TIMEOUT_MS = 120000' in html


class TestAzLogoutEndpoint:
    """Tests for the az logout endpoint."""

    @patch('app.routes.msx.az_logout')
    def test_az_logout_success(self, mock_logout, client, app):
        """POST /api/msx/az-logout should call az_logout and return result."""
        mock_logout.return_value = {"success": True, "message": "Logged out"}
        response = client.post('/api/msx/az-logout')
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        mock_logout.assert_called_once()

    @patch('app.routes.msx.az_logout')
    def test_az_logout_error(self, mock_logout, client, app):
        """POST /api/msx/az-logout should return error on failure."""
        mock_logout.return_value = {"success": False, "error": "something broke"}
        response = client.post('/api/msx/az-logout')
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is False


class TestAzWrongTenantDetection:
    """Tests for wrong tenant detection in az-status."""

    @patch('app.routes.msx.get_az_cli_status')
    def test_az_status_wrong_tenant(self, mock_status, client, app):
        """GET /api/msx/az-status should return wrong_tenant when on wrong tenant."""
        mock_status.return_value = {
            "az_installed": True,
            "logged_in": True,
            "wrong_tenant": True,
            "user_email": "user@contoso.com",
            "message": "Signed in as user@contoso.com but on the wrong tenant.",
        }
        response = client.get('/api/msx/az-status')
        assert response.status_code == 200
        data = response.get_json()
        assert data['logged_in'] is True
        assert data['wrong_tenant'] is True

    @patch('app.routes.msx.get_az_cli_status')
    def test_az_status_correct_tenant(self, mock_status, client, app):
        """GET /api/msx/az-status should not flag wrong_tenant for correct tenant."""
        mock_status.return_value = {
            "az_installed": True,
            "logged_in": True,
            "wrong_tenant": False,
            "user_email": "user@microsoft.com",
            "message": "Logged in as user@microsoft.com",
        }
        response = client.get('/api/msx/az-status')
        assert response.status_code == 200
        data = response.get_json()
        assert data['logged_in'] is True
        assert data['wrong_tenant'] is False

    @patch('app.services.msx_auth.check_az_cli_installed')
    @patch('app.services.msx_auth.check_az_logged_in')
    def test_get_az_cli_status_wrong_tenant_unit(self, mock_logged_in,
                                                  mock_installed, app):
        """get_az_cli_status() should set wrong_tenant when tenantId doesn't match."""
        mock_installed.return_value = (True, None)
        mock_logged_in.return_value = (True, "user@contoso.com", "aaaabbbb-0000-0000-0000-ccccddddeeee")
        from app.services.msx_auth import get_az_cli_status
        result = get_az_cli_status()
        assert result['logged_in'] is True
        assert result['wrong_tenant'] is True

    @patch('app.services.msx_auth.check_az_cli_installed')
    @patch('app.services.msx_auth.check_az_logged_in')
    def test_get_az_cli_status_correct_tenant_unit(self, mock_logged_in,
                                                    mock_installed, app):
        """get_az_cli_status() should not flag wrong_tenant for correct tenantId."""
        mock_installed.return_value = (True, None)
        mock_logged_in.return_value = (True, "user@microsoft.com", "72f988bf-86f1-41af-91ab-2d7cd011db47")
        from app.services.msx_auth import get_az_cli_status
        result = get_az_cli_status()
        assert result['logged_in'] is True
        assert result['wrong_tenant'] is False

    @patch('app.services.msx_auth.subprocess.run')
    def test_check_az_cli_installed_nonzero_with_output(self, mock_run, app):
        """check_az_cli_installed() returns (True, None) when az --version exits non-zero but outputs version info."""
        # Clear cache to ensure fresh check
        from app.services import msx_auth
        msx_auth._az_cli_installed_cache = {"installed": None, "last_error": None}
        
        mock_run.return_value = type('Result', (), {
            'returncode': 2,
            'stdout': 'azure-cli                         2.67.0\ncore                              2.67.0\n',
            'stderr': 'WARNING: Extension update available\n',
        })()
        from app.services.msx_auth import check_az_cli_installed
        installed, error = check_az_cli_installed()
        assert installed is True
        assert error is None

    @patch('app.services.msx_auth.subprocess.run')
    def test_check_az_cli_installed_nonzero_no_output(self, mock_run, app):
        """check_az_cli_installed() returns (False, 'check_failed') when az --version exits non-zero with no version output."""
        # Clear cache to ensure fresh check
        from app.services import msx_auth
        msx_auth._az_cli_installed_cache = {"installed": None, "last_error": None}
        
        mock_run.return_value = type('Result', (), {
            'returncode': 1,
            'stdout': '',
            'stderr': 'az: command not found\n',
        })()
        from app.services.msx_auth import check_az_cli_installed
        installed, error = check_az_cli_installed()
        assert installed is False
        assert error == "check_failed"

    @patch('app.services.msx_auth.subprocess.run')
    def test_check_az_cli_installed_success(self, mock_run, app):
        """check_az_cli_installed() returns (True, None) on returncode 0."""
        # Clear cache to ensure fresh check
        from app.services import msx_auth
        msx_auth._az_cli_installed_cache = {"installed": None, "last_error": None}
        
        mock_run.return_value = type('Result', (), {
            'returncode': 0,
            'stdout': 'azure-cli 2.67.0\n',
            'stderr': '',
        })()
        from app.services.msx_auth import check_az_cli_installed
        installed, error = check_az_cli_installed()
        assert installed is True
        assert error is None

    @patch('app.services.msx_auth.subprocess.run')
    def test_check_az_cli_installed_timeout(self, mock_run, app):
        """check_az_cli_installed() returns (False, 'timeout') when subprocess times out."""
        # Clear cache to ensure fresh check
        from app.services import msx_auth
        msx_auth._az_cli_installed_cache = {"installed": None, "last_error": None}
        
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="az --version", timeout=10)
        from app.services.msx_auth import check_az_cli_installed
        installed, error = check_az_cli_installed()
        assert installed is False
        assert error == "timeout"

    @patch('app.services.msx_auth.subprocess.run')
    def test_check_az_cli_installed_not_found(self, mock_run, app):
        """check_az_cli_installed() returns (False, 'not_found') when FileNotFoundError raised."""
        # Clear cache to ensure fresh check
        from app.services import msx_auth
        msx_auth._az_cli_installed_cache = {"installed": None, "last_error": None}
        
        mock_run.side_effect = FileNotFoundError("az not found")
        from app.services.msx_auth import check_az_cli_installed
        installed, error = check_az_cli_installed()
        assert installed is False
        assert error == "not_found"

    @patch('app.services.msx_auth.subprocess.run')
    def test_check_az_cli_installed_caches_positive_result(self, mock_run, app):
        """Once CLI is confirmed installed, the result is cached and subprocess not called again."""
        # Clear cache to start fresh
        from app.services import msx_auth
        msx_auth._az_cli_installed_cache = {"installed": None, "last_error": None}
        
        mock_run.return_value = type('Result', (), {
            'returncode': 0,
            'stdout': 'azure-cli 2.67.0\n',
            'stderr': '',
        })()
        from app.services.msx_auth import check_az_cli_installed
        
        # First call should invoke subprocess
        installed1, _ = check_az_cli_installed()
        assert installed1 is True
        assert mock_run.call_count == 1
        
        # Second call should use cache, not call subprocess again
        installed2, _ = check_az_cli_installed()
        assert installed2 is True
        assert mock_run.call_count == 1  # Still 1 - cached


class TestImportUiConsistency:
    """Tests that each import phase exposes the same progress UI."""

    def test_every_phase_has_a_progress_bar(self, client, app):
        """Accounts, milestones, and revenue each get their own bar."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        for phase in ('Accounts', 'Milestones', 'Revenue'):
            assert 'id="phase%sBarWrap"' % phase in html
            assert 'id="phase%sBar"' % phase in html

    def test_every_phase_has_status_summary_and_retry(self, client, app):
        """Each phase reports live status, a result summary, and its own retry."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        for phase in ('Accounts', 'Milestones', 'Revenue'):
            assert 'id="phase%sIcon"' % phase in html
            assert 'id="phase%sStatus"' % phase in html
            assert 'id="phase%sSummary"' % phase in html
            assert 'id="phase%sRetry"' % phase in html

    def test_accounts_import_no_broken_type_check(self, client, app):
        """JS should NOT check evt.type (old broken pattern)."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        # The old broken pattern was: evt.type === 'progress'
        assert "evt.type === 'progress'" not in html
        assert "evt.type === 'complete'" not in html


class TestWizardResumeLogic:
    """Tests for wizard step-skip logic when user has existing data."""

    def test_has_milestones_in_template_context(self, client, app):
        """Template context should include has_milestones flag."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        # With no milestones, the JS var should be false
        assert 'let milestonesImported = false;' in html

    def test_has_milestones_true_when_milestones_exist(self, client, app):
        """has_milestones should be true when milestone sync completed successfully."""
        with app.app_context():
            from app.models import db, Milestone, Customer, User, SyncStatus
            user = User.query.first()
            customer = Customer(name='Test Corp', tpid=999999)
            db.session.add(customer)
            db.session.flush()
            milestone = Milestone(
                customer_id=customer.id,
                url='https://example.com/milestone'
            )
            db.session.add(milestone)
            db.session.commit()

            # Mark milestone sync as completed (required by SyncStatus check)
            SyncStatus.mark_started('milestones')
            SyncStatus.mark_completed('milestones', success=True, items_synced=1)

        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'let milestonesImported = true;' in html

        # Clean up
        with app.app_context():
            from app.models import db, Milestone, Customer, SyncStatus
            db.session.query(Milestone).delete()
            db.session.query(Customer).filter_by(name='Test Corp').delete()
            db.session.query(SyncStatus).delete()
            db.session.commit()

    def test_has_customers_pre_sets_accounts_imported(self, client, app):
        """When accounts sync is complete, accountsImported should be pre-set to true."""
        with app.app_context():
            from app.models import db, Customer, User, SyncStatus
            user = User.query.first()
            customer = Customer(name='Existing Corp', tpid=888888)
            db.session.add(customer)
            db.session.commit()
            SyncStatus.mark_started('accounts')
            SyncStatus.mark_completed('accounts', success=True, items_synced=1)

        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'let accountsImported = true;' in html

        # Clean up
        with app.app_context():
            from app.models import db, Customer, SyncStatus
            db.session.query(Customer).filter_by(name='Existing Corp').delete()
            SyncStatus.reset('accounts')
            db.session.commit()

    def test_wizard_has_init_function(self, client, app):
        """Wizard should have async initWizard function for resume detection."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'async function initWizard()' in html
        assert 'initWizard();' in html

    def test_wizard_has_import_helpers(self, client, app):
        """Step 2 retains individual phase progress and resumable import state."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'function setPhaseState(' in html
        assert 'function setPhaseProgress(' in html
        assert 'function hydrateImportState()' in html
        # Per-step skipping is gone now that one button drives everything.
        assert 'function nextIncompleteStep()' not in html

    def test_wizard_shows_already_done_state_for_phases(self, client, app):
        """Finished phases show as done and only the missing ones re-run."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'hydrateImportState' in html
        assert 'Already imported' in html
        assert "Import What\\'s Missing" in html

    def test_fresh_user_starts_at_step_1(self, client, app):
        """With no existing data, wizard JS initializes accountsImported = false."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'let accountsImported = false;' in html
        assert 'let milestonesImported = false;' in html


class TestWizardImportStep:
    """Tests for the combined import step (accounts, milestones, revenue)."""

    def test_one_button_kicks_off_everything(self, client, app):
        """A single Start Import drives all three phases."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'id="onboardStartImport"' in html
        assert 'Start Import' in html
        # The old per-phase buttons are gone
        assert 'onboardImportAccounts' not in html
        assert 'onboardImportMilestones' not in html
        assert 'onboardImportRevenue' not in html

    def test_accounts_run_before_the_other_two(self, client, app):
        """Milestones and revenue have nothing to attach to until accounts land."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'const ready = accountsImported || await runAccounts();' in html

    def test_milestones_and_revenue_run_together_after_accounts(self, client, app):
        """Independent syncs overlap while the shared worker serializes their writes."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'if (!milestonesImported) imports.push(runMilestones());' in html
        assert 'if (!revenueImported) imports.push(runRevenue());' in html
        assert 'await Promise.all(imports);' in html
        assert 'then milestones and revenue together' in html

    def test_completed_imports_scroll_the_dialog_to_setup_selections(self, client, app):
        """The modal's own scroll area brings the post-sync selections into view."""
        html = client.get('/').data.decode('utf-8')
        assert 'class="sb-dialog-body" id="onboardingBody"' in html
        assert 'body.scrollTop = body.scrollHeight;' in html

    def test_wizard_has_no_skip_button(self, client, app):
        """Required setup choices cannot be bypassed with a Skip button."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'onboardSkipStepBtn' not in html

    def test_all_syncs_required_to_finish(self, client, app):
        """Everything runs automatically, so hold the user until it all lands."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'const allDone = accountsImported && milestonesImported && revenueImported;' in html
        assert 'if (!reviewMode && !(allDone || escapeHatch))' in html
        assert 'return getSeSetupBlockReason();' in html
        assert 'nextBtn.disabled = Boolean(reason);' in html

    def test_failure_lets_the_user_out_after_accounts(self, client, app):
        """Sync failures allow exit once the required SE choices are available."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'const escapeHatch = accountsImported && importFailed && !importRunning;' in html
        assert 'importFailed = true;' in html

    def test_inline_revenue_sync(self, client, app):
        """Revenue syncs from MSXI inline (no CSV, no link to another page)."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert "fetch('/api/revenue/sync'" in html
        # The CSV upload path is gone from the wizard
        assert 'revenueFileInput' not in html
        # Should NOT link to separate page
        assert 'onboardGoToRevenue' not in html
        # Should NOT have the old "You're all set" hero section
        assert "You're all set!" not in html

    def test_import_step_has_vpn_warning(self, client, app):
        """The import step warns about VPN once, not per phase."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'Requires VPN and usually takes a few minutes.' in html

    def test_revenue_tip_no_longer_asks_for_an_export(self, client, app):
        """The old tip walked through a CSV export; the sync needs no prep."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'id="revenueTipInstructionsStep4"' not in html
        assert 'export your revenue CSV' not in html

    def test_has_bucket_picker(self, client, app):
        """Specialty and bucket choices share one row without a separate save button."""
        from bs4 import BeautifulSoup

        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'onboardBucketPickerCard' in html
        assert 'onboardBucketList' in html
        assert 'renderOnboardBucketPicker' in html
        soup = BeautifulSoup(response.data, 'html.parser')
        card = soup.select_one('#onboardBucketPickerCard')
        columns = card.select_one(':scope > .sb-setup-se-columns')
        assert len(columns.find_all(recursive=False)) == 2
        assert columns.select_one(':scope > .sb-setup-se-specialty #onboardSeSpecialty')
        assert columns.select_one(':scope > .sb-setup-se-buckets #onboardBucketList')
        assert not card.select('button:not(#onboardReloadBuckets)')
        assert card.select_one(':scope > #onboardSeSetupStatus')

    @pytest.mark.parametrize('specialty', ['data', 'infra', 'apps_ai', 'other'])
    def test_se_specialty_starts_blank_on_first_run(self, client, app, specialty):
        """A default or previously saved value must not make a first-run choice for an SE."""
        from bs4 import BeautifulSoup
        from app.models import UserPreference, db

        with app.app_context():
            pref = UserPreference.query.first()
            original = (pref.user_role, pref.se_specialty, pref.first_run_modal_dismissed)
            pref.user_role = 'se'
            pref.se_specialty = specialty
            pref.first_run_modal_dismissed = False
            db.session.commit()
        try:
            soup = BeautifulSoup(client.get('/customers').data, 'html.parser')
            picker = soup.select_one('#onboardSeSpecialty')
            assert [option['value'] for option in picker.select('option')] == [
                '', 'data', 'infra', 'apps_ai', 'other',
            ]
            assert not picker.select('option[selected]')
            assert picker.select_one('option[value="other"]').get_text() == 'Other'
            assert soup.select_one('label[for="onboardSeSpecialty"]')
            assert soup.select_one('#onboardNextBtn').has_attr('disabled')
            action = soup.select_one('#onboardNextAction')
            assert action['aria-describedby'] == 'onboardNextReason'
            assert action.select_one('#onboardNextBtn')['aria-describedby'] == 'onboardNextReason'
            assert action.select_one('#onboardNextReason[role="tooltip"]')
        finally:
            with app.app_context():
                pref = UserPreference.query.first()
                pref.user_role, pref.se_specialty, pref.first_run_modal_dismissed = original
                db.session.commit()

    def test_se_review_preserves_saved_specialty(self, client, app):
        """Review shows an existing Other selection without changing any preferences."""
        from bs4 import BeautifulSoup
        from app.models import UserPreference, db

        with app.app_context():
            pref = UserPreference.query.first()
            original = (pref.user_role, pref.se_specialty, pref.first_run_modal_dismissed)
            pref.user_role = 'se'
            pref.se_specialty = 'other'
            pref.first_run_modal_dismissed = True
            db.session.commit()
        try:
            soup = BeautifulSoup(client.get('/').data, 'html.parser')
            assert soup.select_one('#onboardSeSpecialty option[selected]')['value'] == 'other'
        finally:
            with app.app_context():
                pref = UserPreference.query.first()
                pref.user_role, pref.se_specialty, pref.first_run_modal_dismissed = original
                db.session.commit()

    def test_revenue_copy_does_not_mention_exports(self, client, app):
        """New users should never learn about a CSV path that no longer exists."""
        from bs4 import BeautifulSoup

        response = client.get('/')
        soup = BeautifulSoup(response.data, 'html.parser')
        import_step = soup.select_one('#onboardingStep2').get_text()
        assert 'spreadsheet' not in import_step.lower()
        assert 'No export' not in import_step

    def test_no_optional_revenue_copy(self, client, app):
        """Revenue is no longer opt-out, so don't tell users they can skip it."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'You can finish once milestones are done' not in html
        assert 'nextBtn.disabled = !revenueImported' not in html

    def test_revenue_state_var_present(self, client, app):
        """revenueImported JS variable should be initialized from server data."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        # No revenue data in test DB, so should be false
        assert 'let revenueImported = false;' in html

    def test_dark_mode_toggle_has_cursor_pointer(self, client, app):
        """The native switch retains its label, focus treatment and clickable affordance."""
        from pathlib import Path
        from bs4 import BeautifulSoup

        response = client.get('/')
        soup = BeautifulSoup(response.data, 'html.parser')
        toggle = soup.select_one('#welcomeModal #onboardDarkModeToggle')
        assert toggle['role'] == 'switch'
        assert toggle['aria-label'] == 'Dark mode'
        assert soup.select_one('label[for="onboardDarkModeToggle"]') is not None
        css = Path('static/css/ui-onboarding.css').read_text(encoding='utf-8')
        shared_css = Path('static/css/ui-base.css').read_text(encoding='utf-8')
        assert 'cursor: pointer;' in shared_css
        assert '.sb-theme-switch-input:checked::before' in shared_css
        assert '.sb-setup :focus-visible' in css
