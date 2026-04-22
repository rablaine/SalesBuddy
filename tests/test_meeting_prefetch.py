"""Tests for the meeting prefetch service (Phase 1 of PREFETCH_MEETINGS_BACKLOG).

Covers:
- WorkIQ JSON extraction (preamble + tail noise tolerated).
- Synthetic ID dedupe / upsert.
- Customer matching via website + contact-email domains.
- External-attendee flagging.
- TTL purge.
- Cache lookup helper used by the note-form attendee scrape.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest

from app.models import (
    Customer,
    CustomerContact,
    PrefetchedMeeting,
    PrefetchedMeetingAttendee,
    db,
)
from app.services import meeting_prefetch


# ---------------------------------------------------------------------------
# Sample WorkIQ-shaped responses
# ---------------------------------------------------------------------------

SAMPLE_RESPONSE = """
Below is every meeting on your calendar, structured exactly as you requested.

```json
[
  {
    "subject": "Redsail - Fabric Cadence",
    "start_time": "2026-04-22T13:00:00-05:00",
    "end_time": "2026-04-22T13:30:00-05:00",
    "organizer_email": "Martinez.Reynaldo@microsoft.com",
    "is_recurring": true,
    "attendees": [
      {"name": "Alex Blaine", "email": "Alex.Blaine@microsoft.com"},
      {"name": "Tammy Kroetch", "email": "Tammy.Kroetch@redsailtechnologies.com"},
      {"name": "Joe Smith", "email": "joe.smith@redsailtechnologies.com"}
    ]
  },
  {
    "subject": "INTERNAL: Mini Tab",
    "start_time": "2026-04-22T09:30:00-05:00",
    "end_time": "2026-04-22T10:00:00-05:00",
    "organizer_email": "acarlisle@microsoft.com",
    "is_recurring": false,
    "attendees": [
      {"name": "Alex Blaine", "email": "Alex.Blaine@microsoft.com"}
    ]
  }
]
```

If you want, I can re-emit this with Graph-exact field names.
"""


@pytest.fixture
def clean_prefetch_tables(app):
    """Wipe prefetch tables before and after each test."""
    with app.app_context():
        PrefetchedMeetingAttendee.query.delete()
        PrefetchedMeeting.query.delete()
        db.session.commit()
        yield
        PrefetchedMeetingAttendee.query.delete()
        PrefetchedMeeting.query.delete()
        db.session.commit()


@pytest.fixture
def redsail_customer(app, clean_prefetch_tables):
    """Customer matched by website domain to the sample meeting."""
    with app.app_context():
        c = Customer(name="Redsail Technologies",
                     tpid=910001,
                     website="https://redsailtechnologies.com")
        db.session.add(c)
        db.session.commit()
        yield c.id
        db.session.delete(db.session.get(Customer, c.id))
        db.session.commit()


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

class TestExtractJsonArray:
    def test_pulls_array_out_of_prose(self):
        items = meeting_prefetch._extract_json_array(SAMPLE_RESPONSE)
        assert len(items) == 2
        assert items[0]['subject'] == 'Redsail - Fabric Cadence'

    def test_returns_empty_on_no_array(self):
        assert meeting_prefetch._extract_json_array("nothing here") == []

    def test_returns_empty_on_invalid_json(self):
        assert meeting_prefetch._extract_json_array("[ {bad json} ]") == []

    def test_handles_empty_input(self):
        assert meeting_prefetch._extract_json_array("") == []


# ---------------------------------------------------------------------------
# Datetime + organizer normalization
# ---------------------------------------------------------------------------

class TestParseDt:
    def test_parses_offset_to_utc_naive(self):
        # 13:00 -05:00 -> 18:00 UTC.
        dt = meeting_prefetch._parse_dt("2026-04-22T13:00:00-05:00")
        assert dt == datetime(2026, 4, 22, 18, 0, 0)
        assert dt.tzinfo is None

    def test_returns_none_on_garbage(self):
        assert meeting_prefetch._parse_dt("not-a-date") is None
        assert meeting_prefetch._parse_dt(None) is None


class TestNormalizeOrganizer:
    def test_x500_dn_treated_as_unknown(self):
        dn = "/O=EXCHANGELABS/OU=EXCHANGE ADMINISTRATIVE GROUP/CN=ALEX"
        assert meeting_prefetch._normalize_organizer(dn) is None

    def test_lowercases_smtp(self):
        assert meeting_prefetch._normalize_organizer("Foo@Bar.com") == "foo@bar.com"


class TestSyntheticId:
    def test_stable_for_same_inputs(self):
        a = meeting_prefetch._synthetic_id("Sub", datetime(2026, 4, 22, 18), "x@y")
        b = meeting_prefetch._synthetic_id("Sub", datetime(2026, 4, 22, 18), "x@y")
        assert a == b

    def test_changes_with_time(self):
        a = meeting_prefetch._synthetic_id("Sub", datetime(2026, 4, 22, 18), "x@y")
        b = meeting_prefetch._synthetic_id("Sub", datetime(2026, 4, 22, 19), "x@y")
        assert a != b


# ---------------------------------------------------------------------------
# Customer domain matching
# ---------------------------------------------------------------------------

class TestDomainMap:
    def test_website_domain_wins(self, app, clean_prefetch_tables):
        with app.app_context():
            c = Customer(name="Acme", tpid=910002, website="https://www.acme.com/foo")
            db.session.add(c)
            db.session.commit()
            try:
                m = meeting_prefetch._build_domain_map()
                assert m.get('acme.com') == (c.id, 'website')
            finally:
                db.session.delete(c)
                db.session.commit()

    def test_contact_email_fallback(self, app, clean_prefetch_tables):
        with app.app_context():
            c = Customer(name="NoSite", tpid=910003)
            db.session.add(c)
            db.session.commit()
            db.session.add(CustomerContact(
                customer_id=c.id, name="A", email="a@nosite.io"
            ))
            db.session.commit()
            try:
                m = meeting_prefetch._build_domain_map()
                assert m.get('nosite.io') == (c.id, 'contact_email')
            finally:
                CustomerContact.query.filter_by(customer_id=c.id).delete()
                db.session.delete(c)
                db.session.commit()

    def test_internal_domain_excluded_from_contact_fallback(
        self, app, clean_prefetch_tables,
    ):
        with app.app_context():
            c = Customer(name="MS-only", tpid=910004)
            db.session.add(c)
            db.session.commit()
            db.session.add(CustomerContact(
                customer_id=c.id, name="X", email="x@microsoft.com"
            ))
            db.session.commit()
            try:
                m = meeting_prefetch._build_domain_map()
                assert 'microsoft.com' not in m
            finally:
                CustomerContact.query.filter_by(customer_id=c.id).delete()
                db.session.delete(c)
                db.session.commit()


# ---------------------------------------------------------------------------
# Full prefetch flow
# ---------------------------------------------------------------------------

class TestPrefetchForDate:
    def test_inserts_meetings_and_attendees(self, app, redsail_customer):
        with app.app_context():
            with patch('app.services.workiq_service.query_workiq',
                       return_value=SAMPLE_RESPONSE):
                stored, err = meeting_prefetch.prefetch_for_date('2026-04-22')

            assert err is None
            assert stored == 2

            redsail = PrefetchedMeeting.query.filter_by(
                subject='Redsail - Fabric Cadence'
            ).first()
            assert redsail is not None
            assert redsail.customer_id == redsail_customer
            assert redsail.matched_via == 'website'
            assert redsail.is_recurring is True
            assert redsail.recurring_key is not None
            assert len(redsail.attendees) == 3
            externals = [a for a in redsail.attendees if a.is_external]
            assert {a.email for a in externals} == {
                'tammy.kroetch@redsailtechnologies.com',
                'joe.smith@redsailtechnologies.com',
            }

            internal = PrefetchedMeeting.query.filter_by(
                subject='INTERNAL: Mini Tab'
            ).first()
            assert internal is not None
            assert internal.customer_id is None
            assert internal.matched_via is None

    def test_idempotent_upsert_dedupes_by_workiq_id(self, app, redsail_customer):
        with app.app_context():
            with patch('app.services.workiq_service.query_workiq',
                       return_value=SAMPLE_RESPONSE):
                meeting_prefetch.prefetch_for_date('2026-04-22')
                meeting_prefetch.prefetch_for_date('2026-04-22')

            assert PrefetchedMeeting.query.count() == 2
            assert PrefetchedMeetingAttendee.query.count() == 4  # 3 + 1

    def test_workiq_failure_returns_error(self, app, clean_prefetch_tables):
        with app.app_context():
            with patch('app.services.workiq_service.query_workiq',
                       side_effect=RuntimeError("WorkIQ down")):
                stored, err = meeting_prefetch.prefetch_for_date('2026-04-22')
            assert stored == 0
            assert err and 'WorkIQ down' in err


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------

class TestPurgeExpired:
    def test_drops_expired_keeps_fresh(self, app, clean_prefetch_tables):
        with app.app_context():
            now = datetime.now()
            old = PrefetchedMeeting(
                workiq_id='old', subject='old', start_time=now - timedelta(days=5),
                meeting_date=date.today() - timedelta(days=5),
                expires_at=now - timedelta(days=4),
            )
            new = PrefetchedMeeting(
                workiq_id='new', subject='new', start_time=now,
                meeting_date=date.today(),
                expires_at=now + timedelta(days=1),
            )
            db.session.add_all([old, new])
            db.session.commit()

            purged = meeting_prefetch.purge_expired()
            assert purged == 1
            remaining = {m.workiq_id for m in PrefetchedMeeting.query.all()}
            assert remaining == {'new'}


# ---------------------------------------------------------------------------
# Cache lookup
# ---------------------------------------------------------------------------

class TestGetCachedAttendees:
    def _seed(self, app):
        with app.app_context():
            m = PrefetchedMeeting(
                workiq_id='wid1',
                subject='Redsail - Fabric Cadence',
                start_time=datetime(2026, 4, 22, 18, 0, 0),
                meeting_date=date(2026, 4, 22),
                expires_at=datetime.now() + timedelta(days=1),
            )
            db.session.add(m)
            db.session.flush()
            db.session.add_all([
                PrefetchedMeetingAttendee(
                    meeting_id=m.id, name='Tammy', email='tammy@redsail.com',
                    domain='redsail.com', is_external=True,
                ),
                PrefetchedMeetingAttendee(
                    meeting_id=m.id, name='Alex', email='alex@microsoft.com',
                    domain='microsoft.com', is_external=False,
                ),
            ])
            db.session.commit()

    def test_exact_subject_match(self, app, clean_prefetch_tables):
        self._seed(app)
        with app.app_context():
            result = meeting_prefetch.get_cached_attendees(
                'Redsail - Fabric Cadence', '2026-04-22'
            )
        assert result is not None
        assert len(result) == 2
        assert all(a['title'] is None for a in result)
        emails = {a['email'] for a in result}
        assert 'tammy@redsail.com' in emails

    def test_case_and_whitespace_insensitive(self, app, clean_prefetch_tables):
        self._seed(app)
        with app.app_context():
            result = meeting_prefetch.get_cached_attendees(
                '  redsail  -  fabric  cadence  ', '2026-04-22'
            )
        assert result is not None
        assert len(result) == 2

    def test_returns_none_on_miss(self, app, clean_prefetch_tables):
        self._seed(app)
        with app.app_context():
            assert meeting_prefetch.get_cached_attendees(
                'Totally Different Meeting', '2026-04-22'
            ) is None

    def test_returns_none_on_wrong_date(self, app, clean_prefetch_tables):
        self._seed(app)
        with app.app_context():
            assert meeting_prefetch.get_cached_attendees(
                'Redsail - Fabric Cadence', '2026-04-23'
            ) is None


# ---------------------------------------------------------------------------
# Integration with attendee scrape
# ---------------------------------------------------------------------------

class TestAttendeeScrapeUsesCache:
    def test_cache_hit_skips_workiq_call(self, app, clean_prefetch_tables):
        from app.services.meeting_attendee_scrape import scrape_meeting_attendees

        with app.app_context():
            m = PrefetchedMeeting(
                workiq_id='wid1', subject='Acme Sync',
                start_time=datetime(2026, 4, 22, 14, 0, 0),
                meeting_date=date(2026, 4, 22),
                expires_at=datetime.now() + timedelta(days=1),
            )
            db.session.add(m)
            db.session.flush()
            db.session.add(PrefetchedMeetingAttendee(
                meeting_id=m.id, name='Jane Doe', email='jane@acme.com',
                domain='acme.com', is_external=True,
            ))
            db.session.commit()

            with patch('app.services.workiq_service.query_workiq') as wq:
                result = scrape_meeting_attendees(
                    'Acme Sync', '2026-04-22',
                )
            wq.assert_not_called()
            assert result['source'] == 'cache'
            assert len(result['attendees']) == 1

    def test_force_refresh_skips_cache(self, app, clean_prefetch_tables):
        from app.services.meeting_attendee_scrape import scrape_meeting_attendees

        with app.app_context():
            m = PrefetchedMeeting(
                workiq_id='wid1', subject='Acme Sync',
                start_time=datetime(2026, 4, 22, 14, 0, 0),
                meeting_date=date(2026, 4, 22),
                expires_at=datetime.now() + timedelta(days=1),
            )
            db.session.add(m)
            db.session.commit()

            fake_response = '{"attendees": [{"name": "Live", "email": "live@x.com"}]}'
            with patch('app.services.workiq_service.query_workiq',
                       return_value=fake_response) as wq:
                result = scrape_meeting_attendees(
                    'Acme Sync', '2026-04-22', force_refresh=True,
                )
            wq.assert_called_once()
            assert result['source'] == 'workiq'
