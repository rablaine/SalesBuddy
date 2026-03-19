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
- See **Gateway Deployment Rules** section for deploy procedures

## Git & Version Control

**Branching Strategy:** Feature branches off `main`
- **ALWAYS create feature branches for new work** - Never commit directly to `main`
- Create feature branches from `main` for all changes
- Merge back to `main` only when feature is complete and tested

**Branch Naming:** When attempting to commit to `main`, stop and prompt user for feature branch name
- Ask: "What should we call this feature branch?"
- Suggested format: `feature/short-description` or `fix/bug-description`
- Examples: `feature/export-import`, `fix/admin-permissions`, `feature/email-fields`

**Commit Message Format:** Conventional Commits
- `feat:` - New feature
- `fix:` - Bug fix
- `docs:` - Documentation changes
- `refactor:` - Code refactoring
- `test:` - Test additions or changes

**Development Workflow:**
1. Create feature branch: `git checkout -b feature/your-feature-name`
2. Write code and corresponding tests together
3. Run scoped tests for the feature you're building (e.g., `pytest tests/test_views.py`)
4. **Prompt user to manually test new features or bug fixes** - Before committing, always ask the user to test the changes in the running app to verify everything works as expected
5. Commit to feature branch with descriptive message
6. **STOP AND WAIT for user confirmation** before merging to `main` - **NEVER merge to main without explicit user approval**
7. When user says ready: merge to `main` with `--no-ff` and push
   - **Always use `git merge --no-ff`** to preserve feature branch history

**CRITICAL - DO NOT AUTO-MERGE:**
- **NEVER merge a feature branch to `main` on your own** - always wait for the user to test and explicitly say to merge
- Building a feature and committing to the feature branch is fine - merging to `main` requires user sign-off
- If the user says "commit" that does NOT mean "merge to main" - it means commit to the current feature branch only
- Merging to `main` is a deployment gate - treat it seriously

**Merge to Production Checklist:**
- Scoped tests passing for changed code
- User explicitly confirms "ready to deploy" or "merge to main"
- Code follows PEP 8 standards
- No secrets or .env file committed
- Tests included for new features or bug fixes

**CRITICAL - Gateway Deployment Rules:**
- **NEVER deploy to prod without verifying staging first.** Deploy to staging → hit `/health` → confirm 200 → only then deploy to prod.
- **NEVER deploy to both slots simultaneously.** Staging is the canary. If staging breaks, prod is unaffected.
- **Before building a deploy zip, include ALL required files** (see manifest below). Do not guess - verify.
- **After deploying, verify with `GET /health`** (returns `{"status": "ok"}`). See HTTP status reference below.

**Gateway Deploy Zip - Required Files:**
All 5 files from `infra/gateway/` must be in the zip root:
1. `gateway.py` - Main Flask app
2. `sharing_hub.py` - Socket.IO sharing server
3. `openai_client.py` - Azure OpenAI client wrapper
4. `prompts.py` - AI prompt templates
5. `requirements.txt` - Python dependencies

If `gateway.py` adds new imports in the future, the new files must also be included.

**Gateway HTTP Status Cheat Sheet:**
| Status | Meaning |
|--------|-----------------------------------------------|
| `200`  | Healthy - app is running and responding |
| `403`  | App is running, but auth rejected the request (check gateway secret / JWT) |
| `404`  | Endpoint doesn't exist (check route definitions - NOT "service is down") |
| `502`  | Container failed to start (check App Service logs: `az webapp log tail`) |
| `503`  | App Service is restarting or overloaded |

**Gateway Rollback Procedure:**
If a deploy breaks a slot:
1. Check logs: `az webapp log tail -g NoteHelper_Resources -n app-notehelper-ai [-s staging]`
2. Redeploy the last known-good zip: `az webapp deploy -g NoteHelper_Resources -n app-notehelper-ai [-s staging] --src-path infra/gateway/gateway-deploy.zip --type zip --clean true`
3. Verify with `GET /health` → 200

**GitHub Interactions:**
- **Use `gh` CLI for all GitHub operations** - issues, PRs, comments, labels, etc.
- Examples: `gh issue comment 46 --body "Fixed in commit abc123"`, `gh issue list`, `gh pr create`
- Do NOT ask about MCP tools or other methods - just use `gh` directly

## External Actions Safety

**Before any action that modifies systems outside the local workspace, pause and confirm:**
- **Deploying to Azure** (staging or prod) - state the target slot and what's being deployed
- **Pushing to remote** (`git push`) - state the branch and what's being pushed
- **Deleting remote resources** (branches, Azure resources, deployed code)
- **Running `az` commands that modify infrastructure** (config changes, restarts, identity assignments)

The rule: **if it leaves your machine, say what you're doing and why before doing it.** For destructive or hard-to-reverse actions, wait for explicit user confirmation.

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
- **Entra App Registration:** `NoteHelper-AI-Gateway`, App ID `0f6db4af-332c-4fd5-b894-77fadb181e5c`
- **Tenant:** Microsoft corp `72f988bf-86f1-41af-91ab-2d7cd011db47` (JWT validation)
- **Gateway client:** `app/gateway_client.py` - hardcoded config, no env vars needed
- **AI is always enabled** - the onboarding wizard enforces consent before the user can access the product, so there is no per-user AI gate

**How AI consent works:**
1. User runs `az login --scope api://0f6db4af-332c-4fd5-b894-77fadb181e5c/.default` (wizard does this automatically)
2. Browser shows Entra consent prompt if first time
3. After accepting, `POST /api/admin/ai-enable` validates token acquisition and records consent in `UserPreference.ai_enabled`
4. AI features are available immediately - no template-level gating

**Revoking AI consent for testing:**

To reset a user to the "no consent" state for testing the first-time consent flow:

```powershell
# 1. Get a Graph API token
$token = (az account get-access-token --resource "https://graph.microsoft.com" --query accessToken -o tsv)
$headers = @{ Authorization = "Bearer $token" }

# 2. Find the gateway app's service principal ID in the corp tenant
$sp = Invoke-RestMethod -Uri "https://graph.microsoft.com/v1.0/servicePrincipals?`$filter=appId eq '0f6db4af-332c-4fd5-b894-77fadb181e5c'" -Headers $headers
$spId = $sp.value[0].id  # Should be 4fdba304-193d-42b9-ad1e-273381ef8265

# 3. Find the user's consent grant for that SP (it's the clientId, not resourceId)
$grants = Invoke-RestMethod -Uri "https://graph.microsoft.com/v1.0/me/oauth2PermissionGrants" -Headers $headers
$grant = $grants.value | Where-Object { $_.clientId -eq $spId }
$grant | Select-Object id, clientId, resourceId, scope  # Verify it's the right one

# 4. Delete the consent grant
Invoke-RestMethod -Method DELETE -Uri "https://graph.microsoft.com/v1.0/oauth2PermissionGrants/$($grant.id)" -Headers $headers

# 5. Clear az CLI token cache and re-login WITHOUT the scope
az account clear
az login --tenant 72f988bf-86f1-41af-91ab-2d7cd011db47

# 6. Verify consent is revoked (should show AADSTS65001 consent_required)
az account get-access-token --resource "api://0f6db4af-332c-4fd5-b894-77fadb181e5c" 2>&1

# 7. (Optional) Reset the DB flag too if testing the full wizard
# Move data/salesbuddy.db to data/salesbuddy.db.bak
```

**Key detail:** The consent grant's `clientId` is the gateway SP ID (not `resourceId`). The `resourceId` on the grant points to the Microsoft Graph SP.

## Additional Notes

**Key Features:**
- Create/edit/delete notes (call logs)
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

**Last Updated:** March 14, 2026
