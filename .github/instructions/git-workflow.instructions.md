---
description: "Use when committing code, creating branches, merging branches, pushing to remote, undoing commits, reverting changes, or any git version control workflow. Trigger words: commit, merge, push, branch, revert, undo, reset, git, feature branch, main."
---

# Git Workflow

## Branching Strategy

- **ALWAYS create feature branches for new work** - never commit directly to `main`
- Create feature branches from `main` for all changes
- Merge back to `main` only when feature is complete and tested

## Branch Naming

When attempting to commit to `main`, stop and prompt user for feature branch name.
- Ask: "What should we call this feature branch?"
- Format: `feature/short-description` or `fix/bug-description`
- Examples: `feature/export-import`, `fix/admin-permissions`, `feature/email-fields`

## Commit Message Format (Conventional Commits)

- `feat:` - New feature
- `fix:` - Bug fix
- `docs:` - Documentation changes
- `refactor:` - Code refactoring
- `test:` - Test additions or changes

## Development Workflow

1. Create feature branch: `git checkout -b feature/your-feature-name`
2. Write code and corresponding tests together
3. Run scoped tests for the feature you're building (e.g., `pytest tests/test_views.py`)
4. **Prompt user to manually test new features or bug fixes** - before committing, always ask the user to test the changes in the running app
5. Commit to feature branch with descriptive message
6. **STOP AND WAIT for user confirmation** before merging to `main` - **NEVER merge to main without explicit user approval**
7. When user says ready: merge to `main` with `--no-ff` and push
   - **Always use `git merge --no-ff`** to preserve feature branch history

## DO NOT Auto-Merge

- **NEVER merge a feature branch to `main` on your own** - always wait for the user to test and explicitly say to merge
- Building a feature and committing to the feature branch is fine - merging to `main` requires user sign-off
- If the user says "commit" that does NOT mean "merge to main" - it means commit to the current feature branch only
- Merging to `main` is a deployment gate - treat it seriously

## No Amending Commits

- **NEVER use `git commit --amend`** - each change gets its own commit with a descriptive message
- Amending squashes history and makes it harder to review what changed when
- If you made a mistake in the last commit, make a NEW commit that fixes it

## Undoing Commits

- **NEVER use `git revert`** - the user does not want revert commits cluttering history
- When asked to "revert", "undo", or "reset" a commit, **ALWAYS use `git reset --hard`** to remove it cleanly
- If the commit was already pushed, reset locally then `git push --force-with-lease`
- When the user says "undo that" or "revert that" in frustration, they mean: hard reset HEAD so we're back at the previous commit with none of the bad changes surviving

## Merge to Production Checklist

- Scoped tests passing for changed code
- User explicitly confirms "ready to deploy" or "merge to main"
- Code follows PEP 8 standards
- No secrets or .env file committed
- Tests included for new features or bug fixes

## External Actions Safety

Before any action that modifies systems outside the local workspace, pause and confirm:
- **Deploying to Azure** (staging or prod) - state the target slot and what's being deployed
- **Pushing to remote** (`git push`) - state the branch and what's being pushed
- **Deleting remote resources** (branches, Azure resources, deployed code)
- **Running `az` commands that modify infrastructure** (config changes, restarts, identity assignments)

The rule: **if it leaves your machine, say what you're doing and why before doing it.** For destructive or hard-to-reverse actions, wait for explicit user confirmation.

## GitHub Interactions

- **Use `gh` CLI for all GitHub operations** - issues, PRs, comments, labels, etc.
- Examples: `gh issue comment 46 --body "Fixed in commit abc123"`, `gh issue list`, `gh pr create`
- Do NOT ask about MCP tools or other methods - just use `gh` directly
