# Monitoring console and design system

The console is an original, lightweight internal operations UI. It is one page, `src/invoice_optimizer/templates/index.html`, plus package-local static files. All labels are English. It contains no remote images, web fonts, analytics, CDN scripts, frontend build tool or JavaScript framework.

| File | Role |
|---|---|
| `static/theme.js` | Loaded synchronously first in `<head>`. Applies display preferences to `<html>` before first paint and exposes `window.IOTheme` (`set`, `get`, `reset`, `toggleSidebar`, `keys`). |
| `static/tokens.css` | Design tokens: OKLCH colour primitives, accent presets, theme steps, semantic tokens, density, motion, sRGB fallback. Loaded before every other stylesheet. |
| `static/app.css` | Console styles in 15 numbered sections (base, controls, status pills, shell, topbar, headings, KPI cards, tables, notices, dialogs/palette/toasts, view components, glass mode, motion, responsive, reduced motion and forced colours). Every value comes from tokens. |
| `static/app.js` | Views, API calls, SSE, forms. |
| `static/charts.js` | `window.MiniCharts`: dependency-free inline SVG/HTML charts. |
| `static/shortcuts.js` | Command palette, keyboard shortcuts and the Appearance panel. DOM-only: never fetches and never touches app state, so every permission check in `app.js` still applies. |
| `static/motion.js` | `window.IOMotion`: view entrance, KPI count-up, segmented-control indicator, relative times. |
| `static/card_deck.*`, `swipe_workspace.js`, `swipe_pdf.*`, `risk_bars.*` | Cards/Swipe layouts, PDF page previews and model parameter bars. |

All static files together are about 525 KB uncompressed (`app.js` about 260 KB, `app.css` about 119 KB). Assets carry a `?v=` cache-busting query and are served with `Cache-Control: no-cache`, so a deploy is never masked by a stale script.

## Design system

### Tokens and colour

Every colour derives from OKLCH primitives in `tokens.css`. Themes and accents only change lightness, chroma and hue numbers; the semantic tokens (`--text`, `--text-muted`, `--surface`, `--accent`, status and chart series colours) are written once.

- **Neutrals** use a faint cool hue (`--n-h: 258`) so greys sit well with every accent.
- **Accent presets** (`data-accent`): indigo (default, hue 274), blue (252), teal (200), emerald (160), amber (58), rose (8). Each has separate light-theme and dark-theme lightness and chroma.
- **Status hues**: success 150, warning 62, danger 27, info 252. Status pills always combine a dot with text; colour is never the only signal.
- **Chart colours**: six categorical `--series-N` tokens in a fixed order, folded into "Other" beyond six, plus a neutral; diverging red/blue for model contribution bars.
- **Fallback**: a generated sRGB block gives engines without `oklch()`/`color-mix()` equivalent hex values.
- **Type and space**: system font stack (`system-ui, -apple-system, "Segoe UI", Roboto, ...`) and a monospace stack; sizes 12/13/14/16/20/28 px; 4 px spacing base; radii 4-12 px.

During the redesign, a contrast script read `tokens.css` directly and checked 342 foreground/background pairs (text and muted text on every surface, accent fills, glass cards over the worst-case background) in both themes and all six accents against WCAG AA 4.5:1. All 342 passed. The script lived in the development workspace and is not part of this repository; re-run an equivalent check when you change tokens.

### Preferences (`theme.js`)

| Attribute on `<html>` | Values | Default |
|---|---|---|
| `data-theme` | `light`, `dark` | absent = follow `prefers-color-scheme` |
| `data-accent` | `indigo`, `blue`, `teal`, `emerald`, `amber`, `rose` | `indigo` |
| `data-surface` | `glass`, `solid` | `glass` when `backdrop-filter` is supported and `prefers-reduced-transparency` is not `reduce`; otherwise `solid` |
| `data-density` | `comfortable`, `compact` | `comfortable` |
| `data-motion` | `full`, `reduced` | absent = follow `prefers-reduced-motion` |
| `data-sidebar` | `expanded`, `collapsed` | automatic by width |

Preferences are stored in `localStorage` under `io.*` keys. They hold display choices only, never tokens, identities or workspace data. Theme, accent and surface changes cross-fade with the View Transitions API when the browser supports it and motion is full. A `io:preferences` event is dispatched on every change.

### Glass mode

`data-surface="glass"` paints a slow ambient background on `body::before` and, inside `@supports (backdrop-filter: blur(1px))`, gives the sidebar, topbar, toasts, command palette, popovers and the detail dialog a translucent surface with `blur(20px) saturate(160%)` (24 px for the dialog). Solid mode uses opaque surfaces with identical layout.

### Motion and reduced motion

Duration tokens `--dur-1` to `--dur-5` are 80, 140, 220, 320 and 480 ms, with `--ease-out`, `--ease-in-out`, `--ease-in` and `--ease-spring` curves, an 8 px entrance distance and a 20 ms stagger step. `motion.js` adds:

- a view entrance on tab change;
- a KPI count-up, only when a value changes between renders (never on first paint, so exact text is stable), at most 600 ms;
- a sliding active indicator on the Cards/List/Swipe switcher;
- relative times (`<time data-relative>`) refreshed every 30 s while the page is visible.

Charts animate only the first time each chart is drawn; live re-renders update without replaying the entrance. With `data-motion="reduced"` or the OS setting (unless the user chose full motion), every duration token becomes 0, distances and scales become neutral, and `motion.js` skips its effects. Short opacity cross-fades of 80 ms or less remain.

### Charts (`charts.js`)

`window.MiniCharts` provides `sparkline`, `bar`, `stackedBar`, `donut`, `histogram`, `paired` and `funnel`, plus number formatters. Each builder returns an escaped string, takes colours from tokens (so charts follow theme and accent), and carries a text summary (`role="img"` with title/description or `aria-label`) and a visually hidden data table. One shared `role="tooltip"` element follows pointer hover and keyboard focus; arrow keys, `Home` and `End` move between points and `Esc` closes the tooltip. Values are always reachable without the tooltip.

### Command palette and keyboard map (`shortcuts.js`)

`Cmd K` / `Ctrl K` (or the topbar search field) opens a combobox palette with groups: Go to, Jump (to `#case/<id>` or `#batch/<id>`), On this page, Actions (only visible, enabled controls), Keyboard shortcuts and Preferences.

| Keys | Action |
|---|---|
| `Cmd K` / `Ctrl K` | Open or close the command palette |
| `/` | Focus case search (or open the palette) |
| `?` | Show keyboard shortcuts |
| `g o`, `g r`, `g i`, `g n`, `g d`, `g m`, `g e`, `g t` | Go to Overview, Review queue, Invoices, Insights, Delivery, Data & model, Email setup, Team access (second key within 1.2 s) |
| `j` / `k` | Focus the next / previous record row |
| `Enter` | Open the focused record |
| `x` | Select the focused case for release |
| `r` | Refresh the current view |
| `Shift T` | Cycle theme: system, light, dark |
| `Shift D` | Toggle density |
| `[` | Collapse or expand the sidebar |

Shortcuts are ignored while typing in an input, while any dialog is open, and inside the card deck, version swipe area, charts and popovers (which have their own keys).

The **Appearance panel** (topbar button) offers Theme, Accent (six swatches), Surface, Density, Motion and Sidebar, plus Reset to defaults, and states that preferences are stored in this browser only.

### Accessibility

- Skip link, landmark navigation and native `<dialog>` elements; focus returns to the trigger when the palette closes.
- Labelled form controls, visible focus indicators, a 24 px minimum target token (`--target-min`).
- Screen-reader announcements through `#sr-status` for view loads and live updates; error toasts use `role="alert"`.
- Charts expose text summaries and data tables; status uses dot plus text.
- `prefers-reduced-motion`, `prefers-reduced-transparency`, `prefers-color-scheme` and `forced-colors` are all handled.
- In-page confirmation dialogs replace the browser's `confirm()`/`prompt()`; the revoke reason must be non-empty.

## Design research (126 sites)

Before the redesign, an automated Playwright harvest visited 136 public product, pricing and documentation pages and recorded objective DOM/CSS metrics; 126 loaded successfully: 49 invoicing/AP-AR products, 46 operations dashboards and SaaS products, and 31 design systems and guideline sites. The metrics describe those public pages (largely marketing and documentation), not the vendors' logged-in applications. The harvest scripts and raw data are not part of this repository.

| Measure | Median (p25-p75) or count of 126 |
|---|---|
| Body text size | 16 px (14-18) |
| Line-height ratio | 1.5 (1.375-1.5) |
| H1 size | 58 px (48-66.6), n = 121 |
| Primary button height | 40 px (34-48), n = 112 |
| Header height | 72 px (64-100), n = 103 |
| Maximum content width | 1316 px (1175-1440), n = 86 |
| Body text contrast | 12.66:1 (6.74-18.34); 15 sites below AA |
| `prefers-reduced-motion` rules | 86 sites |
| `:focus-visible` styles | 91 sites |
| `<main>` landmark | 97 sites |
| Skip link | 43 sites |
| Sticky header / blurred header | 59 / 9 sites |
| Cmd-K pattern visible | 5 sites |
| Sites with targets under 24 px | 84 sites |
| Transfer size | 752 KB (250-1311) |
| JavaScript files | 15.5 (6-35) |
| `domInteractive` | 1167.5 ms (686-3131) |
| Most common primary hue | neutral black/grey 45, blue 32 |
| Most common first font | Inter 29, Arial 14, `-apple-system` 11 |

How it informed the console: system fonts instead of a downloaded Inter (no remote assets); a denser scale than marketing pages for an all-day work tool (14 px body, 32 px controls, `--main-max: 1440px` at the upper quartile of content width); reduced-motion, focus-visible, skip link and a 24 px target minimum as defaults rather than exceptions; a Cmd-K palette and keyboard map, which few of the measured pages offered; and a neutral-plus-single-accent palette with indigo as the default accent.

## Familiar interaction patterns

Official product references inspected on 27 October 2026:

- [Stripe: Use the Dashboard](https://docs.stripe.com/invoicing/dashboard): invoice list, document details, review before sending and an explicit recipient/message inspection step. The documentation includes an invoice-details screenshot and editor recording.
- [Xero: Sales overview](https://central.xero.com/s/article/Sales-dashboard): a concise overview, status summaries and direct navigation from a count to its underlying records.
- [Intuit: Getting Around QuickBooks Online, page 9](https://www.intuit.com/oidam/intuit/ic/en_ca/content/Intuit-education-program-ca-qbo-ch2-getting-around-quickbooks-online.pdf): the official invoice-list screenshot was inspected. It uses left navigation, summary indicators, filters and a compact invoice table.

These references inform navigation and information hierarchy. Their screenshots, branding and exact layouts are not embedded or copied into the application. The implementation uses original HTML/CSS. The cited Intuit guide illustrates a familiar pattern; it is not evidence of the current product release.

## Views and decisions

| View | First question answered | Available work |
| --- | --- | --- |
| Overview | What should I do next? | One suggested action, four KPI cards, compact workflow counts, latest five cases and administrator automation controls |
| Review queue | Why is this movement held? | Cards (default) / List / Swipe, customer/status filters, retained amounts and risk score, exact version, original source files, individual decision or bounded bulk release, append-only correction, reviewed CSV worklist import, paginated identity/evidence history |
| Invoices | What am I approving? | Cards/List/Swipe display, customer filter, customer/period close, on-demand exact-PDF page preview, immutable document/message and source provenance, required approval checkbox, server dispatch policy and optional immediate queue request |
| Insights | Which patterns deserve a closer look this period? | Period and customer filters, overview KPIs and state mix, supplier concentration (HHI), duplicate candidates, amount outliers, Benford, 12-period trend, risk histogram, review ageing and data quality, all from one read-only snapshot ([INSIGHTS.md](INSIGHTS.md)) |
| Delivery | What actually happened? | Customer filter, original source references, separate provider submission and delivery evidence, paginated outbox, simulated provider/events |
| Data & model | Can I verify this result? | Customer-filtered source batches, original-file download, upload, verification and explicit replacement, queued/running/failed jobs, scheduler outcomes, model metadata and audit records |
| Email setup | Which Gmail account is configured, and what will connecting do? | Three-step OAuth setup, exact callback URI, local JSON upload, explicit Google consent continuation, connection status and confirmed local disconnect |
| Team access (administrators) | Who can make attributable decisions? | Bounded personal-account list, create reviewer/admin accounts and explicitly deactivate/reactivate another account |

Unknown values render as unavailable, unknown or an em dash; missing scores never become zero. Scores support review and do not establish transaction truth. Money is formatted directly from decimal strings. Deterministic failures cannot be cleared by accepting a model concern. Corrections of source amounts are disabled for absent/ambiguous source objects, and the API independently enforces the rule.

Review cards show the customer, external movement reference, case version, recorded amount, separate source A/B amounts and currencies, recorded Logistic Regression score, model ID, retained reason codes and source-file links. The case's version supplies these values even when a newer movement version exists. The initial list can expose a threshold from a matching persisted mathematical receipt. Cards and Swipe then request one bounded driver batch for the displayed page: the server verifies the specifically recorded model artifact, policy, feature schema, score and flag, and reconstructs its additive explanation. Legacy cases without a receipt can therefore display the verified frozen-model threshold without rewriting their assessment. A contradictory receipt makes the explanation unavailable. No active-model score or threshold is substituted.

Cards and List share one paginated response (50 rows in the console, API maximum 100). Changing layout reuses the same list and cached driver batch, and retains independent inspection and eligible-release selections. A page first opened in List requests its driver batch only when Cards or Swipe is selected. Card construction does not request separate detail/history endpoints for every card; model verification is batched on the server, once per recorded model version per batch. Inspect opens at most eight cases in the workbench; release remains a separate personal-account action with explicit evidence and live version checks. Filtering or paging clears selections; pending updates retain the existing stale-review safeguards.

The Cards layout displays invoice package metadata rather than PDF thumbnails. In Swipe, the active card automatically displays a page rendered from the exact retained PDF in place of the schematic paper. It binds the preview directly to the SHA-256 in the frozen list row, so no full package/document GET is needed. Previous/Next controls load one page at a time, and Enlarge PDF opens the existing detailed viewer. Only the front card mounts an image; the two inert underlying cards contain placeholders. Changing cards, layout, filter, session or hiding the tab aborts in-flight requests and revokes the image object URL. Count-only and stale-event updates preserve the current card, image and page without another preview request. Missing PDFs, mismatched hashes and invalid image responses show explicit retry/download guidance. Switching between Cards and List reuses the same bounded list without another request. An explicit Preview action loads metadata and a single PNG page rendered from the retained PDF, avoiding dependence on native browser PDF plug-ins. The UI requires the metadata hash to match the inspected package and validates each page response's PDF hash, one-based page number and PNG signature. Page controls retain the same frozen hash and show the exact page count. The server caps the largest image dimension at 1,600 pixels; the browser checks a 16 MiB response cap and decoded dimensions before display. It retains one image object URL and revokes it on page navigation, close/hide, dialog changes, hidden tab or logout; outstanding downloads are aborted. No full PDF is downloaded for embedded previews. The Cards layout and inactive Swipe cards make no PDF page requests. Missing renderers, busy responses, stale hashes and invalid page counts produce explicit retry/download guidance. The original exact PDF remains independently downloadable. Image blobs are allowed by the page CSP; object embeds remain disabled, and frames receive no blob exception. Approvals bind to the fingerprint returned with the inspected package. A checked box alone is never authority: the server enforces package version, approval and issuance gates. Original observations remain visible as evidence. No automatically checked approvals or automatic user decisions exist.

The dashboard supplies `auto_dispatch_after_approval`. When enabled, the worker automatically queues an approved package; the approval screen states this before the user approves. “Queue now” is an optional immediate request. The browser never dispatches as a side effect of posting an approval. If the policy is disabled, the screen explains that an explicit queue request is needed.

Review worklists can be exported, edited in Excel and re-imported as CSV. Only non-empty proposed correction fields are applied; `current_*` columns remain evidence. The browser previews at most the first 16 KiB and sends the original file with native multipart upload. It does not parse or load the whole worklist. The import cap is 5 MiB and 1,000 rows, enforced by the server before application. A separate unchecked confirmation requires the reviewer to inspect changes and reasons. Per-row results display 50 at a time; server version checks reject stale writes. This worklist cannot approve invoices. Exported `#case/{id}` links open the selected case after sign-in. Source-change-after-issue cases show recent observations separately from the frozen snapshot evidence.

Source intake sends a native multipart file reference without parsing the data in JavaScript. Browser and server enforce a 250 MiB upload cap. The operator explicitly supplies source, period, expected data rows and, for XLSX, a worksheet name. Complete-period delivery is an unchecked declaration by default; the server verifies coverage independently. Batch inspection exposes observed/expected/duplicate/quarantined counts and hashes. Replacement registration requires the exact replacement ID, an evidence reason and an explicit checkbox; the server checks retained coverage and source revisions before recording supersession.

An unknown send attempt exposes “Check provider evidence”. It reconciles the existing attempt through `/api/attempts/{id}/reconcile`, never submits the message again. Unavailable or inconclusive provider evidence leaves the attempt unknown.

Processing checks show internal jobs queued, running and failed, plus the latest stored monthly scheduler report and approved-dispatch report. Missing values explicitly remain unavailable. These counts and retained report timestamps are not represented as a worker heartbeat; a timestamp can remain unchanged when the scheduler outcome is unchanged.

## Swipe worklists and model parameter bars

Review Queue and Invoices expose a third **Swipe** display. A card follows horizontal pointer movement with lift, rotation and directional drop hints. Releasing beyond the threshold requests the action; short drags, vertical scrolling, pointer cancellation and Escape snap back. Inputs, links and model tooltips do not start a drag. Buttons provide the same actions, and focused-stage arrow keys provide keyboard access. Reduced-motion preferences disable decorative motion. At most three card layers exist in the DOM; underlying cards are inert. No animation library or remote asset is required.

- **Right / Review & confirm:** opens the exact case version or frozen invoice package, with complete evidence. No decision is recorded until a personal reviewer supplies a reason, checks an initially unchecked confirmation and submits. An invoice shows its amounts, recipients, message and exact PDF preview; approval states the server dispatch policy.
- **Left / Later:** an authenticated personal reviewer postpones that exact case version or invoice fingerprint for 24 hours. The action and stable reviewer identity are audited. Shared read-only access moves the card only within the current local worklist. Later never releases a movement, rejects a package or suspends an already authorised invoice dispatch.
- **Reject:** opens a separate evidenced confirmation. A case records `CONFIRMED_ISSUE`, remains open for remediation and keeps its movement blocked. Quarantined rows need corrected source intake and cannot use this decision. An unissued invoice becomes `REJECTED`, loses any approval and holds its billing scope. An explicit **Reopen rejected draft** action rechecks source coverage, frozen membership and PDF integrity before restoring DRAFT; it does not approve or send the invoice.

Pending, Later, Confirmed and Rejected counters describe the loaded page and current worklist, not all customers or lifetime decisions. The Later tab exposes deferred cards and a Return to queue action. Layout switches preserve the same page's state; filters, pagination, explicit refresh and session changes create a new snapshot. Personal postponements are projected from one bounded SQL lookup; expired or mismatched bindings are ignored. A stream update marks the worklist stale and retains the visible evidence and unsaved reason. The server independently checks versions, fingerprints and permissions. Submitted commands are never cancelled merely because the UI moved: replies update only their originating session/worklist, with a matching audited actor and exact decision receipt. Idempotent retries reuse the same request ID for the same payload.

Each scored transaction includes a **Logistic regression · parameters** box with all six features, their exact retained raw inputs where verifiable, signed direction and animated bars. Hover, keyboard focus or tap reveals the percentage, transformed input, signed log-odds contribution and transformation. Orange means increasing the score; teal means decreasing it. The width is `100 × abs(feature contribution) / sum(abs(all feature contributions))`; if every contribution is zero, all bars are zero. This is a relative contribution share, **not a separate risk probability**, a causal effect or a calibrated real-world fraud probability. The intercept is outside this denominator. Contributions remain tied to the recorded synthetic model and case version. Missing scores, inconsistent artifacts/receipts or unbound raw inputs are explicitly unavailable; they are not filled with zero or current-model predictions.

`GET /api/case-drivers` accepts 1–50 unique case IDs and performs one read-only SQL join. Each distinct frozen artifact is verified once per request; no assessment is persisted or rescored. Browser batch responses must match the page request, case version, assessment score and model before being displayed. Money remains decimal strings, including values beyond JavaScript's safe integer range.

Interaction references consulted for this implementation: [W3C dragging alternatives](https://www.w3.org/WAI/WCAG22/Understanding/dragging-movements.html), [W3C pointer cancellation](https://www.w3.org/WAI/WCAG22/Understanding/pointer-cancellation.html), [MDN Pointer Events](https://developer.mozilla.org/en-US/docs/Web/API/Pointer_events), and [Motion drag examples](https://motion.dev/docs/drag). These informed the mechanics; implementation uses native Pointer Events and CSS.

## A focused overview and automation controls

Overview prioritises the next review or approval action, four operational counts and a short workflow strip. Secondary verification counts remain under More verification details. The automation panel separates configuration from actual worker-process health; enabled configuration does not establish that a worker is running.

Manage automation loads the current server configuration and revision. Personal administrators may edit the master pause, monthly draft preparation, dispatch of already-approved packages and simulated email-processing switches. Numeric throughput/admission limits are under a collapsed Advanced section. Saving requires an operational reason, the inspected revision, the current personal session, CSRF and an idempotent request ID. An identical retry reuses that identity. Reviewer and shared sessions can inspect settings but cannot save them. Incoming events never replace an open settings form.

Pausing stops new work claims. It does not cancel work already in progress or disable read access/intake; source admission still obeys the queue limit. The controls do not approve invoices, bypass source/model checks, or switch the simulation to real email delivery. Actual worker health comes from the dashboard heartbeat projection.

## Customer filters and source-file evidence

Review, Invoices, Delivery and Data each retain their own customer filter. Changing a filter resets pagination and clears Review selection. Case CSV exports carry both customer and status; **All statuses** explicitly sends `status=` because the export endpoint's default is Open. Delivery CSV exports carry the selected customer. Delivery summary counts and Data processing/model/audit sections are labelled as workspace-wide so they are not confused with the filtered lists.

Source filenames are visible in tabular lists and details, and are accessible under Original source files on each invoice card, with authenticated same-origin API download links. The Data filter selects batches relevant to the chosen customer; downloading a batch returns the whole retained original file, which may contain multiple customers. It is not a filtered extract. A separate `#batch/{id}` link opens exact metadata in another tab and fetches the batch independently of the current page cache. Internal hashes are constrained visually, and Review prefers the external business movement identifier while preserving the full internal ID in its tooltip and detail evidence. Desktop Review and bulk tables keep their source columns within the available width; narrow screens scroll within the table rather than widening the page.

Invoice provenance comes from frozen invoice membership. Lists use compact server projections; an explicit source-list action requests at most 50 file references per page. The UI displays the total file count, pagination and incomplete/missing-reference warnings. Source loading inside an open review or approval dialog replaces only its provenance region, preserving form inputs. Original snapshot evidence remains separate from latest observations on source-change-after-issue cases.

## Personal identity and review history

The sign-in screen offers a personal username/password and a clearly labelled shared **read-only access** mode. When `/api/auth/status` reports no operators, an empty first-account form requests the setup token, username, display name and a new password with confirmation. It never supplies or invents a person's identity. Password fields are cleared after successful sign-in; no credentials or session tokens are stored in localStorage/sessionStorage.

The header always displays the signed-in name, username and role, or **Shared access · read-only / Individual reviewer unknown**. Its profile dialog exposes the stable actor ID and a sign-out action on desktop and mobile. The shared-access banner links to personal sign-in/setup. Every mutation form displays a read-only **Recorded as** identity; the client never accepts a freely editable actor label. Shared access disables review decisions, corrections, selection/bulk release, invoice preparation/approval/dispatch, intake, provider controls and Gmail mutations. Downloads, filters, source inspection and history remain readable. Pure hypothetical model previews are also available to authenticated read-only sessions with CSRF; they do not save decisions or inputs. The API independently enforces permissions; disabled controls are not an authorisation boundary.

Administrators alone see Team access. It lists at most 50 accounts per page, supports explicit account creation and confirmed deactivation/reactivation, and omits self-deactivation. Server checks remain authoritative for active identity, role, last-administrator protection and concurrency. There is no automatic creation of a named account on the live demonstration database.

Each case displays **Last review action by** and its UTC timestamp. The history section loads only when expanded, with 20 records per stream per request. Structured review events, historical legacy records and rejected single-command attempts have independent cursors. Only the displayed page is retained in the DOM; cursor stacks do not accumulate event payloads. History responses are bound to their case and dialog generation, so an old response cannot overwrite a newer case or its unsaved notes.

Structured events show the recorded reviewer name/username and stable actor ID, exact UTC timestamp, action, outcome, reason, reviewed version and origin. Expandable evidence includes before/after state, actual correction values, frozen version/model evidence, source-record IDs, request/parent correlation, CSV hash and logical/physical row metadata. Valid retained-CSV hashes expose an authenticated exact-file download. Explicit local actors are labelled separately from authenticated personal operators. Bulk results display the reviewer and original UTC time returned by the server, including replayed results.

Legacy records always state **Legacy shared identity — individual reviewer unknown**. Original actor labels, timestamps and stored details remain inspectable; no current user's name is retroactively assigned. Movement-level legacy records explain that exact case attribution is unavailable. Rejected attempts are shown separately because they did not change the case. Structured-history CSV exports are explicitly labelled: they contain structured events, while legacy records remain in their own history section. Review CSV exports retain server-provided last-action attribution as evidence columns.

Invoice inspection displays the package's recorded approver and approval time independently of movement review history. It does not request case history with an invoice ID. UTC rendering preserves fractional timestamp precision supplied by the API, including database strings with a space separator.

An `auth_expired` SSE event closes the stream and returns to sign-in when access expires or the server invalidates an account. It does not enter an infinite automatic reconnect loop. The UI preserves ordinary unsaved forms and selected versions during normal update events.

## Case workbench and investigation filters

The Review queue has two explicit, independent selections. **Inspect** accepts any displayed case, including quarantined records without a business movement; **Release** remains restricted to eligible model concerns. Inspect selection is capped at eight and opens only the selected cases. A pinned workbench keeps at most eight bounded case snapshots in memory. Selecting two or three pinned cases opens a read-only comparison of customer, amount, version, status, score, check reasons and source-file references.

Each open case retains its own review/correction drafts, hypothetical inputs and expected business version. Dialog tabs switch between cases; closing the dialog keeps the cases pinned. Closing a dirty case tab or explicitly refreshing its snapshot requires a discard confirmation. Navigating to another view and ordinary SSE updates preserve drafts. New-data notices do not silently change the reviewed version. Logout clears the workbench, and leaving/reloading the page warns when there are unsaved notes. No case data or passwords are written to browser storage.

Case fetches, explanations and hypothetical responses are bound to their case and request generation. Editing hypothetical inputs invalidates an older in-flight result. A pending decision prevents another decision for that case, including after switching away and back; its delayed completion cannot overwrite another case's form. These controls complement server-side optimistic concurrency and do not replace it.

Advanced filters support an external movement-ID prefix (minimum three characters, maximum 100), exact reason, severity, minimum/maximum risk and the server's five sort orders. Risk percentages convert to bounded 0–1 query values and exclude unavailable scores when supplied. Search/input changes use a 350 ms debounce; sort/select changes apply immediately. Filter changes reset pagination and both page selections. Filtered worklist exports carry the same query. The search is a prefix match, not an unbounded substring scan.

## Read-only movement version carousel

Browse versions is loaded only on explicit interaction. The endpoint returns five versions per request; the browser retains one page per pinned case, at most eight cases, and at most 20 lightweight backward cursor strings per case. It never requests all versions or all movements. A version card shows current/historical and requested-case-snapshot labels, the exact retained amount, recorded Logistic Regression score (including exact numerical p), model version, readable recorded reasons, rule evidence, changes and source-file references. Missing scores remain unavailable; a recorded zero stays zero. Bounded/truncated evidence is labelled.

Prev/next buttons, arrow keys on the focused card and deliberate horizontal pointer/touch swipes navigate read-only history. They cannot resolve, correct, release or approve anything. The decision form and notes remain bound to the pinned requested-case version even when a different historical card is visible. A server-reported newer current business version preserves the notes but disables the stale form's submit action and requires explicit case refresh. Refreshing a dirty snapshot retains the existing discard confirmation. Related cases must be opened explicitly; another case's current version is never silently selected as the decision target.

## Itemised invoices and format revisions

Prepare invoice defaults to **Itemised**, with **Summary** as an explicit alternative. Lists and package details show the format and the number of billed movements. A legacy list record without a stored count displays “Movement count unavailable”; the package calculates its exact count on demand. `line_count` means membership count, including summary invoices; it is not a page count or a count of grouped summary rows. The itemised PDF must include every billed movement. The 10,000-movement limit is explicit: the server rejects oversized requests instead of truncating them.

The package API returns financial headers separately from a 25-row movement preview. Preview pages show external movement reference, exact event time, description, supplier, net amount, VAT rate, VAT amount and gross amount, with retained movement/version identifiers expandable per row. Monetary values are rendered directly from the frozen decimal strings; the browser does not recompute tax. The invoice-wide reconciliation panel is outside the paginated rows and labels all-movement VAT/gross subtotals, the explicit rounding adjustment, and final invoice totals. The recorded `line_half_up_reconciled_v1` policy rounds each line VAT half up to two decimals and reconciles it with the retained invoice VAT. Missing historical policy or row amounts are unavailable, never substituted with zero. Recorded zero amounts remain visible as zero. Pagination fetches only the next bounded page and preserves source links and approval inputs. The browser does not eagerly render or fetch the complete movement-detail document. Horizontal table scrolling on mobile stays within the table rather than expanding the page.

A personal reviewer may explicitly request a format replacement for an unapproved draft. A historical itemised draft without a VAT policy also offers **Add VAT breakdown**, using the same version-bound replacement workflow without changing the invoice totals. Current-policy packages do not offer this upgrade. The confirmation explains that the old revision remains retained, billed movements and totals remain unchanged, and the new PDF requires a fresh approval. Commands bind to the inspected fingerprint and reuse the same request ID on an identical retry. A per-invoice pending lock rejects duplicate submissions; late movement-page and format-command responses cannot replace a newer dialog. At most 20 unconfirmed request identities are retained in memory, and they are cleared at logout. Approved or issued packages have no format-replacement action. Shared access remains read-only.

A replacement returned as `BUILDING` displays **PDF generation pending**, no approval form, and an explicit package refresh. A completed PDF is never automatically approved, and changing format never sends an email.

## Model explanation, hypothetical preview and training

Opening a case automatically fetches its **recorded frozen model** explanation once per pinned snapshot. Concurrent opens reuse the same bounded request, and prefetched but inactive cases do not trigger model explanation requests. The prominent **Why this case was flagged** panel first explains the recorded source-control, model-risk, unavailable-score or quarantine reason. It then presents the API's evidence-derived narrative, increasing drivers and expandable reducing drivers, with the frozen model/version and source-to-feature binding. It never substitutes a generic fraud claim for recorded evidence. Exact numerical calculations and the what-if form remain expandable below. The frozen model may legitimately differ from the active model shown in Data & model, which serves future assessments. A changed case version, mismatched case reason or unavailable/corrupt model evidence prevents a misleading explanation or preview and asks for an explicit refresh; no missing score becomes zero.

The explanation displays the recorded assessment/version, exact numerical model output and threshold, baseline log-odds, baseline score and model identifier. Positive/negative CSS bars encode each additive log-odds contribution. They are explicitly labelled as neither probability-percentage contributions nor causal explanations. Transformation, standardisation, coefficient and full reconstruction evidence are expandable. Raw values absent from the numerical assessment are labelled specifically as absent from that assessment. The readable narrative may use retained source inputs only when the server verifies that their transformed features match the recorded assessment. Otherwise it explains recorded transformed features and makes the provenance limitation visible.

The six-input what-if sandbox sends only entered hypothetical changes and the expected version to the pure preview API. Financial amounts remain decimal strings, ages/indicators are integers, and event time must include a timezone. The server's after-narrative is labelled **Hypothetical model explanation**, independently of the recorded narrative. Before/after output is labelled hypothetical and original data unchanged; no correction, release or approval follows automatically. The UI requires `hypothetical=true` and `persisted=false` and rejects stale responses. Shared read-only sessions may calculate previews with CSRF while actual review mutations remain disabled.

Data & model reports actual server artifact integrity/readiness and worker heartbeat status/time/age. Missing, stale and stopped worker states are distinct. Separate validation and chronological held-out synthetic test rows expose their denominators, average precision, ROC AUC and Brier score, with full metrics/splits/costs available on expansion. Synthetic performance is prominently limited to generated scenarios; it is not a claim of real-world accuracy, calibration, fraud detection or compliance.

A personal administrator can explicitly queue 1,000–50,000 synthetic training rows, a reproducible data seed and a serving period. The request returns a background run rather than fitting in the browser/request. Identical retries reuse the request ID. At most 20 recent runs show status, requester, parameters, resulting model, retained Parquet/manifest links and full results. Successful activation affects future assessments only. Existing cases and invoices are not automatically rescored or changed.

Queued, running, activating and activation-uncertain runs disable new training. For the latter two states, an administrator can inspect and explicitly confirm **Resume recorded activation**. This queues recovery of the same recorded model target, without requesting new training. Live UI verification does not start training or invoke recovery.

## Bounded bulk release

Bulk release exists only in the Review queue. Operators select eligible rows on the displayed page, at most 50. Select-all includes only rows the server marks eligible; missing eligibility or version information disables selection. Eligibility is limited to current, open model-risk concerns with passing deterministic checks, scored model evidence, no other open blocker and no issued allocation. The server rechecks these conditions when applying the request.

The review dialog lists the selected cases, frozen current versions, scores and original source files. It requires a shared evidence reason and a separate unchecked confirmation that every selected movement was reviewed. Different evidence reasons should use separate reviews. The request sends only the explicit selected IDs and their expected versions, a reason and an idempotency key. An identical retry reuses that key. This action does not approve or dispatch an invoice.

SSE updates do not replace selected rows or their versions. They show a new-data notice; **Clear selection & refresh** explicitly discards the selection before loading newer evidence. Filter and page changes also clear selection. Results distinguish cases resolved from movements actually released, show every partial failure, and retain the latest bounded result for reopening. A delayed completion cannot replace another dialog opened in the meantime.

Asynchronous list responses commit their row caches only when their request generation is current. Old selection controls and list links are disabled during loading. This prevents a late response for a previous customer from silently changing the records acted on by select-all or export. Case, invoice, batch and source dialogs have independent lifetime checks; late responses cannot overwrite a newer dialog. Inline provenance additionally checks that its target is still attached and still belongs to its own request.

## Gmail connection setup

The Email setup view, `#email`, separates account connection from delivery. A persistent notice states that connecting does not send an email or activate real dispatch. The current provider remains simulated. No send or test-email control exists in this view.

The operator enables the Gmail API, configures the consent screen and creates a Google OAuth **Web application** client. The screen exposes the exact server-supplied callback URI with a copy action and links to the [Gmail API console page](https://console.cloud.google.com/apis/library/gmail.googleapis.com) and [Google's OAuth client setup instructions](https://developers.google.com/workspace/guides/create-credentials#oauth-client-id). The downloaded client JSON is submitted through native multipart upload with a 32 KiB limit. JavaScript does not read, parse, preview or persist the file's contents. Credentials should be uploaded locally, never pasted into chat.

Consent is prepared through a CSRF-protected command and displayed as an explicit **Continue with Google** link inside a dialog. The browser accepts only HTTPS URLs whose exact hostname is `accounts.google.com`, with no embedded credentials or non-default port. It never automatically navigates when the API returns the URL. Requested permissions are Gmail send and account identity; inbox-reading access is not requested. See the [official Gmail scope definitions](https://developers.google.com/workspace/gmail/api/auth/scopes).

Google can reject embedded browsers. The instructions tell the operator to open the canonical local workspace URL in Chrome, Edge or Safari and initiate consent there. The same browser must complete the callback because the flow is bound to its local cookie. Copying only the Google consent link into another browser does not preserve this binding. Consent preparation is disabled when the current origin differs from the callback origin, including a `localhost` versus `127.0.0.1` mismatch. Clipboard fallback selects the copyable value without replacing an open form.

When the backend supplies an expected account, its input is read-only; the server independently enforces the match. Otherwise an optional expected account can be entered. Dialogs preserve unsaved values during SSE updates. Safe callback errors are rendered as text and retained across refreshes until dismissed or a connection succeeds. Local disconnect requires a separate unchecked confirmation; the UI correctly explains that removing the local connection does not revoke Google-side permissions.

The setup guide notes that External OAuth apps in Testing requesting Gmail access have refresh tokens expiring after seven days, so reconnection can be necessary. This is documented in [Google's token expiration guidance](https://developers.google.com/identity/protocols/oauth2#expiration). A visible connected state reports stored connection metadata; it is not evidence of production mail delivery.

## Low resource use and live updates

- One same-origin `EventSource` connection per visible page subscribes to `/api/events`.
- The server's shared two-second summary cache emits `event: update` with audit `revision`, `stream_version`, `changed_resources` and `summary`. The UI uses stream version for freshness, including heartbeat/late-commit changes without a new maximum audit ID. It retains audit revision separately. Only relevant active views reload; unknown or absent resource hints retain conservative refresh behavior. Initial connection does not reload the whole page.
- List requests use `limit=50&offset=N`; complete source movement tables are never loaded into the browser. Invoice previews are bounded at 25 rows, and version history at five snapshots per request. Overview loads at most five cases, audit loads ten events. The customer lookup is bounded at 1,000, above the expected approximately 200 customers.
- When a review dialog is open, incoming changes update summary badges but do not overwrite the form. A new-data notice offers a refresh; optimistic version checks reject stale writes. Closing the dialog refreshes pending changes.
- Concurrent identical GETs share one in-flight promise. Obsolete view reads and invoice-package reads are aborted; mutation requests are not cancelled. Generation guards remain authoritative for late replies. A relevant SSE change during an active list request queues one coalesced follow-up refresh. Switching away from the browser tab aborts view work, unloads the PDF and closes the event stream. Visibility restoration reconnects and refreshes the active view. Native EventSource handles reconnect backoff.
- There are no continuous animation loops, third-party charting libraries, timers that poll whole tables, browser token persistence, or copies of all source data. A small toast timeout, a 350 ms filter debounce, bounded `requestAnimationFrame` count-ups and a 30-second relative-time refresh (paused while hidden) are used. Data & model additionally refreshes only `/api/model` status every five seconds while visible, independently of audit revisions; it does not poll tables or training-run lists.
- Requests carry the session cookie; unsafe requests also carry the in-memory CSRF token returned by login or `/api/session`. Server session recovery supports normal refreshes.
- The five-second model-status refresh replaces only the readiness/heartbeat fragment, preserving expanded training runs, metrics details and modal forms. It pauses on hidden pages, tab changes and logout; generation, session and DOM checks reject late responses. Each request has an eight-second timeout, and failures explicitly show current status unavailable until a successful refresh. Run progress remains on SSE. Evaluation metrics retain an explicit captured model ID; scientific notation preserves tiny nonzero Brier scores. Exact numerical outputs and thresholds accompany rounded percentage summaries.
- Gmail metadata is fetched only when Email setup is the active view, using the same refresh mechanism. It adds no background polling, Google SDK or remote assets. Static assets are about 525 KB before compression in total, without frontend dependencies.

## Market layer (investing-platform visual language)

`static/market.css` loads last and restyles existing hooks only. It follows patterns measured on 20 investing and
finance platforms (TradingView, Kraken, Binance, Coinbase, Yahoo Finance, Google Finance and others):

- **Up / down / flat tokens.** `--up` and `--down` sit near the same lightness in light mode (about L 0.53) and are
  brighter in dark mode (about L 0.72-0.76). Colour is never the only cue: delta chips carry ▲/▼ and a real minus sign.
  A missing value stays a grey "Unavailable" or `—`, never 0 and never coloured.
- **Risk score meter.** Every recorded score shows its percentage plus a small three-band bar (low, mid, high).
  A NULL score keeps the text "Unavailable" in neutral grey.
- **Dark theme.** Three cool tonal steps about 4-5 lightness points apart, with hairline borders and no card shadows.
- **Cards and tables.** 12-16 px radius, flat, hairline dividers; tabular figures on every number; whole rows open
  their record; uppercase 12 px KPI labels with large tabular values.
- **Command palette.** A 720 px panel (never wider than the window minus 32 px) anchored at 12 % of the viewport
  height, with a 56 px input at 17 px, filter pills (All, Pages, Records, Actions, Settings; Tab cycles them),
  grouped results with icons, a second line for movement ID and amount, highlighted matches and a key legend.
  Below 640 px it stays a floating panel with an 8 px gutter. A movement ID such as `MOV-…` offers "Find movement",
  which filters the Review queue by that prefix; only 64-hex identifiers open a case or batch directly.
- **Overview.** "Your next step" with a four-step path (review, approve, automatic sending, delivery evidence), the KPI
  strip, and an Automation card: next month-end run (Europe/London), data refresh, email transport, worker state and
  the test-redirect banner "All emails are redirected to your connected address" when the server reports it active.
  The card reads `GET /api/automation/status`; if that request fails, it falls back to the dashboard summary.
- **Insights trend.** An area chart: a 2 px line coloured by the direction of the selected range, a gradient fill, a
  dotted start reference, a right-hand axis with a value pill, a dashed crosshair with a pinned readout, and a
  3M / 6M / 12M / All range control. Empty leading periods are hidden, and a change against a near-zero period reads
  "new this period" rather than a four-digit percentage.
- **Dialogs.** The approval form sticks to the bottom of the invoice dialog. From 1100 px, the case dialog shows the
  evidence on the left and keeps the review decision visible on the right.
- **Phones.** Navigation becomes a bottom tab bar.
- **Email setup.** A four-step progress strip. Administrators get "Send test email to my connected address"
  (`POST /api/mail/gmail/test`, one request ID reused until the server answers).

## Accessibility and operation

Native buttons, labelled forms, focus indicators, skip navigation, an accessible native dialog, visible loading/empty/error states, and text status labels support keyboard use. Colour is supplementary. Responsive layouts retain the same actions on narrower screens. Live rendering does not replace an open review form. The application clearly identifies synthetic simulation and distinguishes delivery from human reading.

## Local container topology

`compose.yaml` provides PostgreSQL 17, a one-off schema initializer, an API service and a separate worker. The optional `demo` profile seeds synthetic data. API and worker use the same non-root image and shared document/model/object volume. Only the API is published, on loopback port 8000. The PostgreSQL password and application access token are required environment variables, not checked-in secrets. No SMTP credentials are mounted; the provider is fixed to simulation in this compose configuration.

The Docker image includes the actual LaTeX compiler. BLAS/OpenMP thread counts are bounded to one by default so a small local machine does not oversubscribe cores. Containers rotate their logs. Resource limits and batch sizes should be sized from measured ingestion and close benchmarks; this file does not claim that a small demonstration proves production throughput.

## Validation boundary

Browser smoke checks should verify login/session recovery, all seven operational views (Overview, Review queue, Invoices, Insights, Delivery, Data & model, Email setup) and the administrator-only Team access view, form preservation during incoming updates, required package approval, CSRF propagation, pagination, uncertain scores, visibility pause and Gmail setup boundaries. API/database tests remain authoritative for accounting, replay, concurrency and approval invariants. A visually correct screen alone proves none of those invariants.

The receipts and screenshots listed below under `output/verification/` were written by local runs and are not published in this repository (they record machine-specific paths); they are cited by path only.

Recorded checks are separated by evidence scope. Earlier live receipts predate personal-account enforcement: their historical shared-token mutation checks do not describe current shared access, which is now read-only.

- `output/verification/live-ui-streaming-smoke.json`: a separate read-only browser observed the active synthetic 100,000-movement harness run `f39a9cb06ac24939b40076aa5777182d` on port 8766. During 15.081 seconds it received eight real SSE summary versions, with observed movement counts 0, 1,000, 4,000, 7,000, 10,000, 12,000, 14,000 and 17,000. The check made 29 browser requests and five direct GETs; shared sign-in was its only POST. Four real retained versions were available. Keyboard navigation displayed historical version 3 with its recorded score/model/reasons and revision-2 source files, while the decision target remained version 4 and shared access remained read-only. Desktop/mobile screenshots use the `live-ui-streaming-` prefix. This is evidence for the observed live interval; complete-workload throughput and final counts belong to the separate streaming harness receipt.
- `output/verification/live-ui-simple-smoke.json`: actual local-server verification made 17 browser requests and seven direct GETs, with shared login as the only POST. The actual production CSP permits image blobs while keeping object embeds disabled. Invoice pages 1 and 2 decoded and visibly rendered from the same retained PDF hash, one image at a time; page navigation/close revoked previous URLs. Invoice snapshots were unchanged. Desktop/mobile screenshots use the `live-ui-simple-` prefix. An earlier native PDF frame remained blank in the embedded/headless browser and was replaced by this PDF-derived image path; a successful file download alone was not accepted as preview evidence.
- `output/verification/ui-simple-workspace-fixture-smoke.json`: intercepted browser evidence for the action center, revision-bound admin automation settings, cards/list without refetching, click-only PDF-derived PNG pages, decoded image dimensions, URL revocation, renderer/stale-hash failures and disabled page boundaries, resource/stream-version refresh behavior, bounded keyboard/swipe version history and preserved review notes. Checks measure requests for these interactions; they do not establish industrial throughput or a production memory ceiling.
- `output/verification/ui-line-vat-fixture-smoke.json`: intercepted fixtures verify frozen per-row net/rate/VAT/gross values, invoice-wide subtotals and a nonzero rounding adjustment across pagination, missing values distinct from zero, explicit personal VAT upgrade with retained fingerprint/CSRF/retry ID, disabled shared/approved actions and desktop/mobile bounds. No live mutation occurs.
- `output/verification/live-ui-line-vat-smoke.json`: final live verification made 12 browser requests and 30 read-only requests, with shared login as the only POST. All 322 movement rows across the four new drafts passed independent integer-cent VAT/gross calculations and invoice-wide reconciliation, including the +GBP 0.03 adjustment. Four PDF hashes and four source-file hashes matched. Invoice snapshots and 197 open cases were unchanged. Desktop/mobile screenshots use the `live-ui-line-vat-` prefix. The fixture rerun timeout boundary is documented in `VALIDATION.md`.
- `output/verification/ui-itemized-fixture-smoke.json`: `scripts/verify_itemized_ui.cjs` intercepts every request to verify itemised-by-default preparation, summary selection, explicit format confirmation, fingerprint/CSRF/idempotent retry, pending replacement guards, stale fingerprint rejection, bounded 25-row pagination, retained source references and approval form state, late-response isolation, shared-account restrictions, no truncation above the cap and desktop/mobile layout. No live format, approval or delivery mutation occurs.
- `output/verification/live-ui-itemized-smoke.json`: final local integration used 12 browser requests and 30 direct read-only requests, with shared login as the only POST. All four replacement drafts remained unchanged; their 12, 24, 36 and 250 movement rows were read through bounded pages, checked for unique membership and exact cent sums, and matched to the retained PDF hashes. Four original source downloads matched their recorded SHA-256 values. The live 250-row preview preserved disabled approval and fitted the desktop dialog; mobile scrolling stayed inside the table. No format, approval or delivery command was issued. Screenshots: `live-ui-itemized-invoices.png`, `live-ui-itemized-movements.png`, `live-ui-itemized-mobile.png`.
- `output/verification/live-ui-narrative-smoke.json`: `scripts/verify_narrative_live.cjs` authenticates with the shared read-only account and then uses GET requests only. It verifies automatic readable reasons against the real frozen model and verified source-feature binding, separates actual amount mismatch and unavailable-model cases, and checks desktop/mobile layout. The three August invoices were generated by the existing backend pipeline from newly supplied synthetic source files; this browser check creates none. All remain `DRAFT`, their downloaded PDF hashes match the package records, both original source downloads match their retained hashes, and before/after case and invoice snapshots are identical. Screenshots use `live-ui-readable-reason*.png`, `live-ui-reason-*.png`, `live-ui-more-invoices.png` and `live-ui-new-invoice-package.png`.
- `output/verification/live-ui-workbench-smoke.json`: `scripts/verify_workbench_live.cjs` uses a fresh local browser context and permits only shared sign-in, GETs and one pure hypothetical preview. It verifies three pinned real synthetic cases, exact historical model explanations, preserved what-if inputs, comparison, advanced filters, active artifact readiness/heartbeat, disabled administrator training and desktop/mobile layout. Recorded assessments remain on `lr-2541be370ecc3747cc2b` while current readiness identifies `lr-12065a722769792dde2f`. The preview explicitly returns `persisted:false`; exact case and invoice snapshots remain unchanged, with 197 open cases and the existing invoice still `DRAFT`. A 346,795-byte `source_a.csv` download matches its retained SHA-256. No account, training job, review or invoice mutation is performed. Viewport screenshots are `live-ui-workbench-explanation.png`, `live-ui-workbench-preview.png`, `live-ui-workbench-comparison.png`, `live-ui-workbench-review.png`, `live-ui-model-operations.png` and `live-ui-workbench-mobile.png`.
- `output/verification/ui-workbench-fixture-smoke.json`: `scripts/verify_workbench_ui.cjs` intercepts every request to exercise independent inspection/release selection, quarantine cases, the eight-case cap, per-case draft preservation, comparison/discard, advanced filters/export queries, automatic active-case narratives with frozen-version caching, distinct source-rule/quarantine/unavailable-feature reasons, recorded explanations, pure previews (including shared read-only access), stale/late responses, duplicate per-case command prevention, background training/retry/recovery controls, current-versus-recorded model separation, SSE updates, heartbeat changes without audit revisions, failed or late status responses, paused hidden-page refresh, mobile layout and logout memory clearing. No actual model training or database mutation occurs in these fixtures.
- `output/verification/ui-identity-fixture-smoke.json`: `scripts/verify_identity_ui.cjs` intercepts every request to validate empty explicit bootstrap, password confirmation, personal sign-in/errors, persistent identity, role-aware navigation, administrator account creation/deactivation, review attribution, lazy independent history cursors, source CSV links, rejected attempts, SSE form preservation/session expiry and late-history response isolation. These fictional accounts and decisions exist only in the test responses, never in the live database. Desktop/mobile screenshots use the `ui-identity-*-fixture.png` prefix.
- `output/verification/live-ui-identity-smoke.json`: 17 browser requests plus six direct local GET snapshots verify the actual first-account form without submission, shared read-only sign-in, disabled case/invoice/Gmail mutations, authentic retained legacy history, draft approval attribution and mobile profile. The sole unsafe request is login. First-account setup remains required; 197 open cases and the existing `DRAFT` invoice are unchanged. Actual screenshots are `live-ui-identity-first-setup.png`, `live-ui-identity-readonly-review.png`, `live-ui-identity-legacy-history.png`, `live-ui-identity-mobile-profile.png` and `live-ui-identity-mobile-readonly.png`.

Earlier evidence:

- `output/verification/ui-fixture-smoke.json`: isolated browser fixtures validate mutation payloads, CSRF, multipart upload, explicit replacement, unknown-attempt reconciliation, null-versus-zero scores, visibility pause, approval gates and layout. These checks do not establish backend integration.
- `output/verification/live-ui-smoke.json`: a real local FastAPI/PostgreSQL session with 2,000 synthetic movements checks login, the original five workflow views, bounded pagination, session recovery, stored PDF retrieval, stale-version rejection and an actual SSE change. The observed update took 1.8 seconds in that run; this is one local observation, not a production latency guarantee. No source records were edited and the demonstration invoice remained a draft.
- `output/verification/live-ui-final-smoke.json`: after the API/worker restart, fresh-login deep links, absolute exported case links, the actual CSV import endpoint with an unchanged 50,438-byte worklist, the bounded preview, explicit confirmation and automatic dispatch policy were checked. Import applied zero corrections; all dashboard counts remained identical and the invoice remained a draft. An SSE change appeared after 2.307 seconds in this local run while preserving unsaved review text.
- `output/verification/live-ui-processing-smoke.json`: the final restarted API's queued/running/failed job counts matched their displayed values; the stored monthly report expanded correctly. This check was read-only apart from session login, and the demonstration invoice still remained a draft. `live-ui-processing.png` captures that panel.
- `output/verification/ui-gmail-fixture-smoke.json`: 37 fixture requests verify the Email setup view with a personal-account fixture, JSON size/CSRF/multipart handling without JavaScript file reads, expected-account controls, preserved forms, callback error escaping, origin checks, rejected malicious consent URLs, explicit continuation and disconnect, and desktop/mobile layout. The sole attempted Google navigation was intercepted and fulfilled by the test; no Google service was contacted. The reproducible optional Playwright runner is `scripts/verify_gmail_ui.cjs`.
- `output/verification/live-ui-email-smoke.json`: 12 browser requests against the restarted local server verify fresh-login `#email` navigation, the expected account and exact callback URI, accurately unconfigured/disconnected status, disabled Connect before configuration, the upload dialog, and desktop/mobile layout. The test permits only login and local GETs, with no Gmail mutation or external request. The existing invoice remained unchanged and `DRAFT`; no account was connected and no email sent. Screenshots are `live-ui-email-setup.png`, `live-ui-email-config.png` and `live-ui-email-mobile.png`.
- `output/verification/ui-operations-fixture-smoke.json`: the isolated `scripts/verify_operations_ui.cjs` runner covers all four filters and export URLs, explicit bounded bulk selection, frozen expected versions, CSRF, idempotent retry, partial results, source pagination, incomplete-reference warnings and preserved form state. Deferred responses reproduce old-customer/new-customer ordering and late dialog/source/bulk completion, verifying that stale data cannot replace the active selection cache or newer form. Fixtures include 64-character identifiers and assert desktop source-column visibility. All network traffic is intercepted.
- `output/verification/live-ui-operations-smoke.json`: login plus local GETs check the four filtered views, the unsubmitted bulk-review dialog, exact invoice source references, batch deep links and desktop/mobile layout. One retained `source_a.csv` download (346,795 bytes) matched its recorded SHA-256. Before/after snapshots contained the same 197 open cases and the same `DRAFT` invoice. No case was released, no invoice approved, and no external request or account mutation occurred. Readable viewport screenshots are `live-ui-operations-review.png`, `live-ui-operations-bulk-review.png`, `live-ui-operations-invoice-sources.png`, `live-ui-operations-delivery.png`, `live-ui-operations-source-batch.png` and `live-ui-operations-mobile.png`.
- `output/verification/live-ui-*.png`: screenshots of the actual seeded application, including invoice inspection, source intake, batch verification and mobile layout.
