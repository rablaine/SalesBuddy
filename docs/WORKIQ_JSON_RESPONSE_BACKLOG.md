# WorkIQ JSON Response Format - Backlog

## Status

Backlogged (2026-04-22). Build on a feature branch **after** the ghost-aura
work currently on `feature/prefetch-meetings-backlog` is merged.

## The Problem

`app/services/workiq_service.py::_parse_summary_response` is ~150 lines of
regex archaeology that reverse-engineers structure from natural-language
WorkIQ responses. It breaks every time WorkIQ tweaks formatting. Recent
real failures (2026-04-22):

1. **`TASKTITLE:` (no underscore)** runs into the previous sentence
   (`...model refreshes.TASKTITLE: ...`). Our regex requires
   `TASK[_\s]TITLE` so it slips through unparsed and lands in the
   summary body as garbage.
2. **CONNECT_IMPACT bullets jammed onto one line.** WorkIQ collapsed
   newlines between bullets so the dash-prefix line regex misses them
   and the whole impact block disappears or merges with summary.
3. **Words running together** ("Dataverse-Fabricintegration",
   "betweencharges"). LLM emits NBSPs / zero-width chars; our cleanup
   doesn't catch all of them.
4. **OSC 8 hyperlink leakage** (separately fixed 2026-04-22) was only
   visible because the regex parser is fragile to *any* unexpected
   bytes in the response.

## The Fix

Ask WorkIQ to return JSON. Parse it like a normal API response.

### Proposed prompt suffix (replaces the structured-text formatting rules
in `DEFAULT_SUMMARY_PROMPT` + `_TASK_PROMPT_SUFFIX` + `_CONNECT_IMPACT_SUFFIX`):

```
Respond with ONLY a JSON object, no prose, no code fences, no markdown.
Schema:
{
  "summary": "string, 200-300 words, plain prose, no markdown links",
  "task_title": "string, under 10 words",
  "task_description": "string, 1-2 sentences",
  "connect_impact": ["string", "string", ...],
  "engagement_data": {
    "key_individuals": "string or null",
    "technical_business_problem": "string or null",
    "business_process_strategy": "string or null",
    "solution_resources": "string or null",
    "business_outcome_acr": "string or null",
    "future_date_timeline": "string or null",
    "risks_blockers": "string or null"
  }
}
Use null for any field with no clear info from the meeting.
Do not include citations, markdown links, or follow-up offers.
```

### Parser shape

```python
def _parse_summary_response(response: str) -> Dict[str, Any]:
    # Find first { and last } - tolerates code fences / preamble
    start = response.find('{')
    end = response.rfind('}')
    if start == -1 or end == -1:
        return _fallback_natural_parse(response)
    try:
        data = json.loads(response[start:end+1])
    except json.JSONDecodeError:
        return _fallback_natural_parse(response)
    return _normalize_json_payload(data)
```

Keep the existing regex parser as `_fallback_natural_parse` so a single
bad WorkIQ response (LLM emits trailing comma, unescaped quote in
summary, etc.) doesn't blow up the import.

### Expected gains

- `_parse_summary_response` shrinks from ~150 lines of regex to ~30
  lines of `json.loads` + dict unpacking.
- Eliminates entire classes of bugs:
  - Missing-underscore section markers (`TASKTITLE:` vs `TASK_TITLE:`)
  - Newline-stripping that breaks dash-prefix bullet matching
  - Section markers running into adjacent prose
  - New escape-sequence families from CLI updates
- Easier to extend: add a new field = add a JSON key, no new regex.

## Risks & Mitigations

- **LLM emits invalid JSON** (trailing commas, unescaped quotes inside
  the summary). Mitigations:
  - Keep regex parser as fallback (no regression on bad output).
  - One auto-retry with "Your previous response was invalid JSON. Try
    again with valid JSON only." (we already have retry infra in
    `app/routes/notes.py` for the import retry button).
- **Slightly more tokens** — JSON braces/keys add maybe 50 tokens per
  response. Negligible cost / latency.
- **WorkIQ might not honor the JSON-only instruction reliably.**
  Validate during testing across 5-10 real meetings before shipping.

## Implementation Plan

1. Feature branch off `main` (after ghost-aura ships).
2. Update `DEFAULT_SUMMARY_PROMPT` + helper suffixes to ask for JSON.
3. Implement `_normalize_json_payload(data)` mapping the JSON schema
   above into the existing result-dict shape (`summary`, `task_subject`,
   `task_description`, `connect_impact`, `engagement_signals`,
   `raw_response`). Existing UI / DB code is unchanged.
4. Rename current `_parse_summary_response` body to
   `_fallback_natural_parse` and have the new top-level function try
   JSON first, fall back on JSONDecodeError.
5. Update tests in `tests/test_workiq_*.py`:
   - Existing natural-language tests now hit the fallback path.
   - Add new `test_parse_json_response_*` tests for the happy path,
     missing optional fields, embedded code fences, leading preamble,
     trailing prose.
6. Manual validation: run 5-10 real meeting imports, confirm no
   fallback hits in logs (add a one-line log when we fall back so we
   can spot the rate).
7. After 1-2 weeks of clean JSON responses in prod, delete the regex
   fallback.

## Effort

~60-90 min for code + tests. Plus a week of "watch the logs" before
deleting the fallback.

## Out of Scope

- Topic extraction (already migrated to OpenAI gateway's
  `/v1/suggest-topics` — unaffected).
- Attendee fetching (separate JSON path in
  `app/services/meeting_prefetch.py`, already JSON, unaffected).
