"""
Pytest configuration and fixtures for Sales Buddy tests.
"""
import pytest
import tempfile
import os
from datetime import datetime, timezone
from unittest.mock import patch


@pytest.fixture(autouse=True)
def _block_msx_comment_calls():
    """Prevent any test from making real MSX milestone comment API calls.

    Routes spawn background daemon threads that call upsert_milestone_comment,
    get_milestone_comments, and get_msx_user_display_name.  Without this
    fixture, route-level tests (e.g. POST /note/new with a milestone) would
    leak real HTTP requests to the MSX Dynamics API.

    We mock at the *tracking service* import path so that:
    - Route-level tests are protected (they call tracking → mocked MSX)
    - Unit tests in test_milestone_comments.py that directly import and
      patch app.services.msx_api still work as expected

    Tests that need specific return values should layer their own
    ``@patch`` on top — the innermost mock wins.
    """
    with patch('app.services.milestone_tracking._upsert_to_msx') as mock_upsert, \
         patch('app.services.msx_api.get_milestone_comments') as mock_read:
        mock_upsert.return_value = {
            "success": True, "comment_count": 1, "action": "created",
        }
        mock_read.return_value = {"success": True, "comments": []}
        yield


@pytest.fixture(autouse=True)
def _clear_vpn_state():
    """Reset VPN blocked state before every test to prevent cross-test leaks."""
    from app.services.msx_auth import clear_vpn_block
    clear_vpn_block()
    yield
    clear_vpn_block()


@pytest.fixture(scope='session')
def app():
    """Create application for testing with isolated database."""
    # Save original environment variables to restore later
    original_database_url = os.environ.get('DATABASE_URL')
    original_testing = os.environ.get('TESTING')
    
    # Create a temporary database file for testing
    db_fd, db_path = tempfile.mkstemp(suffix='.db')
    
    # Set test database URI BEFORE importing app
    os.environ['DATABASE_URL'] = f'sqlite:///{db_path}'
    os.environ['TESTING'] = 'true'
    
    # NOW import app - it will use the test database URI
    from app import create_app
    from app.models import db, UserPreference, User
    
    flask_app = create_app()
    
    # Configure additional test settings
    flask_app.config['TESTING'] = True
    flask_app.config['WTF_CSRF_ENABLED'] = False
    flask_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    flask_app.config['LOGIN_DISABLED'] = True  # Disable login requirement for tests
    
    # Create tables
    with flask_app.app_context():
        db.create_all()
        
        # Create test user with Microsoft account and admin privileges
        test_user = User(
            microsoft_azure_id='test-user-12345',
            email='test@microsoft.com',
            name='Test User',
            is_admin=True
        )
        db.session.add(test_user)
        db.session.flush()
        
        # Create default user preference
        pref = UserPreference(dark_mode=False, customer_view_grouped=False, topic_sort_by_calls=False, territory_view_accounts=False)
        db.session.add(pref)
        db.session.commit()
    
    yield flask_app
    
    # Cleanup
    with flask_app.app_context():
        db.session.remove()
        db.drop_all()
    os.close(db_fd)
    
    # Try to delete temp file, but ignore Windows file locking errors
    try:
        os.unlink(db_path)
    except (PermissionError, OSError):
        # Windows may keep the file locked; it will be cleaned up by OS temp cleanup
        pass
    
    # Restore original environment variables
    if original_database_url is not None:
        os.environ['DATABASE_URL'] = original_database_url
    elif 'DATABASE_URL' in os.environ:
        del os.environ['DATABASE_URL']
    
    if original_testing is not None:
        os.environ['TESTING'] = original_testing
    elif 'TESTING' in os.environ:
        del os.environ['TESTING']


@pytest.fixture
def client(app):
    """Create a test client with user context."""
    client = app.test_client()
    
    # Set up user context for all tests (single-user mode)
    with app.app_context():
        from app.models import User
        test_user = User.query.first()
        
        with client.session_transaction() as sess:
            # Set the user ID in the session
            sess['user_id'] = str(test_user.id)
    
    return client


@pytest.fixture
def runner(app):
    """Create a test CLI runner."""
    return app.test_cli_runner()


@pytest.fixture
def sample_data(app):
    """Create sample data for tests."""
    with app.app_context():
        from app.models import db, Territory, Seller, Customer, Topic, Note, User
        
        # Get the test user
        test_user = User.query.first()
        
        # Create territories
        territory1 = Territory(name='West Region')
        territory2 = Territory(name='East Region')
        db.session.add_all([territory1, territory2])
        db.session.flush()
        
        # Create sellers
        seller1 = Seller(name='Alice Smith', alias='alices', seller_type='Growth')
        seller2 = Seller(name='Bob Jones', alias='bobj', seller_type='Acquisition')
        db.session.add_all([seller1, seller2])
        db.session.flush()
        
        # Associate sellers with territories
        seller1.territories.append(territory1)
        seller2.territories.append(territory2)
        
        # Create customers - Note: tpid is BigInteger so use numbers
        customer1 = Customer(
            name='Acme Corp',
            tpid=1001,  # Numeric TPID
            tpid_url='https://example.com/acme',
            seller_id=seller1.id,
            territory_id=territory1.id
        )
        customer2 = Customer(
            name='Globex Inc',
            tpid=1002,  # Numeric TPID
            seller_id=seller2.id,
            territory_id=territory2.id
            # No tpid_url - for testing TPID workflow
        )
        customer3 = Customer(
            name='Initech LLC',
            tpid=1003,  # Numeric TPID
            seller_id=seller1.id,
            territory_id=territory1.id
            # No tpid_url - for testing TPID workflow batch updates
        )
        db.session.add_all([customer1, customer2, customer3])
        db.session.flush()
        
        # Create topics
        topic1 = Topic(name='Azure VM', description='Virtual Machines')
        topic2 = Topic(name='Storage', description='Azure Storage')
        db.session.add_all([topic1, topic2])
        db.session.flush()
        
        # Create call logs - Use correct field name 'content'
        # Note: seller and territory are now derived from customer relationship
        call1 = Note(
            customer_id=customer1.id,
            call_date=datetime.now(timezone.utc),
            content='Discussed VM migration strategy and cloud architecture options.'
        )
        call1.topics.append(topic1)
        
        call2 = Note(
            customer_id=customer2.id,
            call_date=datetime.now(timezone.utc),
            content='Storage optimization review with focus on blob storage.'
        )
        call2.topics.append(topic2)
        
        db.session.add_all([call1, call2])
        db.session.commit()
        
        # Return IDs for use in tests
        return {
            'territory1_id': territory1.id,
            'territory2_id': territory2.id,
            'seller1_id': seller1.id,
            'seller2_id': seller2.id,
            'customer1_id': customer1.id,
            'customer2_id': customer2.id,
            'customer3_id': customer3.id,
            'topic1_id': topic1.id,
            'topic2_id': topic2.id,
            'call1_id': call1.id,
            'call2_id': call2.id,
        }


@pytest.fixture(autouse=True)
def reset_db(app):
    """Reset database between tests."""
    with app.app_context():
        from app.models import db, UserPreference
        
        # SAFETY CHECK: Ensure we're using SQLite for tests
        db_uri = str(db.engine.url)
        if 'sqlite' not in db_uri.lower():
            raise RuntimeError(
                f"CRITICAL: Tests attempted to run against non-SQLite database: {db_uri}\n"
                f"This could destroy production data! Tests must only use SQLite."
            )
        
        # Drop and recreate all tables for clean slate
        db.drop_all()
        db.create_all()
        
        # Recreate test user with admin privileges and preferences
        from app.models import User
        test_user = User(
            microsoft_azure_id='test-user-12345',
            email='test@microsoft.com',
            name='Test User',
            is_admin=True
        )
        db.session.add(test_user)
        db.session.flush()
        
        pref = UserPreference(dark_mode=False, customer_view_grouped=False, topic_sort_by_calls=False, territory_view_accounts=False)
        db.session.add(pref)
        db.session.commit()
    
    yield

@pytest.fixture
def db_session(app):
    """Provide database session for tests."""
    with app.app_context():
        from app.models import db
        yield db.session


@pytest.fixture
def sample_user(app):
    """Get the test user."""
    with app.app_context():
        from app.models import User
        return User.query.first()


@pytest.fixture
def sample_customer(app):
    """Create a sample customer for tests and return its ID."""
    with app.app_context():
        from app.models import db, Customer, User
        test_user = User.query.first()
        
        customer = Customer(
            name='Test Customer',
            tpid=9999
        )
        db.session.add(customer)
        db.session.commit()
        
        # Create a simple object to hold the ID (avoid detached instance issues)
        class CustomerData:
            def __init__(self, id, name, tpid):
                self.id = id
                self.name = name
                self.tpid = tpid
        
        return CustomerData(customer.id, customer.name, customer.tpid)