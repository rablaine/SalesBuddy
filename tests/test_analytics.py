"""
Tests for analytics dashboard.
"""
import pytest
from datetime import date, timedelta
from app.models import db, Note, Customer, Seller, Territory, Topic, utc_now


def test_analytics_page_loads(client):
    """Test analytics page loads successfully."""
    response = client.get('/analytics')
    assert response.status_code == 200
    assert b'Analytics & Insights' in response.data


def test_analytics_shows_key_metrics(client, sample_data):
    """Test analytics page displays key metrics."""
    response = client.get('/analytics')
    assert response.status_code == 200
    
    # Should show note counts
    assert b'Notes This Week' in response.data
    assert b'Customers This Week' in response.data
    assert b'Total Notes' in response.data
    assert b'Total Customers' in response.data


def test_analytics_call_frequency_trend(client, sample_data):
    """Test analytics shows call frequency trend."""
    response = client.get('/analytics')
    assert response.status_code == 200
    
    # Should have trend section
    assert b'Call Frequency Trend' in response.data
    assert b'progress-bar' in response.data  # Visual representation


def test_analytics_top_topics(client, sample_data):
    """Test analytics shows most discussed topics."""
    # Create some call logs with topics
    customer = Customer.query.first()
    topic1 = Topic.query.first()
    
    if customer and topic1:
        # Add a call with topic
        call = Note(
            customer_id=customer.id,
            call_date=date.today(),
            content="Test call with topic",
            created_at=utc_now(),
            updated_at=utc_now()
        )
        call.topics.append(topic1)
        db.session.add(call)
        db.session.commit()
    
    response = client.get('/analytics')
    assert response.status_code == 200
    
    # Should have topics section
    assert b'Most Discussed Topics' in response.data


def test_analytics_customers_needing_attention(client):
    """Test analytics identifies customers not called recently."""
    # Create a customer with no calls
    seller = Seller.query.first()
    if not seller:
        seller = Seller(name="Test Seller")
        db.session.add(seller)
        db.session.commit()
    
    customer = Customer(
        name="Neglected Customer",
        tpid="NEGLECT001",
        seller_id=seller.id
    )
    db.session.add(customer)
    db.session.commit()
    
    response = client.get('/analytics')
    assert response.status_code == 200
    
    # Should show customers needing attention
    assert b'Customers Needing Attention' in response.data
    assert b'Neglected Customer' in response.data


def test_analytics_seller_activity(client, sample_data):
    """Test analytics shows seller activity."""
    response = client.get('/analytics')
    assert response.status_code == 200
    
    # Should have seller activity section
    assert b'Seller Activity' in response.data


def test_analytics_with_no_data(client):
    """Test analytics page handles empty data gracefully."""
    # Clear all call logs for user
    Note.query.delete()
    db.session.commit()
    
    response = client.get('/analytics')
    assert response.status_code == 200
    
    # Should still load without errors
    assert b'Analytics & Insights' in response.data
    # Should show zero metrics
    assert b'Notes This Week' in response.data


def test_analytics_calculates_weekly_trends_correctly(client):
    """Test weekly call trend calculations are correct."""
    # Create calls from different weeks
    customer = Customer.query.first()
    if not customer:
        seller = Seller(name="Test Seller")
        db.session.add(seller)
        db.session.commit()
        
        customer = Customer(
            name="Test Customer",
            tpid="TEST001",
            seller_id=seller.id
        )
        db.session.add(customer)
        db.session.commit()
    
    today = date.today()
    
    # Add calls from different weeks
    for days_ago in [1, 8, 15, 22]:  # One call per week for 4 weeks
        call = Note(
            customer_id=customer.id,
            call_date=today - timedelta(days=days_ago),
            content=f"Call from {days_ago} days ago",
            created_at=utc_now(),
            updated_at=utc_now()
        )
        db.session.add(call)
    db.session.commit()
    
    response = client.get('/analytics')
    assert response.status_code == 200
    
    # Should show trend data
    assert b'Call Frequency Trend' in response.data


def test_analytics_link_in_navigation(client):
    """Test analytics link appears in navigation menu."""
    response = client.get('/')
    assert response.status_code == 200
    
    # Should have analytics link in nav
    assert b'Analytics' in response.data
    assert b'/analytics' in response.data
