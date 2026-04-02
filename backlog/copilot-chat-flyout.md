# Copilot Chat - Global Right Flyout

## Summary

Add a persistent, app-wide Copilot chat flyout that lives in `base.html`, is triggered from the navbar, and always occupies the rightmost position on screen. Existing note-form flyouts shift to accommodate it.

---

## Current State

### Navbar right-side buttons (in order)
1. **New Note** (btn-success, opens `#quickNoteModal`)
2. **Help / Keyboard Shortcuts** (btn-light, opens `#keyboardShortcutsModal`)
3. **Settings gear** dropdown (btn-light)

### Existing flyouts (all in `note_form.html`)
- 6 flyouts: `partnerFlyout`, `projectFlyout`, `engTaskFlyout`, `engagementFlyout`, `contactsFlyout`, `customerInfoFlyout`
- All are `offcanvas-end`, `width: 400px`, `data-bs-backdrop="false"`, `data-bs-scroll="true"`
- Positioned below navbar: `top: var(--sb-navbar-height, 56px)`, `height: calc(100% - 56px)`
- Stacking system shifts earlier flyouts left by 399px increments (`flyout-shifted`, `flyout-shifted-2`, `flyout-shifted-3`)
- Z-index: shifted flyouts use 1042-1044, navbar sits at 1050

### Z-index map
| Element | z-index |
|---------|---------|
| Navbar | 1050 |
| Flyout (newest/rightmost) | Bootstrap default (~1045) |
| `flyout-shifted` | 1044 |
| `flyout-shifted-2` | 1043 |
| `flyout-shifted-3` | 1042 |

### Existing stacking bug (pre-copilot)

The current stacking system only has 3 shift levels but there are 6 flyouts. The `adjustStack()` JS caps at `shift >= 3`, so if 5+ flyouts are open the oldest ones pile up at the same position.

**Repro:** Open contacts, partner, engagement, action item, then customer details. The leftmost flyout overlaps with the one next to it because both get `flyout-shifted-3` (-1197px).

**Fix required before copilot work:** Add `flyout-shifted-4` and `flyout-shifted-5` to handle up to 6 simultaneous flyouts (the newest always stays at the right edge, so 5 shift levels covers all 6).

```css
.offcanvas.flyout-shifted-4 {
    transform: translateX(-1596px) !important;
    z-index: 1041 !important;
}
.offcanvas.flyout-shifted-5 {
    transform: translateX(-1995px) !important;
    z-index: 1040 !important;
}
```

And update `adjustStack()` to remove the `>= 3` cap:

```javascript
// Replace the shift assignment block with:
for (let i = 0; i < sorted.length - 1; i++) {
    const shift = sorted.length - 1 - i;
    if (shift >= 1 && shift <= 5) {
        sorted[i].classList.add('flyout-shifted' + (shift === 1 ? '' : '-' + shift));
    }
}
```

Also update the `forEach` that removes classes:

```javascript
flyouts.forEach(f => {
    f.classList.remove('flyout-shifted', 'flyout-shifted-2', 'flyout-shifted-3',
                        'flyout-shifted-4', 'flyout-shifted-5');
});
```

**Does copilot need yet another level?** No. The copilot chat flyout is handled via the `body.copilot-chat-open` CSS override, which adds +400px to each existing shift level. The same 5 shift levels cover both cases - copilot just moves the whole note stack left by one flyout width.

---

## Design

### Copilot chat flyout properties
- **ID:** `copilotChatFlyout`
- **Position:** `offcanvas-end` (right edge of viewport)
- **Width:** `400px` (matches standard flyout width)
- **Top:** `var(--sb-navbar-height, 56px)` (below navbar, same as other flyouts)
- **Height:** `calc(100% - var(--sb-navbar-height, 56px))`
- **Backdrop:** `false` (user can still interact with the page)
- **Scroll:** `true` (page scrolls behind it)
- **Z-index:** `1046` - above all note flyouts (1042-1045) but below navbar (1050)
- **Persist state:** Remember open/closed in `sessionStorage` so it survives page navigation

### Navbar trigger button
- Insert **before** the Help button (after New Note) or as the rightmost button after the gear - either works, but a dedicated icon is cleaner as the last item
- **Recommended placement:** After the gear dropdown, as the rightmost navbar item
- Icon: `bi-chat-dots` or `bi-robot` (Bootstrap Icons)
- Toggle class: `active` state when flyout is open
- Keyboard shortcut: `Ctrl+Shift+C` (or similar, check for conflicts)

```html
<button class="btn btn-light" type="button" id="navCopilotChat"
        title="Copilot Chat (Ctrl+Shift+C)" aria-label="Toggle Copilot Chat">
    <i class="bi bi-chat-dots"></i>
</button>
```

---

## Implementation Plan

### 1. Add flyout HTML to `base.html`

Place the offcanvas markup after the existing modals. This makes it available on every page.

```html
<div class="offcanvas offcanvas-end" tabindex="-1" id="copilotChatFlyout"
     aria-labelledby="copilotChatFlyoutLabel"
     data-bs-backdrop="false" data-bs-scroll="true"
     style="width: 400px;">
    <div class="offcanvas-header">
        <h5 class="offcanvas-title" id="copilotChatFlyoutLabel">
            <i class="bi bi-chat-dots"></i> Copilot Chat
        </h5>
        <button type="button" class="btn-close" data-bs-dismiss="offcanvas" aria-label="Close"></button>
    </div>
    <div class="offcanvas-body d-flex flex-column p-0">
        <!-- Chat message list (scrollable) -->
        <div id="copilotChatMessages" class="flex-grow-1 overflow-auto p-3">
            <!-- Messages rendered here -->
        </div>
        <!-- Input area (pinned to bottom) -->
        <div class="border-top p-3">
            <div class="input-group">
                <textarea id="copilotChatInput" class="form-control" rows="1"
                          placeholder="Ask Copilot..." aria-label="Chat message"></textarea>
                <button class="btn btn-primary" id="copilotChatSend" type="button">
                    <i class="bi bi-send"></i>
                </button>
            </div>
        </div>
    </div>
</div>
```

### 2. Add CSS to `base.html` `<style>` block

```css
/* Copilot Chat flyout - always rightmost */
#copilotChatFlyout {
    top: var(--sb-navbar-height, 56px) !important;
    height: calc(100% - var(--sb-navbar-height, 56px)) !important;
    z-index: 1046 !important;  /* Above note flyouts, below navbar */
}

/* When copilot chat is open, note flyouts need an extra 400px left shift */
body.copilot-chat-open .offcanvas.offcanvas-end.show:not(#copilotChatFlyout) {
    transform: translateX(-400px) !important;
}
body.copilot-chat-open .offcanvas.flyout-shifted {
    transform: translateX(-799px) !important;
}
body.copilot-chat-open .offcanvas.flyout-shifted-2 {
    transform: translateX(-1198px) !important;
}
body.copilot-chat-open .offcanvas.flyout-shifted-3 {
    transform: translateX(-1597px) !important;
}
body.copilot-chat-open .offcanvas.flyout-shifted-4 {
    transform: translateX(-1996px) !important;
}
body.copilot-chat-open .offcanvas.flyout-shifted-5 {
    transform: translateX(-2395px) !important;
}
```

**Key insight:** Adding the `copilot-chat-open` class to `<body>` lets us override the stacking shifts with pure CSS. The note-form flyouts don't need to know about the chat flyout at all - the CSS cascade handles it.

### 3. Add JS to `base.html`

```javascript
// Copilot Chat flyout management
(function() {
    const chatFlyoutEl = document.getElementById('copilotChatFlyout');
    const chatBtn = document.getElementById('navCopilotChat');
    if (!chatFlyoutEl || !chatBtn) return;

    const chatFlyout = new bootstrap.Offcanvas(chatFlyoutEl);

    // Toggle on button click
    chatBtn.addEventListener('click', () => chatFlyout.toggle());

    // Track open/closed state on <body> for CSS stacking
    chatFlyoutEl.addEventListener('shown.bs.offcanvas', () => {
        document.body.classList.add('copilot-chat-open');
        chatBtn.classList.add('active');
        sessionStorage.setItem('copilotChatOpen', '1');
    });
    chatFlyoutEl.addEventListener('hidden.bs.offcanvas', () => {
        document.body.classList.remove('copilot-chat-open');
        chatBtn.classList.remove('active');
        sessionStorage.setItem('copilotChatOpen', '0');
    });

    // Restore state on page load
    if (sessionStorage.getItem('copilotChatOpen') === '1') {
        chatFlyout.show();
    }

    // Keyboard shortcut: Ctrl+Shift+C
    document.addEventListener('keydown', (e) => {
        if (e.ctrlKey && e.shiftKey && e.key === 'C') {
            e.preventDefault();
            chatFlyout.toggle();
        }
    });
})();
```

### 4. Adjust `note_form.html` stacking system (minimal)

The existing `adjustStack()` in `note_form.html` doesn't need changes. The CSS approach above uses `body.copilot-chat-open` to add an extra 400px shift to all note flyouts when the chat is open. This means:

- **Chat closed:** Note flyouts stack exactly as they do today (399px shifts)
- **Chat open:** Note flyouts get an additional 400px shift so they start at the left edge of the chat flyout

The note_form stacking JS remains untouched.

### 5. Main content margin (optional but recommended)

When the chat is open, the main content area could shrink to avoid being hidden behind it:

```css
body.copilot-chat-open main#main-content {
    margin-right: 400px;
    transition: margin-right 0.3s ease;
}
```

This is optional - the flyout could just overlay the content like the existing note flyouts do. Decide based on UX preference.

---

## File Changes Summary

| File | Change |
|------|--------|
| `templates/note_form.html` | **Pre-req fix:** Add `flyout-shifted-4` and `flyout-shifted-5` CSS classes, update `adjustStack()` to remove the `>= 3` cap |
| `templates/base.html` | Add navbar button, flyout HTML, CSS (including copilot-aware shift overrides), and JS |
| `app/routes/ai.py` (or new `chat.py`) | New POST endpoint for chat messages (built in other session) |

---

## Stacking Behavior Matrix

Shows the `transform` on note flyouts under various scenarios:

| Scenario | Note flyout (newest) | shifted-1 | shifted-2 | shifted-3 | shifted-4 | Copilot Chat |
|----------|---------------------|-----------|-----------|-----------|-----------|--------------|
| Chat closed, 1 note flyout | `0` | - | - | - | - | hidden |
| Chat closed, 2 note flyouts | `0` | `-399px` | - | - | - | hidden |
| Chat closed, 5 note flyouts | `0` | `-399px` | `-798px` | `-1197px` | `-1596px` | hidden |
| Chat open, 0 note flyouts | - | - | - | - | - | `0` |
| Chat open, 1 note flyout | `-400px` | - | - | - | - | `0` |
| Chat open, 2 note flyouts | `-400px` | `-799px` | - | - | - | `0` |
| Chat open, 5 note flyouts | `-400px` | `-799px` | `-1198px` | `-1597px` | `-1996px` | `0` |

The chat flyout never moves. Note flyouts always shift left to make room for it.

---

## Edge Cases to Handle

1. **Narrow screens:** On viewports < 900px, the chat flyout could overlap too much. Consider hiding the navbar button or making the flyout full-width on mobile (like Bootstrap's default offcanvas behavior).

2. **sessionStorage restore timing:** The flyout should restore after DOM ready but before visible paint. The IIFE at end of `<body>` handles this naturally.

3. **Page transitions:** `sessionStorage` persists for the browser tab session. If the user opens the chat on the notes page and navigates to customers, the chat stays open - which is the desired behavior for a global assistant.

4. **Keyboard shortcut conflicts:** `Ctrl+Shift+C` is unused in the current shortcut map (checked `keyboardShortcutsModal` in base.html). Chrome uses it for DevTools Elements inspector, but that's a dev-only concern.

5. **Chat state preservation across navigation:** Message history should either:
   - Be fetched from the server on each page load (simplest, chat history lives in DB)
   - Be stored in `sessionStorage` as JSON (faster, no server round-trip)
   - Recommendation: Server-side storage via a `ChatMessage` model so history persists across sessions

---

## Dependencies

- Bootstrap 5 Offcanvas (already included)
- Bootstrap Icons (already included)
- Chat backend endpoint (being built in parallel session)
- No new libraries needed for the flyout shell itself
