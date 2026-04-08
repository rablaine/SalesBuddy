"""Tests for customer merge service and stale detection."""
import pytest
from datetime import datetime, timezone, timedelta


@pytest.fixture
def two_customers(app):
    """Create two customers with linked data for merge testing."""
    from app.models import (
        db, Customer, Note, Engagement, Milestone, CustomerContact,
        MarketingSummary, utc_now,
    )

    with app.app_context():
        source = Customer(name='Old Corp', tpid=11111)
        dest = Customer(name='New Corp', tpid=22222)
        db.session.add_all([source, dest])
        db.session.flush()

        # Add notes to source
        n1 = Note(content='Meeting notes', customer_id=source.id,
                   call_date=datetime(2026, 1, 15))
        n2 = Note(content='Follow-up', customer_id=source.id,
                   call_date=datetime(2026, 2, 10))
        db.session.add_all([n1, n2])

        # Add engagement to source
        eng = Engagement(title='Azure Migration', customer_id=source.id)
        db.session.add(eng)

        # Add milestone to source
        ms = Milestone(title='Phase 1 Go-Live', customer_id=source.id,
                       url='https://example.com/ms/1')
        db.session.add(ms)

        # Add contacts to source (one will be a dupe)
        c1 = CustomerContact(name='Alice', email='alice@oldcorp.com',
                              customer_id=source.id)
        c2 = CustomerContact(name='Bob', email='bob@oldcorp.com',
                              customer_id=source.id)
        db.session.add_all([c1, c2])

        # Add a contact to dest that dupes one from source
        c3 = CustomerContact(name='Alice Smith', email='alice@oldcorp.com',
                              customer_id=dest.id)
        db.session.add(c3)

        # Marketing summary on source
        ms_src = MarketingSummary(customer_id=source.id, tpid='11111',
                                  total_interactions=50)
        db.session.add(ms_src)

        # Add a note to dest too
        n3 = Note(content='Intro call', customer_id=dest.id,
                   call_date=datetime(2026, 3, 1))
        db.session.add(n3)

        db.session.commit()

        yield {
            'source_id': source.id,
            'dest_id': dest.id,
            'source_tpid': source.tpid,
            'dest_tpid': dest.tpid,
        }

        # Cleanup (dest may have absorbed everything, source may be deleted)
        db.session.rollback()


class TestMergePreview:
    """Tests for merge preview (dry run)."""

    def test_preview_shows_counts(self, app, two_customers):
        from app.services.customer_merge import get_merge_preview

        with app.app_context():
            preview = get_merge_preview(
                two_customers['source_id'], two_customers['dest_id'])

            assert 'error' not in preview
            assert preview['source']['tpid'] == 11111
            assert preview['destination']['tpid'] == 22222
            counts = preview['counts']
            assert counts['notes'] == 2
            assert counts['engagements'] == 1
            assert counts['milestones'] == 1
            assert counts['contacts'] == 2
            assert counts['marketing_summary'] == 1

    def test_preview_same_customer_error(self, app, two_customers):
        from app.services.customer_merge import get_merge_preview

        with app.app_context():
            preview = get_merge_preview(
                two_customers['source_id'], two_customers['source_id'])
            assert preview['error'] == 'Cannot merge a customer into itself'

    def test_preview_nonexistent_customer(self, app):
        from app.services.customer_merge import get_merge_preview

        with app.app_context():
            preview = get_merge_preview(99999, 99998)
            assert 'error' in preview


class TestMergeCustomer:
    """Tests for the actual merge operation."""

    def test_merge_moves_notes(self, app, two_customers):
        from app.services.customer_merge import merge_customer
        from app.models import Note

        with app.app_context():
            result = merge_customer(
                two_customers['source_id'], two_customers['dest_id'])

            assert result['success']
            assert result['migrated']['notes'] == 2

            # All notes should now be on dest
            dest_notes = Note.query.filter_by(
                customer_id=two_customers['dest_id']).count()
            assert dest_notes == 3  # 2 moved + 1 original

    def test_merge_moves_engagements(self, app, two_customers):
        from app.services.customer_merge import merge_customer
        from app.models import Engagement

        with app.app_context():
            result = merge_customer(
                two_customers['source_id'], two_customers['dest_id'])

            assert result['migrated']['engagements'] == 1
            assert Engagement.query.filter_by(
                customer_id=two_customers['dest_id']).count() == 1

    def test_merge_dedupes_contacts(self, app, two_customers):
        from app.services.customer_merge import merge_customer
        from app.models import CustomerContact

        with app.app_context():
            result = merge_customer(
                two_customers['source_id'], two_customers['dest_id'])

            # Bob moves, Alice is a dupe (same email) so gets deleted
            assert result['migrated']['contacts'] == 1
            dest_contacts = CustomerContact.query.filter_by(
                customer_id=two_customers['dest_id']).all()
            assert len(dest_contacts) == 2  # Alice (original) + Bob (moved)

    def test_merge_handles_marketing_summary(self, app, two_customers):
        from app.services.customer_merge import merge_customer
        from app.models import MarketingSummary

        with app.app_context():
            result = merge_customer(
                two_customers['source_id'], two_customers['dest_id'])

            # Source had a summary, dest didn't, so it should be moved
            assert result['migrated']['marketing_summary'] == 1
            dest_summary = MarketingSummary.query.filter_by(
                customer_id=two_customers['dest_id']).first()
            assert dest_summary is not None
            assert dest_summary.total_interactions == 50

    def test_merge_deletes_source(self, app, two_customers):
        from app.services.customer_merge import merge_customer
        from app.models import Customer

        with app.app_context():
            merge_customer(
                two_customers['source_id'], two_customers['dest_id'])

            source = Customer.query.get(two_customers['source_id'])
            assert source is None

    def test_merge_preserves_account_context(self, app, two_customers):
        from app.services.customer_merge import merge_customer
        from app.models import db, Customer

        with app.app_context():
            source = Customer.query.get(two_customers['source_id'])
            source.account_context = 'Important notes about old corp'
            dest = Customer.query.get(two_customers['dest_id'])
            dest.account_context = 'Existing dest notes'
            db.session.commit()

            merge_customer(
                two_customers['source_id'], two_customers['dest_id'])

            dest = Customer.query.get(two_customers['dest_id'])
            assert 'Existing dest notes' in dest.account_context
            assert 'Important notes about old corp' in dest.account_context
            assert 'Merged from' in dest.account_context

    def test_merge_carries_nickname(self, app, two_customers):
        from app.services.customer_merge import merge_customer
        from app.models import db, Customer

        with app.app_context():
            source = Customer.query.get(two_customers['source_id'])
            source.nickname = 'OldCo'
            db.session.commit()

            merge_customer(
                two_customers['source_id'], two_customers['dest_id'])

            dest = Customer.query.get(two_customers['dest_id'])
            assert dest.nickname == 'OldCo'

    def test_merge_same_customer_raises(self, app, two_customers):
        from app.services.customer_merge import merge_customer

        with app.app_context():
            with pytest.raises(ValueError, match='Cannot merge'):
                merge_customer(
                    two_customers['source_id'], two_customers['source_id'])


class TestStaleDetection:
    """Tests for stale customer flagging."""

    def test_stale_since_column_exists(self, app):
        from app.models import Customer

        with app.app_context():
            c = Customer(name='Test Co', tpid=99999)
            assert c.stale_since is None

    def test_stale_customers_api(self, client, app):
        from app.models import db, Customer, utc_now

        with app.app_context():
            c = Customer(name='Stale Co', tpid=88888,
                         stale_since=utc_now())
            db.session.add(c)
            db.session.commit()

        resp = client.get('/api/admin/stale-customers')
        assert resp.status_code == 200
        data = resp.get_json()
        stale_names = [c['name'] for c in data]
        assert 'Stale Co' in stale_names

    def test_dismiss_stale(self, client, app):
        from app.models import db, Customer, utc_now

        with app.app_context():
            c = Customer(name='Dismiss Me', tpid=77777,
                         stale_since=utc_now())
            db.session.add(c)
            db.session.commit()
            cid = c.id

        resp = client.post(f'/api/admin/stale-customers/{cid}/dismiss')
        assert resp.status_code == 200

        with app.app_context():
            c = Customer.query.get(cid)
            assert c.stale_since is None


class TestMergeAPI:
    """Tests for merge REST endpoints."""

    def test_merge_preview_api(self, client, app, two_customers):
        resp = client.get(
            f'/api/admin/merge-preview?source_id={two_customers["source_id"]}'
            f'&dest_id={two_customers["dest_id"]}')

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['source']['tpid'] == 11111
        assert data['counts']['notes'] == 2

    def test_merge_execute_api(self, client, app, two_customers):
        resp = client.post('/api/admin/merge-customer',
                           json={
                               'source_id': two_customers['source_id'],
                               'dest_id': two_customers['dest_id'],
                           })

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success']
        assert data['source_tpid'] == 11111

    def test_merge_api_missing_params(self, client):
        resp = client.post('/api/admin/merge-customer', json={})
        assert resp.status_code == 400

    def test_customers_search_api(self, client, app, two_customers):
        resp = client.get('/api/admin/customers-search?q=Corp')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) >= 1

    def test_delete_stale_no_data(self, client, app):
        from app.models import db, Customer, MarketingSummary, utc_now

        with app.app_context():
            c = Customer(name='Empty Stale', tpid=66666,
                         stale_since=utc_now())
            db.session.add(c)
            db.session.flush()
            # Add auto-imported marketing data (should be cascade-deleted)
            ms = MarketingSummary(customer_id=c.id, tpid='66666',
                                  total_interactions=5)
            db.session.add(ms)
            db.session.commit()
            cid = c.id

        resp = client.delete(f'/api/admin/stale-customers/{cid}')
        assert resp.status_code == 200
        assert resp.get_json()['success']

        with app.app_context():
            assert Customer.query.get(cid) is None
            assert MarketingSummary.query.filter_by(customer_id=cid).count() == 0

    def test_delete_stale_with_user_data_blocked(self, client, app, two_customers):
        """Cannot delete a customer that has notes/engagements."""
        from app.models import db, Customer, utc_now

        with app.app_context():
            source = Customer.query.get(two_customers['source_id'])
            source.stale_since = utc_now()
            db.session.commit()

        resp = client.delete(f'/api/admin/stale-customers/{two_customers["source_id"]}')
        assert resp.status_code == 400
        assert 'linked data' in resp.get_json()['error']
