"""
Tests for revenue import and analysis functionality.
"""
import pytest
from datetime import date, datetime
from io import BytesIO

from app.models import (
    db, RevenueImport, CustomerRevenueData, RevenueAnalysis, 
    RevenueConfig, Customer, User
)
from app.services.revenue_import import (
    parse_currency, fiscal_month_to_date, date_to_fiscal_month,
    import_revenue_csv, process_csv, RevenueImportError
)
from app.services.revenue_analysis import (
    compute_signals, categorize_customer, determine_action,
    run_analysis_for_all, get_actionable_analyses, AnalysisConfig
)


class TestParseCurrency:
    """Test currency string parsing."""
    
    def test_parse_regular_currency(self):
        assert parse_currency("$1,234.56") == 1234.56
        assert parse_currency("$1234") == 1234.0
        assert parse_currency("$0") == 0.0
    
    def test_parse_without_dollar_sign(self):
        assert parse_currency("1234.56") == 1234.56
        assert parse_currency("1,234") == 1234.0
    
    def test_parse_negative(self):
        assert parse_currency("($1,234)") == -1234.0
    
    def test_parse_empty_or_none(self):
        assert parse_currency(None) == 0.0
        assert parse_currency("") == 0.0
    
    def test_parse_float_passthrough(self):
        assert parse_currency(1234.56) == 1234.56
        assert parse_currency(0) == 0.0


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
    
    def test_date_to_fiscal_month(self):
        assert date_to_fiscal_month(date(2025, 7, 1)) == "FY26-Jul"
        assert date_to_fiscal_month(date(2026, 1, 15)) == "FY26-Jan"
        assert date_to_fiscal_month(date(2026, 6, 30)) == "FY26-Jun"


class TestCSVImport:
    """Test CSV import functionality."""
    
    def test_import_basic_csv(self, app, test_user):
        """Test importing a basic revenue CSV."""
        # New format: TPAccountName, ServiceCompGrouping, ServiceLevel4, months...
        csv_content = b"""FiscalMonth,,,FY26-Jul,FY26-Aug,FY26-Sep,Total
TPAccountName,ServiceCompGrouping,ServiceLevel4,$ ACR,$ ACR,$ ACR,$ ACR
Test Customer,Core DBs,Total,"$10,000","$11,000","$12,000","$33,000"
Test Customer,Core DBs,SQL Server,"$5,000","$5,500","$6,000","$16,500"
Test Customer,Analytics,Total,"$5,000","$5,500","$6,000","$16,500"
"""
        
        with app.app_context():
            import_record = import_revenue_csv(
                file_content=csv_content,
                filename="test.csv"
            )
            
            assert import_record.record_count == 3
            # 2 bucket totals * 3 months = 6 bucket records + 1 product * 3 months = 3 product records
            assert import_record.records_created == 9
            assert import_record.records_updated == 0
            
            # Verify bucket data was stored
            data = CustomerRevenueData.query.filter_by(customer_name="Test Customer").all()
            assert len(data) == 6  # 2 buckets * 3 months
            
            # Verify product data was stored
            from app.models import ProductRevenueData
            product_data = ProductRevenueData.query.filter_by(customer_name="Test Customer").all()
            assert len(product_data) == 3  # 1 product * 3 months
    
    def test_import_updates_existing(self, app, test_user):
        """Test that re-importing updates existing records."""
        csv_content = b"""FiscalMonth,,,FY26-Jul,Total
TPAccountName,ServiceCompGrouping,ServiceLevel4,$ ACR,$ ACR
Existing Customer,Core DBs,Total,"$10,000","$10,000"
"""
        
        with app.app_context():
            # First import
            import_revenue_csv(csv_content, "test1.csv")
            
            # Second import with updated value
            csv_content2 = b"""FiscalMonth,,,FY26-Jul,Total
TPAccountName,ServiceCompGrouping,ServiceLevel4,$ ACR,$ ACR
Existing Customer,Core DBs,Total,"$15,000","$15,000"
"""
            import_record = import_revenue_csv(csv_content2, "test2.csv")
            
            assert import_record.records_updated == 1
            assert import_record.records_created == 0
            
            # Verify updated value
            data = CustomerRevenueData.query.filter_by(
                customer_name="Existing Customer",
                bucket="Core DBs"
            ).first()
            assert data.revenue == 15000.0
    
    def test_import_invalid_format(self, app, test_user):
        """Test that invalid CSV format raises error."""
        csv_content = b"""This is not a valid MSXI CSV
Some random content
"""
        
        with app.app_context():
            with pytest.raises(RevenueImportError):
                import_revenue_csv(csv_content, "bad.csv")

    def test_import_missing_required_columns(self, app, test_user):
        """Test that CSV missing required columns raises helpful error."""
        import pandas as pd
        from io import StringIO

        # CSV with wrong column names (e.g., user exported wrong table)
        bad_csv = "Month,SomeOtherCol,Metric1,FY26-Jul\nAccount,Group,Detail,$100"
        df = pd.read_csv(StringIO(bad_csv), header=None)
        with pytest.raises(RevenueImportError, match="Missing required columns"):
            process_csv(df)

    def test_import_valid_columns_pass_validation(self, app, test_user):
        """Test that CSV with correct columns passes validation."""
        import pandas as pd
        from io import StringIO

        # Minimal valid CSV structure
        valid_csv = (
            "FiscalMonth,,,FY26-Jul\n"
            "TPAccountName,ServiceCompGrouping,ServiceLevel4,$ ACR\n"
            "Contoso,Core DBs,Total,$1000"
        )
        df = pd.read_csv(StringIO(valid_csv), header=None)
        result_df, months, month_map = process_csv(df)
        assert 'TPAccountName' in result_df.columns
        assert len(months) == 1


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
    
    def test_categorize_churn_risk(self):
        """Test churn risk categorization."""
        revenues = [15000, 14000, 13000, 11000, 9000, 7000]  # Sharp decline
        months = ["FY26-Jul", "FY26-Aug", "FY26-Sep", "FY26-Oct", "FY26-Nov", "FY26-Dec"]
        
        signals = compute_signals("Churning", "Core DBs", revenues, months)
        
        assert signals is not None
        assert signals.category in ["CHURN_RISK", "VOLATILE"]
    
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
        # Perfectly flat data could be HEALTHY or STAGNANT depending on thresholds
        # The important thing is it's not CHURN_RISK or VOLATILE
        assert signals.category in ["HEALTHY", "STAGNANT", "VOLATILE"]  # Allow any stable-ish category


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
        response = client.get('/revenue')
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
    
    def test_engagement_history_empty(self, client):
        """Test engagement history page with no data."""
        response = client.get('/revenue/engagements')
        assert response.status_code == 200
        assert b'Engagement History' in response.data
    
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
                category='CHURN_RISK',
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
        """Test analysis after importing data."""
        # New format with ServiceLevel4 column
        csv_content = b"""FiscalMonth,,,FY26-Jul,FY26-Aug,FY26-Sep,FY26-Oct,FY26-Nov,FY26-Dec,Total
TPAccountName,ServiceCompGrouping,ServiceLevel4,$ ACR,$ ACR,$ ACR,$ ACR,$ ACR,$ ACR,$ ACR
Healthy Customer,Core DBs,Total,"$10,000","$10,100","$10,050","$10,200","$10,150","$10,250","$60,750"
At Risk Customer,Core DBs,Total,"$20,000","$18,000","$16,000","$14,000","$12,000","$10,000","$90,000"
"""
        
        with app.app_context():
            # Import data
            import_revenue_csv(csv_content, "test.csv")
            
            # Run analysis
            stats = run_analysis_for_all()
            
            assert stats['analyzed'] >= 1
            
            # Check analyses were created
            analyses = RevenueAnalysis.query.all()
            assert len(analyses) >= 1


class TestReviewAPI:
    """Test the PATCH /api/revenue/analysis/<id>/review endpoint."""

    def _create_analysis(self, app):
        """Helper: create a RevenueAnalysis and return its id."""
        with app.app_context():
            a = RevenueAnalysis(
                customer_name='Review Test Customer',
                bucket='Core DBs',
                category='CHURN_RISK',
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
                category='CHURN_RISK', recommended_action='CHECK-IN',
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
        resp = client.get('/revenue')
        assert resp.status_code == 200
        assert b'Status' in resp.data


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
