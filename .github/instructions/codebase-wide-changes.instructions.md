---
description: "Use when removing features, dead code, renaming symbols, or replacing terms across the codebase. Covers grep-everything, fix-everything, final-grep-verification workflow. Trigger words: rip out, remove, rename everywhere, replace all, codebase-wide, search and replace."
---

# Codebase-Wide Changes

This applies to ANY "do X everywhere" instruction: removals, renames, replacements.

## Removing Features or Dead Code

When the user says "rip out X" or "remove X from the app":

1. **Search the ENTIRE codebase** for every reference (code, comments, docstrings, templates, JS, tests, scripts, docs, config files, migration files, backlog items, copilot instructions)
2. **Remove or update EVERY reference** in a single pass. Do not leave stragglers.
3. **Do NOT invent reasons to keep dead code.** "Backward compatibility," "the DB column still exists," and "the INSERT needs it" are not valid reasons to preserve references to something that nothing reads, writes, or depends on. If the model doesn't have it, nothing should reference it.
4. **Do NOT half-remove and wait for the user to notice.** The user should never have to ask twice.
5. **After removal, run a final grep** for the removed term across the entire repo. If any matches remain (outside of unrelated contexts like third-party docs), fix them before reporting done.

The bar: if you grep for the removed term after cleanup, the only matches should be in files that genuinely talk about something else (e.g., a WorkIQ doc mentioning WorkIQ's own consent is fine when removing SalesBuddy consent).

## Renames, Replacements, Bulk Changes

When the user says "rename A to B everywhere," "replace all mentions of X with Y," or any variation:

1. **grep the ENTIRE codebase** for the old term - code, comments, docstrings, templates, JS, CSS, tests, scripts, docs, config files, migration files, backlog items, copilot instructions, README, .env.example, everything.
2. **Fix EVERY match** in a single pass. Comments, docstrings, variable names, UI strings, test assertions, documentation - ALL of it.
3. **Do NOT skip files** because they're "just comments" or "just docs" or "just backlog." If it contains the old term, update it.
4. **Do NOT stop at code files.** Templates, JS, markdown, YAML, PowerShell scripts, batch files - search them all.
5. **After the change, run a final grep** for the old term. If matches remain, fix them before reporting done.
6. **The user should NEVER have to ask twice.** If you find yourself thinking "that one doesn't matter," you're wrong. Fix it.
