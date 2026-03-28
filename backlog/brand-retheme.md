# Brand Retheme: Logo Colors to UI

## Context

New logo commissioned for release. Logo uses a teal-to-blue "SB" monogram with light and dark background variants. The top two mocks in `static/Sales-Buddy.v2.jpg` are the final choices (same logo, white vs navy backgrounds).

The goal is to map the logo colors onto Bootstrap's existing semantic color system - no layout redesign, just theming.

## Brand Palette

| Role             | Color     | Usage                                  |
|------------------|-----------|----------------------------------------|
| Primary Blue     | `#2A86D1` | Buttons, links, primary actions        |
| Accent Teal      | `#27C3B3` | Secondary accent, highlights, badges   |
| Dark Navy        | `#0E1A2B` | Dark mode navbar, dark backgrounds     |
| Neutral Dark     | `#1C2430` | Dark mode card surfaces                |
| Light Background | `#F5F7FA` | Light mode page background             |

## Logo Assets Needed

- [ ] Get final logo files without Fiverr watermark (PNG/SVG)
- [ ] Light variant (dark text) for light mode navbar
- [ ] Dark variant (white text) for dark mode navbar
- [ ] Favicon / app icon from the "SB" monogram (bottom-right mock)
- [ ] Update `static/manifest.json` icons

## Theme Tokens (CSS Custom Properties)

### Dark Mode

| Element    | Color     |
|------------|-----------|
| Background | `#121821` |
| Cards      | `#1C2430` |
| Navbar     | `#0E1A2B` |
| Borders    | `#2B3648` |
| Primary    | `#2A86D1` |
| Accent     | `#27C3B3` |

### Light Mode

| Element    | Color     |
|------------|-----------|
| Background | `#F5F7FA` |
| Cards      | `#FFFFFF` |
| Navbar     | `#FFFFFF` |
| Borders    | `#E2E8F0` |
| Primary    | `#2A86D1` |
| Accent     | `#27C3B3` |

## Implementation Plan

### 1. CSS Theme Override File

Create a single CSS file (e.g., `static/css/theme.css`) that:

- Overrides Bootstrap color variables (`--bs-primary`, `--bs-link-color`, etc.)
- Defines `--sb-*` custom properties for brand tokens (background, card, navbar, border, text)
- Supports both dark and light mode via `[data-theme="dark"]` / `[data-theme="light"]`
- Does NOT touch status colors (success, warning, danger)
- Preserves all existing Bootstrap component structure

### 2. Navbar Retheme

- Replace existing purple navbar with dark navy (`#0E1A2B`) in dark mode
- Light mode navbar: white (`#FFFFFF`) with subtle bottom border
- Swap logo variant based on theme (dark text on light, white text on dark)

### 3. Card Elevation

Subtle depth to make cards stand off the background:

```css
.card {
  border: 1px solid var(--sb-border);
  border-radius: 10px;
  box-shadow: 0 2px 6px rgba(0,0,0,0.08);
}

[data-theme="dark"] .card {
  box-shadow: 0 1px 3px rgba(0,0,0,0.35);
}
```

### 4. Page Title Hierarchy

Add spacing and weight to section headers so they stand apart from widget titles:

```css
.page-title {
  font-size: 1.75rem;
  font-weight: 600;
  margin-bottom: 1rem;
}
```

### 5. Button Rounding

Slightly more rounded buttons to modernize the Bootstrap defaults:

```css
.btn {
  border-radius: 8px;
}
```

### 6. Accent Gradient (Sparingly)

Use the teal-to-blue gradient for select highlights only:

- Selected nav item underline
- Active tab indicator
- Loading/progress bars

```css
background: linear-gradient(90deg, #27C3B3, #2A86D1);
```

## What NOT to Change

- Page layouts and component structure
- Status colors (success green, warning yellow, danger red)
- Badge semantics (seller = primary, territory = info, topic = warning)
- Any functional behavior

## Contrast Checklist

- [ ] Primary blue (`#2A86D1`) on white - must pass WCAG AA for normal text
- [ ] Primary blue on dark navy - must pass WCAG AA
- [ ] Accent teal (`#27C3B3`) on dark backgrounds - verify readability
- [ ] Light mode text on `#F5F7FA` background
- [ ] Dark mode text on `#121821` background
- [ ] Navbar links visible in both themes

## Navbar Approach Decision

Going with **Option B: theme-adaptive navbar** (dark in dark mode, light in light mode). This feels cleaner for a productivity tool and keeps light mode feeling airy.

## Open Questions

- Badge colors: Should `bg-info` (currently used for territories) shift to match accent teal? Or keep Bootstrap default info blue?
- Gradient usage: Worth adding to the navbar brand area, or keep it minimal?
- Font: Staying with Bootstrap defaults, or picking a specific font to match the logo wordmark?
