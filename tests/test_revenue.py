"""
Tests for revenue queries and analysis functionality.
"""
import pytest
from datetime import date, datetime
from io import BytesIO

from app.models import (
    db, RevenueImport, CustomerRevenueData, RevenueAnalysis, 
    RevenueConfig, Customer, User
)
from app.services.revenue_import import fiscal_month_to_date
from app.services.revenue_analysis import (
    compute_signals, categorize_customer, determine_action,
    run_analysis_for_all, get_actionable_analyses, AnalysisConfig,
    _normalize_revenues
)


class TestFiscalMonthConversion:
    """Test fiscal month to date conversion."""
    
    def test_fiscal_month_to_date_first_half(self):
        # FY26-Jul = July 2025 (FY26 starts July 2025)
        assert fiscal_month_to_date("FY26-Jul") == date(2025, 7, 1)
        assert fiscal_month_to_date("FY26-Aug") == date(2025, 8, 1)
        assert fiscal_month_to_date("FY26-Dec") == date(2025, 12, 1)
    
    def test_fiscal_month_to_date_second_half(self):
        # FY26-Jan through Jun = calendar 2026
        assert fiscal_month_to_date("FY26-Jan") == date(2026, 1, 1)
        assert fiscal_month_to_date("FY26-Jun") == date(2026, 6, 1)
    
    def test_fiscal_month_to_date_invalid(self):
        assert fiscal_month_to_date("invalid") is None
        assert fiscal_month_to_date("FY26-Xyz") is None
        assert fiscal_month_to_date("") is None


class TestSignalComputation:
    """Test revenue signal computation."""
    
    def test_compute_signals_basic(self):
        """Test basic signal computation."""
        revenues = [10000, 11000, 12000, 13000, 14000, 15000]
        months = ["FY26-Jul", "FY26-Aug", "FY26-Sep", "FY26-Oct", "FY26-Nov", "FY26-Dec"]
        
        signals = compute_signals(
            customer_name="Test Customer",
            bucket="Core DBs",
            revenues=revenues,
            month_names=months
        )
        
        assert signals is not None
        assert signals.avg_revenue > 0
        assert signals.trend_slope > 0  # Growing revenue
        assert signals.customer_name == "Test Customer"
        assert signals.bucket == "Core DBs"
    
    def test_compute_signals_declining(self):
        """Test signals for declining revenue."""
        revenues = [15000, 14000, 13000, 12000, 11000, 10000]
        months = ["FY26-Jul", "FY26-Aug", "FY26-Sep", "FY26-Oct", "FY26-Nov", "FY26-Dec"]
        
        signals = compute_signals(
            customer_name="Declining Customer",
            bucket="Core DBs",
            revenues=revenues,
            month_names=months
        )
        
        assert signals is not None
        assert signals.trend_slope < 0  # Declining
    
    def test_compute_signals_insufficient_data(self):
        """Test that insufficient data returns None."""
        # Only 2 data points
        revenues = [10000, 11000]
        months = ["FY26-Jul", "FY26-Aug"]
        
        signals = compute_signals(
            customer_name="Short History",
            bucket="Core DBs",
            revenues=revenues,
            month_names=months
        )
        
        assert signals is None
    
    def test_compute_signals_low_revenue(self):
        """Test that very low revenue customers get computed but won't be actionable."""
        revenues = [100, 100, 100, 100, 100, 100]  # Below action threshold
        months = ["FY26-Jul", "FY26-Aug", "FY26-Sep", "FY26-Oct", "FY26-Nov", "FY26-Dec"]
        
        signals = compute_signals(
            customer_name="Tiny Customer",
            bucket="Core DBs",
            revenues=revenues,
            month_names=months
        )
        
        # Signals are computed, but action will be NO ACTION due to low revenue
        assert signals is not None
        assert signals.avg_revenue == 100.0


class TestCategorization:
    """Test customer categorization logic."""
    
    def test_categorize_DECLINING(self):
        """Test declining categorization."""
        revenues = [15000, 14000, 13000, 11000, 9000, 7000]  # Sharp decline
        months = ["FY26-Jul", "FY26-Aug", "FY26-Sep", "FY26-Oct", "FY26-Nov", "FY26-Dec"]
        
        signals = compute_signals("Churning", "Core DBs", revenues, months)
        
        assert signals is not None
        assert signals.category in ["DECLINING", "VOLATILE"]
    
    def test_categorize_expansion(self):
        """Test expansion opportunity categorization."""
        revenues = [10000, 11500, 13000, 15000, 17000, 19500]  # Strong growth
        months = ["FY26-Jul", "FY26-Aug", "FY26-Sep", "FY26-Oct", "FY26-Nov", "FY26-Dec"]
        
        signals = compute_signals("Growing", "Core DBs", revenues, months)
        
        assert signals is not None
        assert signals.category in ["EXPANSION_OPPORTUNITY", "VOLATILE"]
    
    def test_categorize_healthy(self):
        """Test healthy/stable categorization."""
        # More stable data with less variation
        revenues = [10000, 10000, 10000, 10000, 10000, 10000]  # Perfectly stable
        months = ["FY26-Jul", "FY26-Aug", "FY26-Sep", "FY26-Oct", "FY26-Nov", "FY26-Dec"]
        
        signals = compute_signals("Stable", "Core DBs", revenues, months)
        
        assert signals is not None
        # Perfectly flat data should be STAGNANT (flat + low vol)
        assert signals.category in ["HEALTHY", "STAGNANT"]


class TestDayNormalization:
    """Test day-normalization for consumption billing accuracy."""

    def test_normalize_revenues(self):
        """Test that normalization adjusts for days in month."""
        revenues = [31000, 28000, 31000]  # $1k/day constant
        month_dates = [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)]

        normed = _normalize_revenues(revenues, month_dates)
        # All should be very close to 30000 (30 standard days * $1k/day)
        for val in normed:
            assert abs(val - 30000) < 100

    def test_stable_customer_not_volatile_with_dates(self):
        """A constant-daily-rate customer must NOT be classified as VOLATILE."""
        daily_rate = 1000
        month_dates = [
            date(2025, 7, 1), date(2025, 8, 1), date(2025, 9, 1),
            date(2025, 10, 1), date(2025, 11, 1), date(2025, 12, 1),
            date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1),
        ]
        import calendar
        revenues = [daily_rate * calendar.monthrange(d.year, d.month)[1] for d in month_dates]
        months = [f"M{i}" for i in range(len(revenues))]

        signals = compute_signals(
            "Stable Daily", "Core DBs", revenues, months,
            month_dates=month_dates,
        )

        assert signals is not None
        assert signals.category in ["HEALTHY", "STAGNANT"]
        assert signals.volatility_cv < 0.10  # Near-zero volatility

    def test_stable_customer_volatile_without_dates(self):
        """Without day-normalization, a stable customer exceeds the CV threshold."""
        daily_rate = 1000
        month_dates = [
            date(2025, 7, 1), date(2025, 8, 1), date(2025, 9, 1),
            date(2025, 10, 1), date(2025, 11, 1), date(2025, 12, 1),
            date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1),
        ]
        import calendar
        revenues = [daily_rate * calendar.monthrange(d.year, d.month)[1] for d in month_dates]
        months = [f"M{i}" for i in range(len(revenues))]

        # Without month_dates, no normalization occurs
        signals = compute_signals(
            "Stable Daily", "Core DBs", revenues, months,
        )

        assert signals is not None
        # CV should be inflated by calendar noise
        assert signals.volatility_cv > 0.50

    def test_real_decline_still_detected_with_normalization(self):
        """A genuinely declining customer should still be flagged even with normalization."""
        month_dates = [
            date(2025, 7, 1), date(2025, 8, 1), date(2025, 9, 1),
            date(2025, 10, 1), date(2025, 11, 1), date(2025, 12, 1),
            date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1),
        ]
        revenues = [50000, 45000, 40000, 35000, 30000, 25000, 20000, 15000, 10000]
        months = [f"M{i}" for i in range(len(revenues))]

        signals = compute_signals(
            "Truly Declining", "Core DBs", revenues, months,
            month_dates=month_dates,
        )

        assert signals is not None
        assert signals.category == "DECLINING"
        assert signals.trend_slope < -5.0


class TestCollapseCheck:
    """Test the tightened collapse threshold."""

    def test_collapse_requires_meaningful_slope(self):
        """Revenue below 70% of peak with a barely negative slope should NOT trigger DECLINING."""
        # One high month creates a peak; remaining months are stable.
        # current_vs_max ~63%, but the slope is only ~-1.5%/mo (above -3.0 threshold).
        revenues = [10000, 15000, 10000, 10000, 10000, 10000, 10000, 10000, 10000]
        months = [f"M{i}" for i in range(len(revenues))]

        signals = compute_signals("Mild Dip", "Core DBs", revenues, months)

        assert signals is not None
        # Should NOT be DECLINING with a mild slope
        assert signals.category != "DECLINING"

    def test_collapse_fires_with_strong_slope(self):
        """Revenue below 70% of peak with steep decline should be DECLINING."""
        revenues = [50000, 48000, 44000, 40000, 35000, 30000, 25000, 20000, 15000]
        months = [f"M{i}" for i in range(len(revenues))]

        signals = compute_signals("Real Collapse", "Core DBs", revenues, months)

        assert signals is not None
        assert signals.category == "DECLINING"


class TestActionDetermination:
    """Test action recommendation logic."""
    
    def test_action_below_threshold(self):
        """Test that low revenue customers get no action."""
        revenues = [1000, 1100, 1200, 1300, 1400, 1500]  # Below default 3000 threshold
        months = ["FY26-Jul", "FY26-Aug", "FY26-Sep", "FY26-Oct", "FY26-Nov", "FY26-Dec"]
        
        signals = compute_signals("Small Customer", "Core DBs", revenues, months)
        assert signals is not None
        
        config = AnalysisConfig()
        signals = determine_action(signals, config)
        
        assert signals.recommended_action == "NO ACTION"
    
    def test_action_high_risk(self):
        """Test that high risk customers get check-in action."""
        revenues = [50000, 45000, 40000, 35000, 30000, 20000]  # Large decline
        months = ["FY26-Jul", "FY26-Aug", "FY26-Sep", "FY26-Oct", "FY26-Nov", "FY26-Dec"]
        
        signals = compute_signals("At Risk Customer", "Core DBs", revenues, months)
        assert signals is not None
        
        config = AnalysisConfig()
        signals = determine_action(signals, config)
        
        assert "CHECK-IN" in signals.recommended_action
        assert signals.dollars_at_risk > 0


class TestRevenueRoutes:
    """Test revenue route endpoints."""
    
    def test_revenue_dashboard_empty(self, client):
        """Test dashboard with no data."""
        response = client.get('/reports/revenue')
        assert response.status_code == 200
        assert b'Revenue Analyzer' in response.data
    
    def test_revenue_import_page(self, client):
        """Test import page loads."""
        response = client.get('/revenue/import')
        assert response.status_code == 200
        assert b'Import Revenue Data' in response.data
    
    def test_revenue_config_page(self, client):
        """Test config page loads."""
        response = client.get('/revenue/config')
        assert response.status_code == 200
        assert b'Configuration' in response.data
    
    def test_review_creates_history(self, app, client):
        """Test that saving a review creates a RevenueReviewNote history entry."""
        from app.models import RevenueAnalysis, RevenueReviewNote

        with app.app_context():
            analysis = RevenueAnalysis(
                customer_name='Review History Test',
                bucket='Azure',
                months_analyzed=6,
                avg_revenue=10000,
                latest_revenue=9000,
                category='DECLINING',
                recommended_action='CHECK-IN (Urgent)',
                confidence='HIGH',
                priority_score=80,
            )
            db.session.add(analysis)
            db.session.commit()
            aid = analysis.id

        resp = client.patch(
            f'/api/revenue/analysis/{aid}/review',
            json={'review_status': 'reviewed', 'review_notes': 'Seasonal dip'},
            content_type='application/json',
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success']
        assert data['review_status'] == 'reviewed'
        assert len(data['history']) == 1
        assert data['history'][0]['review_status'] == 'reviewed'
        assert data['history'][0]['review_notes'] == 'Seasonal dip'

        # Second review appends
        resp2 = client.patch(
            f'/api/revenue/analysis/{aid}/review',
            json={'review_status': 'actioned', 'review_notes': 'Talked to DSS'},
            content_type='application/json',
        )
        data2 = resp2.get_json()
        assert len(data2['history']) == 2
        assert data2['history'][0]['review_status'] == 'actioned'  # newest first

    def test_review_history_api(self, app, client):
        """Test the review history GET endpoint."""
        from app.models import RevenueAnalysis, RevenueReviewNote

        with app.app_context():
            analysis = RevenueAnalysis(
                customer_name='History API Test',
                bucket='Azure',
                months_analyzed=6,
                avg_revenue=5000,
                latest_revenue=4000,
                category='RECENT_DIP',
                recommended_action='MONITOR',
                confidence='MEDIUM',
                priority_score=40,
            )
            db.session.add(analysis)
            db.session.commit()
            aid = analysis.id

        resp = client.get(f'/api/revenue/analysis/{aid}/review-history')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['history'] == []

    def test_seller_view_with_revenue_alerts(self, app, client, test_user):
        """Test seller page renders correctly when revenue analysis exists."""
        from app.models import Seller, Customer, RevenueAnalysis
        
        with app.app_context():
            # Create a seller and customer
            seller = Seller(name='Test Seller')
            db.session.add(seller)
            db.session.flush()
            
            customer = Customer(name='Test Revenue Customer', tpid=99999, seller_id=seller.id)
            db.session.add(customer)
            db.session.flush()
            
            # Create a revenue analysis for this customer
            analysis = RevenueAnalysis(
                customer_name='Test Revenue Customer',
                bucket='Core DBs',
                seller_name='Test Seller',
                customer_id=customer.id,
                category='DECLINING',
                recommended_action='CHECK-IN',
                avg_revenue=10000,
                latest_revenue=8000,
                priority_score=75,
                dollars_at_risk=5000,
                months_analyzed=6,
                confidence='HIGH'
            )
            db.session.add(analysis)
            db.session.commit()
            
            # Now test the seller page loads with revenue analysis
            response = client.get(f'/seller/{seller.id}')
            assert response.status_code == 200
            assert b'Revenue Analyzer' in response.data
    
    def test_customer_view_with_revenue_analysis(self, app, client, test_user):
        """Test customer page renders correctly when revenue analysis exists."""
        from app.models import Seller, Customer, RevenueAnalysis
        
        with app.app_context():
            # Create a customer
            customer = Customer(name='Revenue Test Customer', tpid=88888)
            db.session.add(customer)
            db.session.flush()
            
            # Create a revenue analysis
            analysis = RevenueAnalysis(
                customer_name='Revenue Test Customer',
                bucket='Analytics',
                category='EXPANSION_OPPORTUNITY',
                recommended_action='UPSELL',
                avg_revenue=15000,
                latest_revenue=18000,
                priority_score=60,
                dollars_opportunity=3000,
                months_analyzed=6,
                confidence='MEDIUM'
            )
            db.session.add(analysis)
            db.session.commit()
            
            # Test customer page loads with revenue card
            response = client.get(f'/customer/{customer.id}')
            assert response.status_code == 200
            assert b'Revenue Analyzer' in response.data


class TestDatabaseAnalysis:
    """Test full database analysis flow."""
    
    def test_run_analysis_empty_database(self, app, test_user):
        """Test analysis with no data."""
        with app.app_context():
            stats = run_analysis_for_all()
            
            assert stats['analyzed'] == 0
            assert stats['actionable'] == 0
    
    def test_run_analysis_with_data(self, app, test_user):
        """Test analysis after revenue lands in the database."""
        months = ["FY26-Jul", "FY26-Aug", "FY26-Sep", "FY26-Oct", "FY26-Nov", "FY26-Dec"]
        series = {
            "Healthy Customer": [10000, 10100, 10050, 10200, 10150, 10250],
            "At Risk Customer": [20000, 18000, 16000, 14000, 12000, 10000],
        }

        with app.app_context():
            imp = RevenueImport(filename='sync', record_count=len(months) * len(series))
            db.session.add(imp)
            db.session.flush()
            for name, revenues in series.items():
                for fm, revenue in zip(months, revenues):
                    db.session.add(CustomerRevenueData(
                        customer_name=name,
                        bucket="Core DBs",
                        fiscal_month=fm,
                        month_date=fiscal_month_to_date(fm),
                        revenue=revenue,
                        last_import_id=imp.id,
                    ))
            db.session.commit()

            stats = run_analysis_for_all()

            assert stats['analyzed'] >= 1

            # Check analyses were created
            analyses = RevenueAnalysis.query.all()
            assert len(analyses) >= 1


class TestPartialMonthExclusion:
    """Don't include the most recent month in the analysis.

    MSXI keeps reporting into it, so its number is still climbing.
    """

    def _seed(self, months, revenues):
        imp = RevenueImport(filename='sync', record_count=len(months))
        db.session.add(imp)
        db.session.flush()
        for fm, revenue in zip(months, revenues):
            db.session.add(CustomerRevenueData(
                customer_name='Partial Month Co',
                bucket='Core DBs',
                fiscal_month=fm,
                month_date=fiscal_month_to_date(fm),
                revenue=revenue,
                last_import_id=imp.id,
            ))
        db.session.commit()

    def test_newest_month_is_excluded(self, app, test_user):
        """Oct is mid-month and has only trickled in, so it must not be analyzed."""
        with app.app_context():
            self._seed(
                ["FY26-Jul", "FY26-Aug", "FY26-Sep", "FY26-Oct"],
                [50000, 51000, 52000, 4000],
            )
            run_analysis_for_all()

            a = RevenueAnalysis.query.filter_by(customer_name='Partial Month Co').first()
            assert a is not None
            assert a.months_analyzed == 3
            # Sep, not the partial Oct that would read as a collapse
            assert a.latest_revenue == 52000

    def test_month_becomes_analyzable_once_the_next_one_appears(self, app, test_user):
        """Oct finished reporting and Nov started, so Oct is now fair game."""
        with app.app_context():
            self._seed(
                ["FY26-Jul", "FY26-Aug", "FY26-Sep", "FY26-Oct", "FY26-Nov"],
                [50000, 51000, 52000, 53000, 3000],
            )
            run_analysis_for_all()

            a = RevenueAnalysis.query.filter_by(customer_name='Partial Month Co').first()
            assert a is not None
            assert a.months_analyzed == 4
            assert a.latest_revenue == 53000


class TestReviewAPI:
    """Test the PATCH /api/revenue/analysis/<id>/review endpoint."""

    def _create_analysis(self, app):
        """Helper: create a RevenueAnalysis and return its id."""
        with app.app_context():
            a = RevenueAnalysis(
                customer_name='Review Test Customer',
                bucket='Core DBs',
                category='DECLINING',
                recommended_action='CHECK-IN',
                avg_revenue=5000,
                latest_revenue=4000,
                priority_score=70,
                months_analyzed=6,
                confidence='HIGH',
            )
            db.session.add(a)
            db.session.commit()
            return a.id

    def test_review_update_status(self, app, client, test_user):
        """PATCH with valid status updates the analysis."""
        aid = self._create_analysis(app)
        resp = client.patch(f'/api/revenue/analysis/{aid}/review',
                            json={'review_status': 'reviewed', 'review_notes': 'Seasonal dip'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['review_status'] == 'reviewed'
        assert data['review_notes'] == 'Seasonal dip'
        assert data['reviewed_at'] is not None

    def test_review_update_to_be_reviewed(self, app, client, test_user):
        """Mark as to_be_reviewed."""
        aid = self._create_analysis(app)
        resp = client.patch(f'/api/revenue/analysis/{aid}/review',
                            json={'review_status': 'to_be_reviewed',
                                  'review_notes': 'Need to check with seller'})
        assert resp.status_code == 200
        assert resp.get_json()['review_status'] == 'to_be_reviewed'

    def test_review_update_actioned(self, app, client, test_user):
        """Mark as actioned with notes."""
        aid = self._create_analysis(app)
        resp = client.patch(f'/api/revenue/analysis/{aid}/review',
                            json={'review_status': 'actioned', 'review_notes': 'Set up meeting with DSS'})
        assert resp.status_code == 200
        assert resp.get_json()['review_status'] == 'actioned'

    def test_review_update_dismissed(self, app, client, test_user):
        """Mark as dismissed."""
        aid = self._create_analysis(app)
        resp = client.patch(f'/api/revenue/analysis/{aid}/review',
                            json={'review_status': 'dismissed', 'review_notes': 'False positive'})
        assert resp.status_code == 200
        assert resp.get_json()['review_status'] == 'dismissed'

    def test_review_reset_to_new(self, app, client, test_user):
        """Can reset back to new."""
        aid = self._create_analysis(app)
        client.patch(f'/api/revenue/analysis/{aid}/review',
                     json={'review_status': 'reviewed', 'review_notes': 'test'})
        resp = client.patch(f'/api/revenue/analysis/{aid}/review',
                            json={'review_status': 'new', 'review_notes': ''})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['review_status'] == 'new'
        assert data['review_notes'] is None

    def test_review_invalid_status(self, app, client, test_user):
        """Invalid status returns 400."""
        aid = self._create_analysis(app)
        resp = client.patch(f'/api/revenue/analysis/{aid}/review',
                            json={'review_status': 'bogus'})
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False

    def test_review_not_found(self, client, test_user):
        """Non-existent analysis returns 404."""
        resp = client.patch('/api/revenue/analysis/99999/review',
                            json={'review_status': 'reviewed'})
        assert resp.status_code == 404

    def test_review_persists_in_db(self, app, client, test_user):
        """Review status persists when re-read from DB."""
        aid = self._create_analysis(app)
        client.patch(f'/api/revenue/analysis/{aid}/review',
                     json={'review_status': 'actioned', 'review_notes': 'Called customer'})
        with app.app_context():
            a = RevenueAnalysis.query.get(aid)
            assert a.review_status == 'actioned'
            assert a.review_notes == 'Called customer'
            assert a.reviewed_at is not None

    def test_review_badge_on_seller_alerts(self, app, client, test_user):
        """Seller alerts page shows review badge."""
        with app.app_context():
            from app.models import Seller, Customer
            seller = Seller(name='Badge Test Seller')
            db.session.add(seller)
            db.session.flush()
            cust = Customer(name='Badge Test Cust', tpid=11111, seller_id=seller.id)
            db.session.add(cust)
            db.session.flush()
            a = RevenueAnalysis(
                customer_name='Badge Test Cust', bucket='Core DBs',
                seller_name='Badge Test Seller', customer_id=cust.id,
                category='DECLINING', recommended_action='CHECK-IN',
                avg_revenue=5000, latest_revenue=4000, priority_score=70,
                months_analyzed=6, confidence='HIGH', review_status='reviewed',
                review_notes='Expected seasonal dip',
            )
            db.session.add(a)
            db.session.commit()
        resp = client.get('/revenue/seller/Badge Test Seller')
        assert resp.status_code == 200
        assert b'Reviewed' in resp.data

    def test_dashboard_shows_review_column(self, app, client, test_user):
        """Dashboard table has the Status column header."""
        resp = client.get('/reports/revenue')
        assert resp.status_code == 200
        assert b'Status' in resp.data

    def test_review_clears_previous_review_fields(self, app, client, test_user):
        """Manual review clears previous_review_status/notes hint."""
        aid = self._create_analysis(app)
        with app.app_context():
            a = RevenueAnalysis.query.get(aid)
            a.previous_review_status = 'dismissed'
            a.previous_review_notes = 'Old note'
            db.session.commit()
        resp = client.patch(f'/api/revenue/analysis/{aid}/review',
                            json={'review_status': 'reviewed', 'review_notes': 'Re-reviewed'})
        assert resp.status_code == 200
        with app.app_context():
            a = RevenueAnalysis.query.get(aid)
            assert a.review_status == 'reviewed'
            assert a.previous_review_status is None
            assert a.previous_review_notes is None


class TestAutoResetOnReimport:
    """Test that reviewed/actioned/dismissed alerts auto-reset when conditions change."""

    def _create_analysis(self, app, **overrides):
        """Helper: create a RevenueAnalysis with sensible defaults."""
        defaults = dict(
            customer_name='Auto Reset Customer',
            bucket='Core DBs',
            category='DECLINING',
            recommended_action='CHECK-IN',
            avg_revenue=5000,
            latest_revenue=4000,
            priority_score=50,
            months_analyzed=6,
            confidence='HIGH',
            review_status='to_be_reviewed',
        )
        defaults.update(overrides)
        with app.app_context():
            a = RevenueAnalysis(**defaults)
            db.session.add(a)
            db.session.commit()
            return a.id

    def test_category_change_resets_reviewed(self, app):
        """Reviewed alert resets to to_be_reviewed when category changes."""
        aid = self._create_analysis(app, review_status='reviewed',
                                    review_notes='Seasonal dip')
        with app.app_context():
            a = RevenueAnalysis.query.get(aid)
            # Simulate upsert with category change
            a.previous_review_status = None
            old_status = a.review_status
            old_notes = a.review_notes
            category_changed = 'GROWTH_OPPORTUNITY' != a.category
            priority_jumped = False
            if old_status in ('reviewed', 'actioned', 'dismissed'):
                if category_changed or priority_jumped:
                    a.previous_review_status = old_status
                    a.previous_review_notes = old_notes
                    a.review_status = 'to_be_reviewed'
                    a.review_notes = None
            a.category = 'GROWTH_OPPORTUNITY'
            db.session.commit()

            a = RevenueAnalysis.query.get(aid)
            assert a.review_status == 'to_be_reviewed'
            assert a.review_notes is None
            assert a.previous_review_status == 'reviewed'
            assert a.previous_review_notes == 'Seasonal dip'

    def test_priority_jump_resets_actioned(self, app):
        """Actioned alert resets when priority jumps 15+ points."""
        aid = self._create_analysis(app, review_status='actioned',
                                    review_notes='Called customer',
                                    priority_score=50)
        with app.app_context():
            a = RevenueAnalysis.query.get(aid)
            old_status = a.review_status
            old_notes = a.review_notes
            new_priority = 66  # jump of 16
            category_changed = False
            priority_jumped = (new_priority - a.priority_score) >= 15
            if old_status in ('reviewed', 'actioned', 'dismissed'):
                if category_changed or priority_jumped:
                    a.previous_review_status = old_status
                    a.previous_review_notes = old_notes
                    a.review_status = 'to_be_reviewed'
                    a.review_notes = None
            a.priority_score = new_priority
            db.session.commit()

            a = RevenueAnalysis.query.get(aid)
            assert a.review_status == 'to_be_reviewed'
            assert a.previous_review_status == 'actioned'
            assert a.previous_review_notes == 'Called customer'

    def test_small_priority_change_no_reset(self, app):
        """Priority increase of less than 15 does not trigger reset."""
        aid = self._create_analysis(app, review_status='dismissed',
                                    review_notes='False positive',
                                    priority_score=50)
        with app.app_context():
            a = RevenueAnalysis.query.get(aid)
            old_status = a.review_status
            new_priority = 60  # jump of 10, below threshold
            category_changed = False
            priority_jumped = (new_priority - a.priority_score) >= 15
            if old_status in ('reviewed', 'actioned', 'dismissed'):
                if category_changed or priority_jumped:
                    a.previous_review_status = old_status
                    a.previous_review_notes = a.review_notes
                    a.review_status = 'to_be_reviewed'
                    a.review_notes = None
            a.priority_score = new_priority
            db.session.commit()

            a = RevenueAnalysis.query.get(aid)
            assert a.review_status == 'dismissed'
            assert a.review_notes == 'False positive'
            assert a.previous_review_status is None

    def test_new_status_not_reset(self, app):
        """Alerts with 'new' or 'to_be_reviewed' status are never reset."""
        aid = self._create_analysis(app, review_status='to_be_reviewed',
                                    priority_score=50)
        with app.app_context():
            a = RevenueAnalysis.query.get(aid)
            old_status = a.review_status
            new_priority = 80  # big jump
            category_changed = True
            priority_jumped = (new_priority - a.priority_score) >= 15
            if old_status in ('reviewed', 'actioned', 'dismissed'):
                if category_changed or priority_jumped:
                    a.previous_review_status = old_status
                    a.previous_review_notes = a.review_notes
                    a.review_status = 'to_be_reviewed'
                    a.review_notes = None
            a.priority_score = new_priority
            db.session.commit()

            a = RevenueAnalysis.query.get(aid)
            assert a.review_status == 'to_be_reviewed'
            assert a.previous_review_status is None

    def test_dismissed_resets_on_category_change(self, app):
        """Dismissed alert resets when category changes."""
        aid = self._create_analysis(app, review_status='dismissed',
                                    review_notes='Not our customer',
                                    category='STABLE')
        with app.app_context():
            a = RevenueAnalysis.query.get(aid)
            old_status = a.review_status
            old_notes = a.review_notes
            category_changed = 'DECLINING' != a.category
            priority_jumped = False
            if old_status in ('reviewed', 'actioned', 'dismissed'):
                if category_changed or priority_jumped:
                    a.previous_review_status = old_status
                    a.previous_review_notes = old_notes
                    a.review_status = 'to_be_reviewed'
                    a.review_notes = None
            a.category = 'DECLINING'
            db.session.commit()

            a = RevenueAnalysis.query.get(aid)
            assert a.review_status == 'to_be_reviewed'
            assert a.previous_review_status == 'dismissed'
            assert a.previous_review_notes == 'Not our customer'


# Fixtures

@pytest.fixture
def test_user(app):
    """Create a test user."""
    with app.app_context():
        user = User.query.first()
        if not user:
            user = User(email='test@test.com', name='Test User')
            db.session.add(user)
            db.session.commit()
        return user


class TestCompensatedBucketsAPI:
    """Tests for compensated buckets save/get API."""

    def test_save_compensated_buckets(self, client, app):
        """Should save filters without treating them as annual confirmation."""
        import json
        resp = client.post('/api/revenue/compensated-buckets',
                           data=json.dumps(['Analytics', 'Modern DBs']),
                           content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True

        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            saved = json.loads(pref.compensated_buckets)
            assert saved == ['Analytics', 'Modern DBs']
            assert pref.compensated_buckets_fiscal_year is None
            assert pref.compensated_buckets_confirmed_taxonomy_version is None

    def test_confirm_compensated_buckets_for_fiscal_year(self, client, app):
        """The Action Center can explicitly confirm the annual priority scope."""
        resp = client.post(
            '/api/revenue/compensated-buckets',
            json={
                'buckets': ['Databases', 'Fabric'],
                'confirm_for_fiscal_year': True,
            },
        )

        assert resp.status_code == 200
        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            assert pref.compensated_buckets_fiscal_year is not None
            assert (
                pref.compensated_buckets_confirmed_taxonomy_version
                == pref.bucket_taxonomy_version
            )

    def test_get_compensated_buckets(self, client, app):
        """Should return saved compensated buckets."""
        import json
        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            pref.compensated_buckets = json.dumps(['Core DBs', 'AI Infra'])
            db.session.commit()

        resp = client.get('/api/revenue/compensated-buckets')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == ['Core DBs', 'AI Infra']

    def test_get_compensated_buckets_empty(self, client):
        """Should return empty array when nothing saved."""
        resp = client.get('/api/revenue/compensated-buckets')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == []

    def test_save_rejects_non_array(self, client):
        """Should return 400 when body is not an array."""
        import json
        resp = client.post('/api/revenue/compensated-buckets',
                           data=json.dumps({'not': 'an array'}),
                           content_type='application/json')
        assert resp.status_code == 400

    def test_clearing_buckets_clears_confirmation(self, client, app):
        """An empty selection is not a confirmed annual priority scope."""
        import json
        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            pref.compensated_buckets_fiscal_year = 'FY27'
            pref.compensated_buckets_confirmed_taxonomy_version = 2
            db.session.commit()

        resp = client.post(
            '/api/revenue/compensated-buckets',
            data=json.dumps([]),
            content_type='application/json',
        )

        assert resp.status_code == 200
        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            assert pref.compensated_buckets_fiscal_year is None
            assert pref.compensated_buckets_confirmed_taxonomy_version is None

    def test_saving_buckets_clears_the_taxonomy_notice(self, client, app):
        """Explicitly confirming buckets retires the taxonomy notice."""
        import json
        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            pref.bucket_taxonomy_notice = json.dumps({'status': 'reset', 'removed': ['Core DBs']})
            db.session.commit()

        resp = client.post(
            '/api/revenue/compensated-buckets',
            json={
                'buckets': ['Databases'],
                'confirm_for_fiscal_year': True,
            },
        )
        assert resp.status_code == 200

        with app.app_context():
            from app.models import UserPreference
            assert UserPreference.query.first().bucket_taxonomy_notice is None
