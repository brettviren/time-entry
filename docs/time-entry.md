# time-entry — developer notes & next-month roadmap

Status: the per-day `apply` flow **works end to end** (a full month was entered
successfully). It is correct but **slow**: each day is one multi-step Workday
dialog with mandatory settle delays, so ~21 days takes a long time.

This document is the prompt for the **next phase**: replacing the one-day-at-a-time
entry with one of Workday's **bulk weekly** entry flows. It also records the
hard-won DOM knowledge so we don't rediscover it.

---

## 1. What the script does today

This uses playwright to let Python run a browser as a puppet.  To get started you will need:
```
uvx playwright install
```

The main workflow is:

1. login
2. plan
3. diff
4. apply
5. submit


`time-entry` (a `uv run` single-file script, dep: `playwright`) is a fiscal-year
project-time allocator + Workday automator. Commands:

- `login` — open a real Chromium (headful), let the user do BNL SSO + DUO, save
  `storage_state` to `time-entry-auth.json`.
- `plan [YYYY-MM]` — compute & save a Hamilton/largest-remainder allocation of
  working days to projects; store in `time-entry.json`.
- `show` / `status` — display the plan / FY-to-date totals.
- `get [YYYY-MM]` — scrape the Workday calendar (read-only) for a month.
- `diff [YYYY-MM]` — compare plan vs Workday, write `time-entry-diff-YYYY-MM.json`
  (the list of `DayChange`s = days needing entry).
- `apply [YYYY-MM] [--yes] [--inspect]` — drive Workday to enter each change.
  Dry-run unless `--yes`. `--inspect` pauses on the **first** day and dumps panel
  HTML for selector debugging. This command does not submit the timecard.
- `submit [YYYY-MM] [--yes]` — run Workday's Review → Submit flow. Dry-run
  unless `--yes`; keep this separate from `apply` so submission is explicit.
- `workflow [YYYY-MM]` — run `login`, `get`, `plan` and `diff`, then prompt
  separately before applying and submitting. Both confirmations default to no.

The top-level `--headless/--headed` option controls Chromium for `get`, `diff`,
`apply` and `submit`; headed is the default. `login` is always headed, and
`apply --inspect` rejects headless mode because it requires visual interaction.

Config: `time-entry.toml` (projects = code/pct/desc, days_off, workday URLs).
Records: `time-entry.json`. Auth: `time-entry-auth.json`.

The time-entry calendar task URL: `https://www.myworkday.com/bnl/d/task/2998$10895.htmld`

### Key entry points in the code
- `cmd_apply` → `_do_apply` (loops over changes) → `_enter_time_for_day` (the
  per-day dialog driver — this is what bulk entry would replace).
- `cmd_submit` → `_do_submit` navigates to the month, clicks Review, waits for
  the review state, and clicks Submit.
- `_DIALOG_SELECTORS` dict holds all the confirmed selectors.
- `_navigate_to_month` walks prev/next-month buttons to the target month.

---

## 2. Why it's slow (and what bulk entry would fix)

The current per-day cost = (open dialog) + (Time Type prompt round-trip) +
(Hours commit + re-render) + (OK + calendar re-stabilize). We had to insert
**explicit settle waits** because Workday's SPA re-renders mid-interaction:
- ~1.2 s after each cell click (panel must finish loading before we type).
- ~1.5 s after each OK (calendar must re-stabilize before the next cell click).
- 0.6–0.8 s after committing Hours.

These delays × 21 days dominate runtime. A **weekly bulk dialog** would cut the
number of dialogs from ~21 to ~5 (one per week) and amortize the round-trips.

---

## 3. The two bulk flows to explore (next month's task)

Both live in the calendar page's **"Actions" menu** (`data-automation-id` around
`dropDownCommandButton` / label "Actions"). Both are multi-dialog:

### A. "Enter Time by Type"
1. Dialog to **select one week** of the month.
2. A **table with per-day rows** (enter hours per day, presumably pick a time
   type per row or per table).
- Likely best when a week mixes multiple time types (our `week_schedule` already
  groups runs of codes per week — see `WeekEntry.days` = ordered `(code, count)`).

### B. "Quick Add"
1. Dialog to **select one week**.
2. Select **a single time type**.
3. Final dialog to **enter hours for each day of the week**.
- Likely best for whole-week single-project blocks (our `assign_weeks` produces
  these when a project has ≥5 remaining days).

**Decision for next month:** prototype both, measure wall-clock vs the per-day
flow, and pick one (or route per-week: Quick Add for single-type weeks, Enter
Time by Type for mixed weeks). The plan data already knows each week's shape via
`record.week_schedule` (list of `WeekEntry`, each `days` = `[(code, count), …]`).

**Unknown / to capture next month:** the exact dialog DOM for each step (week
picker, the per-day table, the per-day hours grid), the field/automation-ids, and
how each commits (Enter? OK? per-row blur?). Use the inspect methodology below.

---

## 4. Confirmed Workday DOM reference (the gold)

These are tenant- and version-sensitive (one flipped mid-session — see §5).

### Calendar page
- Day cell: `[data-automation-id="calendarDateCell-{M}-{D}"]` where **M is
  0-indexed month** (June = 5) and D is day-of-month. Cell `aria-label` carries
  the date and "Holiday"/event hints; `_extract_cell_date` parses it.
- Hours-so-far: `[data-automation-id="calendarDateHoursAccumulationLabel"]`.
- Month label: `[data-automation-id="dateRangeTitle"]`; nav buttons
  `prevMonthButton` / `nextMonthButton`.
- **Overlay gotcha:** `[data-automation-id="entriesContainer"]` is an
  absolutely-positioned overlay of calendar-event buttons (e.g. "Monthly Target
  Hours", holidays) that sit at the **top** of each cell and intercept a centered
  click. Fix: click **low in the cell** (`position y = height-6`).

### "Enter My Time" per-day panel (current single-day flow)
- Panel title: `[data-automation-id="viewStackHeaderTitle"]` = "Enter My Time".
- Loads body **asynchronously** (`wd-LoadingPanel` / `glassPanel` show first).
- Fields by `formLabel`: **Time Type**, **Hours**, **Comment**; after Hours gets
  focus the panel re-renders to add **"Charge Out Project"** (+ Comment) under
  "Details".
- **Time Type** prompt — renders **two ways** (see §5):
  - `[data-automation-id="searchBox"]` (selectinput), OR
  - `[data-automation-id="multiselectInputContainer"] input` (multiselect; input
    has **no** automation-id). Current selector matches both.
  - Results appear as `[data-automation-id="promptLeafNode"]` rows
    (`multiselectlistitem`) inside a `wd-popup`.
  - **Commit with Enter**, NOT by clicking a leaf (clicking only toggles a
    multiselect item and leaves the popup open, which then overlays Hours and
    blocks it). Enter adds a selected "card"/pill, clears the input, reveals
    Charge Out Project, and closes the popup.
  - Committed pill `aria-label` looks like `"15841 - AI/ML Intensity Frontier Res"`.
  - Popup-closed signal: `[data-automation-activepopup="true"]` goes hidden.
- **Hours**: `[data-automation-id="numericInput"]` (gwt TextBox; display label is
  `[data-automation-id="numericText"]`). **Commits only on Enter**; reverts to
  "0" and errors on any focus loss. Read back via `input_value()` (fallback:
  `numericText`).
- **OK**: `[data-automation-id="wd-CommandButton"][title="OK"]` — commits + closes.
- **Cancel**: `[data-automation-id="wd-CommandButton_uic_cancelButton"]`.
  ⚠️ The panel's OK and a discard-confirm's OK **share `title="OK"`** — a cancel
  helper must click Cancel ONLY, never OK, or it will SAVE the entry it's trying
  to abandon (we hit this bug).

---

## 5. Hard-won quirks / lessons (don't relearn these)

1. **Playwright async has no `triple_click`** → use `click(click_count=3)`.
2. **Type the code, don't navigate the dropdown** — we know the time-type code,
   so type it and commit; the dropdown is only for humans who don't know it.
3. **Enter commits, focus-loss destroys** — both Time Type and Hours need an
   explicit Enter; clicking away or losing focus reverts/errs.
4. **Mid-interaction re-renders** force settle waits and re-locating elements
   after each step (the Hours widget is *replaced* when Charge Out Project loads).
5. **Selector drift mid-session**: Time Type flipped `searchBox` →
   `multiselectInputContainer input` between runs. Keep multi-variant selectors;
   when a wait times out on a field that's visibly present, re-capture and check
   the field's actual `data-automation-id`.
6. **Config typos cost a month-half**: time-type codes must exactly match
   Workday's official codes (a typo'd code finds no `promptLeafNode` → Time Type
   fails). Two were wrong this month: `08656→08565`, `27466→27446`.
   → **Improvement idea:** after committing Time Type, read the selected pill's
   `aria-label` and assert it contains the expected code; abort the day (and warn
   loudly) on mismatch, so typos/wrong-matches surface immediately instead of
   silently entering wrong data.
7. **Holidays** in the plan are skipped (e.g. Juneteenth) — `_compute_diff`
   puts them in `skipped`, not `changes`.

---

## 6. Diagnostic methodology that worked

`apply --yes --inspect` pauses and dumps live DOM to files next to the script:
- `workday_dialog_inspect.html` — the panel just after open (press Enter when it's
  visually fully loaded).
- `workday_dialog_dropdown.html` — after typing the code (the prompt popup).
- `workday_dialog_filled.html` — after Time Type + Hours, before OK.
- `workday_debug_YYYY_MM.html` — full page saved at end of `get`/`apply`.

Then parse with quick Python: count/locate `data-automation-id`s, list
`formLabel` texts, list `<input>`/`<button>` tags, check visibility hints. This
is exactly how we'll map the two bulk dialogs next month — **run each bulk flow
once manually with the browser open via a temporary inspect harness, capture the
DOM at each step, then script it.**

---

## 7. Suggested plan for next month

1. Add a temporary command/flag (e.g. `apply --bulk --inspect`) that opens the
   Actions menu, launches each bulk flow, and pauses to dump DOM at each dialog
   step (week picker → table/grid → confirm).
2. From the captures, record selectors for: Actions menu item, week selector,
   per-day hours cells, time-type selector (Quick Add), and the commit buttons.
3. Implement a `_enter_week_*` driver that consumes a `WeekEntry`
   (`week_start` + `days=[(code,count),…]`) and fills a whole week per dialog.
4. Route: single-type week → Quick Add; mixed-type week → Enter Time by Type
   (or whichever benchmarks faster overall).
5. Keep the per-day `_enter_time_for_day` as a fallback for partial weeks / fixups.
6. Re-validate the Time-Type-pill assertion (§5.6) in the bulk path too.

---

## 8. Files
- `time-entry` — the script.
- `time-entry.toml` — config (projects, days_off, URLs).
- `time-entry.json` — saved plans/records.
- `time-entry-auth.json` — Playwright storage_state (from `login`).
- `time-entry-diff-YYYY-MM.json` — current month's todo list (input to `apply`).
- `workday_*.html` — debug/inspect captures (safe to delete).
