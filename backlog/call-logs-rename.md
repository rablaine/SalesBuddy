# Rename "Call Logs" to "Notes" Everywhere

## Problem

The app originally called notes "call logs". The UI was updated a month ago but the codebase still has "call log" references in docstrings, comments, copilot-instructions, migrations, backup service, and model descriptions. This leaks into AI responses (GPT calls notes "call logs" because the code does).

## Scope

Replace "call log(s)" with "notes" in all non-migration code. Migrations are historical and should be left as-is.

### Files to update

- `.github/copilot-instructions.md` - "Create/edit/delete notes (call logs)"
- `app/models.py` - docstrings on Note, Customer, Seller, Topic, Partner, Milestone, MsxTask
- `app/services/backup.py` - module docstring, function docstrings, comments
- `docs/APP_INSIGHTS.md` - category reference

### Files to NOT touch

- `app/migrations.py` - historical migration comments, leave as-is
- Database column names - no schema changes
- Any variable names that would break functionality
