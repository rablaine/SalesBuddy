"""
Idempotent database migrations for Sales Buddy.

This module provides safe, repeatable schema migrations that can run on every
deployment without risk of data loss. Each migration checks if it needs to run
before making changes.

Usage:
    from app.migrations import run_table_renames, run_migrations
    run_table_renames(db)   # Must run BEFORE db.create_all()
    run_migrations(db)      # Runs AFTER db.create_all()

Guidelines for adding new migrations:
1. Always check if the change is needed before applying (idempotent)
2. Use inspector.get_columns() to check for existing columns
3. Use inspector.get_table_names() to check for existing tables
4. Never use DROP TABLE or DROP COLUMN without explicit user confirmation
5. Add a descriptive print statement so deploy logs show what happened
"""
from sqlalchemy import inspect, text


def run_table_renames(db):
    """
    Rename tables from old call_logs naming to new notes naming.

    Must run BEFORE db.create_all() so SQLAlchemy sees the tables by their
    new names and doesn't create duplicate empty tables alongside the old ones.

    Uses ALTER TABLE RENAME TO (safe on SQLite 3.26+ which auto-updates FK
    references in other tables).
    """
    inspector = inspect(db.engine)
    existing = set(inspector.get_table_names())

    renames = [
        ('call_logs', 'notes'),
        ('call_logs_topics', 'notes_topics'),
        ('call_logs_partners', 'notes_partners'),
        ('call_logs_milestones', 'notes_milestones'),
    ]

    any_renamed = False
    with db.engine.connect() as conn:
        for old, new in renames:
            if old in existing and new not in existing:
                conn.execute(text(f"ALTER TABLE [{old}] RENAME TO [{new}]"))
                print(f"  Renamed table '{old}' -> '{new}'")
                any_renamed = True
        conn.commit()

    if any_renamed:
        print("Table renames complete.")


def run_migrations(db):
    """
    Run all idempotent schema migrations.
    
    Safe to run multiple times - only applies changes that haven't been made yet.
    This replaces Flask-Migrate/Alembic for simpler, safer deployments.
    
    Args:
        db: SQLAlchemy database instance
    """
    inspector = inspect(db.engine)
    existing_tables = inspector.get_table_names()
    
    # =========================================================================
    # Add new migrations below this line
    # =========================================================================
    
    # Migration: Upgrade milestones table for MSX integration
    _migrate_milestones_for_msx(db, inspector)
    
    # Migration: Upgrade call_date from Date to DateTime for meeting timestamps
    _migrate_call_date_to_datetime(db, inspector)
    
    # Migration: Add milestone tracker fields (due_date, dollar_value, workload, etc.)
    _migrate_milestone_tracker_fields(db, inspector)
    
    # Migration: Add due_date column to msx_tasks table
    _migrate_msx_tasks_due_date(db, inspector)
    
    # Migration: Create opportunities table and add opportunity_id FK to milestones
    _migrate_opportunities_table(db, inspector)

    # Migration: Make note_id nullable on msx_tasks (tasks can be created from milestone view)
    _migrate_msx_tasks_nullable_note(db, inspector)
    
    # Migration: Create sync_status table for tracking sync completion
    _migrate_sync_status_table(db, inspector)
    
    # Migration: Add on_deal_team column to opportunities table
    _migrate_opportunities_deal_team(db, inspector)
    
    # Migration: Add workiq_summary_prompt to user_preferences
    _migrate_workiq_summary_prompt(db, inspector)
    
    # Migration: Add guided_tour_completed to user_preferences
    _migrate_guided_tour_completed(db, inspector)
    
    # Migration: Add dismissed_update_commit to user_preferences
    _migrate_dismissed_update_commit(db, inspector)
    
    # Migration: Drop orphan tables from old auth/migration systems
    _drop_orphan_tables(db, existing_tables)

    # Migration: Create connect_exports table for Connect self-evaluation tracking
    _migrate_connect_exports_table(db, inspector)

    # Migration: Add workiq_connect_impact to user_preferences
    _migrate_workiq_connect_impact(db, inspector)

    # Migration: Drop colored_sellers column from user_preferences (feature removed)
    _drop_colored_sellers_column(db, inspector)

    # Migration: Add ai_summary column to connect_exports
    _migrate_connect_exports_ai_summary(db, inspector)

    # Migration: Normalize string TPIDs to integers (fixes type mismatch from early imports)
    _migrate_customer_tpid_to_int(db, inspector)

    # Migration: Enforce unique constraint on customers.tpid
    _migrate_customer_tpid_unique(db, inspector)

    # Migration: Drop user_id columns from all tables (single-user system)
    _drop_user_id_columns(db, inspector)

    # Migration: Add website and favicon_b64 columns to customers
    _migrate_customer_favicon_columns(db, inspector)

    # Migration: Make customer_id nullable on notes (support non-customer overview)
    _migrate_notes_nullable_customer(db, inspector)

    # Migration: Rename columns from old call_log naming to note naming
    _rename_calllog_columns(db, inspector)

    # Migration: Rename customers.overview -> account_context
    _rename_overview_to_account_context(db, inspector)

    # Migration: Add ai_enabled column to user_preferences
    _add_column_if_not_exists(db, inspector, 'user_preferences', 'ai_enabled', 'BOOLEAN DEFAULT 0 NOT NULL')

    # Migration: Add default template FK columns to user_preferences
    _add_column_if_not_exists(db, inspector, 'user_preferences', 'default_template_customer_id', 'INTEGER REFERENCES note_templates(id) ON DELETE SET NULL')
    _add_column_if_not_exists(db, inspector, 'user_preferences', 'default_template_noncustomer_id', 'INTEGER REFERENCES note_templates(id) ON DELETE SET NULL')

    # Migration: Seed built-in note templates if table is empty
    _seed_note_templates(db, inspector)

    # Migration: Add MSX detail columns to opportunities (cache-on-read)
    _migrate_opportunity_details_columns(db, inspector)

    # Migration: Rename partners.notes -> overview (avoid conflict with notes relationship)
    _rename_partner_notes_to_overview(db, inspector)

    # Migration: Add website and favicon_b64 columns to partners
    _migrate_partner_favicon_columns(db, inspector)

    # Migration: Add DAE (account owner) columns to customers
    _add_column_if_not_exists(db, inspector, 'customers', 'dae_name', 'VARCHAR(200)')
    _add_column_if_not_exists(db, inspector, 'customers', 'dae_alias', 'VARCHAR(100)')

    # Migration: Add CSAM table and customer FK
    _migrate_csam_support(db, inspector)

    # Migration: Create solution_engineers_territories M2M table (DSS territory linking)
    _migrate_se_territories(db, inspector)

    # Note: milestone_comments table is created by db.create_all() — no migration needed

    # Migration: Add review_status, review_notes, reviewed_at to revenue_analyses
    if _table_exists(inspector, 'revenue_analyses'):
        _add_column_if_not_exists(db, inspector, 'revenue_analyses', 'review_status',
                                  "VARCHAR(20) NOT NULL DEFAULT 'new'")
        _add_column_if_not_exists(db, inspector, 'revenue_analyses', 'review_notes', 'TEXT')
        _add_column_if_not_exists(db, inspector, 'revenue_analyses', 'reviewed_at', 'DATETIME')

    # =========================================================================
    # End migrations
    # =========================================================================
    
    print("Database schema up to date.")


def _add_column_if_not_exists(db, inspector, table: str, column: str, column_def: str):
    """
    Add a column to a table if it doesn't already exist.
    
    Args:
        db: SQLAlchemy database instance
        inspector: SQLAlchemy inspector instance
        table: Name of the table to modify
        column: Name of the column to add
        column_def: SQL column definition (e.g., 'TEXT', 'INTEGER DEFAULT 0')
    
    Example:
        _add_column_if_not_exists(db, inspector, 'customers', 'priority', 'INTEGER DEFAULT 0')
    """
    columns = [c['name'] for c in inspector.get_columns(table)]
    if column not in columns:
        with db.engine.connect() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}"))
            conn.commit()
        print(f"  Added column '{column}' to '{table}'")


def _add_index_if_not_exists(db, inspector, table: str, index_name: str, columns: list):
    """
    Add an index to a table if it doesn't already exist.
    
    Args:
        db: SQLAlchemy database instance
        inspector: SQLAlchemy inspector instance
        table: Name of the table
        index_name: Name for the index
        columns: List of column names to index
    
    Example:
        _add_index_if_not_exists(db, inspector, 'notes', 'ix_notes_date', ['call_date'])
    """
    existing_indexes = [idx['name'] for idx in inspector.get_indexes(table)]
    if index_name not in existing_indexes:
        cols = ', '.join(columns)
        with db.engine.connect() as conn:
            conn.execute(text(f"CREATE INDEX {index_name} ON {table} ({cols})"))
            conn.commit()
        print(f"  Added index '{index_name}' on '{table}'")


def _table_exists(inspector, table: str) -> bool:
    """Check if a table exists in the database."""
    return table in inspector.get_table_names()


def _column_exists(inspector, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    columns = [c['name'] for c in inspector.get_columns(table)]
    return column in columns


def _migrate_milestones_for_msx(db, inspector):
    """
    Migrate milestones table for MSX integration.
    
    This migration:
    1. Drops and recreates milestones table with new schema (SQLite limitation)
    2. Creates msx_tasks table
    
    Note: SQLite can't ALTER TABLE to add UNIQUE columns, so we recreate the table.
    Milestone data is cleared - will be re-created via MSX picker.
    """
    # Check if migration is needed by looking for new columns
    if _table_exists(inspector, 'milestones'):
        if not _column_exists(inspector, 'milestones', 'msx_milestone_id'):
            print("  Migrating milestones table for MSX integration...")
            
            with db.engine.connect() as conn:
                # Clear junction table first (foreign keys)
                conn.execute(text("DELETE FROM notes_milestones"))
                # SQLite can't add UNIQUE column via ALTER TABLE, so drop and recreate
                conn.execute(text("DROP TABLE milestones"))
                conn.execute(text("""
                    CREATE TABLE milestones (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        msx_milestone_id VARCHAR(50) UNIQUE,
                        milestone_number VARCHAR(50),
                        url VARCHAR(2000) NOT NULL,
                        title VARCHAR(500),
                        msx_status VARCHAR(50),
                        msx_status_code INTEGER,
                        opportunity_name VARCHAR(500),
                        customer_id INTEGER REFERENCES customers(id),
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                    )
                """))
                conn.commit()
            print("    Recreated milestones table with MSX schema")
    
    # Create msx_tasks table if it doesn't exist
    if not _table_exists(inspector, 'msx_tasks'):
        print("  Creating msx_tasks table...")
        with db.engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE msx_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    msx_task_id VARCHAR(50) NOT NULL UNIQUE,
                    msx_task_url VARCHAR(2000),
                    subject VARCHAR(500) NOT NULL,
                    description TEXT,
                    task_category INTEGER NOT NULL,
                    task_category_name VARCHAR(100),
                    duration_minutes INTEGER DEFAULT 60 NOT NULL,
                    is_hok BOOLEAN DEFAULT 0 NOT NULL,
                    note_id INTEGER REFERENCES notes(id),
                    milestone_id INTEGER NOT NULL REFERENCES milestones(id),
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
            """))
            conn.commit()
        print("    Created msx_tasks table")
    
    
def _migrate_call_date_to_datetime(db, inspector):
    """
    Upgrade call_date column from Date to DateTime for meeting timestamps.
    
    SQLite stores DATE as TEXT 'YYYY-MM-DD' and DATETIME as 'YYYY-MM-DD HH:MM:SS'.
    This migration:
    1. Adds a temporary call_datetime column
    2. Copies data with time conversion
    3. Drops old column and renames new one via table rebuild (SQLite limitation)
    
    Idempotent: Checks column type before running. Safe if already DateTime.
    """
    if not _table_exists(inspector, 'notes'):
        return
    
    # Check if call_date is already DateTime by inspecting column type
    columns = {c['name']: c for c in inspector.get_columns('notes')}
    if 'call_date' not in columns:
        return
    
    col_type = str(columns['call_date']['type']).upper()
    
    # If it's already DATETIME, skip
    if 'DATETIME' in col_type:
        return
    
    print("  Upgrading call_date from Date to DateTime...")
    
    with db.engine.connect() as conn:
        # Step 1: Add temporary datetime column (check first - may exist from partial migration)
        if 'call_datetime' not in columns:
            conn.execute(text("ALTER TABLE notes ADD COLUMN call_datetime DATETIME"))
        
        # Step 2: Copy and convert data
        # Handles various date formats safely
        conn.execute(text("""
            UPDATE notes 
            SET call_datetime = CASE
                WHEN call_date LIKE '____-__-__ %' THEN call_date
                WHEN call_date LIKE '____-__-__T%' THEN REPLACE(call_date, 'T', ' ')
                WHEN call_date LIKE '____-__-__' THEN call_date || ' 00:00:00'
                ELSE call_date || '-01-01 00:00:00'
            END
        """))
        
        # Step 3: Rebuild table with new column type (SQLite can't ALTER COLUMN)
        # Get all current columns except call_date and call_datetime
        all_cols = [c['name'] for c in inspector.get_columns('notes') 
                    if c['name'] not in ('call_date', 'call_datetime')]
        
        # Build column list for copy
        cols_csv = ', '.join(all_cols)
        
        conn.execute(text("ALTER TABLE notes RENAME TO notes_backup"))
        
        # Get the CREATE TABLE statement and modify it
        result = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='notes_backup'"
        ))
        create_sql = result.scalar()
        
        # Replace table name and fix the column type
        create_sql = create_sql.replace('notes_backup', 'notes', 1)
        # Replace DATE with DATETIME for call_date column
        create_sql = create_sql.replace(
            'call_date DATE NOT NULL', 
            'call_date DATETIME NOT NULL'
        )
        # Also handle if it was defined without explicit type keyword
        create_sql = create_sql.replace(
            'call_date DATE', 
            'call_date DATETIME'
        )
        
        conn.execute(text(create_sql))
        
        # Copy data back, using call_datetime for the call_date column
        conn.execute(text(f"""
            INSERT INTO notes ({cols_csv}, call_date)
            SELECT {cols_csv}, call_datetime FROM notes_backup
        """))
        
        conn.execute(text("DROP TABLE notes_backup"))
        conn.commit()
    
    print("    Upgraded call_date to DateTime successfully")


def _migrate_milestone_tracker_fields(db, inspector):
    """
    Add milestone tracker fields to the milestones table.
    
    New columns: due_date, dollar_value, workload, monthly_usage, last_synced_at
    """
    if not _table_exists(inspector, 'milestones'):
        return
    
    tracker_columns = [
        ('due_date', 'DATETIME'),
        ('dollar_value', 'FLOAT'),
        ('workload', 'VARCHAR(200)'),
        ('monthly_usage', 'FLOAT'),
        ('last_synced_at', 'DATETIME'),
        ('on_my_team', 'BOOLEAN DEFAULT 0'),
    ]
    
    for col_name, col_def in tracker_columns:
        _add_column_if_not_exists(db, inspector, 'milestones', col_name, col_def)
    
    # Add index on due_date for efficient tracker queries
    _add_index_if_not_exists(
        db, inspector, 'milestones', 'ix_milestones_due_date', ['due_date']
    )
    _add_index_if_not_exists(
        db, inspector, 'milestones', 'ix_milestones_msx_status', ['msx_status']
    )


def _migrate_msx_tasks_due_date(db, inspector):
    """
    Add due_date column to msx_tasks table.
    
    Stores the scheduledend value from MSX (Dynamics 365 task due date).
    """
    if not _table_exists(inspector, 'msx_tasks'):
        return
    
    _add_column_if_not_exists(db, inspector, 'msx_tasks', 'due_date', 'DATETIME')


def _migrate_opportunities_table(db, inspector):
    """
    Create opportunities table and add opportunity_id FK to milestones.
    
    Opportunities are parent entities to milestones in MSX.
    This migration:
    1. Creates the opportunities table if it doesn't exist
    2. Adds opportunity_id FK column to milestones table
    """
    if not _table_exists(inspector, 'opportunities'):
        print("  Creating opportunities table...")
        with db.engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE opportunities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    msx_opportunity_id VARCHAR(50) NOT NULL UNIQUE,
                    opportunity_number VARCHAR(50),
                    name VARCHAR(500) NOT NULL,
                    customer_id INTEGER REFERENCES customers(id),
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
            """))
            conn.commit()
        print("    Created opportunities table")
    
    # Add opportunity_id FK to milestones
    if _table_exists(inspector, 'milestones'):
        _add_column_if_not_exists(
            db, inspector, 'milestones', 'opportunity_id',
            'INTEGER REFERENCES opportunities(id)'
        )
        _add_index_if_not_exists(
            db, inspector, 'milestones', 'ix_milestones_opportunity_id',
            ['opportunity_id']
        )


def _migrate_msx_tasks_nullable_note(db, inspector):
    """
    Make note_id nullable on msx_tasks table.
    
    Tasks can now be created directly from the milestone view page
    without an associated call log. SQLite doesn't support ALTER COLUMN,
    so we rebuild the table if note_id is currently NOT NULL.
    """
    if not _table_exists(inspector, 'msx_tasks'):
        return
    
    # Check if note_id is currently NOT NULL
    columns = inspector.get_columns('msx_tasks')
    note_col = next((c for c in columns if c['name'] == 'note_id'), None)
    
    if note_col and note_col.get('nullable') == False:
        print("  Making note_id nullable on msx_tasks...")
        with db.engine.connect() as conn:
            # SQLite requires table rebuild to change NOT NULL constraint
            conn.execute(text("ALTER TABLE msx_tasks RENAME TO msx_tasks_old"))
            conn.execute(text("""
                CREATE TABLE msx_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    msx_task_id VARCHAR(50) NOT NULL UNIQUE,
                    msx_task_url VARCHAR(2000),
                    subject VARCHAR(500) NOT NULL,
                    description TEXT,
                    task_category INTEGER NOT NULL,
                    task_category_name VARCHAR(100),
                    duration_minutes INTEGER DEFAULT 60 NOT NULL,
                    is_hok BOOLEAN DEFAULT 0 NOT NULL,
                    due_date DATETIME,
                    note_id INTEGER REFERENCES notes(id),
                    milestone_id INTEGER NOT NULL REFERENCES milestones(id),
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
            """))
            conn.execute(text("""
                INSERT INTO msx_tasks (id, msx_task_id, msx_task_url, subject, description,
                    task_category, task_category_name, duration_minutes, is_hok, due_date,
                    note_id, milestone_id, created_at)
                SELECT id, msx_task_id, msx_task_url, subject, description,
                    task_category, task_category_name, duration_minutes, is_hok, due_date,
                    note_id, milestone_id, created_at
                FROM msx_tasks_old
            """))
            conn.execute(text("DROP TABLE msx_tasks_old"))
            conn.commit()
        print("    Made note_id nullable on msx_tasks")


def _migrate_sync_status_table(db, inspector):
    """
    Create sync_status table if it doesn't exist.
    
    This table tracks sync start/completion times so the onboarding wizard
    can distinguish between a completed sync and an interrupted one.
    db.create_all() handles table creation for new installs;
    this migration handles existing databases.
    """
    if not _table_exists(inspector, 'sync_status'):
        with db.engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE sync_status (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sync_type VARCHAR(50) NOT NULL UNIQUE,
                    started_at DATETIME,
                    completed_at DATETIME,
                    success BOOLEAN,
                    items_synced INTEGER,
                    details TEXT
                )
            """))
            conn.commit()
        print("  Created sync_status table")


def _migrate_opportunities_deal_team(db, inspector):
    """Add on_deal_team column to opportunities table."""
    if 'opportunities' in inspector.get_table_names():
        _add_column_if_not_exists(
            db, inspector, 'opportunities', 'on_deal_team',
            'BOOLEAN NOT NULL DEFAULT 0'
        )


def _migrate_workiq_summary_prompt(db, inspector):
    """Add workiq_summary_prompt column to user_preferences table."""
    if 'user_preferences' in inspector.get_table_names():
        _add_column_if_not_exists(
            db, inspector, 'user_preferences', 'workiq_summary_prompt',
            'TEXT'
        )


def _migrate_guided_tour_completed(db, inspector):
    """Add guided_tour_completed column to user_preferences table."""
    if 'user_preferences' in inspector.get_table_names():
        _add_column_if_not_exists(
            db, inspector, 'user_preferences', 'guided_tour_completed',
            'BOOLEAN NOT NULL DEFAULT 0'
        )


def _migrate_dismissed_update_commit(db, inspector):
    """Add dismissed_update_commit column to user_preferences table."""
    if 'user_preferences' in inspector.get_table_names():
        _add_column_if_not_exists(
            db, inspector, 'user_preferences', 'dismissed_update_commit',
            'VARCHAR(7)'
        )


def _drop_orphan_tables(db, existing_tables):
    """Drop tables left over from old auth system and Alembic migrations."""
    orphan_tables = ['account_linking_requests', 'ai_config', 'alembic_version']
    for table in orphan_tables:
        if table in existing_tables:
            db.session.execute(text(f'DROP TABLE IF EXISTS {table}'))
            db.session.commit()
            print(f"  Dropped orphan table: {table}")


def _migrate_connect_exports_table(db, inspector):
    """Create connect_exports table for tracking Connect self-evaluation exports."""
    if 'connect_exports' not in inspector.get_table_names():
        db.session.execute(text("""
            CREATE TABLE connect_exports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(200) NOT NULL,
                start_date DATE NOT NULL,
                end_date DATE NOT NULL,
                note_count INTEGER NOT NULL DEFAULT 0,
                customer_count INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        db.session.commit()
        print("  Created table: connect_exports")


def _migrate_workiq_connect_impact(db, inspector):
    """Add workiq_connect_impact boolean to user_preferences table."""
    if 'user_preferences' in inspector.get_table_names():
        _add_column_if_not_exists(
            db, inspector, 'user_preferences', 'workiq_connect_impact',
            'BOOLEAN NOT NULL DEFAULT 1'
        )


def _drop_colored_sellers_column(db, inspector):
    """Drop colored_sellers column from user_preferences (feature removed).
    
    SQLite doesn't support DROP COLUMN directly in older versions, but Python 3.13
    uses SQLite 3.39+ which supports it. We check if the column exists first.
    """
    if 'user_preferences' in inspector.get_table_names():
        columns = [col['name'] for col in inspector.get_columns('user_preferences')]
        if 'colored_sellers' in columns:
            db.session.execute(text(
                'ALTER TABLE user_preferences DROP COLUMN colored_sellers'
            ))
            db.session.commit()
            print("  Dropped column: user_preferences.colored_sellers")


def _migrate_connect_exports_ai_summary(db, inspector):
    """Add ai_summary TEXT column to connect_exports table."""
    if 'connect_exports' in inspector.get_table_names():
        _add_column_if_not_exists(
            db, inspector, 'connect_exports', 'ai_summary', 'TEXT'
        )


def _migrate_customer_tpid_to_int(db, inspector):
    """Cast any string-typed TPIDs to integers.

    Early MSX imports stored TPIDs as strings (e.g. '2853766') because the
    API returns them that way.  The column is BigInteger, but SQLite's
    flexible typing allowed strings through.  This causes type-mismatch
    issues when doing in-memory set lookups.  Idempotent -- only updates
    rows where typeof(tpid) != 'integer'.
    """
    if 'customers' not in inspector.get_table_names():
        return

    from sqlalchemy import text

    rows = db.session.execute(text(
        "SELECT COUNT(*) FROM customers WHERE typeof(tpid) != 'integer' AND tpid IS NOT NULL"
    )).scalar()

    if rows and rows > 0:
        print(f"  Migrating: casting {rows} string TPIDs to integers")
        db.session.execute(text(
            "UPDATE customers SET tpid = CAST(tpid AS INTEGER) "
            "WHERE typeof(tpid) != 'integer' AND tpid IS NOT NULL"
        ))
        db.session.commit()
        print(f"    Converted {rows} TPIDs to integer type")


def _migrate_customer_tpid_unique(db, inspector):
    """Enforce a unique constraint on customers.tpid.

    Before adding the constraint, remove duplicate rows (keeping the one
    with the most call logs for each TPID).  This is idempotent -- if the
    unique index already exists, we skip.
    """
    if 'customers' not in inspector.get_table_names():
        return

    # Check if a unique index on tpid already exists
    indexes = inspector.get_indexes('customers')
    for idx in indexes:
        if idx.get('unique') and idx.get('column_names') == ['tpid']:
            return  # Already migrated

    # Also check unique constraints
    uniques = inspector.get_unique_constraints('customers')
    for uc in uniques:
        if uc.get('column_names') == ['tpid']:
            return  # Already migrated

    print("  Migrating: enforcing unique constraint on customers.tpid")

    # Step 1: deduplicate -- keep the row with the most notes per TPID
    from sqlalchemy import text
    dupes = db.session.execute(text("""
        SELECT tpid, COUNT(*) AS cnt
        FROM customers
        GROUP BY tpid
        HAVING COUNT(*) > 1
    """)).fetchall()

    removed = 0
    for tpid_val, cnt in dupes:
        rows = db.session.execute(text("""
            SELECT c.id,
                   (SELECT COUNT(*) FROM notes WHERE customer_id = c.id) AS log_count
            FROM customers c
            WHERE c.tpid = :tpid
            ORDER BY log_count DESC, c.id ASC
        """), {"tpid": tpid_val}).fetchall()

        # Keep the first (most call logs, lowest id as tiebreaker)
        keep_id = rows[0][0]
        for row in rows[1:]:
            delete_id = row[0]
            # Re-parent any call logs from the duplicate to the keeper
            db.session.execute(text(
                "UPDATE notes SET customer_id = :keep WHERE customer_id = :dup"
            ), {"keep": keep_id, "dup": delete_id})
            # Re-parent milestones
            db.session.execute(text(
                "UPDATE milestones SET customer_id = :keep "
                "WHERE customer_id = :dup"
            ), {"keep": keep_id, "dup": delete_id})
            # Re-parent opportunities
            db.session.execute(text(
                "UPDATE opportunities SET customer_id = :keep "
                "WHERE customer_id = :dup"
            ), {"keep": keep_id, "dup": delete_id})
            # Re-parent revenue data
            db.session.execute(text(
                "UPDATE customer_revenue_data SET customer_id = :keep "
                "WHERE customer_id = :dup"
            ), {"keep": keep_id, "dup": delete_id})
            # Re-parent revenue analysis
            db.session.execute(text(
                "UPDATE customer_revenue_analysis SET customer_id = :keep "
                "WHERE customer_id = :dup"
            ), {"keep": keep_id, "dup": delete_id})
            # Remove M2M references (customers_verticals)
            db.session.execute(text(
                "DELETE FROM customers_verticals WHERE customer_id = :dup"
            ), {"dup": delete_id})
            # Delete the duplicate customer
            db.session.execute(text(
                "DELETE FROM customers WHERE id = :dup"
            ), {"dup": delete_id})
            removed += 1

    if removed:
        db.session.commit()
        print(f"    Removed {removed} duplicate customer rows")

    # Step 2: create the unique index
    db.session.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_customers_tpid ON customers (tpid)"
    ))
    db.session.commit()
    print("    Created unique index: uq_customers_tpid")


def _drop_user_id_columns(db, inspector):
    """Drop user_id columns from all tables.

    This is a single-user system.  The user_id FK was vestigial from a
    multi-user era and is no longer referenced anywhere in the codebase.

    SQLite cannot ALTER TABLE DROP COLUMN when the column participates
    in a FOREIGN KEY constraint, so we use the standard table-rebuild
    pattern: rename the old table, create a fresh copy from the current
    SQLAlchemy model (which no longer defines user_id), copy data over,
    and drop the old copy.
    """
    tables_with_user_id = [
        'pods', 'solution_engineers', 'verticals', 'territories',
        'sellers', 'customers', 'topics', 'specialties', 'partners',
        'partner_contacts', 'notes', 'opportunities', 'milestones',
        'user_preferences', 'ai_query_log', 'revenue_imports',
        'revenue_config', 'connect_exports',
    ]

    # Determine which tables still carry user_id
    tables_to_migrate = []
    for table in tables_with_user_id:
        if not _table_exists(inspector, table):
            continue
        columns = [col['name'] for col in inspector.get_columns(table)]
        if 'user_id' in columns:
            tables_to_migrate.append(
                (table, [c for c in columns if c != 'user_id'])
            )

    if not tables_to_migrate:
        return  # Already migrated

    # Disable FK enforcement for the rebuild
    db.session.commit()
    db.session.execute(text('PRAGMA foreign_keys=OFF'))

    for table, keep_cols in tables_to_migrate:
        tmp = f'_old_{table}'

        # 1. Rename original -> temp
        db.session.execute(text(f'ALTER TABLE "{table}" RENAME TO "{tmp}"'))
        db.session.commit()

        # 2. Drop indexes that carried over to the temp table so the
        #    new table can recreate them with the same names.
        idx_rows = db.session.execute(text(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name=:t "
            "AND name NOT LIKE 'sqlite_%'"
        ), {'t': tmp}).fetchall()
        for row in idx_rows:
            db.session.execute(text(f'DROP INDEX IF EXISTS "{row[0]}"'))
        db.session.commit()

        # 3. Create the table fresh from current model metadata
        model_table = db.metadata.tables.get(table)
        if model_table is None:
            # No model found -- undo rename and skip
            db.session.execute(text(
                f'ALTER TABLE "{tmp}" RENAME TO "{table}"'
            ))
            db.session.commit()
            continue
        model_table.create(db.engine)

        # 4. Copy data (only columns present in both old and new schema)
        new_col_names = {col.name for col in model_table.columns}
        copy_cols = [c for c in keep_cols if c in new_col_names]
        cols_csv = ', '.join(f'"{c}"' for c in copy_cols)
        db.session.execute(text(
            f'INSERT INTO "{table}" ({cols_csv}) '
            f'SELECT {cols_csv} FROM "{tmp}"'
        ))
        db.session.commit()

        # 5. Drop the old copy
        db.session.execute(text(f'DROP TABLE "{tmp}"'))
        db.session.commit()
        print(f"  Dropped column: {table}.user_id")

    # Re-enable FK enforcement
    db.session.execute(text('PRAGMA foreign_keys=ON'))
    db.session.commit()


def _migrate_customer_favicon_columns(db, inspector):
    """Add website and favicon_b64 columns to customers table for favicon support."""
    if 'customers' not in inspector.get_table_names():
        return
    _add_column_if_not_exists(db, inspector, 'customers', 'website', 'VARCHAR(500)')
    _add_column_if_not_exists(db, inspector, 'customers', 'favicon_b64', 'TEXT')


def _migrate_notes_nullable_customer(db, inspector):
    """Make customer_id nullable on notes to support non-customer overview.
    
    SQLite doesn't support ALTER COLUMN, so we recreate the table with the
    new schema and copy data over.
    """
    if 'notes' not in inspector.get_table_names():
        return
    
    # Check if customer_id is already nullable
    columns = inspector.get_columns('notes')
    for col in columns:
        if col['name'] == 'customer_id':
            if col.get('nullable', True):
                # Already nullable (or can't determine -- skip)
                return
            break
    else:
        # customer_id column not found, nothing to do
        return
    
    print("Migration: Making customer_id nullable on notes...")
    
    db.session.execute(text('PRAGMA foreign_keys=OFF'))
    db.session.commit()
    
    # Get existing columns for the copy
    col_names = [c['name'] for c in columns]
    col_list = ', '.join(col_names)
    
    db.session.execute(text('''
        CREATE TABLE notes_new (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER REFERENCES customers(id),
            call_date DATETIME NOT NULL,
            content TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
    '''))
    
    # Copy data (only columns that exist in both old and new)
    new_cols = ['id', 'customer_id', 'call_date', 'content', 'created_at', 'updated_at']
    shared = [c for c in new_cols if c in col_names]
    shared_list = ', '.join(shared)
    db.session.execute(text(f'INSERT INTO notes_new ({shared_list}) SELECT {shared_list} FROM notes'))
    
    db.session.execute(text('DROP TABLE notes'))
    db.session.execute(text('ALTER TABLE notes_new RENAME TO notes'))
    
    db.session.execute(text('PRAGMA foreign_keys=ON'))
    db.session.commit()
    
    print("  -> customer_id is now nullable on notes")


def _rename_calllog_columns(db, inspector):
    """
    Rename columns from old call_log naming to new note naming.

    Uses ALTER TABLE RENAME COLUMN (requires SQLite 3.25+, Python 3.13
    ships with 3.46+). Idempotent: only renames if old column exists and
    new column does not.
    """
    renames = [
        # Association tables: call_log_id -> note_id
        ('notes_topics', 'call_log_id', 'note_id'),
        ('notes_partners', 'call_log_id', 'note_id'),
        ('notes_milestones', 'call_log_id', 'note_id'),
        # FK columns in other tables
        ('msx_tasks', 'call_log_id', 'note_id'),
        ('revenue_engagements', 'call_log_id', 'note_id'),
        # Renamed value columns
        ('connect_exports', 'call_log_count', 'note_count'),
        # Customer "notes" text column -> "overview" (avoids collision with Note relationship)
        ('customers', 'notes', 'overview'),
    ]

    for table, old_col, new_col in renames:
        if not _table_exists(inspector, table):
            continue
        if _column_exists(inspector, table, old_col) and not _column_exists(inspector, table, new_col):
            with db.engine.connect() as conn:
                conn.execute(text(
                    f"ALTER TABLE [{table}] RENAME COLUMN [{old_col}] TO [{new_col}]"
                ))
                conn.commit()
            print(f"  Renamed column '{table}.{old_col}' -> '{new_col}'")


def _rename_overview_to_account_context(db, inspector):
    """Rename customers.overview -> account_context for clarity."""
    if not _table_exists(inspector, 'customers'):
        return
    if _column_exists(inspector, 'customers', 'overview') and not _column_exists(inspector, 'customers', 'account_context'):
        with db.engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE [customers] RENAME COLUMN [overview] TO [account_context]"
            ))
            conn.commit()
        print("  Renamed column 'customers.overview' -> 'account_context'")


def _rename_partner_notes_to_overview(db, inspector):
    """Rename partners.notes -> overview to avoid conflict with notes relationship."""
    if not _table_exists(inspector, 'partners'):
        return
    if _column_exists(inspector, 'partners', 'notes') and not _column_exists(inspector, 'partners', 'overview'):
        with db.engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE [partners] RENAME COLUMN [notes] TO [overview]"
            ))
            conn.commit()
        print("  Renamed column 'partners.notes' -> 'overview'")


def _migrate_partner_favicon_columns(db, inspector):
    """Add website and favicon_b64 columns to partners table for favicon support."""
    if not _table_exists(inspector, 'partners'):
        return
    _add_column_if_not_exists(db, inspector, 'partners', 'website', 'VARCHAR(255)')
    _add_column_if_not_exists(db, inspector, 'partners', 'favicon_b64', 'TEXT')


def _seed_note_templates(db, inspector):
    """Seed built-in note templates if the table is empty."""
    if not _table_exists(inspector, 'note_templates'):
        return

    with db.engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM note_templates")).scalar()
        if count > 0:
            return  # Already has templates

        templates = [
            (
                'Standard Call Log',
                '<h2>Attendees</h2><ul><li><br></li></ul>'
                '<h2>Discussion Points</h2><ul><li><br></li></ul>'
                '<h2>Action Items</h2><ul><li><br></li></ul>'
                '<h2>Next Steps</h2><ul><li><br></li></ul>',
            ),
            (
                'Quick Check-In',
                '<h2>Summary</h2><p><br></p>'
                '<h2>Follow-up</h2><ul><li><br></li></ul>',
            ),
            (
                'Technical Deep Dive',
                '<h2>Architecture Discussed</h2><p><br></p>'
                '<h2>Challenges</h2><ul><li><br></li></ul>'
                '<h2>Recommendations</h2><ul><li><br></li></ul>'
                '<h2>Resources Shared</h2><ul><li><br></li></ul>',
            ),
        ]

        for name, content in templates:
            conn.execute(text(
                "INSERT INTO note_templates (name, content, is_builtin, created_at, updated_at) "
                "VALUES (:name, :content, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ), {"name": name, "content": content})
        conn.commit()
        print(f"  Seeded {len(templates)} built-in note templates")


def _migrate_opportunity_details_columns(db, inspector):
    """Add MSX detail columns to opportunities table (cache-on-read pattern)."""
    if not _table_exists(inspector, 'opportunities'):
        return

    columns = [
        ('statecode', 'INTEGER'),
        ('state', 'VARCHAR(50)'),
        ('status_reason', 'VARCHAR(100)'),
        ('estimated_value', 'FLOAT'),
        ('estimated_close_date', 'VARCHAR(30)'),
        ('owner_name', 'VARCHAR(200)'),
        ('compete_threat', 'VARCHAR(100)'),
        ('customer_need', 'TEXT'),
        ('description', 'TEXT'),
        ('msx_url', 'VARCHAR(500)'),
        ('cached_comments_json', 'TEXT'),
        ('details_fetched_at', 'DATETIME'),
    ]
    for col_name, col_def in columns:
        _add_column_if_not_exists(db, inspector, 'opportunities', col_name, col_def)

    # Migration: Add FY transition columns to user_preferences
    _add_column_if_not_exists(db, inspector, 'user_preferences', 'fy_transition_active', 'BOOLEAN DEFAULT 0 NOT NULL')
    _add_column_if_not_exists(db, inspector, 'user_preferences', 'fy_transition_label', 'VARCHAR(10)')
    _add_column_if_not_exists(db, inspector, 'user_preferences', 'fy_transition_started', 'DATETIME')
    _add_column_if_not_exists(db, inspector, 'user_preferences', 'fy_sync_complete', 'BOOLEAN DEFAULT 0 NOT NULL')
    _add_column_if_not_exists(db, inspector, 'user_preferences', 'fy_last_completed', 'VARCHAR(10)')


def _migrate_csam_support(db, inspector):
    """Add customer_csams table, customers_csams M2M table, and csam_id FK to customers.

    The customer_csams table stores CSAM records from MSX. The customers_csams
    table is the M2M association showing which CSAMs MSX lists for each customer.
    The csam_id FK on customers is the user's chosen primary CSAM.
    """
    # customer_csams table (created by db.create_all() but we guard for safety)
    if not _table_exists(inspector, 'customer_csams'):
        with db.engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE customer_csams (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(200) NOT NULL,
                    alias VARCHAR(100),
                    created_at DATETIME NOT NULL DEFAULT (CURRENT_TIMESTAMP)
                )
            """))
            conn.commit()
        print("  Created table 'customer_csams'")

    # customers_csams M2M association table
    if not _table_exists(inspector, 'customers_csams'):
        with db.engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE customers_csams (
                    customer_id INTEGER NOT NULL REFERENCES customers(id),
                    csam_id INTEGER NOT NULL REFERENCES customer_csams(id),
                    PRIMARY KEY (customer_id, csam_id)
                )
            """))
            conn.commit()
        print("  Created table 'customers_csams'")

    # csam_id FK on customers
    _add_column_if_not_exists(db, inspector, 'customers', 'csam_id',
                              'INTEGER REFERENCES customer_csams(id) ON DELETE SET NULL')


def _migrate_se_territories(db, inspector):
    """Create solution_engineers_territories M2M table for DSS territory linking."""
    if not _table_exists(inspector, 'solution_engineers_territories'):
        with db.engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE solution_engineers_territories (
                    solution_engineer_id INTEGER NOT NULL REFERENCES solution_engineers(id),
                    territory_id INTEGER NOT NULL REFERENCES territories(id),
                    PRIMARY KEY (solution_engineer_id, territory_id)
                )
            """))
            conn.commit()
        print("  Created table 'solution_engineers_territories'")
