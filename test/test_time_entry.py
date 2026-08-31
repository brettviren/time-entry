"""Tests for the pure (non-browser) parts of time_entry.

The Workday automation itself needs a real browser and a real login, so the
tests here cover the allocation algorithm, the config/records I/O and the CLI
wiring, including where configuration and state are looked for.
"""

import json
from datetime import date

import pytest
import click
from click.testing import CliRunner

import time_entry as te

GOOD_TOML = """\
fiscal_year = 2026
days_off = ["2026-07-03"]

[workday]
home_url = "https://example.com/home"
time_entry_url = "https://example.com/time"

[[projects]]
code = "AAAAA"
pct = 50
desc = "Project A"

[[projects]]
code = "BBBBB"
pct = 50
desc = "Project B"
"""


@pytest.fixture()
def xdg(tmp_path, monkeypatch):
    """Point the XDG directories at a temporary area."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


@pytest.fixture()
def config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(GOOD_TOML)
    return te.load_config(path)


# ---------------------------------------------------------------------------
# XDG locations
# ---------------------------------------------------------------------------

def test_xdg_dir_honors_environment(xdg):
    assert te._xdg_dir("config") == xdg / "config" / "time-entry"
    assert te._xdg_dir("state") == xdg / "state" / "time-entry"
    assert te._xdg_dir("config").is_dir()


def test_xdg_dir_falls_back_to_config_home(tmp_path, monkeypatch):
    """With no XDG_*_HOME set, both config and state live in ~/.config/time-entry.

    Existing installations keep their records there, so this fallback must not
    change.
    """
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = tmp_path / ".config" / "time-entry"
    assert te._xdg_dir("config") == expected
    assert te._xdg_dir("state") == expected


# ---------------------------------------------------------------------------
# Fiscal year and calendar helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("year", "month", "fy"), [
    (2025, 10, 2026),
    (2025, 12, 2026),
    (2026, 1, 2026),
    (2026, 9, 2026),
    (2026, 10, 2027),
])
def test_fy_of(year, month, fy):
    assert te.fy_of(year, month) == fy


def test_months_in_fy():
    months = te.months_in_fy(2026)
    assert len(months) == 12
    assert months[0] == (2025, 10)
    assert months[-1] == (2026, 9)
    assert all(te.fy_of(y, m) == 2026 for y, m in months)


def test_get_working_days_excludes_weekends_and_days_off():
    days = te.get_working_days(2026, 7, set())
    assert len(days) == 23
    assert all(d.weekday() < 5 for d in days)

    off = {date(2026, 7, 3), date(2026, 7, 4)}   # Fri, Sat
    days = te.get_working_days(2026, 7, off)
    assert len(days) == 22                        # only the Friday counted
    assert date(2026, 7, 3) not in days


def test_get_weeks_in_month():
    weeks = te.get_weeks_in_month(2026, 7, set())
    assert [str(monday) for monday, _ in weeks] == [
        "2026-06-29", "2026-07-06", "2026-07-13", "2026-07-20", "2026-07-27",
    ]
    assert [len(days) for _, days in weeks] == [3, 5, 5, 5, 5]
    # The partial first week only holds days of the target month.
    assert all(d.month == 7 for d in weeks[0][1])


def test_get_weeks_in_month_drops_fully_off_weeks():
    off = set(te.get_working_days(2026, 7, set())[:3])   # all of Jul 1-3
    weeks = te.get_weeks_in_month(2026, 7, off)
    assert str(weeks[0][0]) == "2026-07-06"


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------

def test_compute_allocation_no_history(config):
    records = te.Records(fiscal_year=2026)
    alloc = te.compute_allocation(2026, 6, config, records)
    assert sum(alloc.values()) == 22            # every working day is assigned
    assert alloc == {"AAAAA": 11, "BBBBB": 11}


def test_compute_allocation_compensates_drift(config):
    """A month that over-serves one project is paid back by the next."""
    records = te.Records(fiscal_year=2026, months=[
        te.MonthRecord(year=2026, month=6, working_days=22, days_off=[],
                       allocation={"AAAAA": 20, "BBBBB": 2}, week_schedule=[]),
    ])
    alloc = te.compute_allocation(2026, 7, config, records)
    working = len(te.get_working_days(2026, 7, config.days_off))
    assert sum(alloc.values()) == working
    # B is far behind, so it takes the whole month.
    assert alloc["BBBBB"] > alloc["AAAAA"]
    # Cannot un-bill the past, so A is clamped rather than negative.
    assert alloc["AAAAA"] >= 0


def test_compute_allocation_empty_month(config):
    """A month with no working days allocates nothing."""
    off = set(te.get_working_days(2026, 7, set()))
    cfg = te.Config(fiscal_year=2026, projects=config.projects, days_off=off)
    alloc = te.compute_allocation(2026, 7, cfg, te.Records(fiscal_year=2026))
    assert sum(alloc.values()) == 0


def test_compute_allocation_ignores_other_fiscal_years(config):
    """Records outside this FY must not perturb the allocation."""
    records = te.Records(fiscal_year=2026, months=[
        te.MonthRecord(year=2025, month=6, working_days=20, days_off=[],
                       allocation={"AAAAA": 20, "BBBBB": 0}, week_schedule=[]),
    ])
    assert te.compute_allocation(2026, 6, config, records) == {"AAAAA": 11, "BBBBB": 11}


def test_assign_weeks_covers_every_working_day(config):
    weeks = te.get_weeks_in_month(2026, 7, config.days_off)
    alloc = te.compute_allocation(2026, 7, config, te.Records(fiscal_year=2026))
    schedule = te.assign_weeks(alloc, weeks, config.projects)

    assert len(schedule) == len(weeks)
    for entry, (monday, workdays) in zip(schedule, weeks, strict=True):
        assert entry.week_start == monday
        assert sum(count for _, count in entry.days) == len(workdays)

    per_code = {}
    for entry in schedule:
        for code, count in entry.days:
            per_code[code] = per_code.get(code, 0) + count
    assert per_code == {code: days for code, days in alloc.items() if days}


# ---------------------------------------------------------------------------
# Config and records I/O
# ---------------------------------------------------------------------------

def test_load_config(config):
    assert config.fiscal_year == 2026
    assert [p.code for p in config.projects] == ["AAAAA", "BBBBB"]
    assert config.projects[0].fraction == 0.5
    assert config.days_off == {date(2026, 7, 3)}
    assert config.workday.time_entry_url == "https://example.com/time"


def test_load_config_writes_template_when_missing(tmp_path):
    path = tmp_path / "missing.toml"
    with pytest.raises(SystemExit) as excinfo:
        te.load_config(path)
    assert excinfo.value.code == 0
    assert path.exists()
    assert "fiscal_year" in path.read_text()


def test_load_config_rejects_bad_percentages(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text(GOOD_TOML.replace("pct = 50", "pct = 40", 1))
    with pytest.raises(click.ClickException) as excinfo:
        te.load_config(path)
    assert "90" in str(excinfo.value)


def test_load_config_reports_template_parse_errors(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(te.TEMPLATE_TOML)

    with pytest.raises(click.ClickException, match="Replace all template placeholders"):
        te.load_config(path)


def test_records_round_trip(tmp_path, config):
    weeks = te.get_weeks_in_month(2026, 7, config.days_off)
    alloc = te.compute_allocation(2026, 7, config, te.Records(fiscal_year=2026))
    record = te.MonthRecord(
        year=2026, month=7, working_days=22, days_off=[date(2026, 7, 3)],
        allocation=alloc,
        week_schedule=te.assign_weeks(alloc, weeks, config.projects),
    )
    path = tmp_path / "records.json"
    te.save_records(path, te.Records(fiscal_year=2026, months=[record]))

    back = te.load_records(path, 2026)
    assert back.fiscal_year == 2026
    assert len(back.months) == 1
    assert back.months[0] == record


def test_load_records_missing_file(tmp_path):
    records = te.load_records(tmp_path / "nope.json", 2026)
    assert records.fiscal_year == 2026
    assert records.months == []


# ---------------------------------------------------------------------------
# Plan vs Workday
# ---------------------------------------------------------------------------

def test_plan_by_date(config):
    record = te.MonthRecord(
        year=2026, month=7, working_days=3, days_off=[],
        allocation={"AAAAA": 2, "BBBBB": 1},
        week_schedule=[te.WeekEntry(week_start=date(2026, 6, 29),
                                    days=[("AAAAA", 2), ("BBBBB", 1)])],
    )
    plan = te._plan_by_date(record, config)
    assert plan == {
        date(2026, 7, 1): ("AAAAA", "Project A"),
        date(2026, 7, 2): ("AAAAA", "Project A"),
        date(2026, 7, 3): ("BBBBB", "Project B"),
    }


def test_compute_diff():
    plan = {
        date(2026, 7, 1): ("AAAAA", "Project A"),   # already 8h -> matched
        date(2026, 7, 2): ("AAAAA", "Project A"),   # 0h -> set
        date(2026, 7, 3): ("BBBBB", "Project B"),   # 4h -> update
        date(2026, 7, 6): ("BBBBB", "Project B"),   # holiday -> skipped
    }
    entries = [
        te.WorkdayDayEntry(day=date(2026, 7, 1), total_hours=8.0),
        te.WorkdayDayEntry(day=date(2026, 7, 2), total_hours=0.0),
        te.WorkdayDayEntry(day=date(2026, 7, 3), total_hours=4.0),
        te.WorkdayDayEntry(day=date(2026, 7, 6), total_hours=8.0,
                           is_holiday=True, holiday_name="Independence Day"),
    ]
    changes, matched, skipped = te._compute_diff(entries, plan)
    assert matched == [date(2026, 7, 1)]
    assert skipped == [date(2026, 7, 6)]
    assert [(c.day, c.action, c.code) for c in changes] == [
        (date(2026, 7, 2), "set", "AAAAA"),
        (date(2026, 7, 3), "update", "BBBBB"),
    ]
    assert all(c.target_hours == 8.0 for c in changes)


def test_parse_period_label():
    assert te._parse_period_label("May 2026") == (2026, 5)
    assert te._parse_period_label("2026-05") == (2026, 5)
    assert te._parse_period_label("Week of May 11, 2026") == (2026, 5)
    assert te._parse_period_label("nonsense") is None
    assert te._parse_period_label("") is None


def test_parse_month():
    today = date(2026, 7, 15)
    assert te.parse_month(None, today) == (2026, 7)
    assert te.parse_month("2025-11", today) == (2025, 11)
    with pytest.raises(SystemExit):
        te.parse_month("nope", today)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_help(xdg):
    result = CliRunner().invoke(te.main, ["--help"])
    assert result.exit_code == 0
    assert "Monthly time allocator" in result.output
    for command in (
        "plan", "show", "status", "init", "install-browser", "login", "get", "diff", "apply", "submit",
        "workflow",
    ):
        assert command in result.output


def test_save_and_submit_selectors_are_separate():
    assert "submit" not in te._DIALOG_SELECTORS["save_button"]
    assert "review" in te._DIALOG_SELECTORS["review_button"]
    assert "submit" in te._DIALOG_SELECTORS["submit_button"]


def test_browser_mode_defaults_to_headed_and_can_be_switched(tmp_path, xdg, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(GOOD_TOML)
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("{}")
    modes = []

    def fake_cmd_get(_month, _auth, _config, _records, headless=False):
        modes.append(headless)

    monkeypatch.setattr(te, "cmd_get", fake_cmd_get)
    runner = CliRunner()
    base = ["--config", str(config_path), "--auth-state", str(auth_path)]

    assert runner.invoke(te.main, [*base, "get", "2026-07"]).exit_code == 0
    assert runner.invoke(te.main, [*base, "--headless", "get", "2026-07"]).exit_code == 0
    assert runner.invoke(te.main, [*base, "--headed", "get", "2026-07"]).exit_code == 0
    assert modes == [False, True, False]


def test_inspect_rejects_headless_browser(tmp_path, xdg):
    config_path = tmp_path / "config.toml"
    config_path.write_text(GOOD_TOML)
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("{}")
    result = CliRunner().invoke(te.main, [
        "--config", str(config_path), "--auth-state", str(auth_path),
        "--headless", "apply", "2026-07", "--inspect",
    ])

    assert result.exit_code != 0
    assert "--inspect requires a visible browser" in result.output


def test_workflow_cli_threads_headless_and_optional_month(tmp_path, xdg, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(GOOD_TOML)
    calls = []

    def fake_cmd_workflow(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(te, "cmd_workflow", fake_cmd_workflow)
    result = CliRunner().invoke(te.main, [
        "--config", str(config_path), "--records", str(tmp_path / "records.json"),
        "--auth-state", str(tmp_path / "auth.json"), "--headless", "workflow",
    ])

    assert result.exit_code == 0, result.output
    assert calls[0][0][0] is None
    assert calls[0][1] == {"headless": True}

    result = CliRunner().invoke(te.main, [
        "--config", str(config_path), "--dry-run", "workflow",
    ])
    assert result.exit_code != 0
    assert "--dry-run is not supported by workflow" in result.output


def _workflow_change(day_number):
    return te.DayChange(
        day=date(2026, 7, day_number),
        code="AAAAA",
        desc="Project A",
        target_hours=8,
        current_hours=0,
        action="set",
    )


def _mock_workflow_steps(monkeypatch, diff_results, answers):
    events = []
    prompts = []

    monkeypatch.setattr(te, "cmd_login", lambda *_args, **_kwargs: events.append("login"))
    monkeypatch.setattr(te, "cmd_get", lambda *_args, **_kwargs: events.append("get"))
    monkeypatch.setattr(te, "cmd_plan", lambda *_args, **_kwargs: events.append("plan"))

    results = iter(diff_results)

    def fake_diff(*_args, **_kwargs):
        events.append("diff")
        return next(results)

    monkeypatch.setattr(te, "cmd_diff", fake_diff)
    monkeypatch.setattr(te, "cmd_apply", lambda *_args, **_kwargs: events.append("apply"))
    monkeypatch.setattr(te, "cmd_submit", lambda *_args, **_kwargs: events.append("submit"))

    responses = iter(answers)

    def fake_confirm(prompt, default):
        prompts.append((prompt, default))
        return next(responses)

    monkeypatch.setattr(te.click, "confirm", fake_confirm)
    return events, prompts


def test_workflow_repeats_apply_until_diff_is_clean(tmp_path, config, monkeypatch):
    events, prompts = _mock_workflow_steps(
        monkeypatch,
        [
            [_workflow_change(1), _workflow_change(2)],
            [_workflow_change(2)],
            [],
        ],
        [True, False],
    )
    te.cmd_workflow(
        "2026-07",
        tmp_path / "auth.json",
        config,
        te.Records(fiscal_year=2026),
        tmp_path / "records.json",
        headless=True,
    )

    assert events == ["login", "get", "plan", "diff", "apply", "diff", "apply", "diff"]
    assert prompts == [
        ("Do you want to apply this?", False),
        ("Do you want to submit this?", False),
    ]


def test_workflow_submit_is_optional_after_clean_diff(tmp_path, config, monkeypatch):
    events, prompts = _mock_workflow_steps(monkeypatch, [[]], [True])
    te.cmd_workflow(
        "2026-07",
        tmp_path / "auth.json",
        config,
        te.Records(fiscal_year=2026),
        tmp_path / "records.json",
    )

    assert events == ["login", "get", "plan", "diff", "submit"]
    assert prompts == [("Do you want to submit this?", False)]


def test_workflow_does_not_submit_with_pending_changes(tmp_path, config, monkeypatch):
    changes = [_workflow_change(1)]
    events, prompts = _mock_workflow_steps(monkeypatch, [changes], [False])
    te.cmd_workflow(
        "2026-07",
        tmp_path / "auth.json",
        config,
        te.Records(fiscal_year=2026),
        tmp_path / "records.json",
    )

    assert events == ["login", "get", "plan", "diff"]
    assert prompts == [("Do you want to apply this?", False)]


def test_workflow_stops_if_apply_makes_no_progress(
    tmp_path, config, monkeypatch, capsys,
):
    changes = [_workflow_change(1)]
    events, prompts = _mock_workflow_steps(monkeypatch, [changes, changes], [True])
    te.cmd_workflow(
        "2026-07",
        tmp_path / "auth.json",
        config,
        te.Records(fiscal_year=2026),
        tmp_path / "records.json",
    )

    assert events == ["login", "get", "plan", "diff", "apply", "diff"]
    assert prompts == [("Do you want to apply this?", False)]
    assert "Apply made no progress" in capsys.readouterr().out


def test_review_happens_before_submit():
    events = []

    class FakeLocator:
        def __init__(self, action):
            self.action = action

        @property
        def first(self):
            return self

        async def wait_for(self, **_kwargs):
            events.append(("wait", self.action))

        async def click(self):
            events.append(("click", self.action))

    class FakePage:
        def locator(self, selector):
            if selector == te._DIALOG_SELECTORS["review_button"]:
                return FakeLocator("review")
            if selector == te._DIALOG_SELECTORS["submit_button"]:
                return FakeLocator("submit")
            raise AssertionError(f"unexpected selector: {selector}")

        async def wait_for_load_state(self, state):
            events.append(("load", state))

    te.asyncio.run(te._review_and_submit(FakePage()))

    clicks = [action for event, action in events if event == "click"]
    assert clicks == ["review", "submit"]


def test_init_writes_config(tmp_path, xdg):
    path = tmp_path / "config.toml"
    runner = CliRunner()
    result = runner.invoke(te.main, ["--config", str(path), "init"])
    assert result.exit_code == 0
    assert path.exists()
    # The template carries placeholders the user must edit before it parses.
    assert "FIX" in path.read_text()

    # A second init must not clobber an edited config.
    path.write_text(GOOD_TOML)
    result = runner.invoke(te.main, ["--config", str(path), "init"])
    assert result.exit_code != 0
    assert path.read_text() == GOOD_TOML


def test_cli_reports_invalid_config_without_traceback(tmp_path, xdg):
    path = tmp_path / "config.toml"
    path.write_text(te.TEMPLATE_TOML)

    result = CliRunner().invoke(te.main, ["--config", str(path), "plan"])
    assert result.exit_code != 0
    assert "Replace all template placeholders" in result.output
    assert "Traceback" not in result.output


def test_install_browser_uses_bundled_playwright(monkeypatch):
    calls = []

    def fake_run(command, check):
        calls.append((command, check))
        return te.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(te.subprocess, "run", fake_run)
    result = CliRunner().invoke(te.main, ["install-browser"])

    assert result.exit_code == 0
    assert calls == [([te.sys.executable, "-m", "playwright", "install", "chromium"], False)]


def test_plan_show_status(tmp_path, xdg):
    config_path = tmp_path / "config.toml"
    config_path.write_text(GOOD_TOML)
    records_path = tmp_path / "records.json"
    base = ["--config", str(config_path), "--records", str(records_path)]
    runner = CliRunner()

    result = runner.invoke(te.main, [*base, "plan", "2026-07"])
    assert result.exit_code == 0, result.output
    assert "July 2026" in result.output

    saved = json.loads(records_path.read_text())
    assert [(m["year"], m["month"]) for m in saved["months"]] == [(2026, 7)]
    assert sum(saved["months"][0]["allocation"].values()) == 22
    assert saved["months"][0]["days_off"] == ["2026-07-03"]

    result = runner.invoke(te.main, [*base, "show", "2026-07"])
    assert result.exit_code == 0
    assert "Project A" in result.output

    result = runner.invoke(te.main, [*base, "status"])
    assert result.exit_code == 0
    assert "FY2026 status" in result.output


def test_plan_dry_run_does_not_save(tmp_path, xdg):
    config_path = tmp_path / "config.toml"
    config_path.write_text(GOOD_TOML)
    records_path = tmp_path / "records.json"
    result = CliRunner().invoke(te.main, [
        "--config", str(config_path), "--records", str(records_path),
        "--dry-run", "plan", "2026-07",
    ])
    assert result.exit_code == 0
    assert "dry-run" in result.output
    assert not records_path.exists()


def test_show_without_plan(tmp_path, xdg):
    config_path = tmp_path / "config.toml"
    config_path.write_text(GOOD_TOML)
    result = CliRunner().invoke(te.main, [
        "--config", str(config_path), "--records", str(tmp_path / "r.json"),
        "show", "2026-07",
    ])
    assert result.exit_code != 0
    assert "Run 'plan' first" in result.output


def test_get_without_auth(tmp_path, xdg):
    """Browser commands must fail cleanly, not launch anything, without auth."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(GOOD_TOML)
    result = CliRunner().invoke(te.main, [
        "--config", str(config_path), "--records", str(tmp_path / "r.json"),
        "--auth-state", str(tmp_path / "auth.json"),
        "get", "2026-07",
    ])
    assert result.exit_code != 0
    assert "time-entry login" in result.output


def test_submit_without_auth(tmp_path, xdg):
    config_path = tmp_path / "config.toml"
    config_path.write_text(GOOD_TOML)
    result = CliRunner().invoke(te.main, [
        "--config", str(config_path), "--records", str(tmp_path / "r.json"),
        "--auth-state", str(tmp_path / "auth.json"),
        "submit", "2026-07",
    ])
    assert result.exit_code != 0
    assert "time-entry login" in result.output


def test_submit_is_dry_run_without_yes(tmp_path, xdg, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(GOOD_TOML)
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("{}")

    def fail_run(_coroutine):
        pytest.fail("dry-run must not open the browser")

    monkeypatch.setattr(te.asyncio, "run", fail_run)
    result = CliRunner().invoke(te.main, [
        "--config", str(config_path), "--records", str(tmp_path / "r.json"),
        "--auth-state", str(auth_path), "submit", "2026-07",
    ])

    assert result.exit_code == 0
    assert "Dry-run" in result.output
    assert "review and submit" in result.output
    assert "--yes" in result.output


def test_submit_yes_runs_review_submit_flow(tmp_path, xdg, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(GOOD_TOML)
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("{}")
    calls = []

    async def fake_do_submit(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(te, "_do_submit", fake_do_submit)
    result = CliRunner().invoke(te.main, [
        "--config", str(config_path), "--records", str(tmp_path / "r.json"),
        "--auth-state", str(auth_path), "submit", "2026-07", "--yes",
    ])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][0][:4] == ("https://example.com/time", auth_path, 2026, 7)
    assert calls[0][1] == {"headless": False}
