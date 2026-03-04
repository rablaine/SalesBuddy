# NoteHelper

A single-user note-taking application for Azure technical sellers to capture and retrieve customer call notes. Enables searching and filtering notes by customer, seller, technologies discussed, and other criteria.

## Getting Started

### Prerequisites

- **Python 3.13+** (the launcher can install this for you)
- **Git** (required for updates; the launcher can install this for you. Without it you can still [download the ZIP](https://github.com/rablaine/NoteHelper/archive/refs/heads/main.zip) to get started, but updates won't work)
- **Azure CLI** (optional - required for MSX and AI features; the launcher can install this too)
- **Node.js 18+** (optional - required for WorkIQ meeting import; the launcher can install this too)
- **VPN connection** - required for MSX integration (account imports, milestones)

### Quick Start

The fastest way to get running — clone (or download and extract the ZIP) and run `start.bat`. It checks for prerequisites, offers to install anything missing via `winget`, then sets up the app:

> **Recommended:** Install to a local path like `C:\prod\NoteHelper` rather than your Desktop or Documents folder. OneDrive will try to sync the Python virtual environment (~100MB+), which slows things down and wastes space.

```powershell
# Option A: Clone with Git
git clone https://github.com/rablaine/NoteHelper.git C:\prod\NoteHelper
cd C:\prod\NoteHelper
start.bat

# Option B: Download ZIP from GitHub, extract to C:\prod\NoteHelper, then run start.bat
```

The script will:
1. Check for Python 3.13+, Azure CLI, and Node.js — offer to install via `winget` if missing
2. Create a Python virtual environment (if one doesn't exist)
3. Install all dependencies from `requirements.txt`
4. Create a `.env` file with a generated secret key (if one doesn't exist)
5. Start the server on `http://localhost:5000`

On subsequent runs, the script detects the existing venv and `.env`, installs any new dependencies, and launches the app.

> **What the script does NOT do:** It does not set up an Azure OpenAI service principal, GPT deployment, or fill in AI credentials in `.env`. Those are optional steps you can do later — see [AI Features](#ai-features-optional) for instructions.

> **Next steps:** Once the server is running, check out [Initial Setup (First Run)](#initial-setup-first-run) to import your accounts and milestones, then optionally [AI Features](#ai-features-optional) to enable auto-tagging and meeting summaries.

### Manual Setup

If you prefer to set things up yourself:

1. **Clone the repository:**
```bash
git clone https://github.com/rablaine/NoteHelper.git
cd NoteHelper
```

2. **Create virtual environment:**
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

3. **Install dependencies:**
```bash
pip install -r requirements.txt
```

4. **Set up environment variables:**
```powershell
copy .env.example .env
# Generate a secret key and add it to .env:
python -c "import secrets; print(secrets.token_hex(32))"
```

5. **Start the server:**
```bash
python run.py
```

6. **Visit** `http://localhost:5000` in your browser

> **Note:** The database will be created automatically in `data/notehelper.db` on first run.

### Initial Setup (First Run)

When you first launch NoteHelper, a **guided setup wizard** walks you through connecting your data:

1. **Welcome** — quick overview of NoteHelper and what it does
2. **Authenticate with Azure** — the wizard checks for an existing `az login` session and prompts you to authenticate if needed. This is required for MSX integration (accounts, milestones).
3. **Import Accounts** — pulls your customer accounts from MSX with one click
4. **Import Milestones (recommended)** — syncs milestone data for your accounts from MSX. It's a single click and gives you milestone tracking right away.
5. **Import Revenue Data (recommended)** — import a revenue CSV from the ACR Service Level Subscription report to power the Revenue Analyzer (trend charts, service breakdowns, growth tracking)

You can skip steps and come back later — the wizard remembers your progress. You can re-run the setup wizard anytime from the **Admin Panel** — it shows your current progress and lets you pick up where you left off.

All of these imports can also be run independently from the Admin Panel, Milestone Tracker, and Revenue Analyzer after initial setup.

## Running Tests

```powershell
pytest
pytest --cov=app tests/  # with coverage
```

## Starting & Updating

NoteHelper uses a single smart script (`scripts/server.ps1`) that handles everything: first-run setup, starting the server, checking for updates, and applying new versions.

### Quick start

Double-click `start.bat`. On first run it will:
1. Check for Python 3.13+
2. Create a virtual environment and install dependencies
3. Create `.env` from `.env.example` with a generated secret key
4. Start the server using [Waitress](https://docs.pylonsproject.org/projects/waitress/) on the port from `.env`

On subsequent runs, it checks for updates from GitHub and applies them automatically before starting.

### Updating

Double-click `update.bat` to update. This runs the full update cycle:

1. Stops the running server
2. **Backs up your database** to `data/notehelper_backup_YYYY-MM-DD_HHMMSS.db`
3. Pulls the latest code from GitHub
4. Installs any new/updated dependencies
5. Runs database migrations
6. Restarts the server

If anything fails, it restarts the server with the previous code.

You can also run it from PowerShell:

```powershell
.\scripts\server.ps1          # Smart mode: bootstrap, update, or start as needed
.\scripts\server.ps1 -Force   # Full update cycle regardless of state
```

### Script files

| File | Purpose |
|------|---------|  
| `start.bat` | Double-click launcher (calls `scripts/server.ps1`) |
| `stop.bat` | Double-click to stop the server |
| `update.bat` | Update shortcut that runs `scripts/server.ps1 -Force` |
| `backup.bat` | Run a backup now or set up automatic backups |
| `restore.bat` | Interactive restore from a backup |
| `scripts/server.ps1` | The brain - handles setup, updates, and server management |
| `scripts/backup.ps1` | Backup engine - copy, rotate, schedule |
| `scripts/restore.ps1` | Restore engine - browse, compare, swap |

> **Admin elevation:** Batch files automatically request admin (UAC prompt) only when `PORT` in `.env` is below 1024 (e.g. port 80). For higher ports like 8080, they run without elevation.

## Backups

NoteHelper can automatically back up your database to OneDrive daily. Backups are simple file copies with daily/weekly/monthly retention rotation.

### Setting Up Automatic Backups

The first time you run `start.bat`, it will offer to configure automatic backups. You can also set them up manually:

```powershell
.\scripts\backup.ps1 -Setup    # Configure OneDrive path + register daily scheduled task
.\scripts\backup.ps1 -Status   # Show backup status and recent backups
.\scripts\backup.ps1 -Remove   # Remove the scheduled task
.\scripts\backup.ps1            # Run a backup right now
```

Or just double-click `backup.bat` to run a backup.

### Restoring from a Backup

Double-click `restore.bat` for an interactive restore. It will:

1. List all available backups (from OneDrive and local) with dates, sizes, and contents
2. Show your current database stats for comparison
3. Stop the server, create a safety backup of the current DB, swap in the selected backup, and restart

### What Gets Backed Up

- **Database only** (`data/notehelper.db`) - contains all your call logs, customers, sellers, revenue data, etc.
- **Configuration** (`data/backup_config.json`) is NOT backed up (it stays local so restore doesn't break the backup setup)

### Retention Policy (defaults)

| Tier | Kept | Description |
|------|------|-------------|
| Daily | 7 | One backup per day for the last week |
| Weekly | 4 | One backup per ISO week for the last month |
| Monthly | 3 | One backup per month for the last quarter |

### Admin Panel

The **Admin Panel** shows backup status, recent backups, and a "Backup Now" button. No need to use the command line for quick backups.

## AI Features (Optional)

NoteHelper can use Azure OpenAI to auto-suggest topics, match milestones, and auto-fill task descriptions. This requires an Azure OpenAI resource and a service principal for authentication.

### 1. Create an Azure OpenAI Resource

1. In the [Azure Portal](https://portal.azure.com), create an **Azure OpenAI** resource
2. Once deployed, go to **Keys and Endpoint** and copy the **Endpoint** URL (e.g. `https://your-resource.openai.azure.com/`)
3. Go to **Model deployments** → **Manage Deployments** and deploy a model (e.g. `gpt-4o-mini`). Note the **deployment name**

### 2. Create a Service Principal

```bash
# Create the service principal
az ad sp create-for-rbac --name "NoteHelper-AI" --skip-assignment

# Note the output values:
# - appId      → AZURE_CLIENT_ID
# - password   → AZURE_CLIENT_SECRET
# - tenant     → AZURE_TENANT_ID
```

### 3. Grant Permissions on the OpenAI Resource

The service principal needs the **Cognitive Services OpenAI User** role on your Azure OpenAI resource:

```bash
# Get your OpenAI resource ID
az cognitiveservices account show \
  --name your-openai-resource-name \
  --resource-group your-resource-group \
  --query id -o tsv

# Assign the role
az role assignment create \
  --assignee <AZURE_CLIENT_ID> \
  --role "Cognitive Services OpenAI User" \
  --scope <resource-id-from-above>
```

### 4. Add to .env

```dotenv
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini
AZURE_OPENAI_API_VERSION=2024-08-01-preview
AZURE_CLIENT_ID=your-app-id
AZURE_CLIENT_SECRET=your-password
AZURE_TENANT_ID=your-tenant-id
```

### 5. Verify in NoteHelper

After restarting the server:

1. Go to **Admin Panel**
2. Look for the **AI Integration** card — it shows whether your AI connection is configured
3. Click **Test AI Connection** to verify NoteHelper can reach your Azure OpenAI endpoint
4. A green checkmark confirms everything is working. The AI-powered "Suggest Topics" and "Match Milestone" buttons will now appear on the call log form.

> **Note:** When AI environment variables are not configured, all AI buttons are automatically hidden from the UI. MSX integration (account imports, milestones) does **not** require these AI variables — it uses your `az login` session.

## WorkIQ Integration (Meeting Import)

NoteHelper integrates with [WorkIQ](https://github.com/nicklhw/workiq) to import meeting summaries from Microsoft Teams. WorkIQ fetches meeting transcripts and generates structured summaries that can be imported directly into call logs.

### Prerequisites

- **Node.js 18+** — WorkIQ runs via `npx`. The `start.bat` launcher will detect if Node.js is missing and offer to install it automatically via `winget`.
- **Microsoft 365 Copilot license** — required for transcript access
- **Delegated authentication** — WorkIQ uses your browser-based Microsoft identity (no service principal needed). You'll be prompted to authenticate in your browser the first time WorkIQ runs.

### How It Works

There are two ways to import meeting data:

- **Import from Meeting** (above the notes editor) — fetches the meeting summary and inserts it into the call log. Requires only WorkIQ/Node.js.
- **Auto-fill** (top right) — does everything Import from Meeting does, plus uses Azure OpenAI to auto-suggest topics, generate a task description, and match a milestone. **Requires [AI Features](#ai-features-optional) to be configured.** The Auto-fill button is hidden when AI is not set up.

The flow:

1. Click either button on the new call log form
2. Select the date — NoteHelper queries WorkIQ for your meetings on that date
3. Pick a meeting from the list (NoteHelper auto-selects the best match if a customer is chosen)
4. NoteHelper fetches a ~250-word summary including discussion points, technologies, and action items
5. The summary is inserted into the call log editor
6. *(Auto-fill only)* AI analyzes the summary to suggest topics, a task, and the best-matching milestone

### Customizing the Summary Prompt

The prompt used to generate meeting summaries can be customized:

- **Global default:** Go to **Settings** → **WorkIQ & AI** → edit the **Meeting Summary Prompt** textarea. Use `{title}` and `{date}` as placeholders.
- **Per-meeting override:** When importing a meeting, click **Customize summary prompt** to edit the prompt for just that import.

### No Extra Configuration Needed

WorkIQ uses delegated auth — it authenticates through your browser session. No environment variables are needed beyond having Node.js installed. If `npx` is available on your PATH, WorkIQ will work.

## Connect Features (Self-Evaluation Support)

NoteHelper includes tools to help you prepare for Microsoft Connect self-evaluations:

- **Connect Export** -- generate a structured summary of your customer engagement over a date range, with per-customer breakdowns and topic frequency. Available from the Admin Panel.
- **Connect Impact Signals** -- when importing meetings via WorkIQ, NoteHelper can extract customer impact signals (adoption milestones, technical wins, business value) and include them in your call log notes.

See [docs/CONNECT_FEATURES.md](docs/CONNECT_FEATURES.md) for full details, including how to toggle impact extraction on/off.

## Scheduled Milestone Sync (Optional — Server Must Be Running)

NoteHelper can automatically sync milestones from MSX on a daily schedule. This keeps your milestone data fresh without manual intervention. **The NoteHelper server must be running at the scheduled time for the sync to execute.**

### Setup

Add the `MILESTONE_SYNC_HOUR` environment variable to your `.env` file:

```dotenv
# Sync milestones daily at 3:00 AM
MILESTONE_SYNC_HOUR=3
```

The value is the hour in 24-hour format (0-23) in your **local time zone**. When configured:

- A background thread checks every 60 seconds if it's time to sync
- The sync runs once per day at the configured hour
- All customers with MSX account links are synced
- Results are logged to the console

To disable scheduled sync, remove or comment out the `MILESTONE_SYNC_HOUR` variable.

### Verifying

Check your server logs for messages like:
```
Scheduled milestone sync started (daily at 03:00)
Starting scheduled milestone sync at 2025-01-15T03:00:12
Scheduled sync complete: 42 customers, 5 new, 18 updated
```

### Windows Task Scheduler Alternative

If you prefer to use Windows Task Scheduler instead of the built-in background sync:

1. Create a new Basic Task in Task Scheduler
2. Set the trigger to **Daily** at your preferred time
3. Set the action to run:
   ```
   powershell.exe -Command "Invoke-RestMethod -Method POST -Uri http://localhost:5000/api/milestone-tracker/sync"
   ```
4. NoteHelper must be running when the task fires

## Compliance

This application stores customer account data locally. The SQLite database is **not encrypted at the application level** — encryption at rest is provided by **BitLocker** full-disk encryption on your managed device. To remain compliant with organizational data handling policies:

- **Must run on a Microsoft-managed device** (Intune-enrolled or domain-joined)
- **Must reside on a BitLocker-encrypted drive** — this is your encryption-at-rest layer
- Do not copy the database file (`data/notehelper.db`) to unmanaged devices or unencrypted storage

## License

MIT License - see LICENSE file for details

## Credits

Built by **Alex Blaine** ([@rablaine](https://github.com/rablaine)).

Thanks to **Ben Magazino** ([@SurfEzBum](https://github.com/SurfEzBum)) for testing, feedback, and helping shape the final product.

## Contact

For questions or suggestions, please open an issue on GitHub.
