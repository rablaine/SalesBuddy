"""
Flask application factory for Sales Buddy.
Single-user local deployment mode.
"""
import os
from flask import Flask, g, flash
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Import db from models module
from app.models import db


def should_run_schedulers(role: str, supervised: bool) -> bool:
    """Whether this process should run the heavy background schedulers.

    Schedulers run in the dedicated worker process, OR inline in a web process
    that has no supervisor (a direct / monolithic launch - e.g. a git-pull user
    running waitress directly, or the transitional first update). Under the
    supervisor the web process is 'supervised' and the worker owns them, so a
    slow or hung sync can never wedge the web server.
    """
    return role == 'worker' or (role == 'web' and not supervised)


def create_app():
    """Create and configure the Flask application."""
    app = Flask(__name__, 
                template_folder='../templates',
                static_folder='../static')
    
    # Configuration
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    
    # SQLite database path. Resolved through app.db_paths so the Flask app, the
    # background scripts, and the C# installer all agree on ONE location. In
    # production the DB lives OUTSIDE the install dir (a sibling of it) so no
    # install / upgrade / uninstall can ever delete user data.
    from app.db_paths import (
        resolve_db_url, resolve_db_path, migrate_db_to_new_location,
        write_data_path_file,
    )

    # One-time move of an existing DB to the external location. Under the
    # supervisor this already ran ONCE in the parent process (before web+worker
    # forked), and children carry SALESBUDDY_SUPERVISED - so they skip it here to
    # avoid two processes migrating at once. Only an UNSUPERVISED launch (bare
    # `flask run` / direct waitress) migrates here. Never under tests.
    _supervised = os.environ.get('SALESBUDDY_SUPERVISED', '').strip().lower() in ('1', 'true', 'yes')
    if not os.environ.get('TESTING') and not _supervised:
        try:
            migrate_db_to_new_location()
        except Exception:
            pass
        write_data_path_file()

    db_url = resolve_db_url()
    try:
        resolve_db_path().parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    
    # Allow large form submissions (notes with inline screenshots are base64-encoded)
    app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB
    app.config['MAX_FORM_MEMORY_SIZE'] = 50 * 1024 * 1024  # 50 MB

    # Bootstrap the isolated Azure CLI auth profile so az-based auth (MSX +
    # gateway) is self-contained regardless of which launcher started us
    # (Electron, supervisor, bare flask run). Skipped under tests so we never
    # touch the real ~/.azure. Guarded on the env var because app.config's
    # TESTING flag is not set yet at this point.
    if not os.environ.get('TESTING'):
        try:
            from app.services.azure_profile import ensure_azure_profile
            ensure_azure_profile()
        except Exception:
            pass

    # Initialize extensions with app
    db.init_app(app)

    # Configure SQLite for concurrent access:
    # - WAL mode lets readers and writers proceed in parallel (background backup
    #   thread no longer blocks user saves/deletes)
    # - busy_timeout gives the engine 10s to wait out a competing writer instead
    #   of immediately failing with "database is locked"
    if db_url.startswith('sqlite'):
        from sqlalchemy import event, text

        with app.app_context():
            engine = db.engine

            @event.listens_for(engine, 'connect')
            def _set_sqlite_pragmas(dbapi_conn, _conn_record):
                cur = dbapi_conn.cursor()
                try:
                    cur.execute('PRAGMA journal_mode=WAL')
                    cur.execute('PRAGMA busy_timeout=10000')
                    cur.execute('PRAGMA synchronous=NORMAL')
                finally:
                    cur.close()

            # Apply pragmas to the already-open connection (the listener
            # only fires on subsequent new connections).
            with engine.connect() as conn:
                conn.execute(text('PRAGMA journal_mode=WAL'))
                conn.execute(text('PRAGMA busy_timeout=10000'))
                conn.execute(text('PRAGMA synchronous=NORMAL'))
                conn.commit()
    
    # Import models to register them with SQLAlchemy
    from app import models
    
    # Create default user and preferences on app startup
    with app.app_context():
        from app.models import User, UserPreference
        from app.migrations import run_table_renames, run_migrations
        
        # Rename old tables (call_logs -> notes) before create_all
        run_table_renames(db)
        
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

        # Reconcile the shell-prefs bridge file to the stored preference so the
        # Electron shell reads the right start-minimized value at next boot
        # (self-heals after a restore, manual edit, or a version predating it).
        try:
            from app.services.shell_prefs import reconcile_shell_prefs
            reconcile_shell_prefs(UserPreference.query.first())
        except Exception:
            pass
    
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
    
    # Initialize usage telemetry hooks (before registering blueprints)
    from app.services.telemetry import init_telemetry
    init_telemetry(app)

    # Initialize diagnostic logging (correlation IDs, error capture, pruning)
    from app.services.diagnostic_log import init_diagnostic_log
    init_diagnostic_log(app)

    # Drain background milestone tracking notifications into flash
    @app.before_request
    def drain_milestone_notifications():
        from app.services.milestone_tracking import drain_notifications
        for category, message in drain_notifications():
            flash(message, category)

    # Register blueprints
    from app.routes.admin import admin_bp
    from app.routes.ai import ai_bp
    from app.routes.territories import territories_bp
    from app.routes.pods import pods_bp
    from app.routes.solution_engineers import solution_engineers_bp
    from app.routes.internal_contacts import internal_contacts_bp
    from app.routes.sellers import sellers_bp
    from app.routes.customers import customers_bp
    from app.routes.topics import topics_bp
    from app.routes.notes import notes_bp
    from app.routes.main import main_bp
    from app.routes.revenue import revenue_bp
    from app.routes.partners import partners_bp
    from app.routes.milestones import bp as milestones_bp
    from app.routes.msx import msx_bp
    from app.routes.opportunities import opportunities_bp
    from app.routes.engagements import engagements_bp
    from app.routes.connect_export import connect_export_bp
    from app.routes.backup import backup_bp
    from app.routes.reports import bp as reports_bp
    from app.routes.metrics import metrics_bp
    from app.routes.one_on_one import one_on_one_bp
    
    app.register_blueprint(admin_bp)
    app.register_blueprint(ai_bp)
    app.register_blueprint(territories_bp)
    app.register_blueprint(pods_bp)
    app.register_blueprint(solution_engineers_bp)
    app.register_blueprint(internal_contacts_bp)
    app.register_blueprint(sellers_bp)
    app.register_blueprint(customers_bp)
    app.register_blueprint(topics_bp)
    app.register_blueprint(notes_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(revenue_bp)
    app.register_blueprint(partners_bp)
    app.register_blueprint(milestones_bp)
    app.register_blueprint(msx_bp)
    app.register_blueprint(opportunities_bp)
    app.register_blueprint(engagements_bp)
    from app.routes.projects import projects_bp
    app.register_blueprint(projects_bp)
    app.register_blueprint(connect_export_bp)
    app.register_blueprint(backup_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(metrics_bp)
    app.register_blueprint(one_on_one_bp)
    from app.routes.alignment import alignment_bp
    app.register_blueprint(alignment_bp)
    
    # Start MSX token refresh job (background thread)
    # This keeps the az login token fresh for CRM API calls
    from app.services.msx_auth import start_token_refresh_job
    start_token_refresh_job(interval_seconds=300)  # Check every 5 minutes
    
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

    # Capture commit date (YYYY-MM-DD) so the changelog viewer can show
    # "what just landed" after an update without depending on commit hashes.
    try:
        date_result = subprocess.run(
            ['git', 'log', '-1', '--format=%cs', 'HEAD'],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        app.config['BOOT_COMMIT_DATE'] = date_result.stdout.strip() or None
    except Exception:
        app.config['BOOT_COMMIT_DATE'] = None

    # Persist the current boot commit to the DB so the admin Updates card
    # can resolve "what just landed" after an update without depending on
    # per-tab sessionStorage.
    #
    # Important: we ONLY update current_commit here. previous_commit is
    # written by /api/admin/update-apply right before it triggers the
    # restart, so a plain restart (crash, config tweak, dev reload) never
    # rotates the pair and the "last update" view stays accurate
    # indefinitely until the user actually deploys again.
    try:
        boot_commit = app.config.get('BOOT_COMMIT')
        if boot_commit:
            with app.app_context():
                from app.models import UserPreference, db as _db
                pref = UserPreference.query.first()
                if pref is None:
                    pref = UserPreference(current_commit=boot_commit)
                    _db.session.add(pref)
                    _db.session.commit()
                elif pref.current_commit != boot_commit:
                    pref.current_commit = boot_commit
                    _db.session.commit()
    except Exception as e:
        # Don't block boot on this - it's a UX nicety, not critical.
        import logging
        logging.getLogger(__name__).warning(f"Could not record boot commit: {e}")

    # Start background update checker (checks GitHub every 12 hours)
    from app.services.update_checker import start_update_checker
    start_update_checker(interval_seconds=43200)

    # Start telemetry flush thread (buffers events, flushes to App Insights every 30s)
    from app.services.telemetry_shipper import start_flush_thread
    start_flush_thread(app)

    # Start Copilot daily action items (sync on startup if stale, then daily at 6 AM)
    # In Flask debug mode, the reloader starts the app twice. Only run background
    # tasks in the child process (WERKZEUG_RUN_MAIN='true') or in non-debug mode.
    import os as _os
    # Process role: 'web' (default, serves requests) or 'worker' (runs the heavy
    # background schedulers + job queue). Set by app/worker.py before create_app.
    _role = _os.environ.get('SALESBUDDY_ROLE', 'web').strip().lower()
    # 'supervised' means a supervisor (or Electron main) is managing this process
    # and running a separate worker, so the web process must NOT run schedulers.
    # When unsupervised, the web falls back to running them inline (monolithic).
    _supervised = _os.environ.get('SALESBUDDY_SUPERVISED', '').strip().lower() in ('1', 'true', 'yes')
    # The werkzeug reloader (flask run --debug) starts the app twice; skip
    # background startup in the parent so threads aren't doubled. This only
    # applies to the web role - the worker never runs under the reloader, and in
    # dev FLASK_DEBUG=True would otherwise make it look like a reloader parent.
    _is_reloader_parent = (
        _role != 'worker'
        and app.debug
        and not _os.environ.get('WERKZEUG_RUN_MAIN')
    )
    if not app.config.get('TESTING') and not _is_reloader_parent:
        schedulers_started = ['update_checker', 'telemetry_flush']

        # Heavy background jobs (MSX / WorkIQ / meeting aura) run in the worker
        # process, or inline in an unsupervised web process (graceful
        # degradation) so a directly-launched web server still does background
        # work. Under the supervisor the worker owns them and the web is idle.
        if should_run_schedulers(_role, _supervised):
            from app.services.copilot_actions import start_copilot_sync_background, start_daily_scheduler
            start_copilot_sync_background(app)
            start_daily_scheduler(app)
            schedulers_started.append('copilot_actions')

            # Start milestone sync scheduler (catchup on startup, then daily at random time)
            from app.services.scheduled_sync import start_milestone_sync_background, start_daily_milestone_scheduler
            start_milestone_sync_background(app)
            start_daily_milestone_scheduler(app)
            schedulers_started.append('milestone_sync')

            # Start daily meeting cache (catchup on startup, then daily at 7 AM)
            from app.services.meeting_sync import start_meeting_sync_background, start_daily_meeting_scheduler
            start_meeting_sync_background(app)
            start_daily_meeting_scheduler(app)
            schedulers_started.append('meeting_aura')

            # Start MSX Account Teams health probe (hourly, with per-instance offset)
            from app.services.msx_health_probe import start_probe_thread
            start_probe_thread(app)
            schedulers_started.append('msx_health_probe')

        # Initialize structured lifecycle/crash logging. Records boot, detects
        # an un-clean previous shutdown, and installs crash/shutdown hooks that
        # flush pending backups. Runs for every real process (web and worker),
        # tagged by role so their run markers don't collide.
        from app.services.lifecycle import init_lifecycle_logging
        init_lifecycle_logging(app, schedulers_started, role=_role)

    return app
