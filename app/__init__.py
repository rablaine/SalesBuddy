"""
Flask application factory for NoteHelper.
Single-user local deployment mode.
"""
import os
from flask import Flask, g
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Import db from models module
from app.models import db


def create_app():
    """Create and configure the Flask application."""
    app = Flask(__name__, 
                template_folder='../templates',
                static_folder='../static')
    
    # Configuration
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    
    # SQLite database path - use absolute path
    db_url = os.environ.get('DATABASE_URL', 'sqlite:///data/notehelper.db')
    if db_url.startswith('sqlite:///') and not db_url.startswith('sqlite:////'):
        # Convert relative path to absolute path
        db_path = db_url.replace('sqlite:///', '')
        if not os.path.isabs(db_path):
            db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), db_path)
            # Ensure directory exists
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            db_url = 'sqlite:///' + db_path
    
    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    
    # Initialize extensions with app
    db.init_app(app)
    
    # Import models to register them with SQLAlchemy
    from app import models
    
    # Create default user and preferences on app startup
    with app.app_context():
        from app.models import User, UserPreference
        from app.migrations import run_migrations
        
        # Ensure database tables exist
        db.create_all()
        
        # Run idempotent migrations (safe to run every startup)
        run_migrations(db)
        
        # Ensure the canonical single user (id=1) exists.
        user = db.session.get(User, 1)
        if not user:
            user = User(
                id=1,
                email='user@localhost',
                name='Local User',
                is_admin=True,  # Single user has all permissions
            )
            db.session.add(user)
            db.session.commit()
            
            # Create default preferences
            pref = UserPreference()
            db.session.add(pref)
            db.session.commit()
    
    # Load app-wide preferences into g
    @app.before_request
    def load_preferences():
        """Load single user and preferences into request context."""
        from app.models import User, UserPreference
        
        # Always use the canonical single user (id=1)
        g.user = db.session.get(User, 1)
        
        # Load preferences
        if g.user:
            g.user_prefs = UserPreference.query.first()
            if not g.user_prefs:
                g.user_prefs = UserPreference()
                db.session.add(g.user_prefs)
                db.session.commit()
    
    # Register blueprints
    from app.routes.admin import admin_bp
    from app.routes.ai import ai_bp
    from app.routes.territories import territories_bp
    from app.routes.pods import pods_bp
    from app.routes.solution_engineers import solution_engineers_bp
    from app.routes.sellers import sellers_bp
    from app.routes.customers import customers_bp
    from app.routes.topics import topics_bp
    from app.routes.call_logs import call_logs_bp
    from app.routes.main import main_bp
    from app.routes.revenue import revenue_bp
    from app.routes.partners import partners_bp
    from app.routes.milestones import bp as milestones_bp
    from app.routes.msx import msx_bp
    from app.routes.opportunities import opportunities_bp
    from app.routes.connect_export import connect_export_bp
    from app.routes.backup import backup_bp
    
    app.register_blueprint(admin_bp)
    app.register_blueprint(ai_bp)
    app.register_blueprint(territories_bp)
    app.register_blueprint(pods_bp)
    app.register_blueprint(solution_engineers_bp)
    app.register_blueprint(sellers_bp)
    app.register_blueprint(customers_bp)
    app.register_blueprint(topics_bp)
    app.register_blueprint(call_logs_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(revenue_bp)
    app.register_blueprint(partners_bp)
    app.register_blueprint(milestones_bp)
    app.register_blueprint(msx_bp)
    app.register_blueprint(opportunities_bp)
    app.register_blueprint(connect_export_bp)
    app.register_blueprint(backup_bp)
    
    # Start MSX token refresh job (background thread)
    # This keeps the az login token fresh for CRM API calls
    from app.services.msx_auth import start_token_refresh_job
    start_token_refresh_job(interval_seconds=300)  # Check every 5 minutes
    
    # Start scheduled milestone sync (if MILESTONE_SYNC_HOUR is configured)
    from app.services.scheduled_sync import start_scheduled_sync
    start_scheduled_sync(app)
    
    # Capture the git commit hash at boot time (frozen in memory)
    import subprocess
    try:
        boot_result = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        app.config['BOOT_COMMIT'] = boot_result.stdout.strip() or None
    except Exception:
        app.config['BOOT_COMMIT'] = None

    # Start background update checker (checks GitHub every 12 hours)
    from app.services.update_checker import start_update_checker
    start_update_checker(interval_seconds=43200)

    return app
