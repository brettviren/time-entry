#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright"]
# ///

# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brett Viren <brett.viren@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

# ruff: noqa: DTZ005, DTZ007, DTZ011, BLE001, S110

import asyncio
import calendar
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import click


def _xdg_dir(what) -> Path:
    what = what.upper()
    cbase = Path(os.environ.get(f"XDG_{what}_HOME", "~/.config")).expanduser()
    cdir = cbase / "time-entry"
    cdir.mkdir(parents=True, exist_ok=True)
    return cdir


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Project:
    "One work-project."

    code: str
    pct: float
    desc: str

    @property
    def fraction(self) -> float:
        "The percentage as a fraction."
        return self.pct / 100.0


@dataclass
class WeekEntry:
    week_start: date
    days: list[tuple[str, int]]  # ordered runs of (code, count)


@dataclass
class MonthRecord:
    "One work-month."

    year: int
    month: int
    working_days: int
    days_off: list[date]
    allocation: dict[str, int]
    week_schedule: list[WeekEntry]


@dataclass
class Records:
    "All work-months in a work-year."

    fiscal_year: int
    months: list[MonthRecord] = field(default_factory=list)


@dataclass
class WorkdayConfig:
    "URLs for Workday."

    home_url: str = "https://www.myworkday.com/bnl/d/pex/home.htmld"
    time_entry_url: str = "https://www.myworkday.com/bnl/d/task/2998$10895.htmld"


@dataclass
class WorkdayDayEntry:
    day: date
    total_hours: float
    is_holiday: bool = False
    holiday_name: str = ""


@dataclass
class DayChange:
    day: date
    code: str
    desc: str
    target_hours: float
    current_hours: float
    action: str  # "set" (was 0) or "update" (was nonzero but wrong)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TEMPLATE_TOML = """\
# time-entry configuration
# fiscal_year is the calendar year of the FY end (Sep 30).
fiscal_year = 2026

# ISO-date strings for holidays and vacation days.
# Weekends are always excluded; listing one here is harmless.
# days_off MUST appear before any [section] headers (TOML constraint).
days_off = [
  "2025-11-27",  # Thanksgiving
  "2025-11-28",
  "2025-12-25",
  "2026-01-01",
  "2026-01-19",  # MLK Day
  "2026-02-16",  # Presidents Day
  "2026-05-25",  # Memorial Day
  "2026-07-03",  # Independence Day (observed)
  "2026-07-04",
  "2026-09-07",  # Labor Day
]

# Workday URLs — needed for 'login' and 'get' commands.
# [workday] must appear before [[projects]] (TOML table-header ordering).
[workday]
home_url      = "https://www.myworkday.com/bnl/d/pex/home.htmld"
# FIX: Give the URL
time_entry_url = "https://www.myworkday.com/bnl/d/task/XXXX$YYYYY.htmld"

# Project codes, percentage targets, and short descriptions.
# Percentages must sum to 100.
[[projects]]
code = "XXXXX"    # FIX: a project code
pct  = XX         # FIX: target percentage
desc = "XXX XXX"  # FIX: a short description

[[projects]]
code = "YYYYY"    # FIX: a project code
pct  = YY         # FIX: target percentage
desc = "YYY YYY"  # FIX: a short description

"""


def load_config(path: Path) -> "Config":
    if not path.exists():
        path.write_text(TEMPLATE_TOML)
        print(f"Created template config at {path}\nEdit it to set your projects and days off, then re-run.", file=sys.stderr)
        sys.exit(0)
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise click.ClickException(
            f"Invalid configuration in {path}: {exc}. "
            "Replace all template placeholders (such as 'XX') and try again."
        ) from exc

    try:
        projects = [
            Project(code=str(p["code"]), pct=float(p["pct"]), desc=str(p["desc"]))
            for p in raw["projects"]
        ]
        fiscal_year = int(raw["fiscal_year"])
        days_off = {date.fromisoformat(d) for d in raw.get("days_off", [])}
    except (KeyError, TypeError, ValueError) as exc:
        raise click.ClickException(
            f"Invalid configuration in {path}: {exc}. "
            "Check fiscal_year, days_off, and each project entry."
        ) from exc

    if not projects:
        raise click.ClickException(f"Invalid configuration in {path}: add at least one project.")
    total_pct = sum(p.pct for p in projects)
    if abs(total_pct - 100) > 0.01:
        raise click.ClickException(
            f"Invalid configuration in {path}: project percentages sum to "
            f"{total_pct:.1f}, expected 100."
        )
    wd_raw = raw.get("workday", {})
    workday = WorkdayConfig(
        home_url=wd_raw.get("home_url", WorkdayConfig.home_url),
        time_entry_url=wd_raw.get("time_entry_url", WorkdayConfig.time_entry_url),
    )
    return Config(fiscal_year=fiscal_year, projects=projects, days_off=days_off, workday=workday)


@dataclass
class Config:
    fiscal_year: int
    projects: list[Project]
    days_off: set[date]
    workday: WorkdayConfig = field(default_factory=WorkdayConfig)


# ---------------------------------------------------------------------------
# Records I/O
# ---------------------------------------------------------------------------

def load_records(path: Path, fiscal_year: int) -> Records:
    if not path.exists():
        return Records(fiscal_year=fiscal_year)
    with path.open() as fh:
        raw = json.load(fh)
    months = []
    for m in raw.get("months", []):
        ws = [
            WeekEntry(
                week_start=date.fromisoformat(w["week_start"]),
                days=[(e[0], e[1]) for e in w["days"]],
            )
            for w in m.get("week_schedule", [])
        ]
        months.append(MonthRecord(
            year=m["year"],
            month=m["month"],
            working_days=m["working_days"],
            days_off=[date.fromisoformat(d) for d in m.get("days_off", [])],
            allocation=m["allocation"],
            week_schedule=ws,
        ))
    return Records(fiscal_year=raw.get("fiscal_year", fiscal_year), months=months)


def save_records(path: Path, records: Records) -> None:
    data = {
        "fiscal_year": records.fiscal_year,
        "months": [
            {
                "year": m.year,
                "month": m.month,
                "working_days": m.working_days,
                "days_off": [d.isoformat() for d in m.days_off],
                "allocation": m.allocation,
                "week_schedule": [
                    {"week_start": w.week_start.isoformat(), "days": [[e[0], e[1]] for e in w.days]}
                    for w in m.week_schedule
                ],
            }
            for m in records.months
        ],
    }
    with path.open("w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]


def fy_of(year: int, month: int) -> int:
    """Fiscal year end-year for a given calendar year/month (FY ends Sep 30)."""
    return year + 1 if month >= 10 else year


def months_in_fy(fy_end_year: int) -> list[tuple[int, int]]:
    result = [(fy_end_year - 1, m) for m in range(10, 13)]
    result += [(fy_end_year, m) for m in range(1, 10)]
    return result


def get_working_days(year: int, month: int, days_off: set[date]) -> list[date]:
    _, last = calendar.monthrange(year, month)
    return [
        date(year, month, d)
        for d in range(1, last + 1)
        if date(year, month, d).weekday() < 5 and date(year, month, d) not in days_off
    ]


def get_weeks_in_month(year: int, month: int, days_off: set[date]) -> list[tuple[date, list[date]]]:
    """
    Return (monday, [in-month workday dates]) for each calendar week
    that has at least one workday in the target month.
    """
    working_set = set(get_working_days(year, month, days_off))
    _, last_day = calendar.monthrange(year, month)
    first = date(year, month, 1)
    last = date(year, month, last_day)
    monday = first - timedelta(days=first.weekday())
    weeks = []
    while monday <= last:
        wdays = [
            monday + timedelta(days=i)
            for i in range(5)
            if (monday + timedelta(days=i)) in working_set
        ]
        if wdays:
            weeks.append((monday, wdays))
        monday += timedelta(weeks=1)
    return weeks


# ---------------------------------------------------------------------------
# Allocation algorithm
# ---------------------------------------------------------------------------

def compute_allocation(year: int, month: int, config: Config, records: Records) -> dict[str, int]:
    """
    Hamilton (largest-remainder) allocation that compensates for cumulative
    drift across months within the fiscal year.
    """
    fy = fy_of(year, month)
    working = get_working_days(year, month, config.days_off)
    D = len(working)
    if D == 0:
        return {p.code: 0 for p in config.projects}

    # Accumulate past days from earlier months in the same FY
    past: dict[str, int] = {p.code: 0 for p in config.projects}
    total_past = 0
    for rec in records.months:
        if fy_of(rec.year, rec.month) != fy:
            continue
        if (rec.year, rec.month) >= (year, month):
            continue
        for code, days in rec.allocation.items():
            if code in past:
                past[code] += days
        total_past += rec.working_days

    total = total_past + D  # total working days through end of this month

    # Ideal days for each project this month
    ideal: dict[str, float] = {
        p.code: p.fraction * total - past[p.code]
        for p in config.projects
    }

    # Floor each project (clamp at 0 — cannot un-bill past months)
    floors: dict[str, int] = {code: max(0, int(v)) for code, v in ideal.items()}
    remainders: dict[str, float] = {code: ideal[code] - int(ideal[code]) for code in ideal}

    leftover = D - sum(floors.values())

    if leftover > 0:
        # Give extra days to the projects with the highest fractional remainders.
        # Tie-break: most behind target (lowest past/target ratio).
        def add_key(code: str) -> tuple:
            p = next(pr for pr in config.projects if pr.code == code)
            target_days = p.fraction * total
            ratio = past[code] / target_days if target_days > 0 else float("inf")
            return (-remainders[code], ratio)
        for code in sorted(ideal, key=add_key)[:leftover]:
            floors[code] += 1

    elif leftover < 0:
        # Over-allocated due to clamping; strip from projects with smallest remainder
        # (those most ahead of target).
        def strip_key(code: str) -> tuple:
            p = next(pr for pr in config.projects if pr.code == code)
            target_days = p.fraction * total
            ratio = past[code] / target_days if target_days > 0 else float("-inf")
            return (remainders[code], -ratio)
        strippable = [c for c in floors if floors[c] > 0]
        for code in sorted(strippable, key=strip_key)[: -leftover]:
            floors[code] -= 1

    return floors


def assign_weeks(
    allocation: dict[str, int],
    weeks: list[tuple[date, list[date]]],
    projects: list[Project],
) -> list[WeekEntry]:
    """
    Greedily fill weeks, always assigning the project with the most remaining
    days first, producing whole-week blocks when a project has enough days.
    """
    remaining = dict(allocation)
    pct_by_code = {p.code: p.pct for p in projects}
    result = []

    for monday, workdays in weeks:
        available = len(workdays)
        entries: list[tuple[str, int]] = []
        while available > 0:
            active = {c: v for c, v in remaining.items() if v > 0}
            if not active:
                break
            best = max(active, key=lambda c: (active[c], pct_by_code.get(c, 0)))
            take = min(active[best], available)
            if entries and entries[-1][0] == best:
                entries[-1] = (best, entries[-1][1] + take)
            else:
                entries.append((best, take))
            remaining[best] -= take
            available -= take
        result.append(WeekEntry(week_start=monday, days=entries))

    return result


# ---------------------------------------------------------------------------
# Display — plan/show/status
# ---------------------------------------------------------------------------

def _cell(d: date, month: int, days_off_set: set[date]) -> str:
    if d.month != month:
        return "    "
    if d in days_off_set:
        return f"[{d.day:2d}]"
    return f" {d.day:2d} "


def _ytd(
    year: int, month: int,
    record: MonthRecord,
    records: Records,
    config: Config,
) -> tuple[dict[str, int], int]:
    """Return (days_per_code, total_days) for FY through end of this month."""
    fy = fy_of(year, month)
    cutoff = (year, month)
    fy_months = set(months_in_fy(fy))

    total: dict[str, int] = {p.code: 0 for p in config.projects}
    total_days = 0

    for rec in records.months:
        if (rec.year, rec.month) not in fy_months:
            continue
        if (rec.year, rec.month) > cutoff:
            continue
        if (rec.year, rec.month) == cutoff:
            continue  # use 'record' parameter instead
        for code, days in rec.allocation.items():
            if code in total:
                total[code] += days
        total_days += rec.working_days

    # Add current month (may not yet be saved)
    for code, days in record.allocation.items():
        if code in total:
            total[code] += days
    total_days += record.working_days

    return total, total_days


def display_calendar(
    year: int,
    month: int,
    record: MonthRecord,
    config: Config,
    records: Records,
) -> None:
    fy = fy_of(year, month)
    days_off_set = set(record.days_off)
    holiday_count = sum(
        1 for d in record.days_off if d.year == year and d.month == month and d.weekday() < 5
    )
    extra = f"  ({holiday_count} holiday{'s' if holiday_count != 1 else ''})" if holiday_count else ""
    print(f"\nFY{fy}  {MONTH_NAMES[month]} {year}    {record.working_days} working days{extra}\n")

    # Calendar grid
    print(" Mo  Tu  We  Th  Fr    assignment")
    print(" --  --  --  --  --    ----------")

    we_by_monday = {we.week_start: we for we in record.week_schedule}

    _, last_day = calendar.monthrange(year, month)
    last = date(year, month, last_day)
    first = date(year, month, 1)
    monday = first - timedelta(days=first.weekday())

    while monday <= last:
        row = "".join(_cell(monday + timedelta(days=i), month, days_off_set) for i in range(5))
        we = we_by_monday.get(monday)
        if we and we.days:
            parts = []
            for code, cnt in we.days:
                desc = next((p.desc for p in config.projects if p.code == code), code)
                parts.append(f"{code} {desc}" + (f" ×{cnt}" if cnt > 1 else ""))
            annotation = ",  ".join(parts)
        else:
            annotation = ""
        print(f"{row}   {annotation}")
        monday += timedelta(weeks=1)

    # Monthly allocation summary
    D = record.working_days
    print(f"\nAllocation — {MONTH_NAMES[month]} {year}:")
    for p in sorted(config.projects, key=lambda p: -record.allocation.get(p.code, 0)):
        days = record.allocation.get(p.code, 0)
        pct = 100 * days / D if D else 0.0
        print(f"  {p.code}  {p.desc:<22s} {days:2d} d  ({pct:5.1f}%)  target {p.pct}%")

    # FY-to-date
    ytd, ytd_days = _ytd(year, month, record, records, config)
    print(f"\nFY{fy}-to-date through {MONTH_NAMES[month]}  ({ytd_days} days):")
    for p in sorted(config.projects, key=lambda p: -ytd.get(p.code, 0)):
        days = ytd.get(p.code, 0)
        pct = 100 * days / ytd_days if ytd_days else 0.0
        diff = pct - p.pct
        sign = "+" if diff >= 0 else ""
        print(f"  {p.code}  {p.desc:<22s} {days:3d} d  ({pct:5.1f}%)  target {p.pct:5.1f}%  {sign}{diff:.1f}%")
    print()


def display_status(config: Config, records: Records) -> None:
    fy = config.fiscal_year
    fy_months_set = set(months_in_fy(fy))
    total: dict[str, int] = {p.code: 0 for p in config.projects}
    total_days = 0
    months_done: list[tuple[int, int]] = []

    for rec in records.months:
        if (rec.year, rec.month) not in fy_months_set:
            continue
        for code, days in rec.allocation.items():
            if code in total:
                total[code] += days
        total_days += rec.working_days
        months_done.append((rec.year, rec.month))

    if not months_done:
        print(f"No records yet for FY{fy}.")
        return

    last_y, last_m = max(months_done)
    print(f"\nFY{fy} status through {MONTH_NAMES[last_m]} {last_y}  ({total_days} working days)\n")
    for p in config.projects:
        days = total.get(p.code, 0)
        pct = 100 * days / total_days if total_days else 0.0
        diff = pct - p.pct
        sign = "+" if diff >= 0 else ""
        print(f"  {p.code}  {p.desc:<22s} {days:3d} d  ({pct:5.1f}%)  target {p.pct:5.1f}%  {sign}{diff:.1f}%")
    print()


def display_workday_get(
    year: int,
    month: int,
    entries: list[WorkdayDayEntry],
    config: Config,
    record: MonthRecord | None,
) -> None:
    by_date = {e.day: e for e in entries}
    fy = fy_of(year, month)
    days_off_set = set(record.days_off) if record else set()
    we_by_monday = {we.week_start: we for we in record.week_schedule} if record else {}

    print(f"\nFY{fy}  {MONTH_NAMES[month]} {year}  — Workday actual\n")
    print(" Mo  Tu  We  Th  Fr    Mo  Tu  We  Th  Fr   plan")
    print(" --  --  --  --  --    --  --  --  --  --   ----")

    _, last_day = calendar.monthrange(year, month)
    first = date(year, month, 1)
    last = date(year, month, last_day)
    monday = first - timedelta(days=first.weekday())
    total_hours = 0.0
    found_days = entered_days = holiday_days = empty_days = 0

    while monday <= last:
        date_row = "".join(_cell(monday + timedelta(days=i), month, days_off_set) for i in range(5))

        # Hours row: same 5 fixed-width slots as the date row
        wd_slots = []
        for i in range(5):
            d = monday + timedelta(days=i)
            if d.month != month or d not in by_date:
                wd_slots.append("    ")
                continue
            e = by_date[d]
            found_days += 1
            if e.is_holiday:
                holiday_days += 1
                wd_slots.append("    ")   # date row already shows [DD]
            elif e.total_hours > 0:
                entered_days += 1
                total_hours += e.total_hours
                h = e.total_hours
                h_str = f"{int(h)}h" if h == int(h) else f"{h:.1f}h"
                wd_slots.append(f"{h_str:>4}")
            else:
                empty_days += 1
                wd_slots.append("    ")

        wd_row = "".join(wd_slots)

        # Plan column
        we = we_by_monday.get(monday)
        plan_col = ""
        if we and we.days:
            plan_col = ",".join(
                f"{code}" + (f"×{cnt}" if cnt > 1 else "")
                for code, cnt in we.days
            )

        print(f"{date_row}   {wd_row}   {plan_col}")
        monday += timedelta(weeks=1)

    summary_parts = [f"{entered_days} entered ({total_hours:.0f} h)"]
    if holiday_days:
        summary_parts.append(f"{holiday_days} holiday")
    if empty_days:
        summary_parts.append(f"{empty_days} empty")
    print(f"\nWorkday {MONTH_NAMES[month]}: {', '.join(summary_parts)}")
    if not entries:
        print("(no cells found — see the saved HTML to identify the right selectors)")
    if record is None:
        print("(no plan record for this month — run 'plan' to compute one)")
    print()


def _plan_by_date(record: MonthRecord, config: "Config") -> dict[date, tuple[str, str]]:
    """Reconstruct {date: (code, desc)} from a saved month record's week_schedule."""
    project_desc = {p.code: p.desc for p in config.projects}
    result: dict[date, tuple[str, str]] = {}
    days_off_set = set(record.days_off)
    for week_entry in record.week_schedule:
        # Collect in-month workdays for this week in Mon→Fri order
        workdays: list[date] = []
        d = week_entry.week_start
        for _ in range(5):
            if d.month == record.month and d not in days_off_set:
                workdays.append(d)
            d += timedelta(days=1)
        # Assign runs of codes to workdays
        idx = 0
        for code, count in week_entry.days:
            for _ in range(count):
                if idx < len(workdays):
                    result[workdays[idx]] = (code, project_desc.get(code, code))
                    idx += 1
    return result


def _compute_diff(
    entries: list[WorkdayDayEntry],
    plan: dict[date, tuple[str, str]],
) -> tuple[list[DayChange], list[date], list[date]]:
    """
    Compare Workday entries against plan.

    Returns (changes, matched, skipped).
    - changes: days that need action (set or update)
    - matched: plan days already at 8h in Workday
    - skipped: holidays not in plan
    """
    wd_hours: dict[date, float] = {e.day: e.total_hours for e in entries}
    wd_holidays: set[date] = {e.day for e in entries if e.is_holiday}

    changes: list[DayChange] = []
    matched: list[date] = []
    skipped: list[date] = []

    for d in sorted(plan):
        if d in wd_holidays:
            skipped.append(d)
            continue
        code, desc = plan[d]
        current = wd_hours.get(d, 0.0)
        if current == 8.0:
            matched.append(d)
        else:
            action = "set" if current == 0.0 else "update"
            changes.append(DayChange(d, code, desc, 8.0, current, action))

    return changes, matched, skipped


def display_diff(
    year: int,
    month: int,
    changes: list[DayChange],
    matched: list[date],
    skipped: list[date],
    diff_path: Path,
) -> None:
    name = MONTH_NAMES[month]
    total_plan = len(changes) + len(matched)
    print(f"\nDiff  {name} {year}   plan={total_plan}d  done={len(matched)}d  "
          f"todo={len(changes)}d  skipped={len(skipped)}d\n")

    if not changes:
        print("  Nothing to do — all planned days already entered.")
    else:
        # Group changes by week (Mon–Sun)
        def week_start(d: date) -> date:
            return d - timedelta(days=d.weekday())

        weeks: dict[date, list[DayChange]] = {}
        for ch in changes:
            ws = week_start(ch.day)
            weeks.setdefault(ws, []).append(ch)

        DAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        for ws in sorted(weeks):
            print(f"  Week of {ws.strftime('%b %d')}:")
            for ch in sorted(weeks[ws], key=lambda x: x.day):
                abbr = DAY_ABBR[ch.day.weekday()]
                tag = f"{ch.action:6s}"
                cur = f"(was {ch.current_hours:.0f}h)" if ch.action == "update" else ""
                print(f"    {abbr} {ch.day.strftime('%b %d')}  {tag}  "
                      f"{ch.code} {ch.desc:<12}  {ch.target_hours:.0f}h  {cur}")
        print()

    # Write JSON
    payload = {
        "year": year,
        "month": month,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "changes": [
            {
                "date": str(ch.day),
                "code": ch.code,
                "desc": ch.desc,
                "target_hours": ch.target_hours,
                "current_hours": ch.current_hours,
                "action": ch.action,
            }
            for ch in changes
        ],
        "matched": [str(d) for d in matched],
        "skipped": [str(d) for d in skipped],
    }
    diff_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Diff saved → {diff_path}")


# ---------------------------------------------------------------------------
# Workday async helpers
# ---------------------------------------------------------------------------

async def _try_selectors_text(page, selectors: list[str]) -> str:
    for sel in selectors:
        loc = page.locator(sel)
        if await loc.count() > 0:
            try:
                return (await loc.first.inner_text()).strip()
            except Exception:
                pass
    return ""


async def _first_visible(page, selectors: list[str]):
    for sel in selectors:
        loc = page.locator(sel)
        if await loc.count() > 0:
            return loc.first
    return None


def _parse_period_label(text: str) -> tuple[int, int] | None:
    """Try to extract (year, month) from a Workday period label string."""
    if not text:
        return None
    # "May 2026", "JANUARY 2026"
    for fmt in ("%B %Y", "%b %Y"):
        try:
            d = datetime.strptime(text.strip(), fmt)
            return d.year, d.month
        except ValueError:
            pass
    # "05/2026", "2026-05"
    for fmt in ("%m/%Y", "%Y-%m"):
        try:
            d = datetime.strptime(text.strip(), fmt)
            return d.year, d.month
        except ValueError:
            pass
    # "Week of May 11, 2026" or "May 11 – May 15, 2026" — grab any parseable date
    m = re.search(r'([A-Za-z]+ \d{1,2},? \d{4})', text)
    if m:
        for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y"):
            try:
                d = datetime.strptime(m.group(1), fmt)
                return d.year, d.month
            except ValueError:
                pass
    return None


async def _extract_cell_date(cell) -> date | None:
    """
    Extract the date from a BNL Workday calendar cell.
    aria-label format: "[Holiday ]DayName, Month DD, YYYY | ..."
    e.g. "Friday, May 1, 2026 | 2 events | Hours: 8"
         "Holiday Monday, May 25, 2026 | 2 events | Memorial Day | Hours: 8"
    """
    aria = await cell.get_attribute("aria-label") or ""
    m = re.search(
        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)'
        r',\s+([A-Za-z]+ \d{1,2}, \d{4})',
        aria,
    )
    if m:
        try:
            return datetime.strptime(m.group(1), "%B %d, %Y").date()
        except ValueError:
            pass
    return None


async def _scrape_calendar(page, year: int, month: int) -> list[WorkdayDayEntry]:
    """
    Read all day cells for the target month using confirmed BNL Workday selectors.
    Hours come from [data-automation-id="calendarDateHoursAccumulationLabel"].
    Calendar event details (project label, approval status) are loaded dynamically
    and absent in a static snapshot; a future enhancement can click each cell.
    """
    cells = page.locator('[data-automation-id^="calendarDateCell-"]')
    count = await cells.count()
    if count == 0:
        print("[warn] No calendarDateCell- elements found — auth may have expired or page changed")
        return []
    print(f"[info] Found {count} calendarDateCell- elements")

    entries: list[WorkdayDayEntry] = []
    for i in range(count):
        cell = cells.nth(i)
        cell_date = await _extract_cell_date(cell)
        if cell_date is None or cell_date.year != year or cell_date.month != month:
            continue

        aria = await cell.get_attribute("aria-label") or ""
        is_holiday = aria.startswith("Holiday ")
        holiday_name = ""
        if is_holiday:
            hm = re.search(r'\| \d+ events \| (.+?) \| Hours:', aria)
            if hm:
                holiday_name = hm.group(1).strip()

        # Total hours from the accumulation label, with aria-label fallback
        total_hours = 0.0
        acc = cell.locator('[data-automation-id="calendarDateHoursAccumulationLabel"]')
        if await acc.count() > 0:
            acc_text = await acc.first.inner_text()
            hm = re.search(r'Hours:\s*(\d+(?:\.\d+)?)', acc_text)
            if hm:
                total_hours = float(hm.group(1))
        else:
            hm = re.search(r'Hours:\s*(\d+(?:\.\d+)?)', aria)
            if hm:
                total_hours = float(hm.group(1))

        entries.append(WorkdayDayEntry(
            day=cell_date,
            total_hours=total_hours,
            is_holiday=is_holiday,
            holiday_name=holiday_name,
        ))

    entries.sort(key=lambda e: e.day)
    return entries


async def _navigate_to_month(page, year: int, month: int) -> None:
    LABEL_SELECTORS = [
        '[data-automation-id="dateRangeTitle"]',   # confirmed: <h2> "May 2026"
        '[data-automation-id="calendarTitle"]',
        '[data-automation-id="periodLabel"]',
    ]
    PREV_SELECTORS = [
        '[data-automation-id="prevMonthButton"]',  # confirmed
        'button[aria-label="Previous Month"]',
        '[data-automation-id="previousPeriod"]',
    ]
    NEXT_SELECTORS = [
        '[data-automation-id="nextMonthButton"]',  # confirmed
        'button[aria-label="Next Month"]',
        '[data-automation-id="nextPeriod"]',
    ]

    for click_count in range(24):
        label_text = await _try_selectors_text(page, LABEL_SELECTORS)
        current = _parse_period_label(label_text)
        if current is None:
            if click_count == 0:
                print(f"[info] Cannot parse month label {label_text!r} — assuming correct month")
            return
        if current == (year, month):
            return
        direction = NEXT_SELECTORS if (current[0] * 12 + current[1]) < (year * 12 + month) else PREV_SELECTORS
        btn = await _first_visible(page, direction)
        if btn is None:
            print("[warn] No prev/next month button found — cannot navigate to target month")
            return
        await btn.click()
        await page.wait_for_load_state("networkidle")

    print(f"[warn] Stopped navigating after 24 clicks — may not be on {MONTH_NAMES[month]} {year}")


async def _do_login(home_url: str, auth_state_path: Path) -> None:
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=False)
        except Exception as e:
            print(f"Could not launch Chromium: {e}", file=sys.stderr)
            print("Install the matching browser:  time-entry install-browser", file=sys.stderr)
            return
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(home_url)
        print(f"\nBrowser opened at {home_url}")
        print("Complete the BNL SSO login and DUO 2FA in the browser window.")
        print("When you are on the Workday home page, press Enter here to save your session: ", end="", flush=True)
        # input() is blocking; fine here since no other coroutines are running
        input()
        await context.storage_state(path=str(auth_state_path))
        print(f"Auth state saved → {auth_state_path}")
        await context.close()
        await browser.close()


async def _do_get(
    time_entry_url: str,
    auth_state_path: Path,
    year: int,
    month: int,
    debug_html_path: Path,
    headless: bool = False,
) -> list[WorkdayDayEntry]:
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=headless)
        except Exception as e:
            print(f"Could not launch Chromium: {e}", file=sys.stderr)
            print("Install the matching browser:  time-entry install-browser", file=sys.stderr)
            return []
        context = await browser.new_context(storage_state=str(auth_state_path))
        page = await context.new_page()
        try:
            print(f"Navigating to {time_entry_url} ...")
            await page.goto(time_entry_url)
            await page.wait_for_load_state("networkidle")
            # Detect session expiry: if we ended up on a non-Workday page
            if "myworkday.com" not in page.url:
                print(f"[warn] Ended up at {page.url} — session may have expired")
                print("Delete the auth-state file and run 'login' again.")
            else:
                await _navigate_to_month(page, year, month)
            entries = await _scrape_calendar(page, year, month)
            return entries
        finally:
            html = await page.content()
            debug_html_path.write_text(html, encoding="utf-8")
            print(f"Page HTML saved → {debug_html_path}")
            await context.close()
            await browser.close()


# Selectors for the time-entry dialog.  Confirmed via DOM inspection; adjust
# if your Workday tenant uses different data-automation-id values.
_DIALOG_SELECTORS = {
    # The "Enter My Time" slide-in panel that opens when you click a day cell.
    "dialog":          '[data-automation-id="promptDialog"], [role="dialog"]',
    # Hours field (a gwt TextBox).  Confirmed: formLabel "Hours".
    "hours_input":     '[data-automation-id="numericInput"]',
    # "Time Type" prompt (formLabel "Time Type").  Workday renders this prompt
    # two ways across tenants/versions: as a selectinput with id "searchBox", or
    # as a multiselect whose typeable <input> lives in "multiselectInputContainer"
    # and carries NO data-automation-id.  Match the multiselect input first (the
    # current rendering), then fall back to searchBox.  Click it and type the
    # code; you cannot .fill() it.
    "project_input":   '[data-automation-id="multiselectInputContainer"] input, '
                       '[data-automation-id="searchBox"]',
    # Option rows that appear in the prompt once you type.
    "suggestion_item": '[data-automation-id="promptOption"]',
    # "OK" button — commits the day's entry and closes the panel.
    "ok_button":       '[data-automation-id="wd-CommandButton"][title="OK"]',
    # "Cancel" button — used to close the panel if an entry fails, so the open
    # modal does not block (and time out) the next day's cell click.
    "cancel_button":   '[data-automation-id="wd-CommandButton_uic_cancelButton"], '
                       '[data-automation-id="wd-CommandButton"][title="Cancel"]',
    # Top-level actions.  Keep Save and Submit strictly separate: apply may
    # save entered days, but only the explicit submit command may submit them.
    "save_button":     'button[data-automation-id="save"]:visible, '
                       'button[title="Save"]:visible, button:text-is("Save"):visible',
    "review_button":   'button[data-automation-id="review"]:visible, '
                       'button[title="Review"]:visible, button:text-is("Review"):visible',
    "submit_button":   'button[data-automation-id="submit"]:visible, '
                       'button[title="Submit"]:visible, button:text-is("Submit"):visible',
}


async def _enter_time_for_day(page, change: DayChange, inspect: bool) -> bool:
    """
    Click a calendar cell and fill in hours + project.
    Returns True on success.
    `inspect` dumps the dialog HTML on the first interaction for selector debugging.
    """
    month_idx = change.day.month - 1  # Workday uses 0-indexed month in cell IDs
    cell_sel = f'[data-automation-id="calendarDateCell-{month_idx}-{change.day.day}"]'
    cell = page.locator(cell_sel)
    if await cell.count() == 0:
        print(f"  [warn] Cell not found for {change.day}: {cell_sel}")
        return False

    # Workday overlays an absolutely-positioned "entriesContainer" of calendar
    # event buttons (e.g. "Monthly Target Hours", holidays) on top of the grid.
    # These sit at the TOP of each day cell, so a centered click is intercepted
    # ("subtree intercepts pointer events" -> click timeout).  Click low in the
    # cell, below any stacked entries, where the surface is unobstructed.
    try:
        box = await cell.bounding_box()
        if box:
            await cell.click(position={"x": box["width"] / 2, "y": box["height"] - 6})
        else:
            await cell.click(force=True)
    except Exception:
        await cell.click(force=True)
    await page.wait_for_load_state("networkidle")

    # Wait for the entry panel to be fully rendered/interactive before acting.
    # On days 2+ there is no inspect pause to absorb this, and the panel body
    # loads asynchronously after the cell click — clicking/typing too early is
    # silently dropped (the "dialog gets no input" symptom).
    try:
        await page.locator(_DIALOG_SELECTORS["project_input"]).first.wait_for(
            state="visible", timeout=10_000)
    except Exception:
        pass
    await page.wait_for_timeout(1200)

    if inspect:
        # The "Enter My Time" panel loads its body asynchronously (a
        # wd-LoadingPanel/glassPanel shows first), so capturing immediately
        # grabs an empty shell.  Let the user confirm the panel is fully
        # rendered, THEN snapshot the live DOM.
        print("  [inspect] Panel opened. Wait until it is FULLY loaded in the")
        print("  [inspect] browser, then press Enter to capture its HTML: ", end="", flush=True)
        input()
        dialog_html_path = _xdg_dir("state") / "workday_dialog_inspect.html"
        dialog_html_path.write_text(await page.content(), encoding="utf-8")
        print(f"  [inspect] Dialog HTML saved → {dialog_html_path}")

    async def _cancel_panel() -> None:
        """Best-effort close of the entry panel so a failed day can't block the
        next cell click.  Clicks Cancel ONLY -- never OK -- because the panel's
        OK and a discard-confirm's OK share title="OK", and clicking it would
        SAVE the very entry we are trying to abandon."""
        try:
            cancel = page.locator(_DIALOG_SELECTORS["cancel_button"]).first
            if await cancel.count() > 0:
                await cancel.click()
                await page.wait_for_load_state("networkidle")
        except Exception:
            pass

    # --- Time Type (project) ---
    # Workday selectinput: click to focus, type the project code; the result
    # list filters on it.  Set this FIRST: selecting a prompt value triggers a
    # server round-trip that re-renders the rest of the form.
    proj_loc = page.locator(_DIALOG_SELECTORS["project_input"]).first
    try:
        await proj_loc.wait_for(state="visible", timeout=8_000)
    except Exception as e:
        print(f"  [warn] Time Type field not found for {change.day}: {e}")
        await _cancel_panel()
        return False

    await proj_loc.click()
    await page.keyboard.type(change.code)

    # The result list renders a moment after typing, as promptLeafNode rows
    # (multiselectlistitem) inside a wd-popup.  CLICKING a leaf only toggles it
    # and leaves the popup open (it then overlays and blocks Hours).  The way to
    # commit is to press Enter: that accepts the exact-match code, adds the
    # selected "card", clears the input, reveals the "Charge Out Project" field,
    # and CLOSES the popup.  Wait for the result to render first so Enter has a
    # match to accept.
    try:
        await page.locator('[data-automation-id="promptLeafNode"]').first.wait_for(
            state="visible", timeout=8_000)
    except Exception:
        pass

    if inspect:
        dd_path = _xdg_dir("state") / "workday_dialog_dropdown.html"
        dd_path.write_text(await page.content(), encoding="utf-8")
        print(f"  [inspect] Dropdown HTML saved \u2192 {dd_path}")

    # The active prompt popup carries data-automation-activepopup="true"; it
    # closing is our signal that the value committed.
    active_popup = page.locator('[data-automation-activepopup="true"]')
    committed = False
    for _ in range(2):
        await page.keyboard.press("Enter")
        try:
            await active_popup.first.wait_for(state="hidden", timeout=5_000)
            committed = True
            break
        except Exception:  # noqa: S112
            continue
    if not committed:
        print(f"  [warn] Could not set Time Type for {change.day} "
              f"({change.code} {change.desc})")
        await _cancel_panel()
        return False

    try:
        await page.wait_for_load_state("networkidle")
    except Exception:
        pass

    # --- Hours ---
    # Focusing Hours makes Workday expand the panel with extra "Details" fields
    # (Charge Out Project, Comment).  That re-render REPLACES the Hours widget,
    # so a value typed before it lands gets wiped (the field reverts to "0" and
    # errors).  So: click Hours, wait for the Details re-render to settle, THEN
    # type the value and commit it with Enter (the gwt numeric widget only
    # commits on Enter).  A late re-render can still clobber it, so verify the
    # committed display value (numericText) and retry.
    target = str(int(change.target_hours))

    def _hours_ok(txt: str) -> bool:
        try:
            return abs(float(txt) - change.target_hours) < 0.01
        except (TypeError, ValueError):
            return False

    async def _read_hours() -> str:
        """Read back the committed hours.  The input's value is most reliable;
        fall back to the numericText display label."""
        loc = page.locator(_DIALOG_SELECTORS["hours_input"]).first
        try:
            v = (await loc.input_value()).strip()
            if v:
                return v
        except Exception:
            pass
        nt = page.locator('[data-automation-id="numericText"]').first
        try:
            if await nt.count() > 0:
                return (await nt.inner_text()).strip()
        except Exception:
            pass
        return ""

    try:
        hours_loc = page.locator(_DIALOG_SELECTORS["hours_input"]).first
        await hours_loc.wait_for(state="visible", timeout=8_000)
        await hours_loc.click()
        # Wait for the "Charge Out Project" detail field to appear (form stable).
        try:
            await page.locator('[data-automation-id="formLabel"]').filter(
                has_text="Charge Out Project").first.wait_for(
                    state="visible", timeout=8_000)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle")
        except Exception:
            pass

        for _ in range(2):
            hours_loc = page.locator(_DIALOG_SELECTORS["hours_input"]).first
            await hours_loc.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
            await page.keyboard.type(target)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(600)  # let the value commit / re-render
            try:
                await page.wait_for_load_state("networkidle")
            except Exception:
                pass
            if _hours_ok(await _read_hours()):
                break
        else:
            # Read-back is unreliable, so do NOT discard on a miss: proceed to
            # OK and let a later 'diff' surface any day that didn't take.
            print(f"  [warn] Could not confirm Hours for {change.day}; "
                  "saving anyway")
    except Exception as e:
        print(f"  [warn] Could not fill hours for {change.day}: {e}")
        await _cancel_panel()
        return False

    # The panel re-renders and can add NEW fields as values are entered.  In
    # inspect mode, snapshot the fully-filled form so its final field set and
    # the OK button's enabled state can be verified before committing.
    if inspect:
        filled_path = _xdg_dir("state") / "workday_dialog_filled.html"
        filled_path.write_text(await page.content(), encoding="utf-8")
        print(f"  [inspect] Filled-form HTML saved \u2192 {filled_path}")
        print("  [inspect] Check the panel in the browser (do NOT touch it),")
        print("  [inspect] then press Enter to click OK (Ctrl-C to abort): ", end="", flush=True)
        input()

    # --- Confirm (OK commits the entry and closes the panel) ---
    ok_loc = page.locator(_DIALOG_SELECTORS["ok_button"]).first
    try:
        await ok_loc.wait_for(state="visible", timeout=5_000)
        await ok_loc.click()
        await page.wait_for_load_state("networkidle")
    except Exception as e:
        print(f"  [warn] Could not click OK for {change.day}: {e}")
        await _cancel_panel()
        return False

    # Wait for the panel to fully close and the calendar to re-stabilize before
    # the caller clicks the next day's cell (otherwise that click lands on a
    # transitioning page and opens an empty panel).
    try:
        await page.locator(_DIALOG_SELECTORS["project_input"]).first.wait_for(
            state="hidden", timeout=8_000)
    except Exception:
        pass
    await page.wait_for_timeout(1500)
    try:
        await page.wait_for_load_state("networkidle")
    except Exception:
        pass

    return True


async def _do_apply(
    time_entry_url: str,
    auth_state_path: Path,
    year: int,
    month: int,
    changes: list[DayChange],
    dry_run: bool,
    inspect: bool,
    debug_html_path: Path,
    headless: bool = False,
) -> None:
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=headless)
        except Exception as e:
            print(f"Could not launch Chromium: {e}", file=sys.stderr)
            print("Install the matching browser:  time-entry install-browser", file=sys.stderr)
            return
        context = await browser.new_context(storage_state=str(auth_state_path))
        page = await context.new_page()
        try:
            print(f"Navigating to {time_entry_url} ...")
            await page.goto(time_entry_url)
            await page.wait_for_load_state("networkidle")
            if "myworkday.com" not in page.url:
                print(f"[warn] Ended up at {page.url} — session may have expired")
                print("Delete the auth-state file and run 'login' again.")
                return
            await _navigate_to_month(page, year, month)

            succeeded = 0
            failed = 0
            first = True
            for ch in changes:
                print(f"  {'[dry-run] ' if dry_run else ''}{ch.action:6s}  "
                      f"{ch.day}  {ch.code} {ch.desc}  {ch.target_hours:.0f}h")
                if dry_run:
                    continue
                ok = await _enter_time_for_day(page, ch, inspect=inspect and first)
                first = False
                if ok:
                    succeeded += 1
                else:
                    failed += 1

            if not dry_run:
                print(f"\n{succeeded} applied, {failed} failed")
                # Save the timesheet
                save_loc = page.locator(_DIALOG_SELECTORS["save_button"])
                if await save_loc.count() > 0:
                    await save_loc.first.click()
                    await page.wait_for_load_state("networkidle")
                    print("Timesheet saved.")
                else:
                    print("Entries committed; no separate Save button found.")

        finally:
            html = await page.content()
            debug_html_path.write_text(html, encoding="utf-8")
            print(f"Page HTML saved → {debug_html_path}")
            await context.close()
            await browser.close()


async def _do_submit(
    time_entry_url: str,
    auth_state_path: Path,
    year: int,
    month: int,
    debug_html_path: Path,
    headless: bool = False,
) -> None:
    """Open a month's timecard and perform Workday's Review -> Submit flow."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=headless)
        except Exception as e:
            raise click.ClickException(
                f"Could not launch Chromium: {e}. "
                "Run 'time-entry install-browser' and try again."
            ) from e
        context = await browser.new_context(storage_state=str(auth_state_path))
        page = await context.new_page()
        try:
            print(f"Navigating to {time_entry_url} ...")
            await page.goto(time_entry_url)
            await page.wait_for_load_state("networkidle")
            if "myworkday.com" not in page.url:
                raise click.ClickException(
                    f"Ended up at {page.url}; the saved session may have expired. "
                    "Run 'time-entry login' again."
                )
            await _navigate_to_month(page, year, month)
            await _review_and_submit(page)
        finally:
            html = await page.content()
            debug_html_path.write_text(html, encoding="utf-8")
            print(f"Page HTML saved -> {debug_html_path}")
            await context.close()
            await browser.close()


async def _review_and_submit(page) -> None:
    """Click the destructive timecard actions in their required order."""
    review = page.locator(_DIALOG_SELECTORS["review_button"]).first
    try:
        await review.wait_for(state="visible", timeout=10_000)
        await review.click()
    except Exception as e:
        raise click.ClickException(
            "Review button not found or could not be clicked; "
            "verify the timecard manually."
        ) from e
    try:
        await page.wait_for_load_state("networkidle")
    except Exception:
        pass
    print("Timecard reviewed.")

    submit = page.locator(_DIALOG_SELECTORS["submit_button"]).first
    try:
        await submit.wait_for(state="visible", timeout=10_000)
        await submit.click()
    except Exception as e:
        raise click.ClickException(
            "Submit button not found or could not be clicked after Review; "
            "verify the timecard manually."
        ) from e
    try:
        await page.wait_for_load_state("networkidle")
    except Exception:
        pass
    print("Timecard submitted.")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def parse_month(s: str | None, today: date) -> tuple[int, int]:
    if s is None:
        return today.year, today.month
    try:
        d = date.fromisoformat(s + "-01")
        return d.year, d.month
    except ValueError:
        sys.exit(f"Invalid month '{s}': expected YYYY-MM.")


def cmd_init(config_path: Path) -> None:
    if config_path.exists():
        sys.exit(f"Config already exists at {config_path}. Remove it first to reinitialize.")
    config_path.write_text(TEMPLATE_TOML)
    print(f"Created {config_path}\nEdit it to set your projects, percentages, and days off.")


def cmd_plan(month_str: str | None, dry_run: bool, config: Config, records: Records, records_path: Path) -> None:
    year, month = parse_month(month_str, date.today())
    fy = fy_of(year, month)
    if fy != config.fiscal_year:
        print(
            f"Warning: {MONTH_NAMES[month]} {year} is FY{fy} but config says FY{config.fiscal_year}.",
            file=sys.stderr,
        )

    allocation = compute_allocation(year, month, config, records)
    working = get_working_days(year, month, config.days_off)
    weeks = get_weeks_in_month(year, month, config.days_off)
    week_schedule = assign_weeks(allocation, weeks, config.projects)
    days_off_in_month = sorted(
        d for d in config.days_off if d.year == year and d.month == month and d.weekday() < 5
    )

    record = MonthRecord(
        year=year,
        month=month,
        working_days=len(working),
        days_off=days_off_in_month,
        allocation=allocation,
        week_schedule=week_schedule,
    )

    if not dry_run:
        records.months = [m for m in records.months if not (m.year == year and m.month == month)]
        records.months.append(record)
        records.months.sort(key=lambda m: (m.year, m.month))
        save_records(records_path, records)

    display_calendar(year, month, record, config, records)

    if dry_run:
        print("(dry-run: not saved)")


def cmd_show(month_str: str | None, config: Config, records: Records) -> None:
    year, month = parse_month(month_str, date.today())
    record = next((m for m in records.months if m.year == year and m.month == month), None)
    if record is None:
        sys.exit(f"No record for {MONTH_NAMES[month]} {year}. Run 'plan' first.")
    display_calendar(year, month, record, config, records)


def cmd_status(config: Config, records: Records) -> None:
    display_status(config, records)


def cmd_install_browser() -> None:
    """Install Chromium using the exact Playwright version time-entry uses."""
    command = [sys.executable, "-m", "playwright", "install", "chromium"]
    print("Installing Chromium for time-entry...")
    result = subprocess.run(command, check=False)
    if result.returncode:
        raise click.ClickException(
            "Chromium installation failed. Check your network connection and retry "
            "'time-entry install-browser'."
        )


def cmd_login(auth_state: Path, config: Config) -> None:
    asyncio.run(_do_login(config.workday.home_url, auth_state))


def cmd_get(
    month_str: str | None,
    auth_state: Path,
    config: Config,
    records: Records,
    headless: bool = False,
) -> None:
    if not auth_state.exists():
        sys.exit(
            f"Auth state not found at {auth_state}.\n"
            "Run 'time-entry login' first to save your session."
        )
    year, month = parse_month(month_str, date.today())
    debug_path = _xdg_dir("state") / f"workday_debug_{year:04d}_{month:02d}.html"
    entries = asyncio.run(_do_get(
        config.workday.time_entry_url,
        auth_state,
        year,
        month,
        debug_path,
        headless=headless,
    ))
    record = next((m for m in records.months if m.year == year and m.month == month), None)
    display_workday_get(year, month, entries, config, record)


def cmd_diff(
    month_str: str | None,
    auth_state: Path,
    config: Config,
    records: Records,
    headless: bool = False,
) -> list[DayChange]:
    if not auth_state.exists():
        sys.exit(
            f"Auth state not found at {auth_state}.\n"
            "Run 'time-entry login' first to save your session."
        )
    year, month = parse_month(month_str, date.today())
    record = next((m for m in records.months if m.year == year and m.month == month), None)
    if record is None:
        sys.exit(
            f"No plan record for {MONTH_NAMES[month]} {year}. "
            "Run 'time-entry plan' first."
        )

    debug_path = _xdg_dir("state") / f"workday_debug_{year:04d}_{month:02d}.html"
    entries = asyncio.run(_do_get(
        config.workday.time_entry_url,
        auth_state,
        year,
        month,
        debug_path,
        headless=headless,
    ))

    plan = _plan_by_date(record, config)
    changes, matched, skipped = _compute_diff(entries, plan)

    diff_path = _xdg_dir("state") / f"time-entry-diff-{year:04d}-{month:02d}.json"
    display_diff(year, month, changes, matched, skipped, diff_path)
    return changes


def cmd_apply(
    month_str: str | None,
    auth_state: Path,
    yes: bool,
    inspect: bool,
    config: Config,
    headless: bool = False,
) -> None:
    if not auth_state.exists():
        sys.exit(
            f"Auth state not found at {auth_state}.\n"
            "Run 'time-entry login' first to save your session."
        )
    year, month = parse_month(month_str, date.today())
    if inspect and headless:
        raise click.UsageError(
            "--inspect requires a visible browser; use --headed instead of --headless."
        )

    # Load changes from the diff JSON produced by 'diff'
    diff_path = _xdg_dir("state") / f"time-entry-diff-{year:04d}-{month:02d}.json"
    if not diff_path.exists():
        sys.exit(
            f"Diff file not found: {diff_path}\n"
            "Run 'time-entry diff' first."
        )
    raw = json.loads(diff_path.read_text(encoding="utf-8"))
    changes = [
        DayChange(
            day=date.fromisoformat(ch["date"]),
            code=ch["code"],
            desc=ch["desc"],
            target_hours=ch["target_hours"],
            current_hours=ch["current_hours"],
            action=ch["action"],
        )
        for ch in raw["changes"]
    ]

    if not changes:
        print(f"No changes to apply for {MONTH_NAMES[month]} {year}.")
        return

    dry_run = not yes
    if dry_run:
        print(f"Dry-run ({len(changes)} changes).  Pass --yes to apply.\n")
    else:
        print(f"Applying {len(changes)} changes to Workday...\n")

    debug_path = _xdg_dir("state") / f"workday_debug_{year:04d}_{month:02d}.html"
    asyncio.run(_do_apply(
        config.workday.time_entry_url,
        auth_state,
        year,
        month,
        changes,
        dry_run=dry_run,
        inspect=inspect,
        debug_html_path=debug_path,
        headless=headless,
    ))


def cmd_submit(
    month_str: str | None,
    auth_state: Path,
    yes: bool,
    config: Config,
    headless: bool = False,
) -> None:
    if not auth_state.exists():
        sys.exit(
            f"Auth state not found at {auth_state}.\n"
            "Run 'time-entry login' first to save your session."
        )
    year, month = parse_month(month_str, date.today())
    if not yes:
        print(
            f"Dry-run: would review and submit the {MONTH_NAMES[month]} {year} "
            "timecard. Pass --yes to submit."
        )
        return

    print(f"Reviewing and submitting the {MONTH_NAMES[month]} {year} timecard...")
    debug_path = _xdg_dir("state") / f"workday_submit_debug_{year:04d}_{month:02d}.html"
    asyncio.run(_do_submit(
        config.workday.time_entry_url,
        auth_state,
        year,
        month,
        debug_path,
        headless=headless,
    ))


_WORKFLOW_MAX_APPLY_ATTEMPTS = 3


def cmd_workflow(
    month_str: str | None,
    auth_state: Path,
    config: Config,
    records: Records,
    records_path: Path,
    headless: bool = False,
) -> None:
    """Run the interactive monthly workflow with destructive confirmation gates."""
    cmd_login(auth_state, config)
    cmd_get(month_str, auth_state, config, records, headless=headless)
    cmd_plan(month_str, False, config, records, records_path)
    changes = cmd_diff(month_str, auth_state, config, records, headless=headless)

    if changes and not click.confirm("Do you want to apply this?", default=False):
        print("Apply skipped; submit is not offered while the diff has changes.")
        return

    attempt = 0
    while changes and attempt < _WORKFLOW_MAX_APPLY_ATTEMPTS:
        attempt += 1
        print(
            f"Apply attempt {attempt}/{_WORKFLOW_MAX_APPLY_ATTEMPTS}: "
            f"{len(changes)} change(s) remain."
        )
        cmd_apply(
            month_str,
            auth_state,
            yes=True,
            inspect=False,
            config=config,
            headless=headless,
        )
        changes = cmd_diff(
            month_str,
            auth_state,
            config,
            records,
            headless=headless,
        )

    if changes:
        raise click.ClickException(
            f"Workflow did not converge after {_WORKFLOW_MAX_APPLY_ATTEMPTS} "
            f"apply attempts; {len(changes)} change(s) remain. "
            "The timecard was not submitted."
        )

    print("Diff is clean; no changes remain to apply.")

    if click.confirm("Do you want to submit this?", default=False):
        cmd_submit(month_str, auth_state, yes=True, config=config, headless=headless)
    else:
        print("Submit skipped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

@click.group()
@click.option("--config", "config_path", type=click.Path(path_type=Path),
              default=lambda: _xdg_dir("config") / "config.toml",
              help="Config TOML (default: ~/.config/time-entry/config.toml)")
@click.option("--records", "records_path", type=click.Path(path_type=Path),
              default=lambda: _xdg_dir("state") / "time-entry.json",
              help="Records JSON (default: ~/.local/state/time-entry/time-entry.json)")
@click.option("--dry-run", is_flag=True, help="Compute but do not save")
@click.option("--headless/--headed", default=False,
              help="Hide/show Chromium after login (default: headed)")
@click.option("--auth-state", "auth_state", type=click.Path(path_type=Path),
              default=lambda: _xdg_dir("state") / "time-entry-auth.json",
              help="Playwright auth-state JSON (default: ~/.local/state/time-entry/time-entry-auth.json)")
@click.pass_context
def main(ctx, config_path, records_path, dry_run, headless, auth_state):
    """
    Monthly time allocator for fiscal-year project reporting.

    Run these sub-commands in sequence:

    1. login        # Opens browser for auth

    2. get          # Get current state

    3. plan         # Local plan

    4. diff         # Local diff

    5. apply --yes  # Enter and save time; omit --yes for dry-run

    6. submit --yes # Review and submit the timecard

    Or run 'workflow' to execute the sequence with confirmation prompts.
    """
    ctx.ensure_object(dict)
    ctx.obj.update(
        config_path=config_path,
        records_path=records_path,
        dry_run=dry_run,
        headless=headless,
        auth_state=auth_state,
    )


def _ctx_load(ctx):
    obj = ctx.obj
    config = load_config(obj["config_path"])
    records = load_records(obj["records_path"], config.fiscal_year)
    return config, records


@main.command()
@click.argument("month", required=False, metavar="YYYY-MM")
@click.pass_context
def plan(ctx, month):
    """Compute (and save) allocation for a month."""
    config, records = _ctx_load(ctx)
    cmd_plan(month, ctx.obj["dry_run"], config, records, ctx.obj["records_path"])


@main.command()
@click.argument("month", required=False, metavar="YYYY-MM")
@click.pass_context
def show(ctx, month):
    """Display calendar for a saved month."""
    config, records = _ctx_load(ctx)
    cmd_show(month, config, records)


@main.command()
@click.pass_context
def status(ctx):
    """Show FY-to-date totals vs targets."""
    config, records = _ctx_load(ctx)
    cmd_status(config, records)


@main.command("init")
@click.pass_context
def init_cmd(ctx):
    """Write a template config file."""
    cmd_init(ctx.obj["config_path"])


@main.command("install-browser")
def install_browser():
    """Install the Chromium version required by time-entry."""
    cmd_install_browser()


@main.command()
@click.pass_context
def login(ctx):
    """Open browser for manual SSO+DUO login and save auth state."""
    config, _ = _ctx_load(ctx)
    cmd_login(ctx.obj["auth_state"], config)


@main.command()
@click.argument("month", required=False, metavar="YYYY-MM")
@click.pass_context
def get(ctx, month):
    """Read current Workday time entries for a month."""
    config, records = _ctx_load(ctx)
    cmd_get(month, ctx.obj["auth_state"], config, records, headless=ctx.obj["headless"])


@main.command()
@click.argument("month", required=False, metavar="YYYY-MM")
@click.pass_context
def diff(ctx, month):
    """Compare Workday entries against plan and save a diff JSON."""
    config, records = _ctx_load(ctx)
    cmd_diff(month, ctx.obj["auth_state"], config, records, headless=ctx.obj["headless"])


@main.command()
@click.argument("month", required=False, metavar="YYYY-MM")
@click.option("--yes", is_flag=True, help="Actually apply (default: dry-run)")
@click.option("--inspect", is_flag=True,
              help="Pause after first cell click and dump dialog HTML for selector debugging")
@click.pass_context
def apply(ctx, month, yes, inspect):
    """Apply diff JSON changes to Workday without submitting."""
    config, _records = _ctx_load(ctx)
    cmd_apply(
        month,
        ctx.obj["auth_state"],
        yes,
        inspect,
        config,
        headless=ctx.obj["headless"],
    )


@main.command()
@click.argument("month", required=False, metavar="YYYY-MM")
@click.option("--yes", is_flag=True, help="Review and submit (default: dry-run)")
@click.pass_context
def submit(ctx, month, yes):
    """Review and submit a Workday timecard."""
    config, _records = _ctx_load(ctx)
    cmd_submit(month, ctx.obj["auth_state"], yes, config, headless=ctx.obj["headless"])


@main.command()
@click.argument("month", required=False, metavar="YYYY-MM")
@click.pass_context
def workflow(ctx, month):
    """Run login through submission with confirmation prompts."""
    if ctx.obj["dry_run"]:
        raise click.UsageError(
            "--dry-run is not supported by workflow; run the individual commands instead."
        )
    config, records = _ctx_load(ctx)
    cmd_workflow(
        month,
        ctx.obj["auth_state"],
        config,
        records,
        ctx.obj["records_path"],
        headless=ctx.obj["headless"],
    )


if __name__ == "__main__":
    main()
