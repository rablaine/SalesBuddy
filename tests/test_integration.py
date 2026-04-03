"""
Integration tests that POST to create endpoints.
These tests verify that all model creations work correctly.
"""
import pytest
from datetime import date
from app.models import db, Note, Customer, Seller, Territory, Topic


def test_note_create_post(client, sample_data):
    """Test that posting to note create works."""
    customer_id = sample_data['customer1_id']
    
    response = client.post('/note/new', data={
        'customer_id': customer_id,
        'call_date': date.today().strftime('%Y-%m-%d'),
        'content': '<p>Test note content</p>',
        'topic_ids': []
    }, follow_redirects=False)
    
    # Should redirect to view page
    assert response.status_code == 302
    
    # Verify note was created
    note = Note.query.filter_by(customer_id=customer_id).order_by(Note.id.desc()).first()
    assert note is not None
    assert note.content == '<p>Test note content</p>'


def test_customer_create_post(client, sample_data):
    """Test that posting to customer create works."""
    seller_id = sample_data['seller1_id']
    
    response = client.post('/customer/new', data={
        'name': 'Test Customer Integration',
        'nickname': '',
        'tpid': '999999',
        'tpid_url': '',
        'seller_id': seller_id,
        'territory_id': ''
    }, follow_redirects=False)
    
    # Should redirect
    assert response.status_code == 302
    
    # Verify customer was created
    customer = Customer.query.filter_by(tpid=999999).first()
    assert customer is not None
    assert customer.name == 'Test Customer Integration'


def test_seller_create_post(client, sample_data):
    """Test that posting to seller create works."""
    response = client.post('/seller/new', data={
        'name': 'Test Seller Integration',
        'territory_ids': []
    }, follow_redirects=False)
    
    # Should redirect
    assert response.status_code == 302
    
    # Verify seller was created
    seller = Seller.query.filter_by(name='Test Seller Integration').first()
    assert seller is not None


def test_territory_create_post(client, sample_data):
    """Test that posting to territory create works."""
    response = client.post('/territory/new', data={
        'name': 'Test Territory Integration',
        'pod_id': ''
    }, follow_redirects=False)
    
    # Should redirect
    assert response.status_code == 302
    
    # Verify territory was created
    territory = Territory.query.filter_by(name='Test Territory Integration').first()
    assert territory is not None


def test_topic_create_post(client, sample_data):
    """Test that posting to topic create works."""
    response = client.post('/topic/new', data={
        'name': 'Test Topic Integration',
        'description': 'Test description'
    }, follow_redirects=False)
    
    # Should redirect
    assert response.status_code == 302
    
    # Verify topic was created
    topic = Topic.query.filter_by(name='Test Topic Integration').first()
    assert topic is not None
    assert topic.description == 'Test description'

