# Copilot Instructions Optimization - Extract Skills

## Problem

`copilot-instructions.md` is growing with behavioral rules that are mostly common sense but needed because of repeated mistakes. These rules eat context window on every conversation, even when irrelevant to the current task.

## Solution

Extract behavioral rules into **Skills** (`.github/SKILL.md` files) that lazy-load only when the task matches the skill description. Keep `copilot-instructions.md` focused on project facts (tech stack, structure, conventions, env vars, deployment targets).

## Skills to Extract

### 1. Codebase-Wide Changes Skill
- **Source sections:** "Removing Features or Dead Code" + "Codebase-Wide Changes (Renames, Replacements, Removals)"
- **Trigger:** User asks to remove, rename, or replace something across the codebase
- **Content:** grep everything, fix everything, final grep verification, never half-do it

### 2. Template Partial Extraction Skill
- **Source section:** "Extracting Template Partials"
- **Trigger:** User asks to extract a partial from a template
- **Content:** Cut everything (HTML + JS + includes), paste into partials/, shell the source, script activator for modals, DOMContentLoaded conversion

### 3. Gateway Deployment Skill
- **Source sections:** "Gateway Deployment Rules" + "Gateway HTTP Status Cheat Sheet" + "Gateway Rollback Procedure"
- **Trigger:** User asks to deploy to staging or production
- **Content:** Deploy staging first, verify /health, required zip files, rollback procedure, HTTP status meanings

### 4. Git Workflow Skill
- **Source sections:** "Git & Version Control" (branching, commit format, merge rules)
- **Trigger:** User asks to commit, merge, push, or create a branch
- **Content:** Feature branches, no amending, no auto-merge, user tests before merge, --no-ff merges

## What Stays in copilot-instructions.md

- Project overview and tech stack
- Project structure
- Naming conventions
- Code organization rules (blueprints, route placement, SalesIQ tools)
- DateTime conventions
- Error handling patterns
- Testing conventions
- Terminal command rules
- Environment variables and setup
- Architecture patterns (MVC, DB migrations, AI gateway infrastructure)
- UI/UX conventions (badge colors, icons)
- Communication style

## Implementation Notes

- Skills use YAML frontmatter with `applyTo` patterns to control when they load
- Each skill is a separate `.md` file under `.github/`
- Test each skill by doing the relevant task and confirming it loads
- After extraction, remove the corresponding sections from `copilot-instructions.md`
