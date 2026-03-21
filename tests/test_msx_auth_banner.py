"""
Tests for MSX auth check banner on MSX-integrated pages.
Verifies that the auth banner partial is included on all pages with MSX integration,
and that the admin panel has the browser-based sign-in flow.
"""
import pytest
from bs4 import BeautifulSoup


class TestMsxAuthBannerPresence:
    """Tests that the MSX auth banner is present on all MSX-integrated pages."""

    def _dismiss_onboarding(self, app):
        """Dismiss onboarding modal so it doesn't interfere with page parsing."""
        from app.models import db, UserPreference
        pref = UserPreference.query.first()
        pref.first_run_modal_dismissed = True
        db.session.commit()

    def test_note_form_has_auth_banner(self, app, client, sample_data):
        """Test that the new call log form includes the MSX auth banner."""
        with app.app_context():
            self._dismiss_onboarding(app)
            from app.models import Customer
            customer = Customer.query.first()
            response = client.get(f'/note/new?customer_id={customer.id}')
            assert response.status_code == 200
            soup = BeautifulSoup(response.data, 'html.parser')
            banner = soup.find(id='msxAuthBanner')
            assert banner is not None, "MSX auth banner should be present on call log form"
            # Should have sign-in button
            sign_in_btn = soup.find(id='msxBannerSignInBtn')
            assert sign_in_btn is not None, "Should have sign-in button"

    def test_fill_my_day_has_auth_banner(self, app, client):
        """Test that Fill My Day page includes the MSX auth banner."""
        with app.app_context():
            self._dismiss_onboarding(app)
            response = client.get('/fill-my-day')
            assert response.status_code == 200
            soup = BeautifulSoup(response.data, 'html.parser')
            banner = soup.find(id='msxAuthBanner')
            assert banner is not None, "MSX auth banner should be present on Fill My Day"

    def test_milestone_tracker_has_auth_banner(self, app, client):
        """Test that Milestone Tracker page includes the MSX auth banner."""
        with app.app_context():
            self._dismiss_onboarding(app)
            response = client.get('/milestone-tracker')
            assert response.status_code == 200
            soup = BeautifulSoup(response.data, 'html.parser')
            banner = soup.find(id='msxAuthBanner')
            assert banner is not None, "MSX auth banner should be present on Milestone Tracker"

    def test_milestone_view_has_auth_banner(self, app, client, sample_data):
        """Test that Milestone View page includes the MSX auth banner."""
        with app.app_context():
            self._dismiss_onboarding(app)
            from app.models import db, Milestone, Customer, User
            user = User.query.first()
            customer = Customer.query.first()
            milestone = Milestone(
                title='Test Milestone',
                customer_id=customer.id,
                url='https://example.com'
            )
            db.session.add(milestone)
            db.session.commit()
            
            response = client.get(f'/milestone/{milestone.id}')
            assert response.status_code == 200
            soup = BeautifulSoup(response.data, 'html.parser')
            banner = soup.find(id='msxAuthBanner')
            assert banner is not None, "MSX auth banner should be present on Milestone View"


class TestMsxAuthBannerStructure:
    """Tests for the structure and content of the MSX auth banner."""

    def _dismiss_onboarding(self, app):
        from app.models import db, UserPreference
        pref = UserPreference.query.first()
        pref.first_run_modal_dismissed = True
        db.session.commit()

    def test_banner_has_all_states(self, app, client):
        """Test that the auth banner has all required state elements."""
        with app.app_context():
            self._dismiss_onboarding(app)
            response = client.get('/fill-my-day')
            assert response.status_code == 200
            soup = BeautifulSoup(response.data, 'html.parser')

            # Check all state divs exist
            assert soup.find(id='msxAuthChecking') is not None
            assert soup.find(id='msxAuthNeeded') is not None
            assert soup.find(id='msxAuthWaiting') is not None
            assert soup.find(id='msxAuthSuccess') is not None
            assert soup.find(id='msxAuthError') is not None
            assert soup.find(id='msxAuthNoCli') is not None

    def test_banner_has_sign_in_button(self, app, client):
        """Test that the auth banner has a Sign In to Azure button."""
        with app.app_context():
            self._dismiss_onboarding(app)
            response = client.get('/fill-my-day')
            soup = BeautifulSoup(response.data, 'html.parser')
            btn = soup.find(id='msxBannerSignInBtn')
            assert btn is not None
            assert 'Sign In to Azure' in btn.get_text()

    def test_banner_has_cancel_button(self, app, client):
        """Test that the auth banner has a Cancel button for the waiting state."""
        with app.app_context():
            self._dismiss_onboarding(app)
            response = client.get('/fill-my-day')
            soup = BeautifulSoup(response.data, 'html.parser')
            btn = soup.find(id='msxBannerCancelBtn')
            assert btn is not None
            assert 'Cancel' in btn.get_text()

    def test_banner_has_retry_button(self, app, client):
        """Test that the auth banner has a retry button for the error state."""
        with app.app_context():
            self._dismiss_onboarding(app)
            response = client.get('/fill-my-day')
            soup = BeautifulSoup(response.data, 'html.parser')
            btn = soup.find(id='msxBannerRetryBtn')
            assert btn is not None
            assert 'Try Again' in btn.get_text()

    def test_banner_starts_hidden(self, app, client):
        """Test that the auth banner starts hidden (d-none class)."""
        with app.app_context():
            self._dismiss_onboarding(app)
            response = client.get('/fill-my-day')
            soup = BeautifulSoup(response.data, 'html.parser')
            banner = soup.find(id='msxAuthBanner')
            assert 'd-none' in banner.get('class', [])

    def test_banner_has_js_endpoints(self, app, client):
        """Test that the banner JS references the correct MSX auth endpoints."""
        with app.app_context():
            self._dismiss_onboarding(app)
            response = client.get('/fill-my-day')
            html = response.data.decode('utf-8')
            assert '/api/msx/status' in html
            assert '/api/msx/az-status' in html
            assert '/api/msx/az-login/start' in html
            assert '/api/msx/az-login/complete' in html


class TestAdminPanelAuthFlow:
    """Tests that the admin panel has the browser-based sign-in flow."""

    def _make_admin(self, app):
        """Make test user admin and dismiss onboarding."""
        from app.models import db, User, UserPreference
        user = User.query.first()
        user.is_admin = True
        pref = UserPreference.query.first()
        pref.first_run_modal_dismissed = True
        db.session.commit()

    def test_admin_panel_has_sign_in_button(self, app, client):
        """Test that admin panel has the Sign In to Azure button."""
        with app.app_context():
            self._make_admin(app)
            response = client.get('/admin')
            assert response.status_code == 200
            soup = BeautifulSoup(response.data, 'html.parser')
            btn = soup.find(id='adminStartAuthBtn')
            assert btn is not None, "Admin panel should have Sign In button"
            assert 'Sign In to Azure' in btn.get_text()

    def test_admin_panel_no_manual_instructions(self, app, client):
        """Test that admin panel no longer has manual CLI instructions."""
        with app.app_context():
            self._make_admin(app)
            response = client.get('/admin')
            html = response.data.decode('utf-8')
            assert 'How to Authenticate' not in html, "Manual instructions should be removed"
            assert 'az login --tenant' not in html, "CLI command should not appear"

    def test_admin_panel_has_auth_states(self, app, client):
        """Test that admin panel has all auth flow states."""
        with app.app_context():
            self._make_admin(app)
            response = client.get('/admin')
            soup = BeautifulSoup(response.data, 'html.parser')

            assert soup.find(id='adminAuthInitial') is not None
            assert soup.find(id='adminAuthWaiting') is not None
            assert soup.find(id='adminAuthSuccess') is not None
            assert soup.find(id='adminAuthError') is not None
            assert soup.find(id='adminAuthNoCli') is not None

    def test_admin_panel_has_cancel_button(self, app, client):
        """Test that admin panel has a cancel button during auth waiting."""
        with app.app_context():
            self._make_admin(app)
            response = client.get('/admin')
            soup = BeautifulSoup(response.data, 'html.parser')
            btn = soup.find(id='adminAuthCancelBtn')
            assert btn is not None
            assert 'Cancel' in btn.get_text()

    def test_admin_panel_still_has_test_connection_button(self, app, client):
        """Test that admin panel still has the Test Connection button."""
        with app.app_context():
            self._make_admin(app)
            response = client.get('/admin')
            soup = BeautifulSoup(response.data, 'html.parser')
            test_btn = soup.find(id='msxTestBtn')
            assert test_btn is not None, "Test Connection button should still exist"

    def test_admin_panel_auth_js_endpoints(self, app, client):
        """Test that admin panel JS references the correct auth endpoints."""
        with app.app_context():
            self._make_admin(app)
            response = client.get('/admin')
            html = response.data.decode('utf-8')
            assert '/api/msx/az-login/start' in html
            assert '/api/msx/az-login/complete' in html
            assert '/api/msx/az-status' in html
