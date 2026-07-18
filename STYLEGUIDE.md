# kAI Cockpit UI Standards

This is the durable single source of truth for the Operator Console UI.
`src/kai/cockpit/static/cockpit.css` is the central styling truth; this
document states the rules new and existing pages must follow.

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
| Landing promise (`.landing-title`) | `--text-landing-display` (44-64px clamp) | 600 |
| Landing proof address (`.landing-proof__email`) | `--text-landing-email` (28-40px clamp) | 600 |

- `--text-md` (17px) is reserved for genuinely emphasized inline body copy
  (`.chat-reply`). It is never used for standard labels, help text, or
  card titles. The pre-auth `.landing-lede` uses standard `--text-base`
  with the secondary ink color, not `--text-md`.
- `--text-2xl` (the clamp hero token) is not used on any Operator Console
  screen. It stays reserved for pre-auth hero/display use.
- `--text-landing-display` is reserved for the single pre-auth landing promise.
  It is not reused for section titles.
- `--text-landing-email` is reserved for the deployed-agent email focal point.
- No page introduces a font size outside this table without updating this
  table first.

## Product language and hierarchy

- The operator UI calls the product unit an **agent**. `Bot` and `deployment`
  are reserved for source code, API/resource names, or technical diagnostics
  where that distinction is necessary.
- Page headers consistently use `.page-eyebrow`, `.page-title`, and
  `.page-subtitle` in that order. The subtitle explains the operator outcome,
  not the implementation detail.
- **Eyebrow vocabulary is a closed set.** Every operator-console
  `.page-eyebrow` uses one of: `Agents` · `Agent` · `Settings` ·
  `Knowledge` · `Channels` · `Tools` · `Readiness` · `Attention` ·
  `Activity`. Subpages of Connections preserve the `Channel setup` /
  `Tool setup` split (a channel is where work arrives; a tool is a
  connected capability — see the definitions below). Pre-auth surfaces
  (login, landing) are exempt. Eyebrows are navigation-breadcrumb-ish,
  not mini-marketing lines; prose goes in the subtitle.
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

Five variants, and only five:

1. **Primary** (`.button--primary`) — green. One per page: the page's single
   dominant commit action (Save / Create / the action the page exists to
   perform).
2. **Secondary** (`.button`, unstyled base) — white surface. Default for
   Cancel, Settings, Restart, Upload trigger, sibling equal-alternative
   submits, and all other non-destructive actions.
3. **Danger** (`.button--danger`) — red. Destructive actions only (Delete
   deployment, per-document Delete in Brain).
4. **Warn** (`.button--warn`) — amber. Used only for non-destructive
   interruption of a running operation (e.g. **Stop** on a running agent).
   Never for confirmation or risk-of-loss; those are Danger.
5. **Ghost** (`.button--ghost`) — text-only, no border/fill. Used exclusively
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

No sixth button variant is introduced without updating this table first.

Dual-primary rule: when the same page renders different primary buttons in
different view states (e.g. `Start` when stopped, `Send` when running on
`deployment.html`), each is the single primary of *that* view state, not of
the page. The two primaries are mutually exclusive by state — they never
render together — so this does not violate the one-primary-per-page rule;
it states that the "page" for primary-count purposes is one view state at a
time. See `deployment.html:91` (`Start`) and `deployment.html:152` (`Send`).

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
- Disabled form controls (`<input>`, `<textarea>`, `<select>`) use the
  same shared disabled treatment as `.button:disabled`:
  `var(--color-surface-alt)` background, `var(--color-subtle)` text,
  `var(--color-border)` border, `cursor: not-allowed`. Do not rely on
  browser-default disabled chrome; the affordance is too quiet next to
  the styled button it ships beside.

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

### Status system — one state machine, many surfaces

A single state→badge mapping is reused across console, agent detail,
connections, brain, and readiness. No ad-hoc severity→badge pairs; every
status rendering derives from this table:

| State | Badge | Optional accent |
|---|---|---|
| Live / ready / connected / running | `badge--live` | green edge / dot |
| Pending / reading / connecting / restart needed | `badge--warn` | amber |
| Failed / down / attention / critical | `badge--danger` | red |
| Stopped / zero / inactive | `badge--muted` | gray |
| Configured (non-runtime) | `badge--ok` | only for "setup complete," not health |

Dot + badge together only when the entity has continuous health (agent,
readiness service). Connections are badge-only when the card already
carries status edge classes (`.status-card--*`). Escalation severity
maps to `danger`/`warn`/`muted` — never `live`, because an unresolved
escalation is not healthy.

## Motion

Default is **zero animation**. No entrance effects, simulated activity, or
decorative motion. The product should feel modern through composition,
typography, contrast, and clear operational state.

Three animated exceptions are permitted:

1. **`.status-dot--running::after`** — radar pulse ring on live dots. Used only
   to read "actively alive" at a glance (agent status dot, readiness live row).
   Never on secondary indicators.
2. **`.chat-picker__spinner`** — loading spinner for async chat-picker fetch.
   Never as a generic progress indicator.
3. **Loading spinners that imply "thinking"** — a small spinner on a button or
   inline next to the action that triggered the request, while the request is
   in flight. Used for any async submit where the operator would otherwise see
   no feedback (a save that doesn't redirect, a chat-style send without a
   visible reply yet, etc.). The spinner is anchored to the action that
   caused the wait — not free-floating — and disappears the moment the result
   arrives or the page state changes. No full-screen overlays, no "AI is
   thinking" copy, no simulated typing.

Subtle hover/focus transitions on interactive controls are acceptable when used
sparingly (button state feedback, card hover). No transforms on hover, no
entrance animations, no staggered reveals. Loading states with a known
duration use the thinking spinner above; loading states with unknown duration
use concise static text or a stable progress indicator.

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
| `.status-dot--running::after` | Radar pulse ring on live status dots — animated exception to the zero-motion default, read as "actively alive" |
| `.source-item--stack` | Opts a `.source-item` out of the compact inline "input + button" two-column layout for items whose controls are a fuller block (a form with a reply panel, a divided sub-panel) rather than a single-line input/button pair |
| `.sleep-panel` / `.sleep-panel__section` / `.sleep-panel__title` | Two-halves-of-one-workflow layout divided by a border (`border-left`, `border-top` on mobile) instead of two boxed subcards |
| `.landing-*` | Pre-auth marketing landing composition: navigation, two-column hero, static workflow illustration, onboarding path, Brain diagram, connection band, and proof invitation. Scoped to the landing page and built from the shared token system. |
| `.landing-workflow__diagram` / `__stage` / `__resources` / `__decision` | Static, connected workflow line art: incoming channels, selected role, Brain and tools, then possible actions. It never renders a fabricated message, reply, timestamp, or runtime status. |
| `.landing-steps` / `.landing-steps__path` | One continuous four-step onboarding path rather than a feature-card grid. |
| `.landing-brain` / `.landing-brain__diagram` | Shared-knowledge explanation showing documents, website, and notes feeding the Brain and configured agent roles. |
| `.landing-connections` / `.landing-connections__group` | Semantic connection band grouping channels where work arrives separately from tools agents use; visible text labels are retained until approved local integration assets are available. |
| `.landing-connections__dialects` | `.mono` micro-line beneath a connection item for code-like detail that recedes for the casual reader (e.g. the Database row's `PostgreSQL · MySQL · SQL Server` dialect list); uses the `.mono` utility, no new font size outside the table. |
| `.landing-proof` / `.landing-proof__path` | Editorial proof invitation with the deployed support email and factual email-to-agent-to-inbox path; its mail action stays secondary to the invite CTA. |
| `.landing-proof__email` | Focal-point treatment for the deployed-agent email address on the proof section. Uses the reserved `--text-landing-email` token (28-40px clamp) and `--font-mono`; the address is the visual climax of the section, with the `Ask the working agent` button as the secondary action. |
| `.landing-footer` | Quiet closing reassurance at the foot of the landing page: private-beta language, the configured contact address, and the GitHub link. Muted text on the open canvas, separated by a `--color-divider` top border; no new surface primitive. |
| `.deployment-card__summary` | Compact, right-aligned operational summary on an agent card; collapses below the card content on mobile. |
| `.deployment-card--attention` / `--running` / `--stopped` | State modifier on `.deployment-card`. Attention gets a danger-tinted border (4% danger mix on surface). Running uses a stronger neutral border. Stopped dims the whole card to 0.85. |
| `.readiness-summary` / `.readiness-summary__section` | Exception-led infrastructure health summary: a concise readiness state followed by divided attention and available-service sections. |
| `.badge--ok` | Green success badge — paired with `.badge` for a non-interactive "configured" / positive-state indicator (template preview tool-availability). |
| `.hidden` | Utility to set `display: none` (used for the template-preview container before the first AJAX fetch populates it). |

### Utilities

Small single-purpose helpers that don't belong to a component family.
Each is defined once in `cockpit.css` and reused across pages:

| Class | Purpose |
|---|---|
| `.muted` | Secondary/metadata text color (`var(--color-muted)`) |
| `.mono` | Monospace font for IDs, ports, counts (`var(--font-mono)`) |
| `.sr-only` | Visually hidden, screen-reader accessible |
| `.hidden` | `display: none` |
| `.img-constrained` | Caps an image's max width (e.g. QR codes, diagrams) |
| `.text-center` / `.text-left` | Text alignment |
| `.text-danger` / `.text-warn` | Inline danger/warn text color |
| `.text-sm` | Smaller text step (`var(--text-sm)`) |
| `.mt-2xl` / `.mb-0` / `.mb-md` | Spacing utilities (top margin 2xl, bottom margin 0, bottom margin md) — prefer layout primitives (`.stack`, card gaps) for routine stacking; these are for one-off overrides only |
| `.card--center` | Centers a card's content (used for narrow centered cards like auth/error states) |
| `.empty-state` / `.empty-state--hero` / `.empty-state__icon` / `.empty-state__hint` | Empty / first-run state shell. Outcome-first copy + optional icon + optional primary CTA. Phase 4 populates; the macro lives at `macros/empty_state.html`. |
| `macros/page_header.html` | Page header macro — single source for `.page-header` / `.page-header--split` with eyebrow, title, subtitle, optional back URL. |
| `macros/form_actions.html` | Save / Cancel (or Save / Test) footer cluster macro — primary with check icon + optional secondary. |
| `macros/status_badge.html` | State→badge mapping macro — routes every status badge through the fixed palette so no inline `badge--{{ ternary }}` patterns drift. |
| `macros/empty_state.html` | Empty-state shell macro — icon + title + body + optional primary CTA + optional hint. |
| `.thinking-spinner` | Small spinning ring anchored to the action that triggered an async request. Rendered with `role="status"` and `aria-label="Loading"` (or an `aria-live="polite"` sibling) at every call site so screen readers get the same in-flight signal. Disabled under `prefers-reduced-motion` (static ring, no spin). Gone the moment the result lands. |
| `.field--error` / `.field__error` | Validation error primitive. `.field--error` on the parent `.field` turns the control border red; the error message renders in the `.field__help` slot in `--color-danger`. Use `.field__error` for a single explicit error block above the form. The affected control gets `aria-invalid="true"` and `aria-describedby` pointing at the help text id. |

New local icons: `icons/search.svg`, `icons/upload.svg`, `icons/check.svg`,
`icons/bot.svg`, `icons/brain.svg`.

## Page shells

Only four page shapes are allowed. Every Operator Console screen is one of:

- **Index** — `.page.stack` → `.page-header` → optional summary strip →
  a vertical stack of `.card`s (e.g. console, connections, readiness).
- **Detail / settings** — `.page-header--split` (copy | ghost `Back` or
  secondary `Settings`) → stacked `.card` sections → form-actions cluster
  (primary + `Cancel`) → optional `.card--danger` last (e.g. agent detail,
  agent settings, connection setup).
- **Wizard** — same shell as Detail / settings, but a single Configuration
  `.card` plus the template picker (e.g. the new-agent wizard).
- **Marketing / auth (exception surface)** — `.landing-*` / `auth-*`
  primitives only; does not use the operator console shell (e.g. landing,
  login).

A new page that does not fit one of these four shapes needs a design
decision in this styleguide before it ships, not a one-off layout.

### Page / component matrix

Every screen answers the same questions. A new block that can't map onto
this matrix needs a primitive plus a styleguide row, not a one-off:

| Dimension | Required shape |
|---|---|
| Shell | `.page.stack` → `.page-header` → cards in a vertical stack |
| Header | `.page-eyebrow` → `.page-title` → `.page-subtitle` (outcome-oriented) |
| Nested action | `.page-header--split` + ghost `Back` **only** for sub-routes |
| Primary | **one** `.button--primary` commit per view state (see Dual-primary rule) |
| Cards | `.card` + optional `.card__eyebrow` / `.card__title` |
| Status | badge palette + optional status-dot; same words everywhere |
| Times | `format_timestamp()` only |
| Forms | label above, help below; narrow fields in one `.field-grid` |
| Danger | shared danger-zone partial |

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
- **Template-side checks** (not only CSS):
  - Every class referenced in a template has either a `cockpit.css`
    selector or an entry in the primitives table — no one-off classes.
  - Every new page maps onto one of the four Page shells and onto every
    row of the page/component matrix; if it can't, add the primitive and
    the styleguide row in the same PR.
  - No legacy button API: `class="btn "` / `btn--` must never reappear;
    buttons use `.button` and its documented variants only.
  - Eyebrows use the closed vocabulary; timestamps go through
    `format_timestamp()`; status color comes from the fixed badge mapping.
