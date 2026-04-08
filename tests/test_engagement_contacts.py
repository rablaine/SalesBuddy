"""Tests for EngagementContact model and engagement contact API endpoints."""
import pytest
from app.models import (
    db, Engagement, EngagementContact, CustomerContact, PartnerContact,
    SolutionEngineer, Seller, Partner, InternalContact, Customer, Note,
)


class TestEngagementContactModel:
    """Test EngagementContact model properties."""

    def test_customer_contact(self, app, sample_data):
        """Customer contact has correct type and display name."""
        with app.app_context():
            customer = db.session.get(Customer, sample_data['customer1_id'])
            contact = CustomerContact(
                customer_id=customer.id, name='Jane Doe',
                email='jane@example.com', title='CTO'
            )
            db.session.add(contact)
            db.session.flush()

            eng = Engagement(
                customer_id=customer.id, title='Test Engagement', status='Active'
            )
            db.session.add(eng)
            db.session.flush()

            ec = EngagementContact(
                engagement_id=eng.id, customer_contact_id=contact.id
            )
            db.session.add(ec)
            db.session.commit()

            assert ec.person_type == 'customer_contact'
            assert ec.display_name == 'Jane Doe'
            assert ec.email == 'jane@example.com'
            assert ec.ref_id == contact.id
            d = ec.to_dict()
            assert d['person_type'] == 'customer_contact'
            assert d['ref_id'] == contact.id

    def test_seller_contact(self, app, sample_data):
        """Seller contact has correct type and display name."""
        with app.app_context():
            customer = db.session.get(Customer, sample_data['customer1_id'])
            eng = Engagement(
                customer_id=customer.id, title='Seller Eng', status='Active'
            )
            db.session.add(eng)
            db.session.flush()

            ec = EngagementContact(
                engagement_id=eng.id, seller_id=sample_data['seller1_id']
            )
            db.session.add(ec)
            db.session.commit()

            assert ec.person_type == 'seller'
            assert ec.display_name == 'Alice Smith'
            assert ec.email == 'alices@microsoft.com'

    def test_internal_contact(self, app, sample_data):
        """Internal contact has correct type and display name."""
        with app.app_context():
            ic = InternalContact(name='DAE Person', alias='daep', role='DAE')
            db.session.add(ic)
            db.session.flush()

            customer = db.session.get(Customer, sample_data['customer1_id'])
            eng = Engagement(
                customer_id=customer.id, title='IC Eng', status='Active'
            )
            db.session.add(eng)
            db.session.flush()

            ec = EngagementContact(
                engagement_id=eng.id, internal_contact_id=ic.id
            )
            db.session.add(ec)
            db.session.commit()

            assert ec.person_type == 'internal_contact'
            assert ec.display_name == 'DAE Person'
            assert ec.email == 'daep@microsoft.com'

    def test_external_contact(self, app, sample_data):
        """External contact uses external fields."""
        with app.app_context():
            customer = db.session.get(Customer, sample_data['customer1_id'])
            eng = Engagement(
                customer_id=customer.id, title='Ext Eng', status='Active'
            )
            db.session.add(eng)
            db.session.flush()

            ec = EngagementContact(
                engagement_id=eng.id,
                external_name='Outside Person',
                external_email='outside@example.com'
            )
            db.session.add(ec)
            db.session.commit()

            assert ec.person_type == 'external'
            assert ec.display_name == 'Outside Person'
            assert ec.email == 'outside@example.com'
            assert ec.ref_id is None

    def test_cascade_delete(self, app, sample_data):
        """Deleting engagement cascades to contacts."""
        with app.app_context():
            customer = db.session.get(Customer, sample_data['customer1_id'])
            eng = Engagement(
                customer_id=customer.id, title='Cascade Eng', status='Active'
            )
            db.session.add(eng)
            db.session.flush()

            ec = EngagementContact(
                engagement_id=eng.id,
                external_name='Will Be Deleted',
            )
            db.session.add(ec)
            db.session.commit()
            ec_id = ec.id

            db.session.delete(eng)
            db.session.commit()

            assert EngagementContact.query.get(ec_id) is None

    def test_story_completeness_counts_contacts(self, app, sample_data):
        """story_completeness should count contacts as a field."""
        with app.app_context():
            customer = db.session.get(Customer, sample_data['customer1_id'])
            eng = Engagement(
                customer_id=customer.id, title='Completeness Eng', status='Active'
            )
            db.session.add(eng)
            db.session.flush()

            # No fields filled - 0%
            assert eng.story_completeness == 0

            # Add a contact - should count as 1/6
            ec = EngagementContact(
                engagement_id=eng.id,
                external_name='Someone',
            )
            db.session.add(ec)
            db.session.flush()
            # Need to expire to re-evaluate the relationship
            db.session.expire(eng, ['contacts'])
            assert eng.story_completeness == 16  # 1/6 = 16%

            # Add technical_problem - should be 2/6
            eng.technical_problem = 'Some problem'
            assert eng.story_completeness == 33  # 2/6 = 33%


class TestEngagementContactRoutes:
    """Test engagement contact form handling and API endpoints."""

    def test_create_engagement_with_contacts(self, app, client, sample_data):
        """Creating engagement with contacts via form should save them."""
        with app.app_context():
            customer = db.session.get(Customer, sample_data['customer1_id'])
            contact = CustomerContact(
                customer_id=customer.id, name='Form Contact',
                email='form@example.com'
            )
            db.session.add(contact)
            db.session.commit()
            contact_id = contact.id
            customer_id = customer.id

        resp = client.post(
            f'/customer/{customer_id}/engagement/new',
            data={
                'title': 'Contact Test Engagement',
                'status': 'Active',
                'contact_types': ['customer_contact'],
                'contact_ref_ids': [str(contact_id)],
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with app.app_context():
            eng = Engagement.query.filter_by(title='Contact Test Engagement').first()
            assert eng is not None
            assert len(eng.contacts) == 1
            assert eng.contacts[0].person_type == 'customer_contact'
            assert eng.contacts[0].display_name == 'Form Contact'

    def test_edit_engagement_updates_contacts(self, app, client, sample_data):
        """Editing engagement should replace contacts."""
        with app.app_context():
            customer = db.session.get(Customer, sample_data['customer1_id'])
            eng = Engagement(
                customer_id=customer.id, title='Edit Contact Eng', status='Active'
            )
            db.session.add(eng)
            db.session.flush()
            ec = EngagementContact(
                engagement_id=eng.id, external_name='Old Contact'
            )
            db.session.add(ec)
            db.session.commit()
            eng_id = eng.id
            customer_id = customer.id

        # Edit to have a different external contact
        resp = client.post(
            f'/engagement/{eng_id}/edit',
            data={
                'title': 'Edit Contact Eng',
                'status': 'Active',
                'contact_types': ['external'],
                'contact_ref_ids': [''],
                'contact_ext_names': ['New Contact'],
                'contact_ext_emails': ['new@example.com'],
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with app.app_context():
            eng = db.session.get(Engagement, eng_id)
            assert len(eng.contacts) == 1
            assert eng.contacts[0].display_name == 'New Contact'
            assert eng.contacts[0].email == 'new@example.com'

    def test_api_list_contacts(self, app, client, sample_data):
        """API should list contacts for an engagement."""
        with app.app_context():
            customer = db.session.get(Customer, sample_data['customer1_id'])
            eng = Engagement(
                customer_id=customer.id, title='API List Eng', status='Active'
            )
            db.session.add(eng)
            db.session.flush()
            ec = EngagementContact(
                engagement_id=eng.id, external_name='API Person'
            )
            db.session.add(ec)
            db.session.commit()
            eng_id = eng.id

        resp = client.get(f'/api/engagement/{eng_id}/contacts')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]['display_name'] == 'API Person'
        assert data[0]['person_type'] == 'external'

    def test_api_add_contact(self, app, client, sample_data):
        """API should add a contact to an engagement."""
        with app.app_context():
            customer = db.session.get(Customer, sample_data['customer1_id'])
            eng = Engagement(
                customer_id=customer.id, title='API Add Eng', status='Active'
            )
            db.session.add(eng)
            db.session.flush()
            eng_id = eng.id
            seller_id = sample_data['seller1_id']
            db.session.commit()

        resp = client.post(
            f'/api/engagement/{eng_id}/contacts',
            json={'type': 'seller', 'id': seller_id},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['contact']['person_type'] == 'seller'

    def test_api_remove_contact(self, app, client, sample_data):
        """API should remove a contact from an engagement."""
        with app.app_context():
            customer = db.session.get(Customer, sample_data['customer1_id'])
            eng = Engagement(
                customer_id=customer.id, title='API Remove Eng', status='Active'
            )
            db.session.add(eng)
            db.session.flush()
            ec = EngagementContact(
                engagement_id=eng.id, external_name='To Remove'
            )
            db.session.add(ec)
            db.session.commit()
            eng_id = eng.id
            ec_id = ec.id

        resp = client.delete(f'/api/engagement/{eng_id}/contacts/{ec_id}')
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True

        with app.app_context():
            assert EngagementContact.query.get(ec_id) is None

    def test_api_contact_search(self, app, client, sample_data):
        """Contact search should return results across person types."""
        with app.app_context():
            customer = db.session.get(Customer, sample_data['customer1_id'])
            contact = CustomerContact(
                customer_id=customer.id, name='Searchable Person',
                email='search@example.com'
            )
            db.session.add(contact)
            db.session.commit()
            customer_id = customer.id

        resp = client.get(
            f'/api/engagement-contact-search?q=searchable&customer_id={customer_id}'
        )
        assert resp.status_code == 200
        data = resp.get_json()
        results = data['results']
        assert any(r['name'] == 'Searchable Person' for r in results)

    def test_api_contact_search_sellers(self, app, client, sample_data):
        """Contact search should find sellers."""
        resp = client.get('/api/engagement-contact-search?q=alice')
        assert resp.status_code == 200
        data = resp.get_json()
        results = data['results']
        assert any(
            r['name'] == 'Alice Smith' and r['type'] == 'seller'
            for r in results
        )
