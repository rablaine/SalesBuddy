"""
Tests for the programmatic revenue sync (MSXI API path).

Focus is on the behaviour that can silently corrupt data: truncated pulls,
bucket-taxonomy transitions, and review-note preservation.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    db, Customer, CustomerRevenueData, ProductRevenueData, RevenueAnalysis,
    RevenueConfig, RevenueImport, RevenueReviewNote, UserPreference,
)
from app.services import revenue_taxonomy as tax


# ---------------------------------------------------------------------------
# Bucket reconciliation
# ---------------------------------------------------------------------------
class TestBucketReconciliation:
    def _seed_stored(self, buckets):
        imp = RevenueImport(filename="t", record_count=0)
        db.session.add(imp)
        db.session.flush()
        for b in buckets:
            db.session.add(CustomerRevenueData(
                customer_name="ACME", bucket=b, fiscal_month="FY26-Jul",
                month_date=datetime(2025, 7, 1).date(), revenue=1.0,
                last_import_id=imp.id))
        db.session.commit()

    def test_first_import_has_nothing_to_compare(self, app):
        assert tax.reconcile_buckets({"Databases"})["status"] == "first_import"

    def test_identical_taxonomy_is_unchanged(self, app):
        self._seed_stored(["Databases", "Fabric"])
        assert tax.reconcile_buckets({"Databases", "Fabric"})["status"] == "unchanged"

    def test_capitalization_only_rename_is_not_a_change(self, app):
        """`Github Copilot` became `GitHub Copilot`; that must not read as retired."""
        self._seed_stored(["Github Copilot"])
        notice = tax.reconcile_buckets({"GitHub Copilot"})
        assert notice["status"] == "unchanged"
        assert notice["removed"] == []

    def test_change_without_losing_a_selection_asks_for_review(self, app):
        self._seed_stored(["Databases", "Core DBs"])
        pref = UserPreference.query.first() or UserPreference()
        pref.compensated_buckets = json.dumps(["Databases"])
        db.session.add(pref)
        db.session.commit()

        notice = tax.reconcile_buckets({"Databases", "Fabric"})
        assert notice["status"] == "review"
        assert notice["missing_selected"] == []
        assert "Fabric" in notice["added"]
        assert "Core DBs" in notice["removed"]

    def test_losing_a_selected_bucket_triggers_reset(self, app):
        self._seed_stored(["Core DBs", "Analytics"])
        pref = UserPreference.query.first() or UserPreference()
        pref.compensated_buckets = json.dumps(["Core DBs", "Analytics"])
        db.session.add(pref)
        db.session.commit()

        notice = tax.reconcile_buckets({"Databases", "Fabric"})
        assert notice["status"] == "reset"
        assert sorted(notice["missing_selected"]) == ["Analytics", "Core DBs"]

    def test_reset_clears_selection_and_bumps_version(self, app):
        self._seed_stored(["Core DBs"])
        pref = UserPreference.query.first() or UserPreference()
        pref.compensated_buckets = json.dumps(["Core DBs"])
        pref.compensated_buckets_fiscal_year = "FY26"
        pref.compensated_buckets_confirmed_taxonomy_version = 3
        pref.bucket_taxonomy_version = 3
        db.session.add(pref)
        db.session.commit()

        tax.apply_bucket_notice(tax.reconcile_buckets({"Databases"}))

        pref = UserPreference.query.first()
        assert pref.compensated_buckets is None
        assert pref.compensated_buckets_fiscal_year is None
        assert pref.compensated_buckets_confirmed_taxonomy_version is None
        assert pref.bucket_taxonomy_version == 4  # invalidates the localStorage copy
        assert json.loads(pref.bucket_taxonomy_notice)["status"] == "reset"

    def test_review_keeps_the_selection(self, app):
        self._seed_stored(["Databases", "Core DBs"])
        pref = UserPreference.query.first() or UserPreference()
        pref.compensated_buckets = json.dumps(["Databases"])
        db.session.add(pref)
        db.session.commit()

        tax.apply_bucket_notice(tax.reconcile_buckets({"Databases", "Fabric"}))
        assert json.loads(UserPreference.query.first().compensated_buckets) == ["Databases"]

    def test_empty_selection_is_never_a_reset(self, app):
        """No selection means 'show everything', so there is nothing to lose."""
        self._seed_stored(["Core DBs", "Analytics"])
        pref = UserPreference.query.first() or UserPreference()
        pref.compensated_buckets = json.dumps([])
        db.session.add(pref)
        db.session.commit()

        notice = tax.reconcile_buckets({"Databases", "Fabric"})
        assert notice["status"] == "review"
        assert notice["missing_selected"] == []


# ---------------------------------------------------------------------------
# Review-note preservation
# ---------------------------------------------------------------------------
class TestReviewPreservation:
    def _analysis(self, name, bucket, customer_id=None, status="reviewed", notes="note"):
        a = RevenueAnalysis(
            customer_name=name, bucket=bucket, customer_id=customer_id,
            analyzed_at=datetime.now(timezone.utc), months_analyzed=6,
            avg_revenue=100.0, latest_revenue=90.0, category="DECLINING",
            recommended_action="MONITOR", confidence="LOW", priority_score=10,
            review_status=status, review_notes=notes)
        db.session.add(a)
        db.session.commit()
        return a

    def test_snapshot_ignores_untouched_rows(self, app):
        self._analysis("ACME", "Databases", status="new", notes=None)
        assert tax.snapshot_review_state() == []

    def test_snapshot_captures_notes_and_history(self, app):
        a = self._analysis("ACME", "Databases")
        db.session.add(RevenueReviewNote(analysis_id=a.id, review_status="reviewed",
                                         review_notes="earlier"))
        db.session.commit()

        snap = tax.snapshot_review_state()
        assert len(snap) == 1
        assert snap[0]["analysis_id"] == a.id
        assert len(snap[0]["history"]) == 1

    def test_surviving_bucket_and_customer_is_kept(self, app):
        c = Customer(name="ACME", tpid=123)
        db.session.add(c)
        db.session.commit()
        a = self._analysis("ACME", "Databases", customer_id=c.id)

        res = tax.classify_review_state(tax.snapshot_review_state(), {"Databases", "Fabric"})
        assert res["kept_count"] == 1
        assert a.id in res["keep_analysis_ids"]

    def test_capitalization_rename_is_kept(self, app):
        c = Customer(name="ACME", tpid=123)
        db.session.add(c)
        db.session.commit()
        self._analysis("ACME", "Github Copilot", customer_id=c.id)

        res = tax.classify_review_state(tax.snapshot_review_state(), {"GitHub Copilot"})
        assert res["kept_count"] == 1, "capitalization-only rename must not retire a note"

    def test_retired_bucket_is_dropped(self, app):
        c = Customer(name="ACME", tpid=123)
        db.session.add(c)
        db.session.commit()
        self._analysis("ACME", "Core DBs", customer_id=c.id)

        res = tax.classify_review_state(tax.snapshot_review_state(), {"Databases"})
        assert res["dropped_count"] == 1
        assert res["dropped"][0]["reason"] == "bucket_retired"

    def test_missing_customer_is_dropped(self, app):
        self._analysis("GHOST ACCOUNT", "Databases", customer_id=None)
        res = tax.classify_review_state(tax.snapshot_review_state(), {"Databases"})
        assert res["dropped_count"] == 1
        assert res["dropped"][0]["reason"] == "customer_missing"

    def test_account_without_current_revenue_is_still_kept(self, app):
        """A coverage lapse must not destroy hand-written notes."""
        c = Customer(name="ACME", tpid=123)
        db.session.add(c)
        db.session.commit()
        self._analysis("ACME", "Databases", customer_id=c.id)
        # No CustomerRevenueData at all for this account.
        res = tax.classify_review_state(tax.snapshot_review_state(), {"Databases"})
        assert res["kept_count"] == 1


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------
class TestPurge:
    def test_purge_preserves_kept_analyses_and_config(self, app):
        cfg = RevenueConfig(min_revenue_for_outreach=4242)
        db.session.add(cfg)
        imp = RevenueImport(filename="t", record_count=0)
        db.session.add(imp)
        db.session.flush()
        db.session.add(CustomerRevenueData(
            customer_name="ACME", bucket="Databases", fiscal_month="FY26-Jul",
            month_date=datetime(2025, 7, 1).date(), revenue=5.0, last_import_id=imp.id))
        keep = RevenueAnalysis(
            customer_name="ACME", bucket="Databases", analyzed_at=datetime.now(timezone.utc),
            months_analyzed=6, avg_revenue=1.0, latest_revenue=1.0, category="HEALTHY",
            recommended_action="NO ACTION", confidence="LOW", priority_score=1,
            review_status="reviewed", review_notes="keep me")
        drop = RevenueAnalysis(
            customer_name="OTHER", bucket="Core DBs", analyzed_at=datetime.now(timezone.utc),
            months_analyzed=6, avg_revenue=1.0, latest_revenue=1.0, category="HEALTHY",
            recommended_action="NO ACTION", confidence="LOW", priority_score=1)
        db.session.add_all([keep, drop])
        db.session.commit()
        db.session.add(RevenueReviewNote(analysis_id=keep.id, review_status="reviewed",
                                         review_notes="history"))
        db.session.commit()

        counts = tax.purge_revenue_data({keep.id})

        assert counts["customer_rows"] == 1
        assert RevenueAnalysis.query.count() == 1
        assert RevenueAnalysis.query.first().review_notes == "keep me"
        assert RevenueReviewNote.query.count() == 1, "kept analysis keeps its history"
        assert RevenueConfig.query.first().min_revenue_for_outreach == 4242, \
            "purge must not touch the user's thresholds"
        assert RevenueImport.query.count() == 1, \
            "the import log is the user's visible history and must survive a re-sync"

    def test_purge_without_keeps_removes_everything(self, app):
        a = RevenueAnalysis(
            customer_name="ACME", bucket="Core DBs", analyzed_at=datetime.now(timezone.utc),
            months_analyzed=6, avg_revenue=1.0, latest_revenue=1.0, category="HEALTHY",
            recommended_action="NO ACTION", confidence="LOW", priority_score=1)
        db.session.add(a)
        db.session.commit()

        tax.purge_revenue_data(set())
        assert RevenueAnalysis.query.count() == 0


# ---------------------------------------------------------------------------
# Pull safety
# ---------------------------------------------------------------------------
class TestPullSafety:
    def test_uses_azure_blue_subscription_report(self):
        """Revenue pulls must use the report with complete account coverage."""
        from app.services import revenue_pull

        assert revenue_pull._REPORT_ID == "4774bb5f-91a6-4e41-8c8a-0cee2142b765"
        assert revenue_pull._DATASET_ID == "f7ecc250-c244-43a6-aea5-7a957f9e9d38"
        assert revenue_pull._MODEL_ID_FALLBACK == 6642435

    def test_mwc_mint_retries_transient_ssl_errors(self, monkeypatch):
        """Power BI bootstrap must ride out transient TLS disconnects."""
        import requests

        from app.services import revenue_pull

        token = "eyJabcdefghijk.eyJabcdefghijk.abcdefghijk"

        class FakeResponse:
            ok = True
            text = token

            @staticmethod
            def json():
                return {"models": []}

        class FakeSession:
            calls = 0

            def get(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise requests.exceptions.SSLError("unexpected EOF")
                if self.calls == 2:
                    raise requests.exceptions.ReadTimeout("TLS handshake timed out")
                return FakeResponse()

        session = FakeSession()
        revenue_pull.clear_token_cache()
        monkeypatch.setattr(revenue_pull, "_get_pbi_token", lambda: "pbi-token")
        monkeypatch.setattr(revenue_pull, "_is_mwc", lambda value: value == token)
        monkeypatch.setattr(revenue_pull, "_qes_url_from_mwc", lambda value: "https://qes.invalid")
        monkeypatch.setattr(revenue_pull.time, "sleep", lambda seconds: None)

        assert revenue_pull._mint_mwc(session) == token
        assert session.calls == 3
        revenue_pull.clear_token_cache()

    def test_truncated_response_without_a_cursor_raises(self, monkeypatch):
        """A partial dataset must never be silently accepted."""
        from app.services import revenue_pull

        class FakeResponse:
            status_code = 200
            ok = True
            # IC=false says "truncated" but no RT means we cannot page.
            text = json.dumps({"results": [{"result": {"data": {
                "descriptor": {"Select": [{"Name": "tpid", "Value": "G0"}]},
                "dsr": {"DS": [{"PH": [{"DM0": [{"S": [{"N": "G0"}], "G0": 1}]}],
                                "IC": False}]},
            }}}]})

        class FakeSession:
            def post(self, *a, **k):
                return FakeResponse()

        monkeypatch.setattr(revenue_pull, "_mint_mwc", lambda s: "tok")
        monkeypatch.setattr(revenue_pull, "_qes_url", "https://example.invalid/q")
        with pytest.raises(revenue_pull.RevenuePullError, match="partial"):
            revenue_pull._qes_post(FakeSession(), {"Select": [{"Name": "tpid"}]}, retries=0)

    def test_pagination_deduplicates_repeated_boundary_row(self, monkeypatch):
        """A restart page may repeat its preceding boundary row."""
        from app.services import revenue_pull

        repeated = {"tpid": 1, "fm": "FY26-Feb", "product": "Event Hubs", "acr": 61.906}
        pages = [
            ([repeated], [["restart"]]),
            ([repeated, {**repeated, "fm": "FY26-Mar"}], None),
        ]
        monkeypatch.setattr(
            revenue_pull,
            "_qes_post_page",
            lambda session, query, restart=None, retries=2: pages.pop(0),
        )

        rows = revenue_pull._qes_post(object(), {})

        assert rows == [repeated, {**repeated, "fm": "FY26-Mar"}]

    def test_default_fiscal_years_spans_three_years(self):
        from app.services.revenue_pull import default_fiscal_years
        fys = default_fiscal_years()
        assert len(fys) == 3, "two prior fiscal years plus the current one"
        assert all(f.startswith("FY") for f in fys)


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------
class TestPullProgress:
    """The pulls are the longest phases, so they must report as batches land."""

    def test_batch_events_are_forwarded_as_progress(self):
        from app.services.revenue_sync import _pull_with_progress

        def fake_pull(tpids, fiscal_years=None, progress=None):
            progress(0, 4, 0)  # emitted once the MWC handshake lands
            for i in range(1, 5):
                progress(i, 4, i * 100)
            return ["row"] * 400

        gen = _pull_with_progress(fake_pull, [1, 2], ["FY27"],
                                  "pull_buckets", 8, 30, "Pulling", lambda: None)
        events = []
        try:
            while True:
                events.append(next(gen))
        except StopIteration as stop:
            rows = stop.value

        assert len(rows) == 400, "the pulled rows still come back to the caller"
        assert len(events) == 5, "the handshake tick plus one per batch"
        assert [e["progress"] for e in events] == [8, 13, 19, 24, 30], \
            "batches map across the phase's span"
        assert all(e["phase"] == "pull_buckets" for e in events)
        assert "running 4 batches" in events[0]["message"]
        assert "4/4 batches" in events[-1]["message"]

    def test_a_failing_pull_raises_instead_of_hanging(self):
        """A worker that dies must surface, not block the stream forever."""
        from app.services.revenue_sync import _pull_with_progress

        def boom(tpids, fiscal_years=None, progress=None):
            progress(1, 2, 10)
            raise RuntimeError("MSXI said no")

        gen = _pull_with_progress(boom, [1], ["FY27"],
                                  "pull_buckets", 8, 30, "Pulling", lambda: None)
        with pytest.raises(RuntimeError, match="MSXI said no"):
            while True:
                next(gen)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
class TestWeeklyCadence:
    """Revenue rides along with the milestone sync but only runs weekly."""

    def test_due_when_never_run(self, app):
        from app.services.scheduled_sync import _revenue_sync_due
        assert _revenue_sync_due() is True

    def test_not_due_the_next_day(self, app):
        from app.models import SyncStatus
        from app.services.scheduled_sync import _revenue_sync_due

        SyncStatus.mark_started('revenue_sync')
        SyncStatus.mark_completed('revenue_sync', success=True)
        assert _revenue_sync_due() is False

    def test_due_again_after_a_week(self, app):
        from app.models import SyncStatus
        from app.services.scheduled_sync import _revenue_sync_due

        SyncStatus.mark_started('revenue_sync')
        SyncStatus.mark_completed('revenue_sync', success=True)
        row = SyncStatus.query.filter_by(sync_type='revenue_sync').first()
        row.completed_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=8)
        db.session.commit()

        assert _revenue_sync_due() is True
