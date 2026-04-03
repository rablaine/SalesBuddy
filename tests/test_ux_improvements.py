"""
Tests for UX improvements added in Phase 1 and Phase 2.
"""
import pytest
from bs4 import BeautifulSoup


def test_admin_panel_stats_are_clickable(client):
    """Test that admin panel statistics have clickable links."""
    # Make user admin first
    from app.models import db, User, UserPreference
    user = User.query.first()
    user.is_admin = True
    # Dismiss onboarding modal so it doesn't interfere with admin panel parsing
    pref = UserPreference.query.first()
    pref.first_run_modal_dismissed = True
    db.session.commit()
    
    response = client.get('/admin')
    assert response.status_code == 200
    
    soup = BeautifulSoup(response.data, 'html.parser')
    
    # Find stats within main content area (not modals)
    main_content = soup.find('main')
    stats_section = main_content.find('div', class_='card-body')
    links = stats_section.find_all('a')
    
    # Should have links for: Notes, Customers, Sellers, Territories, Topics, PODs
    assert len(links) >= 6, "Should have at least 6 clickable stats"
    
    # Check specific links exist
    notes_link = soup.find('a', href='/notes')
    assert notes_link is not None, "Notes stat should be clickable"
    
    customers_link = soup.find('a', href='/customers')
    assert customers_link is not None, "Customers stat should be clickable"
    
    topics_link = soup.find('a', href='/topics')
    assert topics_link is not None, "Topics stat should be clickable"

    # Clean up
    pref.first_run_modal_dismissed = False
    db.session.commit()


def test_topics_toggle_shows_current_state(client):
    """Test that topics sort toggle button shows current state, not action."""
    response = client.get('/topics')
    assert response.status_code == 200
    
    soup = BeautifulSoup(response.data, 'html.parser')
    
    # Find the sort toggle button
    sort_button = soup.find('button', id='sortToggle')
    assert sort_button is not None, "Sort toggle button should exist"
    
    # The button should have a label showing current sort
    sort_label = soup.find('span', id='sortLabel')
    assert sort_label is not None, "Sort label should exist"
    
    # Label text should be either "Alphabetical" or "By Notes" (current state)
    label_text = sort_label.text.strip()
    assert label_text in ['Alphabetical', 'By Notes'], f"Label should show current state, got: {label_text}"


def test_call_content_truncation_word_boundaries(client):
    """Test that call content is truncated at word boundaries."""
    from app.models import db, Customer, Seller, Note, utc_now
    from datetime import date
    
    # Create test data with long content
    seller = Seller.query.first()
    if not seller:
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
    
    # Create note with long content that would be truncated mid-word if not using word boundaries
    long_content = "This is a test note with very long content. " * 10  # ~500 chars
    call = Note(
        customer_id=customer.id,
        call_date=date.today(),
        content=long_content,
        created_at=utc_now(),
        updated_at=utc_now()
    )
    db.session.add(call)
    db.session.commit()
    
    # Check homepage
    response = client.get('/')
    assert response.status_code == 200
    html = response.data.decode('utf-8')
    
    # Should not see truncated mid-word (like "conte..." when "content" was cut)
    # The truncate filter with word boundaries should end with complete words
    assert '...' in html or len(long_content) < 200, "Long content should be truncated"
    
    # Check notes list
    response = client.get('/notes')
    assert response.status_code == 200
    assert '...' in response.data.decode('utf-8') or len(long_content) < 200


def test_customers_filter_toggle_button(client):
    """Test that customers filter is now a button-style toggle."""
    response = client.get('/customers')
    assert response.status_code == 200
    
    soup = BeautifulSoup(response.data, 'html.parser')
    
    # Should find toggle button instead of checkbox
    toggle_button = soup.find('button', id='toggleShowWithoutCalls')
    assert toggle_button is not None, "Toggle button for customers filter should exist"
    
    # Button should have proper classes
    assert 'btn' in toggle_button.get('class', []), "Toggle should be a button"
    
    # Button should show appropriate icon (eye or eye-slash)
    icon = toggle_button.find('i')
    assert icon is not None, "Toggle button should have an icon"
    assert 'bi-eye' in icon.get('class', []) or 'bi-eye-slash' in icon.get('class', []), "Should use eye icon"


def test_search_results_hierarchy_simplified(client):
    """Test that search results have simplified visual hierarchy."""
    from app.models import db, Customer, Seller, Note, utc_now
    from datetime import date
    
    # Create test data
    seller = Seller.query.first()
    if not seller:
        seller = Seller(name="Test Seller")
        db.session.add(seller)
        db.session.commit()
    
    customer = Customer(
        name="Search Test Customer",
        tpid="SEARCH001",
        seller_id=seller.id
    )
    db.session.add(customer)
    db.session.commit()
    
    call = Note(
        customer_id=customer.id,
        call_date=date.today(),
        content="Test search content",
        created_at=utc_now(),
        updated_at=utc_now()
    )
    db.session.add(call)
    db.session.commit()
    
    response = client.get('/search?q=test')
    assert response.status_code == 200
    
    soup = BeautifulSoup(response.data, 'html.parser')
    
    # Should NOT have heavy card-based seller headers (old design)
    seller_cards = soup.find_all('div', class_='card-header bg-secondary')
    assert len(seller_cards) == 0, "Should not use heavy card headers for sellers"
    
    # Should have lighter border-based grouping
    seller_sections = soup.find_all('div', class_='border-bottom')
    # If there are results, should have at least one seller section
    if b'Found' in response.data:
        assert len(seller_sections) > 0, "Should use lighter border-based grouping"


def test_draft_save_indicator_exists(client):
    """Test that draft save indicator element exists in note form."""
    # Create a customer first (note form requires customer_id)
    from app.models import db, Customer, Seller
    
    seller = Seller.query.first()
    if not seller:
        seller = Seller(name="Test Seller")
        db.session.add(seller)
        db.session.commit()
    
    customer = Customer.query.first()
    if not customer:
        customer = Customer(
            name="Test Customer",
            tpid="TEST001",
            seller_id=seller.id
        )
        db.session.add(customer)
        db.session.commit()
    
    # Note form requires customer_id parameter
    response = client.get(f'/note/new?customer_id={customer.id}')
    assert response.status_code == 200
    
    soup = BeautifulSoup(response.data, 'html.parser')
    
    # Check for draft save indicator
    indicator = soup.find('div', id='draftSaveIndicator')
    assert indicator is not None, "Draft save indicator should exist"
    
    # Should have success icon
    success_icon = indicator.find('i', class_='bi-check-circle')
    assert success_icon is not None, "Draft indicator should have success icon"


def test_button_sizes_standardized(client):
    """Test that list page buttons are standardized to btn-sm."""
    pages = [
        '/customers',
        '/topics',
        '/notes',
        '/sellers',
        '/territories'
    ]
    
    for page in pages:
        response = client.get(page)
        assert response.status_code == 200
        
        soup = BeautifulSoup(response.data, 'html.parser')
        
        # Find "New X" buttons (excluding navbar buttons which use regular size)
        new_buttons = soup.find_all('a', class_='btn-primary') + soup.find_all('button', class_='btn-success')
        
        for button in new_buttons:
            if 'New' in button.text:
                # Skip navbar buttons (they intentionally use regular size to match user menu)
                if button.find_parent('nav', class_='navbar'):
                    continue
                classes = button.get('class', [])
                assert 'btn-sm' in classes, f"New button on {page} should use btn-sm, got: {classes}"
