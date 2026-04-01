"""Tests for contact photo upload and face detection."""
import base64
import io
import pytest
from unittest.mock import patch

import cv2
import numpy as np

from app.models import db, Customer, CustomerContact, Partner, PartnerContact


def _make_test_image_b64(width=100, height=100):
    """Create a valid test image as base64."""
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:] = (200, 100, 50)  # Blue-ish
    _, buf = cv2.imencode('.png', img)
    return base64.b64encode(buf).decode('ascii')


class TestContactPhotoAPI:
    """Tests for contact photo upload endpoints."""

    def test_customer_contact_photo_upload(self, client, app, sample_data):
        """Upload a photo to a customer contact."""
        with app.app_context():
            contact = CustomerContact(
                customer_id=sample_data['customer1_id'],
                name='Photo Test', email='pt@test.com'
            )
            db.session.add(contact)
            db.session.commit()
            cid = contact.id

        resp = client.post(f'/api/customer/contact/{cid}/photo', json={
            'photo_b64': _make_test_image_b64()
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['photo_b64']  # Should have cropped version

        # Verify DB
        with app.app_context():
            c = db.session.get(CustomerContact, cid)
            assert c.photo_b64 is not None
            assert c.photo_full_b64 is not None
            db.session.delete(c)
            db.session.commit()

    def test_customer_contact_photo_remove(self, client, app, sample_data):
        """Remove a photo from a customer contact."""
        with app.app_context():
            contact = CustomerContact(
                customer_id=sample_data['customer1_id'],
                name='Remove Test', photo_b64='old', photo_full_b64='oldfull'
            )
            db.session.add(contact)
            db.session.commit()
            cid = contact.id

        resp = client.post(f'/api/customer/contact/{cid}/photo', json={
            'photo_b64': None
        })
        assert resp.status_code == 200

        with app.app_context():
            c = db.session.get(CustomerContact, cid)
            assert c.photo_b64 is None
            assert c.photo_full_b64 is None
            db.session.delete(c)
            db.session.commit()

    def test_partner_contact_photo_upload(self, client, app, sample_data):
        """Upload a photo to a partner contact."""
        with app.app_context():
            p = Partner(name='PhotoPartner')
            db.session.add(p)
            db.session.flush()
            contact = PartnerContact(partner_id=p.id, name='PPhoto Test')
            db.session.add(contact)
            db.session.commit()
            cid = contact.id

        resp = client.post(f'/api/partner/contact/{cid}/photo', json={
            'photo_b64': _make_test_image_b64()
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['photo_b64']

        # Cleanup
        with app.app_context():
            c = db.session.get(PartnerContact, cid)
            p = c.partner
            db.session.delete(c)
            db.session.delete(p)
            db.session.commit()

    def test_customer_contacts_api_includes_photo(self, client, app, sample_data):
        """Customer contacts API returns photo_b64."""
        with app.app_context():
            contact = CustomerContact(
                customer_id=sample_data['customer1_id'],
                name='API Photo', photo_b64='abc123'
            )
            db.session.add(contact)
            db.session.commit()

        resp = client.get(f'/api/customer/{sample_data["customer1_id"]}/contacts')
        data = resp.get_json()
        found = [c for c in data if c['name'] == 'API Photo']
        assert len(found) == 1
        assert found[0]['photo_b64'] == 'abc123'

        with app.app_context():
            c = CustomerContact.query.filter_by(name='API Photo').first()
            if c:
                db.session.delete(c)
                db.session.commit()

    def test_partner_api_includes_photo(self, client, app, sample_data):
        """Partner GET API returns photo_b64 in contacts."""
        with app.app_context():
            p = Partner(name='APIPhotoPartner')
            db.session.add(p)
            db.session.flush()
            contact = PartnerContact(partner_id=p.id, name='PAPIPhoto', photo_b64='xyz789')
            db.session.add(contact)
            db.session.commit()
            pid = p.id

        resp = client.get(f'/api/partners/{pid}')
        data = resp.get_json()
        found = [c for c in data['contacts'] if c['name'] == 'PAPIPhoto']
        assert len(found) == 1
        assert found[0]['photo_b64'] == 'xyz789'

        with app.app_context():
            p = db.session.get(Partner, pid)
            for c in p.contacts:
                db.session.delete(c)
            db.session.delete(p)
            db.session.commit()


class TestContactPhotoProcessing:
    """Tests for face detection and cropping service."""

    def test_process_returns_cropped_and_full(self, app):
        """process_contact_photo returns both cropped and full b64."""
        from app.services.contact_photo import process_contact_photo
        cropped, full = process_contact_photo(_make_test_image_b64())
        assert cropped  # Non-empty
        assert full  # Non-empty
        # Both should be valid base64
        base64.b64decode(cropped)
        base64.b64decode(full)

    def test_invalid_image_raises(self, app):
        """Invalid image data raises ValueError."""
        from app.services.contact_photo import process_contact_photo
        with pytest.raises(Exception):
            process_contact_photo(base64.b64encode(b'not an image').decode())
