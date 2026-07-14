# KAI Cockpit UI Standards

This is the durable single source of truth for the Operator Console UI.
`src/kai/cockpit/static/cockpit.css` is the central styling truth; this
document states the rules new and existing pages must follow.

The one-time project record that produced this guide is
`OPERATOR_CONSOLE_UI_CONSISTENCY_PLAN.md`.

## Architecture

The Cockpit is a server-rendered Jinja UI with static CSS and small static
JavaScript enhancements. Do not introduce a frontend framework, a build step,
or new package dependencies.

New pages/components use existing primitives in `cockpit.css`. Propose
additions to the primitives, not one-off page styles. The `tokens` cascade
layer is the only place raw `px`/`rem` values legitimately live; everywhere
else, use tokens/classes.

## Typography

| Use | Token | Weight |
|---|---|---|
| Page titles | `--text-xl` | 600 |
| Card titles (`.card__title`) | `--text-lg` | 600 |
| Body copy, form values | `--text-base` | 400 |
| Form labels | `--text-sm` | 600 |
| Help text, metadata, eyebrows, table headers | `--text-sm` | 400 |
| Micro labels / badges | `--text-xs` | 600 |

- `--text-md` (16px) is reserved for genuinely emphasized inline body copy
  (currently only `.chat-reply`). It is never used for standard labels, help
  text, or card titles.
- `--text-2xl` (the clamp hero token) is not used on any Operator Console
  screen. It stays reserved for pre-auth hero/display use.
- No page introduces a font size outside this table without updating this
  table first.

## Product language and hierarchy

- The operator UI calls the product unit an **agent**. `Bot` and `deployment`
  are reserved for source code, API/resource names, or technical diagnostics
  where that distinction is necessary.
- Page headers consistently use `.page-eyebrow`, `.page-title`, and
  `.page-subtitle` in that order. The subtitle explains the operator outcome,
  not the implementation detail.
- Card headers use `.card__eyebrow` only for a grouping label and
  `.card__title` for the actionable section name. Help text explains a control
  or its operator impact; it does not repeat the title.
- `Brain` is the product name for shared knowledge. In explanatory copy,
  describe it as knowledge that agents consult before replying.
- `channel` means where work arrives or replies are delivered; `tool` means a
  connected capability an agent can use; `readiness` means the availability of
  supporting services.
- Surface AI capability through observable facts such as connected knowledge,
  channels, tools, activity, and attention states. Do not add decorative
  "AI" graphics, fabricated thought traces, or simulated activity.
- Persist timestamps as ISO-8601 values, but render them with the shared
  `format_timestamp()` template global. Raw ISO timestamps do not appear in
  operator-facing copy.

## Spacing

- Card padding: `var(--space-lg)`, no exceptions per-page.
- Compact row/list item padding: `var(--space-sm) var(--space-md)`.
- Field-to-help-text gap: `var(--space-2xs)`.
- Gap between major cards on a page: `var(--space-lg)`.
- `var(--space-xl)`+ is reserved for true two-column/section separation, never
  for routine form field stacking.

## Cards

- `.card` is the only page surface primitive: white background, subtle border,
  shallow shadow, `var(--radius-lg)`, `var(--space-lg)` padding. No new card
  variants are created.
- The only permitted semantic exception is a danger-zone treatment
  (`.card--danger`, border/background tint) for destructive sections.
- `.card__eyebrow` is used for grouping labels (`Identity`, `Triggers`,
  `Chats`); `.card__title` is used for the section name.
- Subcards, when needed, use a border + `var(--color-surface-alt)` background.
  Nested shadows inside cards are not used.
- **Prefer a single card divided by borders over multiple boxed subcards**
  when a card's sections are two halves of one workflow rather than
  genuinely separate modules (e.g. Sleep mode's "put to sleep" / "sleeping
  chats" halves, or Brain's `.source-card` list). Use `border-left`
  (`border-top` on mobile) between sections instead of giving each section
  its own border + background, mirroring `.source-card`/`.source-list`.
  Reserve boxed subcards for cases where sections are truly independent,
  self-contained actions.

## Buttons

Four variants, and only four:

1. **Primary** (`.button--primary`) — green. One per page: the page's single
   dominant commit action (Save / Create / the action the page exists to
   perform).
2. **Secondary** (`.button`, unstyled base) — white surface. Default for
   Cancel, Settings, Restart, Upload trigger, sibling equal-alternative
   submits, and all other non-destructive actions.
3. **Danger** (`.button--danger`) — red. Destructive actions only (Delete
   deployment, per-document Delete in Brain).
4. **Ghost** (`.button--ghost`) — text-only, no border/fill. Used exclusively
   for low-emphasis header navigation (`Back` in `.page-header__action`).

Primary-count rules:

- One page = one primary, and it is the page's dominant commit action.
- **Equal-alternatives exception:** when a page hosts multiple sibling forms
  that are interchangeable ways to do the same job (the Sources card's
  Upload / Add website / Add text), none is promoted. All stay secondary.
- Destructive form submits use `.button--danger`, never primary, and do not
  count toward the one-primary limit.
- `Back` (header, ghost) and `Cancel` (form footer, secondary) can point at
  the same URL yet intentionally differ: `Back` is header navigation (ghost)
  and `Cancel` is the secondary half of the Save/Cancel pair.

Icon-on-primary convention: every `.button--primary` commit action carries a
leading icon (`Save changes`, `Save usage rules`, Create deployment, Create
Brain, Start, Connect WhatsApp, Refresh, Send). `Back` does not get an icon.
No other button receives an icon outside this convention.

`.button--sm` is reserved for compact in-row actions only (chat picker
toggles, Wake). It is never used for a standalone control like `Load more
chats`, which is a normal-size, full-width (`--block`) secondary button.

No fifth button variant is introduced without updating this table first.

## Forms

- Text, number, URL, search, select, and textarea controls share one visual
  contract: 40px tall, same border, radius, font size, shadow, and focus ring
  (`--shadow-focus-accent`).
- `input[type="checkbox"]` is never left in native/browser-default form. It
  is styled to match the app's accent, radius, and focus ring
  (`--color-accent-600`, `--radius-sm`, `--shadow-focus-accent`), everywhere
  a checkbox appears (Settings toggles, Brain's mandatory rule, danger-zone
  confirm). A native blue checkbox next to custom-styled buttons and inputs
  is the single biggest "unfinished form" signal in the app; this rule exists
  to remove it everywhere at once, not page by page.
- `.checkbox-grid` groups a set of equal-weight toggles (e.g. Capabilities)
  into a scannable two-column layout instead of a long single-file list;
  collapses to one column below tablet width.
- `.field--narrow` caps `input` **and** `select` width to 320px, so `Voice`
  matches `Language` and `Timezone`.
- `.search-field` is a wrapper that overlays a leading icon and adds left
  padding to the inner `input[type="search"]`. It does not introduce a second
  border, background, or focus ring.
- `.field-grid` groups related compact fields (e.g. `Language`/`Voice`/
  `Timezone`) in a responsive grid that collapses to one column below tablet
  width. `Goal` stays full-width above the grid as the dominant field.
- A group of related compact fields is always one flat `.field-grid`, even
  when some fields are conceptually a sub-group (e.g. the voice-note fields
  under Participation). Splitting a field group into nested/sibling
  `.field-grid`s makes their column widths drift apart because each grid
  computes its own column count independently; a shared help line for the
  sub-group goes below the fields it describes, not nested around its own
  grid. That trailing note uses the standard field-to-field gap
  (`var(--space-sm)`, via `.field-grid + .field__help`), not the tighter
  field-to-caption gap, since it describes the whole row of fields rather
  than captioning a single one.
- Labels sit immediately above their control; help text sits immediately
  below. Placeholder text is never used as a label substitute.
- `input[type="file"]` is never shown in its native form. It is visually
  hidden (`.sr-only`) and paired with a `<label>` styled as a secondary
  `.button` that triggers the picker; the selected file name is shown as
  helper text via the `.upload-control` pattern.

## Status badges and health indicators

- `.badge` is exclusively for non-interactive status text. It is never reused
  as a clickable filter or button.
- Color mapping is fixed: green = Running/Ready/Healthy, amber = Not
  connected/Reading/Restart needed, red = Failed/Down/Attention, gray =
  Stopped/zero counts.
- Status dots represent entity state; badges represent textual state. Both are
  used together where relevant, never as substitutes for each other.
- Decorative green borders on cards are not used outside genuine state
  emphasis.

## Mobile behavior

- Existing breakpoints (`640px`, `700px`, `760px`) are preserved.
- All Settings grids (`.field-grid`, form action clusters) collapse to one
  column below tablet width.
- `Load more chats` and form action clusters become full-width on mobile.
- Minimum touch target for any standalone control is 40px high.
- No two-column form layout renders below tablet width, anywhere in the app.

## Primitives added by the UI consistency pass

| Class / selector | Purpose |
|---|---|
| `.button--ghost` | Text-only header `Back` navigation |
| `.cluster--between` | Pins a cluster's children to opposite ends (card title + status badge, card title + action) |
| `.field--narrow input, .field--narrow select` | Narrow width covers selects, not just inputs |
| `.field-grid` / `.field-grid .field` | Responsive compact field group |
| `.field-grid + .field__help` | Group-level help note after a field-grid uses the field-to-field gap, not the tighter field-to-caption gap |
| `.search-field` | Leading-icon wrapper for `input[type="search"]` |
| `.upload-control` / `.upload-control__filename` | Styled file upload affordance |
| `.chat-picker__row:hover` / `:focus-within` | Interactive row states |
| `input[type="checkbox"]` (accent-styled) | Consistent checkbox appearance app-wide |
| `.checkbox-grid` | Two-column layout for a group of equal-weight toggles |
| `.checkbox-row--locked` | Dimmed modifier for an unentitled/disabled toggle row |
| `.chat-picker__spinner` | Spinner for the async chat-picker loading state |
| `.chat-picker__empty` | Empty-state message when a chat fetch returns zero rows |
| `.source-item--stack` | Opts a `.source-item` out of the compact inline "input + button" two-column layout for items whose controls are a fuller block (a form with a reply panel, a divided sub-panel) rather than a single-line input/button pair |
| `.sleep-panel` / `.sleep-panel__section` / `.sleep-panel__title` | Two-halves-of-one-workflow layout divided by a border (`border-left`, `border-top` on mobile) instead of two boxed subcards |
| `.landing-*` | Pre-auth marketing landing composition: navigation, two-column hero, workflow illustration, and support-demo callout. Scoped to the landing page and built from the shared token system. |
| `.deployment-card__summary` | Compact, right-aligned operational summary on an agent card; collapses below the card content on mobile. |
| `.readiness-summary` / `.readiness-summary__section` | Exception-led infrastructure health summary: a concise readiness state followed by divided attention and available-service sections. |

New local icons: `icons/search.svg`, `icons/upload.svg`, `icons/check.svg`,
`icons/bot.svg`.

## Review guardrails

Before merging a Cockpit UI change, check:

- No new inline `<style>` blocks or `style="..."` attributes in
  `src/kai/cockpit/templates/*.html`.
- No new hard-coded `px` font sizes or ad-hoc `padding`/`margin` literals
  added in `cockpit.css` outside the `tokens` layer. (The `tokens` layer is
  excluded — that is where raw values legitimately live.)
- Any new primitive class is documented in the table above in the same change
  that introduces it.
- No CSS deadcode.
- No Javascript deadcode.
