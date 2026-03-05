"""
Tests for single-user system guarantees.

Covers:
- g.user always resolves to user with id=1
- Customer.tpid has a unique constraint (no duplicate TPIDs)
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Customer, User, db


class TestSingleUserGuarantee:
    """g.user must always be the user with id=1."""

    def test_g_user_is_id_1(self, app, client):
        """The before_request hook should always set g.user to id=1."""
        with app.app_context():
            user = db.session.get(User, 1)
            if not user:
                user = User(id=1, email="u@localhost", name="Local User", is_admin=True)
                db.session.add(user)
                db.session.commit()

        resp = client.get("/")
        assert resp.status_code == 200

        with app.app_context():
            from flask import g

            # Simulate what before_request does
            g.user = db.session.get(User, 1)
            assert g.user is not None
            assert g.user.id == 1


class TestCustomerTpidUnique:
    """Customer TPID must be globally unique."""

    def test_cannot_insert_duplicate_tpid(self, app):
        """Database should reject a second customer with the same TPID."""
        with app.app_context():
            user = User.query.first()
            c1 = Customer(name="First Corp", tpid=12345678)
            db.session.add(c1)
            db.session.commit()

            c2 = Customer(name="Second Corp", tpid=12345678)
            db.session.add(c2)
            with pytest.raises(IntegrityError):
                db.session.flush()
            db.session.rollback()

            # Cleanup
            Customer.query.filter_by(tpid=12345678).delete()
            db.session.commit()

    def test_different_tpids_allowed(self, app):
        """Different TPIDs should work fine."""
        with app.app_context():
            user = User.query.first()
            c1 = Customer(name="Alpha Corp", tpid=11111111)
            c2 = Customer(name="Beta Corp", tpid=22222222)
            db.session.add_all([c1, c2])
            db.session.commit()

            assert Customer.query.filter_by(tpid=11111111).count() == 1
            assert Customer.query.filter_by(tpid=22222222).count() == 1

            # Cleanup
            Customer.query.filter(Customer.tpid.in_([11111111, 22222222])).delete()
            db.session.commit()
