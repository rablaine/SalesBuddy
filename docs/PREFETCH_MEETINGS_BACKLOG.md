# Pre-fetched Meetings & Ghost-Meeting Notes - Backlog

## Status

Backlog / not started. Blocked on WorkIQ being available so we can theorycraft
realistic latency, payload shape, and per-day attendee limits before locking in
a model.

## The Idea

Today, when a user opens a note for a customer call, attendee data has to be
fetched on-demand from WorkIQ (slow, sometimes broken, mid-call is the worst
possible time for it to fail). Tester request: pre-fetch each morning during
the same window we already use to refresh meetings, so attendees are local and
instant.

Once we have that data sitting locally, it unlocks a more interesting feature:
show each known meeting as a **ghost meeting** in the Activities calendar
(the existing past-notes calendar on the home page), with click-to-create that
auto-fills customer, time, and attendees.

## Goals

- Eliminate the mid-call wait for attendee population.
- Surface the day's customer meetings as one-click "create note" candidates.
- Auto-match meetings to customers using domain-level matching.
- Auto-suggest partner organizations from non-Microsoft, non-customer attendees.
- Make ahead-of-time prep possible (look at tomorrow's meetings, draft a note).

## Non-Goals

- No standalone calendar UI in the navbar / new-note picker. Ghosts live only
  in the existing Activities calendar on the home page.
- No external-domain configurability. Internal = `microsoft.com`, full stop.
  Anyone running Sales Buddy is on a Microsoft tenant.
- No ML / fuzzy name matching for customer assignment. Domain match or unmatched.

---

## Architecture Sketch

### New Models (proposed - validate against actual WorkIQ payload first)

```python
class PrefetchedMeeting(db.Model):
    id = Column(Integer, primary_key=True)
    workiq_id = Column(String, unique=True, index=True)  # dedupe key
    subject = Column(String)
    start_time = Column(DateTime)  # UTC; render local in UI
    end_time = Column(DateTime)
    organizer_email = Column(String)
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=True, index=True)
    matched_via = Column(String)  # 'website', 'contact_email', 'manual', null
    dismissed = Column(Boolean, default=False)  # user X'd it from ghost list
    fetched_at = Column(DateTime, default=utc_now)
    expires_at = Column(DateTime, index=True)  # TTL - end of day after meeting
    note_id = Column(Integer, ForeignKey('notes.id'), nullable=True)  # set when promoted

class PrefetchedMeetingAttendee(db.Model):
    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey('prefetched_meetings.id'), index=True)
    name = Column(String)
    email = Column(String, index=True)
    domain = Column(String, index=True)  # denormalized for fast match
    response_status = Column(String, nullable=True)  # accepted, tentative, declined
    is_external = Column(Boolean)  # not microsoft.com
```

TTL strategy: every meeting expires at midnight local on the day after it
ends. Prefetch run drops expired rows before inserting fresh ones, so we never
accumulate. Dismissed rows persist until expiry (so they don't reappear on
re-fetch within the same day).

### Domain Matching

Two-tier, in order:
1. **Website match**: `customer.website == attendee.domain` for any external attendee.
2. **Contact-email match**: any `CustomerContact.email` ends in `@<attendee.domain>`.

Build an in-memory `{domain: customer_id}` map at the start of each prefetch
run. If a domain matches multiple customers, prefer the one most recently
updated (proxy for "active"). Log multi-match cases.

Unmatched meetings: `customer_id = NULL`, still shown as ghost, click triggers
the customer picker (see below).

### External Filter

A meeting is shown as a ghost only if it has at least one attendee where
`is_external = True` (i.e. domain != `microsoft.com`). Pure-internal meetings
(team standups, lunch, all-hands) are stored if it's cheap to do so but never
surfaced to the UI.

### Partner Auto-suggestion

Group external attendees by domain. For each domain that:
- doesn't match the meeting's resolved customer, AND
- matches an existing `Partner.website` OR has 2+ attendees,

surface as a suggested partner to attach when the user promotes the ghost to
a real note. (Don't auto-attach. Suggest with a checkbox.)

### Per-recurring-meeting Dismissal

When user dismisses a ghost, also store a `recurring_key` (subject + organizer
hash) so future instances of the same recurring meeting auto-hide. Tiny
`DismissedMeetingPattern` table or a JSON list on user prefs - decide once we
see what WorkIQ gives us for recurrence info.

---

## Phased Plan

### Phase 0 - WorkIQ Probe (1-2 hours, do first the moment WorkIQ is up)

Goal: don't write any model code until we know what WorkIQ can actually
return. Build a throwaway script.

- [ ] `scripts/probe_workiq_meetings.py` - prompt WorkIQ for "today's meetings
  with attendees" and dump the raw JSON response to a file.
- [ ] Repeat for "this week's meetings". Time both.
- [ ] Document findings: payload shape, attendee field names, recurrence info,
  response statuses, latency for 1 day vs 7 days, any pagination, max returnable.

**Decision gate:** based on probe results, lock in the model schema and
decide whether week-ahead prefetch is one query or N queries.

### Phase 1 - Storage & Daily Prefetch (no UI yet)

- [ ] Create `PrefetchedMeeting` and `PrefetchedMeetingAttendee` models.
- [ ] Add idempotent migrations (per repo convention - no Alembic).
- [ ] `app/services/meeting_prefetch.py`:
  - [ ] `prefetch_today()` - query WorkIQ, parse, match domains to customers,
    upsert by `workiq_id`.
  - [ ] `purge_expired()` - delete rows past `expires_at`.
  - [ ] Internal-only meetings stored (cheap) but flagged for UI exclusion.
- [ ] Wire into the existing morning prefetch / scheduled refresh job (find
  where current meeting refresh runs and add this alongside).
- [ ] Privacy opt-in: a settings toggle (default OFF) gating the prefetch.
  Settings page copy explains we're caching attendee names+emails locally so
  that calls don't stall on WorkIQ. User can flip on for the feature to do
  anything at all.
- [ ] Tests: `tests/test_meeting_prefetch.py` covering domain matching,
  external filter, dedupe by `workiq_id`, TTL purge.

**Ship gate:** verify a morning run populates rows, check matching accuracy
against a few known customers, no UI changes yet.

### Phase 2 - Use Prefetched Attendees in Note Form

This is the tester's actual ask. Cash in the value of phase 1 immediately.

- [ ] When the note form loads with a `customer_id` and `call_date`, look for
  `PrefetchedMeeting` matching that customer + that day. If found, populate
  attendees from the meeting record instead of (or in parallel with) a fresh
  WorkIQ call.
- [ ] If no prefetched match, fall back to current on-demand WorkIQ behavior.
- [ ] Show a small "from cached meeting at HH:MM" badge so user knows the
  source.
- [ ] Tests: route test with a seeded `PrefetchedMeeting`, assert attendees
  pre-populate and no WorkIQ call is made.

**Ship gate:** tester confirms attendees appear instantly mid-call.

### Phase 3 - Ghost Meetings on Activities Calendar

The "click a meeting to start a note" flow. Lives only in the existing
Activities calendar on the home page (NOT the new-note date picker).

- [ ] On the home page Activities calendar, render `PrefetchedMeeting` rows
  alongside existing notes.
- [ ] Visual treatment: "ghost" styling - lower opacity, dashed border, faded
  customer color, time label. Clearly distinct from real notes.
- [ ] Filter rule: only render meetings with `is_external = True` attendees.
- [ ] Hover shows attendee list + matched customer.
- [ ] Per-row dismiss X. Dismissing also flags the recurring pattern.
- [ ] Click handler:
  - **Matched customer**: jump straight to note form with `customer_id`,
    `call_date` (= meeting start), pre-tagged customer contacts (matching
    attendee emails), pre-suggested partner orgs, and meeting subject as
    initial title.
  - **Unmatched customer**: open the existing `quickNoteModal` customer picker
    (same modal launched by the navbar "New Note" button, see
    `templates/base.html`). After customer is picked, proceed to note form
    with the rest of the prefill data, AND save the chosen customer's domain
    so future meetings on that domain auto-match.

- [ ] Tests: page test that ghost meetings render, dismissed ones don't,
  click handler hits the right URL with the right query params for both
  matched and unmatched cases.

**Ship gate:** tester uses ghost meetings for a full day, confirms it saves
time and doesn't get noisy.

### Phase 4 - Week-Ahead Prefetch (toggle-gated)

Only after phase 3 is shipping smoothly and we know WorkIQ latency for
multi-day pulls.

- [ ] Settings toggle: "Pre-fetch meetings for the next 7 days" (default OFF).
- [ ] If on, prefetch job pulls 7 days instead of 1. Watch for timeouts;
  may need to chunk by day if WorkIQ can't do it in one call.
- [ ] Activities calendar shows future ghosts in addition to today.
- [ ] Tests: prefetch covers correct date range, future ghosts render with
  a "future" visual state distinct from today/past.

**Ship gate:** if morning prefetch takes longer than ~60s, gate even harder
or chunk by day.

---

## Open Questions (resolve during phase 0)

- Does WorkIQ return recurrence info, or do we hash subject+organizer
  ourselves?
- Does WorkIQ give us response status (accepted / tentative / declined)? Use
  it to dim the noise (don't suggest as ghost if user declined).
- What's the latency curve - is 7 days really one query, or N?
- Are there any rate limits we'd hit on a daily 7-day pull?
- Does WorkIQ surface online-meeting links / Teams URLs we could use for
  one-click-join?

## Risks

- **Customer-matching accuracy**: known weak point today. Domain match alone
  will mis-match shared-domain customers (very rare for external) and miss
  customers with unset websites. Mitigation: every unmatched click stores a
  domain mapping, so accuracy improves with use.
- **Ghost-list noise**: even with external-only filter, recurring partner
  syncs and broad invites can clutter. Mitigation: per-recurrence dismissal
  + tester feedback loop in phase 3.
- **WorkIQ flakiness**: prefetch failures shouldn't break the app. Run in
  background, log failures, fall back to on-demand. The whole point is to
  decouple the user from WorkIQ's bad days.
- **Stale data mid-call**: if a meeting is moved last minute, the cached
  attendee list could be wrong. Mitigation: small "refresh from WorkIQ" link
  on the note form when prefetch was used.
