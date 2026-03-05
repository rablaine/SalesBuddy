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
        # The wizard modal HTML should be rendered
        assert b'id="welcomeModal"' in response.data
        assert b'onboardingStep1' in response.data
        assert b'onboardingStep2' in response.data
        assert b'onboardingStep3' in response.data
        assert b'onboardingStep4' in response.data
        assert b'onboardingStep5' in response.data

    def test_onboarding_modal_hidden_when_dismissed(self, client, app):
        """Onboarding wizard should not appear when first_run_modal_dismissed is True."""
        with app.app_context():
            from app.models import db, UserPreference
            pref = UserPreference.query.first()
            pref.first_run_modal_dismissed = True
            db.session.commit()

        response = client.get('/')
        assert response.status_code == 200
        # The wizard modal HTML should NOT be rendered
        assert b'id="welcomeModal"' not in response.data
        assert b'onboardingStep1' not in response.data

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
        """Verify all wizard steps have proper structure."""
        response = client.get('/')
        html = response.data.decode('utf-8')

        # Step 1: Welcome + Dark Mode
        assert 'Choose Your Theme' in html
        assert 'onboardDarkModeToggle' in html

        # Step 2: Authentication (az login flow)
        assert 'Connect to MSX' in html
        assert 'onboardStartAuth' in html
        assert 'Sign In to Azure' in html

        # Step 3: Import Accounts (with progress bar)
        assert 'Import Your Accounts' in html
        assert 'onboardImportAccounts' in html
        assert 'importAccountsProgressBar' in html

        # Step 4: Import Milestones (with progress bar)
        assert 'Import Milestones' in html
        assert 'onboardImportMilestones' in html
        assert 'importMilestonesProgressBar' in html

        # Step 5: Revenue & Finish (inline upload)
        assert 'One More Thing: Revenue Data' in html
        assert 'onboardImportRevenue' in html
        assert 'revenueFileInput' in html
        assert 'importRevenueProgress' in html

    def test_onboarding_has_skip_button(self, client, app):
        """Verify the skip button is present."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'onboardSkipBtn' in html
        assert "Skip setup" in html

    def test_onboarding_has_navigation_buttons(self, client, app):
        """Verify Next/Back buttons exist."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'onboardNextBtn' in html
        assert 'onboardBackBtn' in html

    def test_onboarding_progress_bar(self, client, app):
        """Verify progress bar and step badge exist."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'onboardingProgress' in html
        assert 'onboardingStepBadge' in html
        assert 'Step 1 of 5' in html


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
        """After dismissing, the wizard should not render on subsequent pages."""
        # Dismiss
        response = client.post('/api/preferences/dismiss-welcome-modal',
                               content_type='application/json')
        assert response.status_code == 200

        # Load page
        response = client.get('/')
        assert response.status_code == 200
        assert b'id="welcomeModal"' not in response.data

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
        assert b'NoteHelper' in response.data


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
        """After resetting, the wizard should render again."""
        # Dismiss
        client.post('/api/preferences/dismiss-welcome-modal',
                     content_type='application/json')
        # Verify dismissed
        response = client.get('/')
        assert b'id="welcomeModal"' not in response.data

        # Reset
        client.post('/api/preferences/reset-onboarding',
                     content_type='application/json')
        # Verify wizard is back
        response = client.get('/')
        assert b'id="welcomeModal"' in response.data

    def test_rerun_button_shown_when_dismissed_no_customers(self, client, app):
        """Re-run button should appear when wizard dismissed and no customers exist."""
        with app.app_context():
            from app.models import db, UserPreference
            pref = UserPreference.query.first()
            pref.first_run_modal_dismissed = True
            db.session.commit()

        response = client.get('/')
        html = response.data.decode('utf-8')
        # The actual button element (not just JS reference)
        assert 'id="rerunSetupBtn"' in html
        assert 'Setup Wizard' in html

        # Clean up
        with app.app_context():
            from app.models import db, UserPreference
            pref = UserPreference.query.first()
            pref.first_run_modal_dismissed = False
            db.session.commit()

    def test_rerun_button_hidden_when_wizard_not_dismissed(self, client, app):
        """Re-run button should NOT appear when wizard hasn't been dismissed yet."""
        # When first_run_modal_dismissed is False, the wizard itself shows
        # and the re-run button HTML element should not be rendered
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'id="welcomeModal"' in html
        # The button element itself (not JS getElementById reference)
        assert 'id="rerunSetupBtn"' not in html


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
        assert 'onboardStartAuth' in html

    def test_step2_has_auth_states(self, client, app):
        """Step 2 should have all auth state divs."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'id="authInitial"' in html
        assert 'id="authWaiting"' in html
        assert 'id="authSuccess"' in html
        assert 'id="authError"' in html
        assert 'id="authNoCli"' in html
        assert 'id="authAlready"' in html

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
        assert 'id="authRetry"' in html
        assert 'id="authRetryNoCli"' in html
        assert 'id="authCancelBtn"' in html

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
        assert 'authentication will fail' in html

    def test_step2_has_20s_timeout(self, client, app):
        """Step 2 JS should use a 20-second auth timeout."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'AUTH_POLL_TIMEOUT_MS = 20000' in html


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
        mock_installed.return_value = True
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
        mock_installed.return_value = True
        mock_logged_in.return_value = (True, "user@microsoft.com", "72f988bf-86f1-41af-91ab-2d7cd011db47")
        from app.services.msx_auth import get_az_cli_status
        result = get_az_cli_status()
        assert result['logged_in'] is True
        assert result['wrong_tenant'] is False

    @patch('app.services.msx_auth.subprocess.run')
    def test_check_az_cli_installed_nonzero_with_output(self, mock_run, app):
        """check_az_cli_installed() returns True when az --version exits non-zero but outputs version info."""
        mock_run.return_value = type('Result', (), {
            'returncode': 2,
            'stdout': 'azure-cli                         2.67.0\ncore                              2.67.0\n',
            'stderr': 'WARNING: Extension update available\n',
        })()
        from app.services.msx_auth import check_az_cli_installed
        assert check_az_cli_installed() is True

    @patch('app.services.msx_auth.subprocess.run')
    def test_check_az_cli_installed_nonzero_no_output(self, mock_run, app):
        """check_az_cli_installed() returns False when az --version exits non-zero with no version output."""
        mock_run.return_value = type('Result', (), {
            'returncode': 1,
            'stdout': '',
            'stderr': 'az: command not found\n',
        })()
        from app.services.msx_auth import check_az_cli_installed
        assert check_az_cli_installed() is False

    @patch('app.services.msx_auth.subprocess.run')
    def test_check_az_cli_installed_success(self, mock_run, app):
        """check_az_cli_installed() returns True on returncode 0."""
        mock_run.return_value = type('Result', (), {
            'returncode': 0,
            'stdout': 'azure-cli 2.67.0\n',
            'stderr': '',
        })()
        from app.services.msx_auth import check_az_cli_installed
        assert check_az_cli_installed() is True


class TestImportUiConsistency:
    """Tests that wizard import steps have proper progress UI matching admin panel."""

    def test_accounts_import_has_progress_bar(self, client, app):
        """Step 3 should have a progress bar for account import."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'id="importAccountsProgressBar"' in html
        assert 'id="importAccountsProgressPercent"' in html
        assert 'id="importAccountsStatusText"' in html

    def test_milestones_import_has_progress_bar(self, client, app):
        """Step 4 should have a progress bar for milestone sync."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'id="importMilestonesProgressBar"' in html
        assert 'id="importMilestonesProgressPercent"' in html
        assert 'id="importMilestonesStatusText"' in html

    def test_accounts_import_has_all_states(self, client, app):
        """Step 3 should have Initial, Progress, Success, Error states."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'id="importAccountsInitial"' in html
        assert 'id="importAccountsProgress"' in html
        assert 'id="importAccountsSuccess"' in html
        assert 'id="importAccountsError"' in html

    def test_milestones_import_has_all_states(self, client, app):
        """Step 4 should have Initial, Progress, Success, Error states."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'id="importMilestonesInitial"' in html
        assert 'id="importMilestonesProgress"' in html
        assert 'id="importMilestonesSuccess"' in html
        assert 'id="importMilestonesError"' in html

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

    def test_wizard_has_next_incomplete_step_helper(self, client, app):
        """Wizard should have nextIncompleteStep helper for smart step skipping."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'function nextIncompleteStep()' in html

    def test_wizard_shows_already_done_state_for_accounts(self, client, app):
        """Step 3 should show 'already imported' state when accounts exist."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        # The showImportAlreadyDone function should exist
        assert 'showImportAlreadyDone' in html
        assert 'Accounts already imported' in html

    def test_wizard_shows_already_done_state_for_milestones(self, client, app):
        """Step 4 should show 'already synced' state when milestones exist."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'Milestones already synced' in html

    def test_fresh_user_starts_at_step_1(self, client, app):
        """With no existing data, wizard JS initializes accountsImported = false."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'let accountsImported = false;' in html
        assert 'let milestonesImported = false;' in html


class TestWizardOptionalSteps:
    """Tests for optional step skip functionality."""

    def test_step4_has_optional_hint(self, client, app):
        """Step 4 should tell user milestones are optional."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'Optional' in html
        assert 'Milestone Tracker' in html

    def test_step4_has_skip_button_in_footer(self, client, app):
        """Footer should have a Skip button for step 4."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'id="onboardSkipStepBtn"' in html

    def test_step4_skip_button_visibility_logic(self, client, app):
        """Skip button should only show on step 4 when milestones not imported."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'currentStep === 4 && !milestonesImported' in html

    def test_milestones_required_for_next_button(self, client, app):
        """Step 4 Next button should be gated on milestonesImported."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert "currentStep === 4 && !milestonesImported" in html

    def test_skip_button_disabled_after_import(self, client, app):
        """Skip button should hide when milestones are imported."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        # The logic hides skip when milestones imported (else branch adds d-none)
        assert "skipStepBtn.classList.add('d-none')" in html

    def test_footer_skip_btn_updates_text_after_accounts(self, client, app):
        """Footer skip button text should change after accounts are imported."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'Done' in html
        assert 'skip remaining steps' in html

    def test_step5_inline_revenue_upload(self, client, app):
        """Step 5 should have inline revenue CSV upload (not a link to another page)."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        # Should have inline upload button (btn-primary btn-lg)
        assert 'btn btn-primary btn-lg' in html
        assert 'Import Revenue CSV' in html
        # Hidden file input for CSV
        assert 'revenueFileInput' in html
        assert 'accept=".csv"' in html
        # Progress, success, and error states
        assert 'importRevenueProgress' in html
        assert 'importRevenueSuccess' in html
        assert 'importRevenueError' in html
        # Should NOT link to separate page
        assert 'onboardGoToRevenue' not in html
        # Should NOT have the old "You're all set" hero section
        assert "You're all set!" not in html
        # Optional hint text
        assert 'Revenue Analyzer' in html
        # Card should have a primary border to stand out
        assert 'border-primary' in html

    def test_step4_has_vpn_warning(self, client, app):
        """Step 4 should have the VPN required warning."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        # There should be VPN warnings (step 3 and step 4)
        assert html.count('Requires VPN.') >= 2

    def test_step4_has_revenue_tip(self, client, app):
        """Step 4 should have the revenue export tip."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'id="revenueTipInstructionsStep4"' in html

    def test_step5_skip_button_visible_in_js(self, client, app):
        """Skip step button should appear on step 5 when revenue not imported (JS logic)."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        # The JS logic should show skipStepBtn on step 5
        assert 'currentStep === 5 && !revenueImported' in html

    def test_step5_finish_gated_on_revenue(self, client, app):
        """Finish button should be disabled until revenue is imported (JS logic)."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        # updateNextButton should gate step 5 on revenueImported
        assert 'currentStep === 5 && !revenueImported' in html
        assert 'nextBtn.disabled = true' in html

    def test_revenue_state_var_present(self, client, app):
        """revenueImported JS variable should be initialized from server data."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        # No revenue data in test DB, so should be false
        assert 'let revenueImported = false;' in html

    def test_dark_mode_toggle_has_cursor_pointer(self, client, app):
        """Dark mode toggle in step 1 should have cursor: pointer to look clickable."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        assert 'cursor: pointer;' in html
        # Should also have the toggle-enhancing CSS
        assert '#onboardDarkModeToggle:not(:checked)' in html

    def test_step5_skip_dismisses_on_last_step(self, client, app):
        """Skip step on step 5 should dismiss the wizard (JS calls dismissOnboarding)."""
        response = client.get('/')
        html = response.data.decode('utf-8')
        # The skipStepBtn handler should dismiss when on the last step
        assert 'currentStep >= totalSteps' in html
