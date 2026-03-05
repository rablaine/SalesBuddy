"""
Tests for the Connect Export feature.

Tests cover:
- Connect export page rendering
- Export generation with date ranges
- Text and JSON export formatting
- Previous export listing and auto-date population
- Export download and deletion
- Edge cases (empty data, invalid dates, etc.)
"""
import json
from datetime import date, datetime, timedelta, timezone

import pytest


class TestConnectExportPage:
    """Tests for the Connect Export page route."""

    def test_page_loads(self, client):
        """Connect export page should load successfully."""
        response = client.get('/connect-export')
        assert response.status_code == 200
        assert b'Connect Export' in response.data

    def test_page_has_form(self, client):
        """Page should have export form with name, start date, and end date."""
        response = client.get('/connect-export')
        assert b'exportName' in response.data
        assert b'startDate' in response.data
        assert b'endDate' in response.data
        assert b'Generate' in response.data

    def test_default_end_date_is_today(self, client):
        """End date should default to today."""
        response = client.get('/connect-export')
        today = date.today().isoformat()
        assert today.encode() in response.data

    def test_back_to_admin_link(self, client):
        """Page should have a link back to admin panel."""
        response = client.get('/connect-export')
        assert b'Back to Admin' in response.data

    def test_no_previous_exports_initially(self, client):
        """Should not show previous exports table when none exist."""
        response = client.get('/connect-export')
        assert b'bi-clock-history' not in response.data

    def test_previous_exports_shown(self, client, app):
        """Should show previous exports when they exist."""
        with app.app_context():
            from app.models import ConnectExport, User, db
            user = User.query.first()
            export = ConnectExport(
                name='Test Export',
                start_date=date(2025, 1, 1),
                end_date=date(2025, 6, 30),
                call_log_count=5,
                customer_count=2,
                user_id=user.id,
            )
            db.session.add(export)
            db.session.commit()

        response = client.get('/connect-export')
        assert b'Previous Exports' in response.data
        assert b'Test Export' in response.data

    def test_auto_populate_start_date(self, client, app):
        """Start date should auto-populate to day after last export's end date."""
        with app.app_context():
            from app.models import ConnectExport, User, db
            user = User.query.first()
            export = ConnectExport(
                name='Previous Export',
                start_date=date(2025, 1, 1),
                end_date=date(2025, 6, 30),
                call_log_count=3,
                customer_count=1,
                user_id=user.id,
            )
            db.session.add(export)
            db.session.commit()

        response = client.get('/connect-export')
        # Should auto-populate start to 2025-07-01 (day after 2025-06-30)
        assert b'2025-07-01' in response.data


class TestGenerateExport:
    """Tests for the export generation API."""

    def test_requires_json(self, client):
        """Should reject non-JSON requests."""
        response = client.post('/api/connect-export/generate',
                               data='not json',
                               content_type='text/plain')
        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False
        assert 'JSON' in data['error']

    def test_requires_name(self, client):
        """Should require export name."""
        response = client.post('/api/connect-export/generate',
                               json={'name': '', 'start_date': '2025-01-01',
                                     'end_date': '2025-06-30'})
        assert response.status_code == 400
        data = response.get_json()
        assert 'name' in data['error'].lower()

    def test_requires_dates(self, client):
        """Should require both start and end dates."""
        response = client.post('/api/connect-export/generate',
                               json={'name': 'Test', 'start_date': '',
                                     'end_date': ''})
        assert response.status_code == 400
        data = response.get_json()
        assert 'date' in data['error'].lower()

    def test_rejects_invalid_dates(self, client):
        """Should reject malformed date strings."""
        response = client.post('/api/connect-export/generate',
                               json={'name': 'Test', 'start_date': 'not-a-date',
                                     'end_date': '2025-06-30'})
        assert response.status_code == 400
        data = response.get_json()
        assert 'Invalid date' in data['error']

    def test_rejects_reversed_dates(self, client):
        """Should reject start date after end date."""
        response = client.post('/api/connect-export/generate',
                               json={'name': 'Test', 'start_date': '2025-12-01',
                                     'end_date': '2025-01-01'})
        assert response.status_code == 400
        data = response.get_json()
        assert 'before' in data['error'].lower()

    def test_empty_export(self, client):
        """Should generate successfully even with no call logs in range."""
        response = client.post('/api/connect-export/generate',
                               json={'name': 'Empty Export',
                                     'start_date': '2020-01-01',
                                     'end_date': '2020-01-31'})
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['summary']['total_call_logs'] == 0
        assert data['summary']['unique_customers'] == 0

    def test_export_with_call_logs(self, client, app, sample_data):
        """Should include call logs in the export when they exist in range."""
        response = client.post('/api/connect-export/generate',
                               json={'name': 'H2 Connect',
                                     'start_date': '2020-01-01',
                                     'end_date': '2030-12-31'})
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['summary']['total_call_logs'] == 2
        assert data['summary']['unique_customers'] == 2

    def test_export_returns_text(self, client, app, sample_data):
        """Should return a text export string."""
        response = client.post('/api/connect-export/generate',
                               json={'name': 'Text Test',
                                     'start_date': '2020-01-01',
                                     'end_date': '2030-12-31'})
        data = response.get_json()
        assert 'text_export' in data
        assert 'Text Test' in data['text_export']
        assert 'Acme Corp' in data['text_export'] or 'Globex' in data['text_export']

    def test_export_returns_json_structure(self, client, app, sample_data):
        """Should return proper JSON export structure."""
        response = client.post('/api/connect-export/generate',
                               json={'name': 'JSON Test',
                                     'start_date': '2020-01-01',
                                     'end_date': '2030-12-31'})
        data = response.get_json()
        assert 'json_export' in data
        json_export = data['json_export']
        assert json_export['export_name'] == 'JSON Test'
        assert 'exported_at' in json_export
        assert 'summary' in json_export
        assert 'customers' in json_export

    def test_export_saves_record(self, client, app):
        """Should save a ConnectExport record to the database."""
        response = client.post('/api/connect-export/generate',
                               json={'name': 'Saved Export',
                                     'start_date': '2025-01-01',
                                     'end_date': '2025-06-30'})
        data = response.get_json()
        assert data['success'] is True
        assert 'export_id' in data

        with app.app_context():
            from app.models import ConnectExport
            export = ConnectExport.query.get(data['export_id'])
            assert export is not None
            assert export.name == 'Saved Export'
            assert export.start_date == date(2025, 1, 1)
            assert export.end_date == date(2025, 6, 30)

    def test_export_includes_topic_summary(self, client, app, sample_data):
        """Should include topic breakdown in summary."""
        response = client.post('/api/connect-export/generate',
                               json={'name': 'Topics Test',
                                     'start_date': '2020-01-01',
                                     'end_date': '2030-12-31'})
        data = response.get_json()
        topics = data['summary']['topics']
        assert len(topics) > 0
        # Check topic structure
        topic = topics[0]
        assert 'name' in topic
        assert 'call_count' in topic
        assert 'customer_count' in topic

    def test_export_groups_by_customer(self, client, app, sample_data):
        """Should group call logs by customer in the export."""
        response = client.post('/api/connect-export/generate',
                               json={'name': 'Customer Group Test',
                                     'start_date': '2020-01-01',
                                     'end_date': '2030-12-31'})
        data = response.get_json()
        customers = data['json_export']['customers']
        assert len(customers) == 2
        # Each customer should have call_logs list
        for cust in customers:
            assert 'call_logs' in cust
            assert len(cust['call_logs']) > 0
            assert 'name' in cust


class TestExportWithMilestones:
    """Tests for milestone revenue in exports."""

    def test_milestone_revenue_included(self, client, app):
        """Should include completed milestone revenue for customer."""
        with app.app_context():
            from app.models import (
                CallLog, Customer, Milestone, Seller, Territory,
                Topic, User, db,
            )
            user = User.query.first()
            territory = Territory(name='West', user_id=user.id)
            seller = Seller(name='Rep', alias='rep', seller_type='Growth',
                            user_id=user.id)
            db.session.add_all([territory, seller])
            db.session.flush()

            customer = Customer(name='Big Co', tpid=9001,
                                seller_id=seller.id,
                                territory_id=territory.id,
                                user_id=user.id)
            db.session.add(customer)
            db.session.flush()

            # Create completed milestone on my team
            milestone = Milestone(
                url='https://example.com/ms1',
                msx_status='Completed',
                dollar_value=500000.0,
                on_my_team=True,
                customer_id=customer.id,
                user_id=user.id,
                updated_at=datetime(2025, 3, 15, tzinfo=timezone.utc),
            )
            db.session.add(milestone)
            db.session.flush()

            # Create a call log in the period
            call = CallLog(
                customer_id=customer.id,
                call_date=datetime(2025, 3, 1, tzinfo=timezone.utc),
                content='Discussed deployment',
                user_id=user.id,
            )
            db.session.add(call)
            db.session.commit()

        response = client.post('/api/connect-export/generate',
                               json={'name': 'Revenue Test',
                                     'start_date': '2025-01-01',
                                     'end_date': '2025-06-30'})
        data = response.get_json()
        assert data['success'] is True
        assert data['summary']['total_milestone_revenue'] == 500000.0
        assert data['summary']['total_milestone_count'] == 1

        # Check per-customer
        customers = data['json_export']['customers']
        assert len(customers) == 1
        assert customers[0]['milestone_revenue'] == 500000.0

    def test_non_completed_milestones_excluded(self, client, app):
        """Should not count milestones that aren't Completed."""
        with app.app_context():
            from app.models import (
                CallLog, Customer, Milestone, Seller, Territory,
                User, db,
            )
            user = User.query.first()
            territory = Territory(name='East', user_id=user.id)
            seller = Seller(name='Rep2', alias='rep2', seller_type='Growth',
                            user_id=user.id)
            db.session.add_all([territory, seller])
            db.session.flush()

            customer = Customer(name='Mid Co', tpid=9002,
                                seller_id=seller.id,
                                territory_id=territory.id,
                                user_id=user.id)
            db.session.add(customer)
            db.session.flush()

            # On Track milestone (not completed)
            milestone = Milestone(
                url='https://example.com/ms2',
                msx_status='On Track',
                dollar_value=200000.0,
                on_my_team=True,
                customer_id=customer.id,
                user_id=user.id,
                updated_at=datetime(2025, 3, 15, tzinfo=timezone.utc),
            )
            db.session.add(milestone)
            db.session.flush()

            call = CallLog(
                customer_id=customer.id,
                call_date=datetime(2025, 3, 1, tzinfo=timezone.utc),
                content='Check-in',
                user_id=user.id,
            )
            db.session.add(call)
            db.session.commit()

        response = client.post('/api/connect-export/generate',
                               json={'name': 'No Revenue',
                                     'start_date': '2025-01-01',
                                     'end_date': '2025-06-30'})
        data = response.get_json()
        assert data['success'] is True
        assert data['summary']['total_milestone_revenue'] == 0

    def test_not_on_team_milestones_excluded(self, client, app):
        """Should not count milestones where on_my_team is False."""
        with app.app_context():
            from app.models import (
                CallLog, Customer, Milestone, Seller, Territory,
                User, db,
            )
            user = User.query.first()
            territory = Territory(name='South', user_id=user.id)
            seller = Seller(name='Rep3', alias='rep3', seller_type='Growth',
                            user_id=user.id)
            db.session.add_all([territory, seller])
            db.session.flush()

            customer = Customer(name='Other Co', tpid=9003,
                                seller_id=seller.id,
                                territory_id=territory.id,
                                user_id=user.id)
            db.session.add(customer)
            db.session.flush()

            # Completed but NOT on my team
            milestone = Milestone(
                url='https://example.com/ms3',
                msx_status='Completed',
                dollar_value=100000.0,
                on_my_team=False,
                customer_id=customer.id,
                user_id=user.id,
                updated_at=datetime(2025, 3, 15, tzinfo=timezone.utc),
            )
            db.session.add(milestone)
            db.session.flush()

            call = CallLog(
                customer_id=customer.id,
                call_date=datetime(2025, 3, 1, tzinfo=timezone.utc),
                content='Update',
                user_id=user.id,
            )
            db.session.add(call)
            db.session.commit()

        response = client.post('/api/connect-export/generate',
                               json={'name': 'Not On Team',
                                     'start_date': '2025-01-01',
                                     'end_date': '2025-06-30'})
        data = response.get_json()
        assert data['summary']['total_milestone_revenue'] == 0


class TestExportTextFormat:
    """Tests for the plain-text export format."""

    def test_text_includes_header(self, client, app, sample_data):
        """Text export should include the export name and date range."""
        response = client.post('/api/connect-export/generate',
                               json={'name': 'My Connect',
                                     'start_date': '2020-01-01',
                                     'end_date': '2030-12-31'})
        data = response.get_json()
        text = data['text_export']
        assert 'My Connect' in text
        assert '2020-01-01' in text
        assert '2030-12-31' in text

    def test_text_includes_customer_detail(self, client, app, sample_data):
        """Text export should include customer sections with call details."""
        response = client.post('/api/connect-export/generate',
                               json={'name': 'Detail Test',
                                     'start_date': '2020-01-01',
                                     'end_date': '2030-12-31'})
        data = response.get_json()
        text = data['text_export']
        assert 'CUSTOMER DETAIL' in text
        # Should include one of the customer names
        assert 'Acme Corp' in text or 'Globex Inc' in text

    def test_text_includes_revenue_when_present(self, client, app):
        """Text export should mention milestone revenue when there is some."""
        with app.app_context():
            from app.models import (
                CallLog, Customer, Milestone, Seller, Territory,
                User, db,
            )
            user = User.query.first()
            territory = Territory(name='Rev Territory', user_id=user.id)
            seller = Seller(name='Rev Seller', alias='revs',
                            seller_type='Growth', user_id=user.id)
            db.session.add_all([territory, seller])
            db.session.flush()

            customer = Customer(name='Revenue Co', tpid=8001,
                                seller_id=seller.id,
                                territory_id=territory.id,
                                user_id=user.id)
            db.session.add(customer)
            db.session.flush()

            milestone = Milestone(
                url='https://example.com/ms-rev',
                msx_status='Completed',
                dollar_value=1500000.0,
                on_my_team=True,
                customer_id=customer.id,
                user_id=user.id,
                updated_at=datetime(2025, 4, 1, tzinfo=timezone.utc),
            )
            db.session.add(milestone)
            db.session.flush()

            call = CallLog(
                customer_id=customer.id,
                call_date=datetime(2025, 4, 1, tzinfo=timezone.utc),
                content='Revenue discussion',
                user_id=user.id,
            )
            db.session.add(call)
            db.session.commit()

        response = client.post('/api/connect-export/generate',
                               json={'name': 'Revenue Text Test',
                                     'start_date': '2025-01-01',
                                     'end_date': '2025-12-31'})
        data = response.get_json()
        text = data['text_export']
        assert 'committed milestone revenue' in text
        assert '$1.5M' in text


class TestExportMarkdownFormat:
    """Tests for the Markdown export format."""

    def test_markdown_included_in_generate(self, client, app, sample_data):
        """Generate response should include markdown_export."""
        response = client.post('/api/connect-export/generate',
                               json={'name': 'MD Test',
                                     'start_date': '2020-01-01',
                                     'end_date': '2030-12-31'})
        data = response.get_json()
        assert 'markdown_export' in data
        assert data['markdown_export'] is not None

    def test_markdown_included_in_view(self, client, app):
        """View response should include markdown_export."""
        with app.app_context():
            from app.models import ConnectExport, User, db
            user = User.query.first()
            export = ConnectExport(
                name='MD View',
                start_date=date(2025, 1, 1),
                end_date=date(2025, 6, 30),
                call_log_count=0,
                customer_count=0,
                user_id=user.id,
            )
            db.session.add(export)
            db.session.commit()
            export_id = export.id

        response = client.get(f'/api/connect-export/{export_id}/view')
        data = response.get_json()
        assert 'markdown_export' in data

    def test_markdown_has_heading(self, client, app, sample_data):
        """Markdown should start with an H1 heading of the export name."""
        response = client.post('/api/connect-export/generate',
                               json={'name': 'FY25 Connect',
                                     'start_date': '2020-01-01',
                                     'end_date': '2030-12-31'})
        data = response.get_json()
        md = data['markdown_export']
        assert md.startswith('# FY25 Connect')

    def test_markdown_has_customer_headings(self, client, app, sample_data):
        """Markdown should have H3 headings for each customer."""
        response = client.post('/api/connect-export/generate',
                               json={'name': 'Customer MD',
                                     'start_date': '2020-01-01',
                                     'end_date': '2030-12-31'})
        data = response.get_json()
        md = data['markdown_export']
        assert '### Acme Corp' in md or '### Globex Inc' in md

    def test_markdown_has_topic_list(self, client, app, sample_data):
        """Markdown should have bullet-list topics."""
        response = client.post('/api/connect-export/generate',
                               json={'name': 'Topic MD',
                                     'start_date': '2020-01-01',
                                     'end_date': '2030-12-31'})
        data = response.get_json()
        md = data['markdown_export']
        assert '## Topics' in md
        assert '- **' in md  # bullet list with bold topic names

    def test_markdown_has_bold_dates(self, client, app, sample_data):
        """Markdown call log entries should have bold dates."""
        response = client.post('/api/connect-export/generate',
                               json={'name': 'Date MD',
                                     'start_date': '2020-01-01',
                                     'end_date': '2030-12-31'})
        data = response.get_json()
        md = data['markdown_export']
        # Should have **YYYY-MM-DD** pattern
        assert '**20' in md


class TestExportView:
    """Tests for the view export endpoint."""

    def test_view_existing_export(self, client, app):
        """Should return export data for a previously saved export."""
        with app.app_context():
            from app.models import ConnectExport, User, db
            user = User.query.first()
            export = ConnectExport(
                name='View Test',
                start_date=date(2025, 1, 1),
                end_date=date(2025, 6, 30),
                call_log_count=0,
                customer_count=0,
                user_id=user.id,
            )
            db.session.add(export)
            db.session.commit()
            export_id = export.id

        response = client.get(f'/api/connect-export/{export_id}/view')
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['name'] == 'View Test'
        assert 'text_export' in data
        assert 'json_export' in data
        assert 'summary' in data

    def test_view_nonexistent_export(self, client):
        """Should return 404 for nonexistent export."""
        response = client.get('/api/connect-export/99999/view')
        assert response.status_code == 404


class TestExportDelete:
    """Tests for the export deletion endpoint."""

    def test_delete_export(self, client, app):
        """Should delete an export record."""
        with app.app_context():
            from app.models import ConnectExport, User, db
            user = User.query.first()
            export = ConnectExport(
                name='Delete Test',
                start_date=date(2025, 1, 1),
                end_date=date(2025, 6, 30),
                call_log_count=0,
                customer_count=0,
                user_id=user.id,
            )
            db.session.add(export)
            db.session.commit()
            export_id = export.id

        response = client.delete(f'/api/connect-export/{export_id}')
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True

        with app.app_context():
            from app.models import ConnectExport
            assert ConnectExport.query.get(export_id) is None

    def test_delete_nonexistent_export(self, client):
        """Should return 404 for nonexistent export."""
        response = client.delete('/api/connect-export/99999')
        assert response.status_code == 404


class TestAdminPanelCard:
    """Tests for the Connect Export card in admin panel."""

    def test_admin_panel_has_connect_export_card(self, client):
        """Admin panel should have a Connect Export card."""
        response = client.get('/admin')
        assert response.status_code == 200
        assert b'Connect Export' in response.data
        assert b'connect-export' in response.data

    def test_admin_card_links_to_export_page(self, client):
        """Admin panel card should link to connect export page."""
        response = client.get('/admin')
        assert b'Go to Connect Export' in response.data


class TestHelperFunctions:
    """Tests for internal helper functions."""

    def test_strip_html(self):
        """Should strip HTML tags from content."""
        from app.routes.connect_export import _strip_html
        assert _strip_html('<p>Hello <b>World</b></p>') == 'Hello World'
        assert _strip_html('') == ''
        assert _strip_html(None) == ''

    def test_format_currency(self):
        """Should format currency values correctly."""
        from app.routes.connect_export import _format_currency
        assert _format_currency(1500000) == '$1.5M'
        assert _format_currency(250000) == '$250.0K'
        assert _format_currency(500) == '$500'
        assert _format_currency(0) == '$0'

    def test_format_currency_edge_cases(self):
        """Should handle boundary values for currency formatting."""
        from app.routes.connect_export import _format_currency
        assert _format_currency(1000000) == '$1.0M'
        assert _format_currency(1000) == '$1.0K'
        assert _format_currency(999) == '$999'

    def test_estimate_tokens(self):
        """Should estimate tokens based on character count."""
        from app.routes.connect_export import _estimate_tokens, CHARS_PER_TOKEN
        text = 'a' * 400
        assert _estimate_tokens(text) == 400 // CHARS_PER_TOKEN

    def test_build_summary_header(self):
        """Should build a compact stats header from export data."""
        from app.routes.connect_export import _build_summary_header
        data = {
            'summary': {
                'start_date': '2025-01-01',
                'end_date': '2025-06-30',
                'total_call_logs': 10,
                'unique_customers': 3,
                'unique_topics': 5,
                'total_milestone_revenue': 500000.0,
                'total_milestone_count': 2,
                'topics': [
                    {'name': 'Azure AI', 'call_count': 5, 'customer_count': 2},
                    {'name': 'Cosmos DB', 'call_count': 3, 'customer_count': 1},
                ],
            },
            'customers': [],
        }
        header = _build_summary_header(data)
        assert '2025-01-01' in header
        assert '2025-06-30' in header
        assert '10' in header
        assert '3' in header
        assert '$500.0K' in header
        assert 'Azure AI' in header

    def test_build_customer_text_block(self):
        """Should build a text block for a single customer."""
        from app.routes.connect_export import _build_customer_text_block
        cust = {
            'name': 'Acme Corp',
            'seller': 'Alice',
            'territory': 'West',
            'topics': ['Azure AI', 'Cosmos DB'],
            'milestone_revenue': 100000.0,
            'milestone_count': 1,
            'call_logs': [
                {
                    'date': '2025-03-01',
                    'topics': ['Azure AI'],
                    'content_text': 'Discussed migration.',
                },
            ],
        }
        block = _build_customer_text_block(cust)
        assert 'Acme Corp' in block
        assert 'Alice' in block
        assert 'West' in block
        assert 'Azure AI' in block
        assert 'Discussed migration.' in block
        assert '$100.0K' in block

    def test_chunk_customers_single_chunk(self):
        """Small dataset should produce a single chunk."""
        from app.routes.connect_export import _chunk_customers
        data = {
            'summary': {
                'start_date': '2025-01-01',
                'end_date': '2025-06-30',
                'total_call_logs': 2,
                'unique_customers': 1,
                'unique_topics': 1,
                'total_milestone_revenue': 0,
                'total_milestone_count': 0,
                'topics': [],
            },
            'customers': [
                {
                    'name': 'Small Co',
                    'seller': None,
                    'territory': None,
                    'topics': ['Azure'],
                    'milestone_revenue': 0,
                    'milestone_count': 0,
                    'call_logs': [
                        {'date': '2025-03-01', 'topics': [], 'content_text': 'Short call.'},
                    ],
                },
            ],
        }
        chunks = _chunk_customers(data)
        assert len(chunks) == 1
        assert len(chunks[0]) == 1

    def test_chunk_customers_multiple_chunks(self):
        """Large dataset should be split into multiple chunks."""
        from app.routes.connect_export import _chunk_customers
        # Create customers with enough text to force chunking at a low threshold
        big_content = 'x' * 50000  # 50K chars ~ 12.5K tokens
        customers = []
        for i in range(10):
            customers.append({
                'name': f'Customer {i}',
                'seller': None,
                'territory': None,
                'topics': [],
                'milestone_revenue': 0,
                'milestone_count': 0,
                'call_logs': [
                    {'date': '2025-03-01', 'topics': [], 'content_text': big_content},
                ],
            })
        data = {
            'summary': {
                'start_date': '2025-01-01',
                'end_date': '2025-06-30',
                'total_call_logs': 10,
                'unique_customers': 10,
                'unique_topics': 0,
                'total_milestone_revenue': 0,
                'total_milestone_count': 0,
                'topics': [],
            },
            'customers': customers,
        }
        # With 10 customers at ~12.5K tokens each = ~125K total,
        # should be multiple chunks at 80K target
        chunks = _chunk_customers(data)
        assert len(chunks) > 1
        # All customers should still be present
        total = sum(len(c) for c in chunks)
        assert total == 10


class TestAiSummaryEndpoint:
    """Tests for the AI summary generation endpoint."""

    def test_ai_summary_requires_ai_enabled(self, client, app, monkeypatch):
        """Should return 400 when AI is not configured."""
        # Explicitly disable AI
        monkeypatch.setattr('app.routes.connect_export.is_ai_enabled', lambda: False)
        with app.app_context():
            from app.models import ConnectExport, User, db
            user = User.query.first()
            export = ConnectExport(
                name='AI Test',
                start_date=date(2025, 1, 1),
                end_date=date(2025, 6, 30),
                call_log_count=0,
                customer_count=0,
                user_id=user.id,
            )
            db.session.add(export)
            db.session.commit()
            export_id = export.id

        response = client.post(f'/api/connect-export/{export_id}/ai-summary',
                               content_type='application/json')
        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False
        assert 'not configured' in data['error']

    def test_ai_summary_nonexistent_export(self, client, app, monkeypatch):
        """Should return 404 for nonexistent export."""
        monkeypatch.setattr('app.routes.connect_export.is_ai_enabled', lambda: True)
        response = client.post('/api/connect-export/99999/ai-summary',
                               content_type='application/json')
        assert response.status_code == 404

    def test_ai_summary_no_data(self, client, app, monkeypatch):
        """Should return 400 when export has no call log data."""
        monkeypatch.setattr('app.routes.connect_export.is_ai_enabled', lambda: True)
        with app.app_context():
            from app.models import ConnectExport, User, db
            user = User.query.first()
            export = ConnectExport(
                name='Empty AI Test',
                start_date=date(2020, 1, 1),
                end_date=date(2020, 1, 31),
                call_log_count=0,
                customer_count=0,
                user_id=user.id,
            )
            db.session.add(export)
            db.session.commit()
            export_id = export.id

        response = client.post(f'/api/connect-export/{export_id}/ai-summary',
                               content_type='application/json')
        assert response.status_code == 400
        data = response.get_json()
        assert 'No call log data' in data['error']

    def test_ai_summary_success(self, client, app, sample_data, monkeypatch):
        """Should generate and cache AI summary successfully."""
        monkeypatch.setattr('app.routes.connect_export.is_ai_enabled', lambda: True)

        # Mock the generate_ai_summary function
        mock_summary = (
            "## What results did you deliver?\n"
            "- Engaged 2 customers on Azure VM and Storage\n\n"
            "## Reflect on setbacks\n"
            "- Could have deepened engagement with Globex\n\n"
            "## Goals for upcoming period\n"
            "- Expand Azure AI adoption across territory"
        )
        mock_usage = {
            'model': 'gpt-4o-mini',
            'prompt_tokens': 500,
            'completion_tokens': 200,
            'total_tokens': 700,
        }
        monkeypatch.setattr(
            'app.routes.connect_export.generate_ai_summary',
            lambda data, text: (mock_summary, mock_usage),
        )

        # First generate an export
        with app.app_context():
            from app.models import ConnectExport, User, db
            user = User.query.first()
            export = ConnectExport(
                name='AI Success Test',
                start_date=date(2020, 1, 1),
                end_date=date(2030, 12, 31),
                call_log_count=2,
                customer_count=2,
                user_id=user.id,
            )
            db.session.add(export)
            db.session.commit()
            export_id = export.id

        response = client.post(f'/api/connect-export/{export_id}/ai-summary',
                               content_type='application/json')
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert 'ai_summary' in data
        assert 'What results did you deliver' in data['ai_summary']
        assert data['usage']['total_tokens'] == 700
        assert data['chunks_used'] == 1

        # Verify it was cached in the database
        with app.app_context():
            from app.models import ConnectExport
            export = ConnectExport.query.get(export_id)
            assert export.ai_summary is not None
            assert 'What results did you deliver' in export.ai_summary

    def test_ai_summary_cached_in_view(self, client, app, monkeypatch):
        """View endpoint should return cached AI summary."""
        with app.app_context():
            from app.models import ConnectExport, User, db
            user = User.query.first()
            export = ConnectExport(
                name='Cached AI Test',
                start_date=date(2025, 1, 1),
                end_date=date(2025, 6, 30),
                call_log_count=0,
                customer_count=0,
                ai_summary='## Cached Summary\n- This was cached',
                user_id=user.id,
            )
            db.session.add(export)
            db.session.commit()
            export_id = export.id

        response = client.get(f'/api/connect-export/{export_id}/view')
        assert response.status_code == 200
        data = response.get_json()
        assert data['ai_summary'] == '## Cached Summary\n- This was cached'

    def test_ai_summary_handles_error(self, client, app, sample_data, monkeypatch):
        """Should return 500 and log failure when AI call fails."""
        monkeypatch.setattr('app.routes.connect_export.is_ai_enabled', lambda: True)
        monkeypatch.setattr(
            'app.routes.connect_export.generate_ai_summary',
            lambda data, text: (_ for _ in ()).throw(
                RuntimeError('API connection failed')
            ),
        )

        with app.app_context():
            from app.models import ConnectExport, User, db
            user = User.query.first()
            export = ConnectExport(
                name='AI Error Test',
                start_date=date(2020, 1, 1),
                end_date=date(2030, 12, 31),
                call_log_count=2,
                customer_count=2,
                user_id=user.id,
            )
            db.session.add(export)
            db.session.commit()
            export_id = export.id

        response = client.post(f'/api/connect-export/{export_id}/ai-summary',
                               content_type='application/json')
        assert response.status_code == 500
        data = response.get_json()
        assert data['success'] is False
        assert 'API connection failed' in data['error']

        # Verify error was logged
        with app.app_context():
            from app.models import AIQueryLog
            log = AIQueryLog.query.filter_by(success=False).order_by(
                AIQueryLog.id.desc()
            ).first()
            assert log is not None
            assert 'API connection failed' in log.error_message

    def test_ai_summary_logs_success(self, client, app, sample_data, monkeypatch):
        """Should log successful AI query with token usage."""
        monkeypatch.setattr('app.routes.connect_export.is_ai_enabled', lambda: True)
        monkeypatch.setattr(
            'app.routes.connect_export.generate_ai_summary',
            lambda data, text: ('## Summary\n- Done', {
                'model': 'gpt-4o-mini',
                'prompt_tokens': 1000,
                'completion_tokens': 300,
                'total_tokens': 1300,
            }),
        )

        with app.app_context():
            from app.models import ConnectExport, User, db
            user = User.query.first()
            export = ConnectExport(
                name='AI Log Test',
                start_date=date(2020, 1, 1),
                end_date=date(2030, 12, 31),
                call_log_count=2,
                customer_count=2,
                user_id=user.id,
            )
            db.session.add(export)
            db.session.commit()
            export_id = export.id

        response = client.post(f'/api/connect-export/{export_id}/ai-summary',
                               content_type='application/json')
        assert response.status_code == 200

        with app.app_context():
            from app.models import AIQueryLog
            log = AIQueryLog.query.filter_by(success=True).order_by(
                AIQueryLog.id.desc()
            ).first()
            assert log is not None
            assert 'Connect AI summary' in log.request_text
            assert log.model == 'gpt-4o-mini'
            assert log.total_tokens == 1300


class TestAiEnabledInTemplate:
    """Tests for AI button visibility in the template."""

    def test_ai_button_hidden_when_disabled(self, client, monkeypatch):
        """AI Generate button should not appear when AI is not configured."""
        monkeypatch.setattr('app.routes.connect_export.is_ai_enabled', lambda: False)
        response = client.get('/connect-export')
        assert response.status_code == 200
        # The button element is inside {% if ai_enabled %} so it won't render,
        # but the AI Summary card is always present (hidden). Check for button markup.
        assert b'<button class="btn btn-primary btn-sm" id="aiGenerateBtn"' not in response.data

    def test_ai_button_shown_when_enabled(self, client, monkeypatch):
        """AI Generate button should appear when AI is configured."""
        monkeypatch.setattr('app.routes.connect_export.is_ai_enabled', lambda: True)
        response = client.get('/connect-export')
        assert response.status_code == 200
        assert b'AI Generate' in response.data
        assert b'aiGenerateBtn' in response.data
