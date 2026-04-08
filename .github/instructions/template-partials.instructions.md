---
description: "Use when extracting a template partial, creating a partial from an existing template, or moving template content into a reusable partial file. Trigger words: extract partial, create partial, move to partial, template partial, partials folder."
---

# Extracting Template Partials

Read this carefully and follow it exactly.

When asked to "extract a partial" from a template, this means:

1. **CUT** the content out of the source template (the actual HTML + Jinja2 code AND JavaScript)
2. **PASTE** it into a new partial file in `templates/partials/`
3. **Replace** the removed content in the source template with `{% include 'partials/the_partial.html' %}` so the original page renders identically
4. The partial can then be included/rendered elsewhere too

## What Goes in the Partial

**EVERYTHING.** That means:
- The HTML layout
- All `<script>` blocks and inline JavaScript (lazy-loading, AJAX, event handlers, etc.)
- All `{% include %}` sub-partials (e.g., `_forecast_comments.html`, `_forecast_comments_js.html`)
- All Jinja2 logic, conditionals, loops, macros

The source template should become a near-empty shell that just `{% extends "base.html" %}` and `{% include %}` the partial. If the extracted page has a `{% block extra_js %}` script section, that entire script block goes in the partial too (wrapped in `<script>` tags since it's no longer inside a block).

## Rules

- **DO NOT** write a simplified or "clean" version from scratch. The partial must be the EXACT same code that was in the source template. If the source has 300 lines of JavaScript for lazy-loading, comment sorting, team join/leave, and task creation, the partial has those same 300 lines. Do not rewrite any of it.
- **DO NOT** look at sub-partials or JS includes and rewrite their logic in the consumer (e.g., seller_view). The whole point of extraction is that the partial carries all its own logic. If `_forecast_comments_js.html` handles comment sorting and lazy-loading, it comes along inside the partial - you never rewrite that logic elsewhere.
- **Guard variables** (like `show_edit_delete`) can be added to conditionally hide elements when the partial is rendered in a different context (e.g., a modal), but the default behavior must match the original page exactly.

## Modal Rendering

When loading a partial via `fetch()` + `innerHTML`, `<script>` tags won't auto-execute. Add a small script activator loop in the modal JS to clone and re-insert script elements. That is the ONLY new code needed - everything else comes from the partial.

## DOMContentLoaded

Scripts in the partial that use `document.addEventListener('DOMContentLoaded', ...)` won't fire when injected into an already-loaded page. Change these to immediate invocations (the script runs at injection time anyway, so this is equivalent).

## The Test

After extraction, the original page must look and behave 100% identically. If it doesn't, you extracted wrong.
