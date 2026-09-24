---
description: "Use when committing code, creating branches, merging branches, pushing to remote, undoing commits, reverting changes, or any git version control workflow. Trigger words: commit, merge, push, branch, revert, undo, reset, git, feature branch, main."
---

# Git Workflow

## Branching Strategy

- **ALWAYS create feature branches for new work** - never commit directly to `main`
- Create feature branches from `main` for all changes
- Merge back to `main` only when feature is complete and tested

## Branch Naming

- **Do not pause to ask the user for a feature branch name.**
- When the user does not supply a name, choose a clear conventional branch name
  from the task context and proceed autonomously.
- This is especially important when the user says they are stepping away or
  asks the agent to continue unattended.
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
6. **Update `CHANGELOG.md`** for any user-visible functionality change (see "Changelog Updates" below)
7. **STOP AND WAIT for user confirmation** before merging to `main` - **NEVER merge to main without explicit user approval**
8. When user says ready: merge to `main` with `--no-ff` and push
   - **Always use `git merge --no-ff`** to preserve feature branch history

## Changelog Updates

The admin panel reads `CHANGELOG.md` from the repo root and shows users what's new before and after they apply an update. Keep it accurate or the feature degrades silently.

**Update before merging to `main`** if the change is user-visible functionality.

**Format: one section per merge commit, tagged with that merge's short SHA.**

```
## M/D/YYYY - <merge-short-sha>

- Bullet describing what shipped in that merge
- Another bullet if relevant
```

The hash is the SHA of the **merge commit** that brought the change into `main` (e.g. `Merge feature/foo-bar`), NOT any individual commit inside the feature branch. One merge = one user-visible update = one changelog entry.

Multiple sections on the same date are fine and expected - one per merge commit.

**Workflow:**

1. On your feature branch, make your code change(s) and commit them.
2. Edit `CHANGELOG.md` and prepend a new section like:
   ```
   ## 4/29/2026

   - what you did
   ```
   Leave the heading without a hash. Commit + push.
3. After the user signs off and you merge to `main` with `git merge --no-ff` (which creates a merge commit), run `.\scripts\tag-changelog.ps1` from the repo root. It looks up the most recent merge commit on the current branch and rewrites the topmost untagged heading to `## 4/29/2026 - <merge-hash>`, then creates a separate "Tag changelog for <hash>" commit on `main`.
4. Push the tag commit.

You can also tag manually if you prefer: `git log --merges -1 --format=%h`, then edit the heading.

**Why merge commits:** the admin Updates card filters changelog entries by hash, not just by date. Tagging the merge commit means a single user-visible update maps to a single hash even if the feature branch had multiple internal commits.

**The tag commit is the one allowed exception to the "always use a feature branch" rule** - it's pure bookkeeping (one-line edit, no code), it references something that's already merged, and it has to land on `main` to be discoverable by the update checker.

**Legacy entries** (untagged `## YYYY-MM-DD` headings) still work via a date-based fallback, so don't bother backfilling old sections.

**Include in CHANGELOG:**
- New features or pages
- Removed features (even if "just" a UI control - if a user could see and use it, it counts)
- Behavior changes (different default, different ordering, new validation)
- Bug fixes users would notice
- Performance improvements users would feel
- Integration changes (new MSX field, changed sync cadence)

**Skip in CHANGELOG:**
- Pure cosmetic tweaks (color, padding, font size, icon swap)
- Typo fixes in labels
- Internal refactors with no user-visible effect
- Test-only changes
- Dev tooling / build script changes

**When in doubt, include it.** A short bullet that turns out to be unimportant is far better than silently shipping a change users care about.

**Bullet style:** plain English, present tense, ~1 line. The reader is a busy seller, not a developer. Skip implementation detail unless it's the point of the change.

Good: `Add changelog viewer to admin Updates card so you can see what's new before and after applying an update`
Bad: `Refactor checkForUpdates to call renderChangelogSection helper`

### Electron Shell Update marker

The desktop shell (`electron/main.js` and anything bundled into the exe) does NOT
update via a plain `git pull` - it needs the shell to be rebuilt and restaged. To
make that automatic, **prefix any changelog bullet describing a change that
requires a shell rebuild with `*Electron Shell Update* - `**:

```
- *Electron Shell Update* - the desktop app can now start minimized to the tray.
```

The `*...*` renders as italic to users but is a precise signal the app scans for.
When a pending update carries this marker, the admin Update button warns the user
and auto-chains the shell rebuild after the pull (see
`app/services/update_checker.py::entries_require_shell_rebuild`).

**Rules:**
- Add the marker for any change to `electron/main.js`, `electron/package.json`,
  the Electron build config, or anything else compiled into `Sales Buddy.exe`.
  Pure backend/template/route/CSS changes do NOT need it (they ship via git pull).
- **Never put the marker on the bootstrap commit** - the first commit that
  introduces this detection logic. The version running before it can't detect
  with code it doesn't have yet; that first shell change relies on the manual
  Admin > Danger Zone > Rebuild desktop app button instead.

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
