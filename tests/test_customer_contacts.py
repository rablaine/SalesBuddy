"""Tests for customer contacts feature."""
import pytest
from app.models import db, Customer, CustomerContact, Partner, PartnerContact


class TestCustomerContactModel:
    """Tests for the CustomerContact model."""

    def test_create_contact(self, app, sample_data):
        """Test creating a customer contact."""
        with app.app_context():
            contact = CustomerContact(
                customer_id=sample_data['customer1_id'],
                name='Jane Doe',
                email='jane@acme.com',
                title='CTO'
            )
            db.session.add(contact)
            db.session.commit()

            assert contact.id is not None
            assert contact.name == 'Jane Doe'
            assert contact.email == 'jane@acme.com'
            assert contact.title == 'CTO'
            assert contact.customer_id == sample_data['customer1_id']

    def test_customer_contacts_relationship(self, app, sample_data):
        """Test that contacts are accessible via customer.contacts."""
        with app.app_context():
            c1 = CustomerContact(customer_id=sample_data['customer1_id'], name='Alice')
            c2 = CustomerContact(customer_id=sample_data['customer1_id'], name='Bob')
            db.session.add_all([c1, c2])
            db.session.commit()

            customer = db.session.get(Customer, sample_data['customer1_id'])
            assert len(customer.contacts) == 2
            names = [c.name for c in customer.contacts]
            assert 'Alice' in names
            assert 'Bob' in names

    def test_contact_nullable_fields(self, app, sample_data):
        """Test that email and title are nullable."""
        with app.app_context():
            contact = CustomerContact(
                customer_id=sample_data['customer1_id'],
                name='Minimal Contact'
            )
            db.session.add(contact)
            db.session.commit()

            assert contact.email is None
            assert contact.title is None


class TestContactsAPI:
    """Tests for the contacts API endpoints."""

    def test_get_contacts_empty(self, client, app, sample_data):
        """Test GET contacts returns empty list for customer with no contacts."""
        resp = client.get(f'/api/customer/{sample_data["customer1_id"]}/contacts')
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_get_contacts(self, client, app, sample_data):
        """Test GET contacts returns existing contacts."""
        with app.app_context():
            c = CustomerContact(
                customer_id=sample_data['customer1_id'],
                name='Test Person',
                email='test@example.com',
                title='VP Engineering'
            )
            db.session.add(c)
            db.session.commit()

        resp = client.get(f'/api/customer/{sample_data["customer1_id"]}/contacts')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]['name'] == 'Test Person'
        assert data[0]['email'] == 'test@example.com'
        assert data[0]['title'] == 'VP Engineering'

    def test_get_contacts_404(self, client):
        """Test GET contacts for non-existent customer returns 404."""
        resp = client.get('/api/customer/99999/contacts')
        assert resp.status_code == 404

    def test_create_contact(self, client, app, sample_data):
        """Test POST to create a new contact."""
        resp = client.post(
            f'/api/customer/{sample_data["customer1_id"]}/contacts',
            json={'name': 'New Person', 'email': 'new@test.com', 'title': 'Director'}
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['name'] == 'New Person'
        assert data['email'] == 'new@test.com'
        assert data['title'] == 'Director'
        assert 'id' in data

    def test_create_contact_name_required(self, client, sample_data):
        """Test POST without name returns 400."""
        resp = client.post(
            f'/api/customer/{sample_data["customer1_id"]}/contacts',
            json={'email': 'no-name@test.com'}
        )
        assert resp.status_code == 400
        assert 'Name is required' in resp.get_json()['error']

    def test_create_contact_empty_name(self, client, sample_data):
        """Test POST with blank name returns 400."""
        resp = client.post(
            f'/api/customer/{sample_data["customer1_id"]}/contacts',
            json={'name': '   ', 'email': 'blank@test.com'}
        )
        assert resp.status_code == 400

    def test_create_contact_minimal(self, client, sample_data):
        """Test POST with only name succeeds."""
        resp = client.post(
            f'/api/customer/{sample_data["customer1_id"]}/contacts',
            json={'name': 'Just A Name'}
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['name'] == 'Just A Name'
        assert data['email'] == ''
        assert data['title'] == ''

    def test_update_contact(self, client, app, sample_data):
        """Test PUT to update an existing contact."""
        with app.app_context():
            c = CustomerContact(
                customer_id=sample_data['customer1_id'],
                name='Old Name',
                email='old@test.com'
            )
            db.session.add(c)
            db.session.commit()
            contact_id = c.id

        resp = client.put(
            f'/api/customer/contact/{contact_id}',
            json={'name': 'New Name', 'email': 'new@test.com', 'title': 'CEO'}
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['name'] == 'New Name'
        assert data['email'] == 'new@test.com'
        assert data['title'] == 'CEO'

    def test_update_contact_name_required(self, client, app, sample_data):
        """Test PUT without name returns 400."""
        with app.app_context():
            c = CustomerContact(
                customer_id=sample_data['customer1_id'],
                name='Existing'
            )
            db.session.add(c)
            db.session.commit()
            contact_id = c.id

        resp = client.put(
            f'/api/customer/contact/{contact_id}',
            json={'name': '', 'email': 'test@test.com'}
        )
        assert resp.status_code == 400

    def test_update_contact_404(self, client):
        """Test PUT for non-existent contact returns 404."""
        resp = client.put(
            '/api/customer/contact/99999',
            json={'name': 'Ghost'}
        )
        assert resp.status_code == 404

    def test_delete_contact(self, client, app, sample_data):
        """Test DELETE removes a contact."""
        with app.app_context():
            c = CustomerContact(
                customer_id=sample_data['customer1_id'],
                name='To Delete'
            )
            db.session.add(c)
            db.session.commit()
            contact_id = c.id

        resp = client.delete(f'/api/customer/contact/{contact_id}')
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True

        # Verify it's gone
        resp2 = client.get(f'/api/customer/{sample_data["customer1_id"]}/contacts')
        assert len(resp2.get_json()) == 0

    def test_delete_contact_404(self, client):
        """Test DELETE for non-existent contact returns 404."""
        resp = client.delete('/api/customer/contact/99999')
        assert resp.status_code == 404


class TestContactsOnCustomerView:
    """Tests for contacts display on customer view page."""

    def test_customer_view_shows_contacts_card(self, client, sample_data):
        """Test that customer view page includes the contacts card."""
        resp = client.get(f'/customer/{sample_data["customer1_id"]}')
        assert resp.status_code == 200
        assert b'Contacts' in resp.data
        assert b'bi-people' in resp.data

    def test_customer_view_shows_existing_contacts(self, client, app, sample_data):
        """Test that existing contacts appear on customer view."""
        with app.app_context():
            c = CustomerContact(
                customer_id=sample_data['customer1_id'],
                name='Visible Person',
                email='visible@test.com',
                title='CTO'
            )
            db.session.add(c)
            db.session.commit()

        resp = client.get(f'/customer/{sample_data["customer1_id"]}')
        assert b'Visible Person' in resp.data
        assert b'visible@test.com' in resp.data
        assert b'CTO' in resp.data

    def test_customer_view_empty_contacts(self, client, sample_data):
        """Test empty state message when no contacts."""
        resp = client.get(f'/customer/{sample_data["customer1_id"]}')
        assert b'No contacts yet' in resp.data


class TestContactsFlyoutOnNoteForm:
    """Tests for contacts flyout on note form."""

    def test_note_form_has_contacts_button(self, client, sample_data):
        """Test that new note form shows contacts button when customer is preselected."""
        resp = client.get(f'/note/new?customer_id={sample_data["customer1_id"]}')
        assert resp.status_code == 200
        assert b'contactsFlyoutBtn' in resp.data
        assert b'contactsFlyout' in resp.data

    def test_note_edit_has_contacts_button(self, client, sample_data):
        """Test that edit note form shows contacts button."""
        resp = client.get(f'/note/{sample_data["call1_id"]}/edit')
        assert resp.status_code == 200
        assert b'contactsFlyoutBtn' in resp.data

    def test_general_note_no_contacts_button(self, client):
        """Test that general note form does not render contacts button element."""
        resp = client.get('/note/new')
        assert resp.status_code == 200
        # The button element with onclick is inside {% if not is_general_note %}, so
        # the actual clickable button should not be in DOM. JS refs may still exist.
        assert b'id="contactsFlyoutBtn"' not in resp.data


class TestCustomerInfoEmailDomains:
    """Tests for email_domains in the customer info API."""

    def test_info_returns_email_domains_from_website(self, client, app, sample_data):
        """Customer with website should include that domain in email_domains."""
        with app.app_context():
            customer = db.session.get(Customer, sample_data['customer1_id'])
            customer.website = 'https://www.acmecorp.com/about'
            db.session.commit()

        resp = client.get(f'/api/customer/{sample_data["customer1_id"]}/info')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'email_domains' in data
        assert 'acmecorp.com' in data['email_domains']

    def test_info_returns_email_domains_from_contacts(self, client, app, sample_data):
        """Contact emails should contribute their domains to email_domains."""
        with app.app_context():
            c1 = CustomerContact(
                customer_id=sample_data['customer1_id'],
                name='Jane', email='jane@streamline.com',
            )
            c2 = CustomerContact(
                customer_id=sample_data['customer1_id'],
                name='Bob', email='bob@streamlinehealth.net',
            )
            db.session.add_all([c1, c2])
            db.session.commit()

        resp = client.get(f'/api/customer/{sample_data["customer1_id"]}/info')
        data = resp.get_json()
        assert 'streamline.com' in data['email_domains']
        assert 'streamlinehealth.net' in data['email_domains']

    def test_info_email_domains_deduped(self, client, app, sample_data):
        """Duplicate domains from multiple contacts should appear only once."""
        with app.app_context():
            c1 = CustomerContact(
                customer_id=sample_data['customer1_id'],
                name='A', email='a@example.com',
            )
            c2 = CustomerContact(
                customer_id=sample_data['customer1_id'],
                name='B', email='b@example.com',
            )
            db.session.add_all([c1, c2])
            db.session.commit()

        resp = client.get(f'/api/customer/{sample_data["customer1_id"]}/info')
        data = resp.get_json()
        assert data['email_domains'].count('example.com') == 1

    def test_info_email_domains_empty_when_no_data(self, client, sample_data):
        """Customer with no website and no contacts should return empty list."""
        resp = client.get(f'/api/customer/{sample_data["customer1_id"]}/info')
        data = resp.get_json()
        assert data['email_domains'] == []


class TestPartnerContactAPI:
    """Tests for the partner contact JSON API endpoint."""

    def _make_partner(self, app):
        with app.app_context():
            p = Partner(name='Contoso Partners', website='contoso.com')
            db.session.add(p)
            db.session.commit()
            return p.id

    def test_create_partner_contact(self, client, app):
        """Test POST to create a new partner contact."""
        pid = self._make_partner(app)
        resp = client.post(
            f'/api/partner/{pid}/contacts',
            json={'name': 'Jane Partner', 'email': 'jane@contoso.com', 'title': 'Director'}
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['name'] == 'Jane Partner'
        assert data['email'] == 'jane@contoso.com'
        assert data['title'] == 'Director'
        assert 'id' in data

    def test_create_partner_contact_minimal(self, client, app):
        """Test POST with only name succeeds."""
        pid = self._make_partner(app)
        resp = client.post(
            f'/api/partner/{pid}/contacts',
            json={'name': 'Just A Name'}
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['name'] == 'Just A Name'
        assert data['email'] == ''

    def test_create_partner_contact_name_required(self, client, app):
        """Test POST without name returns 400."""
        pid = self._make_partner(app)
        resp = client.post(
            f'/api/partner/{pid}/contacts',
            json={'email': 'no-name@test.com'}
        )
        assert resp.status_code == 400

    def test_create_partner_contact_404(self, client):
        """Test POST for non-existent partner returns 404."""
        resp = client.post(
            '/api/partner/99999/contacts',
            json={'name': 'Ghost'}
        )
        assert resp.status_code == 404
