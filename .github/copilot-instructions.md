# Copilot Instructions

## Project Overview

**Project Name:** Sales Buddy

**Description:** A note-taking application for Azure technical sellers to capture and retrieve customer call notes. Enables searching and filtering notes by customer, seller, technologies discussed, and other criteria.

**Target Users:** Azure technical sellers and their teammates

## Technology Stack

**Language(s):** Python 3.13

**Framework(s):** Flask

**Database:** SQLite

**Key Libraries/Packages:**
- Flask - Web framework
- Flask-SocketIO - Real-time WebSocket communication (partner sharing hub)
- Bootstrap 5 - UI components and styling
- SQLAlchemy - Database ORM (with custom idempotent migrations)
- python-dotenv - Environment variable management
- pytest - Testing framework

**Build/Package Tools:** pip, venv

## Project Structure

```
/
├── app/
│   ├── __init__.py         - Flask app factory
│   ├── models.py           - SQLAlchemy models
│   ├── migrations.py       - Custom idempotent migrations
│   ├── gateway_client.py   - AI gateway client (APIM calls)
│   ├── routes/             - Flask blueprints (one per domain)
│   │   ├── customers.py, notes.py, sellers.py, territories.py, ...
│   │   ├── ai.py           - AI-powered endpoints
│   │   ├── admin.py        - Admin panel routes
│   │   └── revenue.py      - Revenue tracking
│   └── services/           - Business logic & integrations
│       ├── partner_sharing.py  - Socket.IO sharing hub client
│       ├── msx_api.py, msx_auth.py - MSX integration
│       ├── revenue_import.py, revenue_analysis.py
│       └── telemetry.py, telemetry_shipper.py
├── infra/gateway/          - AI Gateway (deployed to Azure App Service)
│   ├── gateway.py          - Flask app (APIM → OpenAI proxy)
│   ├── sharing_hub.py      - Socket.IO sharing server
│   ├── openai_client.py    - Azure OpenAI wrapper
│   ├── prompts.py          - AI prompt templates
│   └── requirements.txt    - Gateway-specific dependencies
├── templates/              - Jinja2 HTML templates
├── static/                 - CSS, JS, images
├── tests/                  - pytest test files
├── scripts/                - Utility scripts (backup, restore, etc.)
├── .env                    - Environment variables (not committed)
└── requirements.txt        - Python dependencies
```

## Coding Standards & Best Practices

### Code Style
- Follow PEP 8 style guide for Python code
- Use type hints for function parameters and return values
- Use 4 spaces for indentation
- Maximum line length: 100 characters
- Use docstrings for all functions, classes, and modules
- **Never use em dashes** (the long dash character). Use a regular hyphen (-), a comma, or rewrite the sentence instead. Do not fake an em dash with two hyphens (--) either.

### DateTime Conventions
**CRITICAL - Follow these rules for all datetime handling:**

- **Use `datetime.now(timezone.utc)` for all server-side timestamps** (created_at, updated_at, completed_at, reviewed_at, synced_at, etc.). Never use `datetime.now()` or `datetime.utcnow()` for these fields.
- **Use `date.today()` for date-only comparisons** (fiscal quarter boundaries, overdue checks, FY season detection). This is intentionally local and correct for date-only logic.
- **`call_date` is the ONE exception** - it stores naive local time from user input. This is by design. Do NOT change it to UTC. When converting `call_date` to UTC for MSX API calls, use `cd.astimezone()` then `.astimezone(timezone.utc)` as done in `milestone_tracking.py`.
- **The `utc_now()` helper in `models.py`** returns `datetime.now(timezone.utc)`. Use it for model column defaults.
- **SQLite stores datetimes as text strings** ("YYYY-MM-DD HH:MM:SS"). No timezone info is preserved. When comparing with SQLAlchemy filters, use `datetime` objects (not `date`) to avoid string comparison issues (e.g., "2026-03-31 00:00:00" > "2026-03-31").
- **Never mock the `datetime` class at module level** in tests - it breaks all datetime usage in the route. Instead, mock `date.today()` for date checks or extract the time-dependent logic into a helper function.
- **Template rendering**: Naive datetimes are passed to Jinja2 strftime as-is. Client-side JS (`local-datetime` class) handles UTC-to-local conversion for display.

### Naming Conventions
- **Files:** snake_case (e.g., `customer_routes.py`, `note_model.py`)
- **Variables:** snake_case (e.g., `customer_name`, `note_content`)
- **Constants:** UPPER_SNAKE_CASE (e.g., `DATABASE_URL`, `MAX_NOTE_LENGTH`)
- **Functions:** snake_case with verb prefixes (e.g., `get_customer`, `create_note`, `search_by_tag`)
- **Classes:** PascalCase (e.g., `Customer`, `Note`, `User`)
- **Database tables:** snake_case plural (e.g., `customers`, `notes`, `tags`)

### Code Organization
- Use Flask blueprints in `app/routes/` - one file per domain
- Keep database models in `app/models.py`
- Keep business logic in `app/services/` - separate from route handlers
- Group related routes together with clear comments
- Keep functions focused and under 50 lines when possible
- Extract magic numbers and strings into constants at top of file
- Separate business logic from route handlers when complexity grows

### Route Placement Rules
- **NEVER put new reports, pages, or API endpoints under the `/revenue/` URL prefix or the `revenue` blueprint** unless they are specifically about revenue data import, revenue configuration, or the revenue dashboard itself. The revenue blueprint (`app/routes/revenue.py`) is strictly for revenue data management.
- **Reports that use revenue data** (whitespace analysis, synapse users, etc.) belong in the `reports` blueprint (`app/routes/reports.py`) under `/reports/` URLs with APIs under `/api/reports/`.
- **When in doubt, put it in `reports.py`** - revenue.py is for CRUD and data pipeline, not for analytical views.
- **When adding a new report**, also add it to the Reports dropdown menu in `templates/base.html` (the `navReports` `<ul>`) and to the `report_groups` list in the `reports_hub()` function in `app/routes/reports.py`. Every report must be accessible from the nav menu.

### SalesIQ Tool Registry
- **When adding a new queryable entity, report, or analytical feature**, add a corresponding read tool to `app/services/salesiq_tools.py` using the `@tool` decorator.
- Tools are thin wrappers over existing service/query code - never duplicate business logic in a tool handler.
- The test `test_salesiq_tools.py::test_tool_coverage` enforces that every core entity and report has at least one registered tool. If you add a new model or report, update the coverage test.
- Run `pytest tests/test_salesiq_tools.py` after adding or modifying tools.





### Debugging User-Reported Errors
- **If the user says there's a bug or error, BELIEVE THEM.** Do not ask them to hard-refresh, clear cache, try a different browser, or re-verify the problem. They already did that.
- **IMMEDIATELY render the page yourself** with browser tools (`open_browser_page` / `navigate_page`) and see the error firsthand.
- If your fix doesn't seem to work from reading code alone, RENDER THE PAGE. The browser doesn't lie - template code can be misleading (e.g., Jinja `{% set %}` is block-scoped and invisible across `{% block %}` boundaries).
- **Never ask the user to re-verify something they've already verified.** Use the tools instead.

### Error Handling
- Use try-except blocks for database operations
- Log errors with context (use Python logging module)
- Return user-friendly error messages in templates
- Handle database constraint violations gracefully
- Use Flask error handlers for 404, 500, etc.

### Testing
- Use pytest for all tests
- **Write tests as you implement features** - Don't wait until later
- Write unit tests for business logic and database operations
- Use Flask test client for route testing
- Tests use isolated SQLite database (configured in `tests/conftest.py`)
- Never run tests against production database
- Aim for 70%+ code coverage
- Test file naming: `test_*.py` or `*_test.py`
- **Run scoped tests during development** - Run only the test file(s) relevant to what you're building (e.g., `pytest tests/test_views.py`). Do NOT run the full suite (`pytest tests/`) - the user runs that manually.
- Add tests for any bugs discovered to prevent regression
- **Fix ALL test failures the user reports** - If the user shows you test failures, fix them ALL. Do not dismiss failures as "pre-existing" or "unrelated to our changes." If the user took the time to show you failures, they want them fixed. Period.

## Terminal Command Rules

**CRITICAL - DO NOT VIOLATE THESE RULES:**
- **NEVER run the full test suite (`pytest tests/`)** - the user runs that manually. During development, run only the relevant scoped test file(s) (e.g., `pytest tests/test_views.py`).
- **NEVER pipe, redirect, or filter pytest output** - always run pytest plain and wait for it to finish. The full suite is 900+ tests and takes 12+ minutes. Let it complete.
- **NEVER kill a running command and re-run it** - if a command is still running, WAIT. Do not start a new terminal command while one is still executing.
- **NEVER chain pytest with `| Select-Object`, `| Out-String`, `| Where-Object`, `2>&1`, or any other output manipulation** - this causes truncation and wastes massive amounts of time re-running.
- **Set timeout to 0 for pytest runs** - the suite can take over 12 minutes. Use `timeout: 0` so it doesn't get killed early.
- If output appears truncated, DO NOT re-run the command with different piping. Just re-run the same plain command and wait.

## Architecture Patterns

**Design Pattern(s):** MVC (Model-View-Controller) pattern with Flask
- Models: SQLAlchemy ORM classes
- Views: Jinja2 templates
- Controllers: Flask route handlers

**State Management:** Single-user mode (no authentication required)

**API Design:** Server-rendered templates with Jinja2 (not REST API)
- Use POST for data modifications
- Use GET for queries and searches

**Real-Time Communication:** Flask-SocketIO for partner sharing hub
- Socket.IO server runs on the AI gateway (`infra/gateway/sharing_hub.py`)
- Client-side connects from the local Flask app (`app/services/partner_sharing.py`)
- Used for sharing partner directories between Sales Buddy instances

**Database Migrations:** Custom idempotent migrations (NOT Flask-Migrate/Alembic)
- Located in `app/migrations.py`
- Runs automatically on every deployment via `startup.sh`
- Safe to run multiple times - checks before making changes
- `db.create_all()` creates new tables (never drops existing)
- Custom migrations handle `ALTER TABLE` operations idempotently
- To add a new migration:
  1. Add the column/change to the model in `models.py`
  2. Add a migration check in `app/migrations.py` using helper functions
  3. Test locally, then deploy - migration runs automatically
- **NEVER use DROP TABLE or DROP COLUMN without explicit data backup**

## Dependencies & Environment

**Required Environment Variables:**
```
SECRET_KEY=your-secret-key-here
FLASK_ENV=development
FLASK_DEBUG=True
```

**Optional Environment Variables (AI Features):**
```
# AI features use the APIM gateway - no env vars needed for AI.
# The gateway URL and Entra app ID are hardcoded in app/gateway_client.py.
# Authentication uses the caller's `az login` credential automatically.
```

**Prerequisites:**
- Python 3.13+
- pip and venv

## Development Workflow

**Setup:**
```powershell
# Create virtual environment
python -m venv venv
.\venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Set up environment
cp .env.example .env
# Edit .env if needed (database auto-creates on first run)
```

**Running Locally:**
```powershell
.\venv\Scripts\Activate.ps1
python app.py
# or
flask run
```

**IMPORTANT - Virtual Environment:**
- **ALWAYS activate venv before running Flask or any Python commands**
- Use `& C:\dev\SalesBuddy\venv\Scripts\Activate.ps1` before Flask commands
- Never run `flask run` or `python` without activating venv first
- When running Flask in background, combine: `& C:\dev\SalesBuddy\venv\Scripts\Activate.ps1 ; flask run`

**Testing:**
```powershell
pytest
pytest --cov=app tests/  # with coverage
```

## Documentation Standards

- Use docstrings for all functions, classes, and modules (Google style preferred)
- Document complex database queries with inline comments
- Keep README.md updated with setup instructions and features
- Document environment variables in .env.example
- Add comments explaining business logic, not obvious code

## Security Considerations

- Never commit .env file or secrets to Git
- Use SQLAlchemy ORM to prevent SQL injection (no raw SQL)
- Sanitize user input before displaying in templates

## Performance Guidelines

- Add database indexes on frequently queried columns (customer_id, tags, created_at)
- Use pagination for large result sets
- Eager load relationships to avoid N+1 queries
- Minimize Bootstrap JavaScript usage (only include what's needed)
- Use Flask caching for expensive queries if needed

## Environments

**Development Environment:**
- Local machine running Flask development server
- Local SQLite database (data/salesbuddy.db)
- Environment: `FLASK_ENV=development`, `FLASK_DEBUG=True`
- Used for developing and testing new features
- Safe to experiment and break things

**Production Environment:**
- Sales Buddy Flask app runs locally via `flask run` or `start.bat`
- SQLite database (persisted in `data/salesbuddy.db`)
- `update.bat` backs up the database before deploying/running migrations
- Real user data - handle with care

**AI Gateway Environment (Azure):**
- App Service: `app-notehelper-ai` in resource group `NoteHelper_Resources`
- Staging slot: `app-notehelper-ai-staging` (canary for deploys)
- Python 3.11, Gunicorn, startup command: `gunicorn --bind=0.0.0.0:8000 --threads 4 gateway:app`
- See **AI Gateway Infrastructure** section for architecture details
- See `.github/instructions/gateway-deployment.instructions.md` for deploy procedures

## UI/UX Conventions

**Visual Styling:**
- **Sellers:** Always display as badge tags with `bg-primary` styling and person icon (`<i class="bi bi-person"></i>`), unless used in page headers/titles
  - Example: `<a href="{{ url_for('seller_view', id=seller.id) }}" class="badge bg-primary text-decoration-none"><i class="bi bi-person"></i> {{ seller.name }}</a>`
- **Territories:** Always display as badge tags with `bg-info text-dark` styling and location icon (`<i class="bi bi-geo-alt"></i>`), unless used in page headers/titles
  - Example: `<a href="{{ url_for('territory_view', id=territory.id) }}" class="badge bg-info text-dark text-decoration-none"><i class="bi bi-geo-alt"></i> {{ territory.name }}</a>`
- **Topics:** Display as badge tags with `bg-warning text-dark` styling and tag icon (`<i class="bi bi-tag"></i>`)
- Maintain consistent badge styling across all views for visual parity

## Communication Style

**Tone & Personality:**
- Be chill and conversational, like you're pair programming with a friend
- Embrace a neurodivergent coding style - hyperfocus on details when they matter, but don't overthink the simple stuff
- Modern slang is fine when it flows naturally, but never force it - if it feels like you're trying too hard, just speak normally
- Appreciate good code the way gamers appreciate a clean speedrun - efficiency is satisfying
- When explaining things, keep it real and straightforward - no corporate speak or needless formality
- If something is genuinely fire or straight up broken, just say it
- Channel that "it's 2am and the code finally works" energy when celebrating successful changes, but only when it's actually earned
- The personality should be subtle background flavor, not the main character - focus on being helpful first, personality second

## AI Gateway Infrastructure

**Architecture:** All AI calls route through APIM → App Service → Azure OpenAI. No direct OpenAI SDK usage.

**Key Resources:**
- **APIM Gateway URL:** `https://apim-notehelper.azure-api.net/ai`
- **Tenant:** Microsoft corp `72f988bf-86f1-41af-91ab-2d7cd011db47` (JWT validation)
- **Gateway client:** `app/gateway_client.py` - hardcoded config, no env vars needed
- **AI is always enabled** for any user signed in with a Microsoft corporate account. No app registration or consent needed - APIM validates a standard Azure Management JWT.

**How AI authentication works:**
1. User runs `az login --tenant 72f988bf-...` (wizard does this automatically)
2. Gateway client acquires a token for `https://management.azure.com`
3. APIM validates the JWT signature, audience, issuer, and tenant
4. Gateway App Service processes the request

## Additional Notes

**Key Features:**
- Create/edit/delete notes
- Tag notes with technologies, customers, sellers, territories
- Search and filter notes by multiple criteria
- Associate notes with customer accounts
- Track who created each note and when
- User preferences (dark mode, view options)
- Clickable UI elements throughout for improved navigation

**Open Source:**
- This project is open source and intended to be easy for others to contribute to
- Write clear, self-documenting code
- Prioritize simplicity and maintainability over clever solutions

---

**Last Updated:** April 8, 2026
