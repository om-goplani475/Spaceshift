---
name: Spaceshift
description: A container-terminal control room for EBS volumes - steel rail, concrete ground, stencilled unit codes, one cell per volume.
colors:
  concrete-ground: "#e6e9e6"
  concrete-surface: "#f6f7f5"
  concrete-inset: "#eceeeb"
  steel-line: "#c3c9c5"
  steel-line-strong: "#8a959a"
  ink: "#151a1e"
  ink-soft: "#424d55"
  ink-quiet: "#59656c"
  rail-steel: "#1b2229"
  oxide-red: "#9b3928"
  safety-mustard: "#d9a21b"
  mustard-ink: "#7c5700"
  tide-green: "#226543"
  ultramarine: "#1f5fa6"
typography:
  display:
    fontFamily: "Barlow Condensed, Barlow, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1.875rem"
    fontWeight: 700
    lineHeight: 1.15
    letterSpacing: "0.01em"
  headline:
    fontFamily: "Barlow Condensed, Barlow, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1.125rem"
    fontWeight: 700
    lineHeight: 1.15
    letterSpacing: "0.01em"
  stat:
    fontFamily: "Barlow Condensed, Barlow, ui-sans-serif, system-ui, sans-serif"
    fontSize: "2.5rem"
    fontWeight: 700
    lineHeight: 1.05
  label:
    fontFamily: "Barlow Condensed, Barlow, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 600
    letterSpacing: "0.04em"
  body:
    fontFamily: "Barlow, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 400
    lineHeight: 1.5
  data:
    fontFamily: "Barlow, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
rounded:
  sm: "2px"
  md: "3px"
  lg: "4px"
  xl: "6px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
  2xl: "32px"
components:
  button:
    backgroundColor: "{colors.concrete-surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "0 16px"
    height: "36px"
  button-primary:
    backgroundColor: "{colors.rail-steel}"
    textColor: "{colors.concrete-surface}"
    rounded: "{rounded.md}"
    padding: "0 16px"
    height: "36px"
  nav-link:
    textColor: "{colors.concrete-surface}"
    height: "44px"
    padding: "0 20px"
  nav-link-active:
    backgroundColor: "{colors.oxide-red}"
    textColor: "#ffffff"
    height: "44px"
  dry-run-strip:
    backgroundColor: "{colors.safety-mustard}"
    textColor: "{colors.ink}"
    padding: "12px 16px"
  unit-tile:
    backgroundColor: "{colors.concrete-surface}"
    textColor: "{colors.ink}"
    padding: "16px 20px"
  bay-cell:
    backgroundColor: "#ffffff"
    textColor: "{colors.ink}"
    rounded: "0"
    height: "64px"
  badge:
    textColor: "{colors.ink-soft}"
    rounded: "{rounded.sm}"
    padding: "1px 8px"
  input:
    backgroundColor: "{colors.concrete-surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    height: "36px"
---

# Design System: Spaceshift

## Overview

**Creative North Star: "The Stowage Plan"**

Spaceshift is read the way a terminal planner reads a bay plan: every volume is a numbered unit in a cell, filled to how full it is, and the one that needs a human is the one painted differently. The interface is a working tool for an on-call engineer, so it is flat, labelled and calm. Colour is paint on steel, used for meaning; nothing glows.

The look is container-terminal rather than control-room: a dark steel rail down the left, a concrete-coloured working ground, stencil-like condensed type for unit codes and headings, and square corners with 1px steel lines. It is built for both light and dark; dark is the same steel and concrete at night, not a different identity.

**Key Characteristics:**
- Flat at rest: depth comes from 1px steel borders and fills, not shadows.
- Unit codes (volume and instance IDs) and headings are set in Barlow Condensed; running text in Barlow.
- State is painted, never glowed: oxide red for danger, mustard for dry-run and caution, ultramarine only for "resizing now".
- Standard controls everywhere: real buttons, selects, tables with sortable headers, a side rail that reads as navigation.
- Safety is visible: dry-run is a full-width mustard strip on every page it applies to.

## Colors

A concrete ground, steel structure and three paints. State text colours are solved, not eyeballed: each is at least 4.5:1 on every surface and on its own 10-18% tint, in both themes. Light and dark are one palette, defined once in `dashboard-base.css` with `light-dark()`; the values below are the light theme, and the dark counterparts are in the sidecar.

### Primary
- **Rail Steel** (#1b2229): the side rail in both themes, and the primary button in light mode (concrete in dark mode). It is the structure colour, not a brand accent.

### Secondary
- **Oxide Red** (#9b3928): danger and the current page. Over-threshold bay cells, "needs attention", failed states, destructive buttons, and the active nav item.
- **Safety Mustard** (#d9a21b): caution and dry-run. The dry-run strip, 70-85% bay cells, warning meters and badges. Text on it is always ink (#151a1e).

### Tertiary
- **Ultramarine** (#1f5fa6): a resize is in progress. Nothing else is ever this colour. As text it is #1d599c in light and #8db9ec in dark, so it stays readable on its own tint.
- **Tide Green** (#226543): healthy and on. Autoresize switches, completed resizes, the watched share of storage.

### Neutral
- **Concrete Ground** (#e6e9e6): the page. **Concrete Surface** (#f6f7f5): tables, panels, tiles, inputs. **Concrete Inset** (#eceeeb): table headers and quiet fills.
- **Steel Line** (#c3c9c5) for row dividers; **Steel Line Strong** (#8a959a) for panel and control borders.
- **Ink** (#151a1e), **Ink Soft** (#424d55), **Ink Quiet** (#59656c): text, in falling order of emphasis. All three meet 4.5:1 on the surfaces they are used on, in both themes.
- **Mustard Ink** (#7c5700): amber used as text, where the mustard fill would fail contrast.

### Named Rules
**The One Blue Rule.** Ultramarine means a resize is in progress, in badges, tiles, bay cells and chart bars. If a design wants a blue for any other reason, it is wrong.
**The Paint, Not Light Rule.** State colours are flat fills and 1px/2px borders. No glows, halos, gradients or tinted shadows.
**The Meaning Before Looks Rule.** A state colour never carries meaning alone: it always comes with text, a number or a shape (percent in the bay cell, a word in the badge).

## Typography

**Display Font:** Barlow Condensed (with Barlow, system sans)
**Body Font:** Barlow (with system sans)
**Label/Mono Font:** Barlow Condensed for labels and unit codes; the system monospace only for code blocks, ARNs and command lines.

**Character:** A stencilled, working-yard condensed face for anything that names or labels, paired with its plainer sibling for reading. Both are self-hosted (SIL OFL, latin subset) because the content security policy is `default-src 'self'`.

### Hierarchy
- **Display** (700, 1.875rem, 1.15, 0.01em): the page heading, one per page.
- **Headline** (700, 1.125rem, 1.15): section and panel headings, drawer titles.
- **Stat** (700, 2.5rem, 1.05, tabular): fleet counts in unit tiles.
- **Label** (600, 0.9375rem, 0.04em, sentence case): table headers, tile labels, badges, the dry-run strip lead. Never upper-cased.
- **Body** (400-500, 0.9375rem, 1.5): running text and descriptions, capped around 64-72ch.
- **Data** (400, 0.875rem, tabular figures): table cells and notes. Unit codes in tables use Label styling at 1rem with 0.05em tracking.

### Named Rules
**The Stencil Rule.** Volume and instance IDs are always condensed, bold-ish and tracked, never in the body face or a monospace.
**The Sentence Case Rule.** Labels, buttons and headings are sentence case. No tracked all-caps eyebrows above headings.

## Layout

A fixed 232px steel rail on the left and a stage that is capped at 1560px, centred. Below 900px the rail becomes a top bar: one row of brand and controls, then a horizontally scrolling nav with visible labels; it is not sticky. The page rhythm is a 4px grid: 4, 8, 12, 16, 20, 24, 32, 40.

Fleet counts sit in a single bordered strip of unit tiles divided by hairlines, not as separate cards. The overview reads top to bottom: dry-run strip, counts, bay plan, charts, then tables. Wide tables scroll sideways inside their panel; on touch screens every control is at least 44px, and a row checkbox's whole cell is the target.

## Elevation & Depth

The system is flat at rest and conveys depth with 1px steel borders and tonal fills (ground, surface, raised). Shadows exist only on things that float above the page.

### Shadow Vocabulary
- **Floating** (`box-shadow: 0 6px 0 -2px rgba(0,0,0,0.12), 0 10px 28px rgba(8,12,16,0.28)`): toast, modal card, batch bar. An offset and a soft blur, never a halo.
- **Drawer** (`box-shadow: -8px 0 0 -2px rgba(0,0,0,0.14), -14px 0 32px rgba(8,12,16,0.3)`): the slide-over detail drawer only.

### Named Rules
**The Flat-By-Default Rule.** Panels, tiles, tables, inputs and buttons have no shadow. A shadow means "this floats above the page".

## Shapes

Square and workmanlike. Radii are 2px (badges, bay-adjacent chips), 3px (buttons, inputs), 4px (panels, modal cards), 6px at most; bay cells and the unit-tile strip are square. Borders are 1px steel at rest, 2px for emphasis (dry-run strip, critical or resizing bay cells, the sticky settings bar). Switches are squared, not pills. Status lamps are 8px squares. Sort arrows are drawn with clip-path, not text glyphs.

## Components

### Buttons
- **Shape:** 3px corners, 36px tall (44px on touch), 16px side padding.
- **Primary:** Rail Steel fill with concrete text in light; concrete fill with graphite text in dark. It is ink, not a brand colour.
- **Secondary:** Concrete Surface with a Steel Line Strong border. **Quiet:** transparent, no border, for toolbars and the sign-out icon.
- **Danger:** outline in Oxide Red; fills red on hover.
- **Hover / Focus:** border darkens on hover; focus is a 2px ring with 2px offset (ink in light, mustard in dark and on the rail).

### Navigation
The steel rail: 44px rows, concrete-grey text, white on the current page over an Oxide Red fill. The Live lamp, theme toggle and account chip live at its foot. On narrow screens the same links become a scrolling row with labels kept.

### Unit tiles
A bordered strip of tiles: a condensed label over a 2.5rem count. "Needs attention" tints red when above zero; "In progress" tints ultramarine. Counts are tabular figures.

### Bay plan (signature)
One 64px square cell per volume in an auto-filling grid (min 76px). The cell is filled from the bottom to the volume's usage in 5% steps, with its unit code top-left and percent bottom-right, each on a small solid chip so they stay legible over any fill. Grey under 70%, mustard 70-85%, oxide red over 85% with a 2px red border; ultramarine fill with a 2px blue border and offset outline while resizing. Critical and resizing volumes come first; the plan is capped at 240 cells.

### Dry-run strip
A full-width Safety Mustard bar with a 2px ink border, a warning icon, a condensed lead ("Dry run is on.") and one sentence with the link to Settings. It is never dismissible while dry-run is on.

### Badges and meters
Badges are 2px-corner outlined tags in condensed type, tinted by state. Meters are 8px tall hairline-bordered bars: tide green, mustard from 70%, oxide red over 85%.

### Inputs / Fields
- **Style:** 1px Steel Line Strong border, Concrete Surface fill, 3px corners, 36px tall (44px on touch).
- **Focus:** the 2px focus ring. Unsaved edits get a 2px amber border; rejected values a 2px red one.
- **Error / Disabled:** red border plus the reason in text; disabled at 45-60% opacity.

### Drawer and dialogs
A 420px slide-over drawer and a centred 440px modal card share one behaviour: focus moves in, the page behind becomes inert, Escape closes, focus returns to the opener. Closed dialogs are not tabbable.

## Do's and Don'ts

### Do:
- **Do** use Oxide Red, Safety Mustard and Tide Green only for danger, caution and healthy; always pair them with text or a number.
- **Do** reserve ultramarine (#1f5fa6) for "a resize is in progress".
- **Do** set every volume and instance ID in Barlow Condensed with 0.05em tracking.
- **Do** keep dry-run visible as the full-width mustard strip whenever it is on.
- **Do** define colours once, with `light-dark()`, and use the tokens in every component.
- **Do** keep controls at least 36px on desktop and 44px on touch or under 900px.
- **Do** keep depth to borders and fills; use the Floating shadow only for things that float.

### Don't:
- **Don't** use gradients, glows, glass blurs or coloured shadows. That is the look this system replaced.
- **Don't** put a coloured stripe on one side of a card, alert or list item; use a full border, an icon or a tint.
- **Don't** add tracked, upper-case eyebrow labels above headings.
- **Don't** add playful or decorative elements: illustrations, mascots, ornamental animation. This tool controls production disks.
- **Don't** make a live-mode action look as safe as a dry-run one, or hide the dry-run state.
- **Don't** use an arrow glyph or emoji as an icon; use the drawn 1.75px square-cap line icons.
- **Don't** load fonts or images from another origin; the content security policy is `default-src 'self'`.
