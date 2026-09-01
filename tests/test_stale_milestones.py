"""Tests for stale milestones feature on the dashboard."""

import json
import zlib
from datetime import datetime, date, timedelta, timezone

import pytest


class TestDaysSinceLastRealComment:
    """Unit tests for _days_since_last_real_comment helper."""

    def _make_milestone(self, app, comments_json=None, **kwargs):
        """Create a milestone and return it."""
        with app.app_context():
            from app.models import db, Milestone
            ms = Milestone(
                url='https://example.com/ms',
                msx_milestone_id=kwargs.get('msx_id', 'test-ms'),
                title=kwargs.get('title', 'Test Milestone'),
                msx_status='On Track',
                on_my_team=True,
                cached_comments_json=comments_json,
            )
            db.session.add(ms)
            db.session.commit()
            return db.session.get(Milestone, ms.id)

    def test_returns_none_when_no_comments_json(self, app):
        """Should return None if cached_comments_json is None."""
        ms = self._make_milestone(app, comments_json=None, msx_id='no-json')
        with app.app_context():
            from app.routes.main import _days_since_last_real_comment
            from app.models import db, Milestone
            ms = db.session.get(Milestone, ms.id)
            assert _days_since_last_real_comment(ms) is None

    def test_returns_none_when_empty_array(self, app):
        """Should return None for empty comments array."""
        ms = self._make_milestone(app, comments_json='[]', msx_id='empty-arr')
        with app.app_context():
            from app.routes.main import _days_since_last_real_comment
            from app.models import db, Milestone
            ms = db.session.get(Milestone, ms.id)
            assert _days_since_last_real_comment(ms) is None

    def test_returns_none_when_only_engagement_comments(self, app):
        """Should ignore engagement comments (2099- dates)."""
        comments = json.dumps([
            {'modifiedOn': '2099-01-01T00:00:00Z', 'comment': 'Engagement note'},
        ])
        ms = self._make_milestone(app, comments_json=comments, msx_id='eng-only')
        with app.app_context():
            from app.routes.main import _days_since_last_real_comment
            from app.models import db, Milestone
            ms = db.session.get(Milestone, ms.id)
            assert _days_since_last_real_comment(ms) is None

    def test_returns_none_when_only_eng_tagged_comments(self, app):
        """Should ignore comments with eng- tag marker."""
        comments = json.dumps([
            {'modifiedOn': '2026-03-20T00:00:00Z',
             'comment': 'Some note \u00b7 eng-12345'},
        ])
        ms = self._make_milestone(app, comments_json=comments, msx_id='eng-tag')
        with app.app_context():
            from app.routes.main import _days_since_last_real_comment
            from app.models import db, Milestone
            ms = db.session.get(Milestone, ms.id)
            assert _days_since_last_real_comment(ms) is None

    def test_returns_days_for_real_comment(self, app):
        """Should return correct day count for a real comment."""
        five_days_ago = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        comments = json.dumps([
            {'modifiedOn': five_days_ago, 'comment': 'Checked in with customer'},
        ])
        ms = self._make_milestone(app, comments_json=comments, msx_id='real-5d')
        with app.app_context():
            from app.routes.main import _days_since_last_real_comment
            from app.models import db, Milestone
            ms = db.session.get(Milestone, ms.id)
            result = _days_since_last_real_comment(ms)
            assert result == 5

    def test_uses_most_recent_real_comment(self, app):
        """Should use the most recent non-engagement comment."""
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        comments = json.dumps([
            {'modifiedOn': old, 'comment': 'Old note'},
            {'modifiedOn': '2099-01-01T00:00:00Z', 'comment': 'Engagement'},
            {'modifiedOn': recent, 'comment': 'Recent note'},
        ])
        ms = self._make_milestone(app, comments_json=comments, msx_id='multi')
        with app.app_context():
            from app.routes.main import _days_since_last_real_comment
            from app.models import db, Milestone
            ms = db.session.get(Milestone, ms.id)
            result = _days_since_last_real_comment(ms)
            assert result == 3

    def test_returns_none_for_invalid_json(self, app):
        """Should handle malformed JSON gracefully."""
        ms = self._make_milestone(app, comments_json='not json', msx_id='bad-json')
        with app.app_context():
            from app.routes.main import _days_since_last_real_comment
            from app.models import db, Milestone
            ms = db.session.get(Milestone, ms.id)
            assert _days_since_last_real_comment(ms) is None


class TestFindStaleMilestones:
    """Tests for _find_stale_milestones query and filtering."""

    def _quarter_due_date(self):
        """Return a due date within the current fiscal quarter."""
        today = date.today()
        fy_month = (today.month - 7) % 12
        fq_start = (fy_month // 3) * 3
        q_start_month = ((fq_start + 7 - 1) % 12) + 1
        # Middle of the quarter
        return datetime(today.year, q_start_month, 15)

    def _create_milestone(self, db, **kwargs):
        """Helper to create a milestone with defaults."""
        from app.models import Customer, Milestone
        if 'customer_id' not in kwargs:
            milestone_key = kwargs.get('msx_milestone_id', 'local')
            customer = Customer(
                name=f"Stale Customer {milestone_key}",
                tpid=zlib.crc32(milestone_key.encode()),
            )
            db.session.add(customer)
            db.session.flush()
            kwargs['customer_id'] = customer.id
        defaults = dict(
            url='https://example.com/ms',
            msx_status='On Track',
            on_my_team=True,
            due_date=self._quarter_due_date(),
        )
        defaults.update(kwargs)
        ms = Milestone(**defaults)
        db.session.add(ms)
        db.session.flush()
        return ms

    def test_includes_milestone_with_no_comments(self, app):
        """Milestone with no cached comments should be stale."""
        with app.app_context():
            from app.models import db
            from app.routes.main import _find_stale_milestones
            ms = self._create_milestone(
                db, msx_milestone_id='stale-no-comments',
                title='No Comments MS',
                cached_comments_json='[]',
            )
            db.session.commit()
            stale = _find_stale_milestones()
            ids = [m.id for m in stale]
            assert ms.id in ids

    def test_includes_milestone_with_old_comment(self, app):
        """Milestone with comment older than 14 days should be stale."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        with app.app_context():
            from app.models import db
            from app.routes.main import _find_stale_milestones
            ms = self._create_milestone(
                db, msx_milestone_id='stale-old',
                title='Old Comment MS',
                cached_comments_json=json.dumps([
                    {'modifiedOn': old_date, 'comment': 'Old update'},
                ]),
            )
            db.session.commit()
            stale = _find_stale_milestones()
            ids = [m.id for m in stale]
            assert ms.id in ids

    def test_excludes_milestone_with_fresh_comment(self, app):
        """Milestone with recent comment should NOT be stale."""
        fresh_date = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        with app.app_context():
            from app.models import db
            from app.routes.main import _find_stale_milestones
            ms = self._create_milestone(
                db, msx_milestone_id='fresh',
                title='Fresh Comment MS',
                cached_comments_json=json.dumps([
                    {'modifiedOn': fresh_date, 'comment': 'Just updated'},
                ]),
            )
            db.session.commit()
            stale = _find_stale_milestones()
            ids = [m.id for m in stale]
            assert ms.id not in ids

    def test_excludes_blocked_milestone(self, app):
        """Blocked milestones should not appear in stale list."""
        with app.app_context():
            from app.models import db
            from app.routes.main import _find_stale_milestones
            ms = self._create_milestone(
                db, msx_milestone_id='blocked',
                title='Blocked MS',
                msx_status='Blocked',
                cached_comments_json='[]',
            )
            db.session.commit()
            stale = _find_stale_milestones()
            ids = [m.id for m in stale]
            assert ms.id not in ids

    def test_excludes_off_team_milestone(self, app):
        """Milestones where on_my_team=False should not appear."""
        with app.app_context():
            from app.models import db
            from app.routes.main import _find_stale_milestones
            ms = self._create_milestone(
                db, msx_milestone_id='off-team',
                title='Off Team MS',
                on_my_team=False,
                cached_comments_json='[]',
            )
            db.session.commit()
            stale = _find_stale_milestones()
            ids = [m.id for m in stale]
            assert ms.id not in ids

    def test_excludes_out_of_quarter_milestone(self, app):
        """Milestones due outside current fiscal quarter should not appear."""
        # 6 months from now is definitely a different quarter
        far_date = datetime.now() + timedelta(days=180)
        with app.app_context():
            from app.models import db
            from app.routes.main import _find_stale_milestones
            ms = self._create_milestone(
                db, msx_milestone_id='out-of-quarter',
                title='Future Quarter MS',
                due_date=far_date,
                cached_comments_json='[]',
            )
            db.session.commit()
            stale = _find_stale_milestones()
            ids = [m.id for m in stale]
            assert ms.id not in ids

    def test_includes_at_risk_milestone(self, app):
        """At Risk milestones should be included."""
        with app.app_context():
            from app.models import db
            from app.routes.main import _find_stale_milestones
            ms = self._create_milestone(
                db, msx_milestone_id='at-risk',
                title='At Risk MS',
                msx_status='At Risk',
                cached_comments_json='[]',
            )
            db.session.commit()
            stale = _find_stale_milestones()
            ids = [m.id for m in stale]
            assert ms.id in ids

    def test_sets_days_since_comment_attribute(self, app):
        """Each stale milestone should have days_since_comment set."""
        with app.app_context():
            from app.models import db
            from app.routes.main import _find_stale_milestones
            self._create_milestone(
                db, msx_milestone_id='attr-check',
                title='Attr Check MS',
                cached_comments_json='[]',
            )
            db.session.commit()
            stale = _find_stale_milestones()
            for ms in stale:
                assert hasattr(ms, 'days_since_comment')

    def test_sort_order_no_comments_first(self, app):
        """Milestones with no comments should sort before old comments."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        with app.app_context():
            from app.models import db
            from app.routes.main import _find_stale_milestones
            ms_none = self._create_milestone(
                db, msx_milestone_id='sort-none',
                title='No Comments Sort',
                cached_comments_json='[]',
            )
            ms_old = self._create_milestone(
                db, msx_milestone_id='sort-old',
                title='Old Comments Sort',
                cached_comments_json=json.dumps([
                    {'modifiedOn': old_date, 'comment': 'Old'},
                ]),
            )
            db.session.commit()
            stale = _find_stale_milestones()
            stale_ids = [m.id for m in stale]
            if ms_none.id in stale_ids and ms_old.id in stale_ids:
                idx_none = stale_ids.index(ms_none.id)
                idx_old = stale_ids.index(ms_old.id)
                assert idx_none < idx_old, "No-comments should sort before old-comments"

    def test_seller_mode_filters_by_seller(self, app, sample_data):
        """Seller mode should only return milestones for that seller's customers."""
        with app.app_context():
            from app.models import db
            from app.routes.main import _find_stale_milestones
            ms = self._create_milestone(
                db, msx_milestone_id='seller-filter',
                title='Seller Filter MS',
                customer_id=sample_data['customer1_id'],
                cached_comments_json='[]',
            )
            db.session.commit()
            seller_id = sample_data['seller1_id']
            stale = _find_stale_milestones(seller_mode_sid=seller_id)
            ids = [m.id for m in stale]
            assert ms.id in ids

    def test_milestone_on_last_day_of_quarter_included(self, app):
        """Milestone due on the last day of the fiscal quarter should be included."""
        today = date.today()
        fy_month = (today.month - 7) % 12
        fq_start = (fy_month // 3) * 3
        q_start_month = ((fq_start + 7 - 1) % 12) + 1
        end_month = q_start_month + 3
        end_year = today.year
        if end_month > 12:
            end_month -= 12
            end_year += 1
        last_day = date(end_year, end_month, 1) - timedelta(days=1)
        due = datetime(last_day.year, last_day.month, last_day.day)

        with app.app_context():
            from app.models import db
            from app.routes.main import _find_stale_milestones
            ms = self._create_milestone(
                db, msx_milestone_id='last-day',
                title='Last Day MS',
                due_date=due,
                cached_comments_json='[]',
            )
            db.session.commit()
            stale = _find_stale_milestones()
            ids = [m.id for m in stale]
            assert ms.id in ids


class TestStaleMilestonesOnDashboard:
    """Integration tests: stale milestones render in index template."""

    def _quarter_due_date(self):
        """Return a due date within the current fiscal quarter."""
        today = date.today()
        fy_month = (today.month - 7) % 12
        fq_start = (fy_month // 3) * 3
        q_start_month = ((fq_start + 7 - 1) % 12) + 1
        return datetime(today.year, q_start_month, 15)

    def test_stale_milestones_visible_on_index(self, client, app, sample_customer):
        """Stale milestones section should appear on dashboard."""
        with app.app_context():
            from app.models import db, Milestone
            ms = Milestone(
                url='https://example.com/ms',
                msx_milestone_id='dash-stale',
                title='Dashboard Stale MS',
                msx_status='On Track',
                on_my_team=True,
                customer_id=sample_customer.id,
                due_date=self._quarter_due_date(),
                cached_comments_json='[]',
            )
            db.session.add(ms)
            db.session.commit()

        resp = client.get('/')
        assert resp.status_code == 200
        assert b'Stale Milestones' in resp.data
        assert b'Dashboard Stale MS' in resp.data

    def test_no_stale_section_when_none_stale(self, client, app, sample_customer):
        """Stale milestones section should not appear if none are stale."""
        fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with app.app_context():
            from app.models import db, Milestone
            ms = Milestone(
                url='https://example.com/ms',
                msx_milestone_id='dash-fresh',
                title='Fresh MS',
                msx_status='On Track',
                on_my_team=True,
                customer_id=sample_customer.id,
                due_date=self._quarter_due_date(),
                cached_comments_json=json.dumps([
                    {'modifiedOn': fresh, 'comment': 'Just updated'},
                ]),
            )
            db.session.add(ms)
            db.session.commit()

        resp = client.get('/')
        assert resp.status_code == 200
        assert b'Stale Milestones' not in resp.data
