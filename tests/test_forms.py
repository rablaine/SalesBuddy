"""
Tests for form routes (create and edit pages).
These tests verify forms load correctly with eager-loaded data.
"""
import pytest


def test_customer_create_form_loads(client, sample_data):
    """Test customer creation form loads with sellers and territories."""
    response = client.get('/customer/new')
    assert response.status_code == 200
    assert b'New Customer' in response.data
    assert b'Alice Smith' in response.data  # Seller in dropdown
    assert b'West Region' in response.data  # Territory in dropdown


def test_customer_create_with_seller_preselect(client, sample_data):
    """Test customer form with seller pre-selected."""
    seller_id = sample_data['seller1_id']
    response = client.get(f'/customer/new?seller_id={seller_id}')
    assert response.status_code == 200
    assert b'Alice Smith' in response.data
    assert b'selected' in response.data


def test_customer_create_with_territory_preselect(client, sample_data):
    """Test customer form with territory pre-selected."""
    territory_id = sample_data['territory1_id']
    response = client.get(f'/customer/new?territory_id={territory_id}')
    assert response.status_code == 200
    assert b'West Region' in response.data
    assert b'selected' in response.data


def test_customer_edit_form_loads(client, sample_data):
    """Test customer edit form loads with existing data."""
    customer_id = sample_data['customer1_id']
    response = client.get(f'/customer/{customer_id}/edit')
    assert response.status_code == 200
    assert b'Edit Customer' in response.data
    assert b'Acme Corp' in response.data
    assert b'1001' in response.data


def test_seller_create_form_loads(client):
    """Test seller creation form loads."""
    response = client.get('/seller/new')
    assert response.status_code == 200
    assert b'New Seller' in response.data


def test_seller_edit_form_loads(client, sample_data):
    """Test seller edit form loads."""
    seller_id = sample_data['seller1_id']
    response = client.get(f'/seller/{seller_id}/edit')
    assert response.status_code == 200
    assert b'Edit Seller' in response.data
    assert b'Alice Smith' in response.data


def test_territory_create_form_loads(client):
    """Test territory creation form loads."""
    response = client.get('/territory/new')
    assert response.status_code == 200
    assert b'New Territory' in response.data


def test_territory_edit_form_loads(client, sample_data):
    """Test territory edit form loads."""
    territory_id = sample_data['territory1_id']
    response = client.get(f'/territory/{territory_id}/edit')
    assert response.status_code == 200
    assert b'Edit Territory' in response.data
    assert b'West Region' in response.data


def test_topic_create_form_loads(client):
    """Test topic creation form loads."""
    response = client.get('/topic/new')
    assert response.status_code == 200
    assert b'New Topic' in response.data


def test_topic_edit_form_loads(client, sample_data):
    """Test topic edit form loads."""
    topic_id = sample_data['topic1_id']
    response = client.get(f'/topic/{topic_id}/edit')
    assert response.status_code == 200
    assert b'Edit Topic' in response.data
    assert b'Azure VM' in response.data


def test_note_create_form_loads(client, sample_data):
    """Test note creation form loads (without customer_id shows general note form)."""
    # Without customer_id, should load as general note form
    response = client.get('/note/new')
    assert response.status_code == 200
    assert b'General Note' in response.data
    
    # With customer_id, should load form with customer context
    customer_id = sample_data['customer1_id']
    response = client.get(f'/note/new?customer_id={customer_id}')
    assert response.status_code == 200
    assert b'New Note' in response.data
    assert b'Acme Corp' in response.data  # Customer dropdown


def test_note_create_with_customer_preselect(client, sample_data):
    """Test note form with customer pre-selected."""
    customer_id = sample_data['customer1_id']
    response = client.get(f'/note/new?customer_id={customer_id}')
    assert response.status_code == 200
    assert b'Acme Corp' in response.data
    # Auto-selected seller
    assert b'Alice Smith' in response.data
    # Territory may or may not be auto-selected depending on logic


def test_note_create_with_date_param(client, sample_data):
    """Test note form with date pre-filled from URL parameter."""
    customer_id = sample_data['customer1_id']
    response = client.get(f'/note/new?customer_id={customer_id}&date=2026-02-03')
    assert response.status_code == 200
    # The date should be pre-filled in the form
    assert b'2026-02-03' in response.data


def test_note_create_with_invalid_date_param(client, sample_data):
    """Test note form falls back to today with invalid date."""
    customer_id = sample_data['customer1_id']
    response = client.get(f'/note/new?customer_id={customer_id}&date=invalid-date')
    assert response.status_code == 200
    # Should still load successfully (falls back to today)
    assert b'New Note' in response.data


def test_note_edit_form_loads(client, sample_data):
    """Test note edit form loads."""
    call_id = sample_data['call1_id']
    response = client.get(f'/note/{call_id}/edit')
    assert response.status_code == 200
    assert b'Edit Note' in response.data
    assert b'Discussed VM migration' in response.data
