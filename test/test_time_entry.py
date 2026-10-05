"""Tests for the pure (non-browser) parts of time_entry.

The Workday automation itself needs a real browser and a real login, so the
tests here cover the allocation algorithm, the config/records I/O and the CLI
wiring, including where configuration and state are looked for.
"""

import json
import shlex
import sys
import tomllib
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


def test_load_config_login_defaults(config):
    assert config.login.mode == "headed"
    assert config.login.username == ""
    assert config.login.password_command is None


def test_load_config_login_section(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(GOOD_TOML.replace(
        "[[projects]]",
        '[login]\nmode = "Headless"\nusername = "jdoe"\npassword_command = "helper"\n\n[[projects]]',
        1,
    ))
    config = te.load_config(path)
    assert config.login.mode == "headless"
    assert config.login.username == "jdoe"
    assert config.login.password_command == "helper"


def test_load_config_rejects_bad_login_mode(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(GOOD_TOML.replace(
        "[[projects]]", '[login]\nmode = "invisible"\n\n[[projects]]', 1))
    with pytest.raises(click.ClickException, match="login mode"):
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
    assert "popUpDialog" in te._DIALOG_SELECTORS["submit_dialog"]
    assert "bpf-submit" in te._DIALOG_SELECTORS["submit_button"]


def test_individual_browser_commands_default_headless_and_can_switch(
    tmp_path, xdg, monkeypatch,
):
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
    assert modes == [True, True, False]


def test_inspect_rejects_headless_browser(tmp_path, xdg):
    config_path = tmp_path / "config.toml"
    config_path.write_text(GOOD_TOML)
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("{}")
    result = CliRunner().invoke(te.main, [
        "--config", str(config_path), "--auth-state", str(auth_path),
        "apply", "2026-07", "--inspect",
    ])

    assert result.exit_code != 0
    assert "--inspect requires a visible browser" in result.output


HEADLESS_LOGIN_TOML = GOOD_TOML.replace(
    "[[projects]]",
    '[login]\nmode = "headless"\nusername = "jdoe"\npassword_command = "helper"\n\n[[projects]]',
    1,
)


def test_login_cli_uses_config_mode_and_override(tmp_path, xdg, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(HEADLESS_LOGIN_TOML)
    auth_path = tmp_path / "auth.json"
    calls = []

    async def fake_headed(home_url, auth):
        calls.append(("headed", home_url))

    async def fake_headless(home_url, auth, username, password):
        calls.append(("headless", username, password))

    monkeypatch.setattr(te, "_do_login", fake_headed)
    monkeypatch.setattr(te, "_do_login_headless", fake_headless)
    monkeypatch.setattr(te, "resolve_password", lambda login: "pw")
    runner = CliRunner()
    base = ["--config", str(config_path), "--auth-state", str(auth_path)]

    assert runner.invoke(te.main, [*base, "login"]).exit_code == 0
    assert runner.invoke(te.main, [*base, "login", "--headed"]).exit_code == 0
    assert runner.invoke(te.main, [*base, "login", "--headless"]).exit_code == 0
    assert calls == [
        ("headless", "jdoe", "pw"),
        ("headed", "https://example.com/home"),
        ("headless", "jdoe", "pw"),
    ]


def test_headless_login_requires_username(tmp_path, xdg):
    config_path = tmp_path / "config.toml"
    config_path.write_text(HEADLESS_LOGIN_TOML.replace('username = "jdoe"\n', ""))
    result = CliRunner().invoke(te.main, [
        "--config", str(config_path),
        "--auth-state", str(tmp_path / "auth.json"),
        "login",
    ])
    assert result.exit_code != 0
    assert "username" in result.output


def test_headless_login_prompts_for_password(tmp_path, xdg, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(HEADLESS_LOGIN_TOML.replace('password_command = "helper"\n', ""))
    calls = []

    async def fake_headless(home_url, auth, username, password):
        calls.append((username, password))

    monkeypatch.setattr(te, "_do_login_headless", fake_headless)
    monkeypatch.setattr(te.getpass, "getpass", lambda prompt: "typed-pw")
    result = CliRunner().invoke(te.main, [
        "--config", str(config_path),
        "--auth-state", str(tmp_path / "auth.json"),
        "login",
    ])
    assert result.exit_code == 0, result.output
    assert calls == [("jdoe", "typed-pw")]


def test_myworkday_auth_page_is_not_a_ready_session():
    class HiddenLocator:
        @property
        def first(self):
            return self

        async def is_visible(self):
            return False

        async def count(self):
            return 0

    class AuthPage:
        url = "https://www.myworkday.com/bnl/login-saml2.htmld"

        def locator(self, _selector):
            return HiddenLocator()

    assert not te.asyncio.run(te._workday_session_ready(AuthPage()))


def test_hidden_workday_shell_marker_is_a_ready_session():
    class ShellLocator:
        def __init__(self, selector):
            self.selector = selector

        @property
        def first(self):
            return self

        async def count(self):
            return int(self.selector == te._WORKDAY_READY_SELECTORS[0])

    class WorkdayPage:
        url = "https://www.myworkday.com/bnl/d/pex/home.htmld"

        def locator(self, selector):
            return ShellLocator(selector)

    assert te.asyncio.run(te._workday_session_ready(WorkdayPage()))


def test_workday_home_title_is_a_ready_session_before_shell_renders():
    class EmptyLocator:
        @property
        def first(self):
            return self

        async def count(self):
            return 0

    class WorkdayHomePage:
        url = "https://www.myworkday.com/bnl/d/pex/home.htmld"

        async def title(self):
            return "Home - Workday"

        def locator(self, _selector):
            return EmptyLocator()

    assert te.asyncio.run(te._workday_session_ready(WorkdayHomePage()))


def test_browser_title_hides_authentication_query_values():
    title = "Loading https://duo.example/exit?code=secret&state=also-secret"
    assert te._safe_browser_title(title) == "Loading https://duo.example/exit"
    assert te._safe_browser_title("Remember this device") == "Remember this device"


def test_headless_login_skips_remember_device_before_saving_session(capsys):
    class FakeLocator:
        def __init__(self, page, selector):
            self.page = page
            self.selector = selector

        @property
        def first(self):
            return self

        async def is_visible(self):
            if self.selector in te._WORKDAY_READY_SELECTORS:
                return self.page.ready
            if self.selector == self.page.skip_selector:
                return self.page.remember_device
            return False

        async def count(self):
            if self.selector in te._WORKDAY_READY_SELECTORS:
                return int(self.page.ready)
            return 0

        async def click(self, timeout):
            assert timeout == 5_000
            self.page.clicked.append(self.selector)
            self.page.remember_device = False
            self.page.ready = True

    class RememberDevicePage:
        url = "https://www.myworkday.com/bnl/login-saml2.htmld"
        ready = False
        remember_device = True
        skip_selector = te._WORKDAY_ACCOUNTS_SKIP_SELECTOR

        def __init__(self):
            self.clicked = []

        def locator(self, selector):
            return FakeLocator(self, selector)

        async def title(self):
            return "Remember this device - Workday Accounts"

        async def wait_for_load_state(self, state, timeout):
            assert state == "networkidle"
            assert timeout == 10_000

        async def wait_for_timeout(self, _timeout):
            raise AssertionError("ready state should follow the Skip click")

    page = RememberDevicePage()
    assert te.asyncio.run(te._wait_for_workday_session(page, timeout_s=4))
    assert page.clicked == [page.skip_selector]
    assert "Remember Device?" in capsys.readouterr().out


def test_headless_login_skips_consecutive_remember_device_pages(capsys):
    class FakeLocator:
        def __init__(self, page, selector):
            self.page = page
            self.selector = selector

        @property
        def first(self):
            return self

        async def is_visible(self):
            expected = [
                te._WORKDAY_ACCOUNTS_SKIP_SELECTOR,
                te._REMEMBER_DEVICE_SKIP_SELECTORS[0],
            ]
            return (
                self.page.prompt_index < 2
                and self.selector == expected[self.page.prompt_index]
            )

        async def count(self):
            return int(
                self.selector in te._WORKDAY_READY_SELECTORS
                and self.page.prompt_index == 2
            )

        async def click(self, timeout):
            assert timeout == 5_000
            self.page.clicks += 1
            self.page.prompt_index += 1
            urls = [
                "https://www.myworkday.com/wday/authgwy/bnl/login.htmld",
                "https://www.myworkday.com/bnl/d/pex/home.htmld",
            ]
            self.page.url = urls[self.page.prompt_index - 1]

    class TwoPromptPage:
        url = "https://wd1-identity.myworkday.com/prompt-0"
        prompt_index = 0
        clicks = 0

        def locator(self, selector):
            return FakeLocator(self, selector)

        async def title(self):
            return [
                "Remember this device - Workday Accounts",
                "Workday bnl",
                "Home - Workday",
            ][self.prompt_index]

        async def wait_for_load_state(self, state, timeout):
            assert state == "networkidle"
            assert timeout == 10_000

        async def wait_for_timeout(self, _timeout):
            raise AssertionError("both prompts should be handled immediately")

    page = TwoPromptPage()
    assert te.asyncio.run(te._wait_for_workday_session(page, timeout_s=8))
    assert page.clicks == 2
    assert capsys.readouterr().out.count("Remember Device?") == 2


def test_headless_login_recovers_when_skip_click_races_with_navigation(capsys):
    class RacingLocator:
        @property
        def first(self):
            return self

        async def count(self):
            return 0

        async def is_visible(self):
            return True

        async def click(self, timeout):
            assert timeout == 5_000
            raise TimeoutError

    class RacingPage:
        url = "https://wd1-identity.myworkday.com/pending=trust"

        def __init__(self):
            self.waits = []

        def locator(self, _selector):
            return RacingLocator()

        async def title(self):
            return "Remember this device - Workday Accounts"

        async def wait_for_timeout(self, timeout):
            self.waits.append(timeout)

    page = RacingPage()
    assert not te.asyncio.run(te._wait_for_workday_session(page, timeout_s=2))
    assert page.waits == [2000]
    assert "retrying" in capsys.readouterr().out


def test_workflow_cli_threads_headless_and_optional_month(tmp_path, xdg, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(GOOD_TOML)
    calls = []

    def fake_cmd_workflow(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(te, "cmd_workflow", fake_cmd_workflow)
    base = [
        "--config", str(config_path), "--records", str(tmp_path / "records.json"),
        "--auth-state", str(tmp_path / "auth.json"),
    ]
    result = CliRunner().invoke(te.main, [*base, "workflow"])
    assert result.exit_code == 0, result.output

    result = CliRunner().invoke(te.main, [*base, "--headless", "workflow"])
    assert result.exit_code == 0, result.output

    result = CliRunner().invoke(te.main, [*base, "--headed", "workflow"])

    assert result.exit_code == 0, result.output
    assert all(call[0][0] is None for call in calls)
    assert [call[1] for call in calls] == [
        {"headless": False},
        {"headless": True},
        {"headless": False},
    ]

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


def test_workflow_errors_after_three_apply_attempts(
    tmp_path, config, monkeypatch, capsys,
):
    changes = [_workflow_change(1)]
    events, prompts = _mock_workflow_steps(
        monkeypatch,
        [changes, changes, changes, changes],
        [True],
    )
    with pytest.raises(click.ClickException, match="did not converge after 3 apply attempts"):
        te.cmd_workflow(
            "2026-07",
            tmp_path / "auth.json",
            config,
            te.Records(fiscal_year=2026),
            tmp_path / "records.json",
        )

    assert events == [
        "login", "get", "plan", "diff",
        "apply", "diff", "apply", "diff", "apply", "diff",
    ]
    assert prompts == [("Do you want to apply this?", False)]
    output = capsys.readouterr().out
    assert "Apply attempt 1/3" in output
    assert "Apply attempt 3/3" in output


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

        async def click(self, **kwargs):
            events.append(("click", self.action, kwargs))

    class FakePage:
        def locator(self, selector):
            if selector == te._DIALOG_SELECTORS["review_button"]:
                return FakeLocator("review")
            if selector == te._DIALOG_SELECTORS["submit_button"]:
                return FakeLocator("submit")
            if selector == te._DIALOG_SELECTORS["submit_dialog"]:
                return FakeLocator("submit-dialog")
            raise AssertionError(f"unexpected selector: {selector}")

        async def wait_for_load_state(self, state):
            events.append(("load", state))

    te.asyncio.run(te._review_and_submit(FakePage()))

    clicks = [event[1] for event in events if event[0] == "click"]
    assert clicks == ["review", "submit"]
    assert ("wait", "submit-dialog") in events


def test_submit_force_clicks_fixed_action_bar_after_normal_click_fails():
    submit_clicks = []

    class FakeLocator:
        def __init__(self, action):
            self.action = action

        @property
        def first(self):
            return self

        async def wait_for(self, **_kwargs):
            return None

        async def click(self, **kwargs):
            if self.action == "submit":
                submit_clicks.append(kwargs)
                if not kwargs.get("force"):
                    raise TimeoutError

    class FakePage:
        def locator(self, selector):
            mapping = {
                te._DIALOG_SELECTORS["review_button"]: "review",
                te._DIALOG_SELECTORS["submit_dialog"]: "submit-dialog",
                te._DIALOG_SELECTORS["submit_button"]: "submit",
            }
            return FakeLocator(mapping[selector])

        async def wait_for_load_state(self, _state):
            return None

    te.asyncio.run(te._review_and_submit(FakePage()))
    assert submit_clicks == [
        {"timeout": 10_000},
        {"force": True, "timeout": 5_000},
    ]


def test_submit_is_not_reported_successful_while_dialog_remains_open():
    class FakeLocator:
        def __init__(self, action):
            self.action = action

        @property
        def first(self):
            return self

        async def wait_for(self, state, **_kwargs):
            if self.action == "submit-dialog" and state == "hidden":
                raise TimeoutError

        async def click(self, **_kwargs):
            return None

    class FakePage:
        def locator(self, selector):
            mapping = {
                te._DIALOG_SELECTORS["review_button"]: "review",
                te._DIALOG_SELECTORS["submit_dialog"]: "submit-dialog",
                te._DIALOG_SELECTORS["submit_button"]: "submit",
            }
            return FakeLocator(mapping[selector])

        async def wait_for_load_state(self, _state):
            return None

    with pytest.raises(click.ClickException, match="could not be confirmed"):
        te.asyncio.run(te._review_and_submit(FakePage()))


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
    assert calls[0][1] == {"headless": True}


@pytest.mark.parametrize("value", ['""', '"  "', "42", "false", "[]"])
def test_reject_invalid_password_command_config(tmp_path, value):
    path = tmp_path / "config.toml"
    path.write_text(GOOD_TOML.replace(
        "[[projects]]", f"[login]\npassword_command = {value}\n[[projects]]", 1,
    ))
    with pytest.raises(click.ClickException, match="non-empty string"):
        te.load_config(path)


def test_reject_plaintext_password_without_exposing_it(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(HEADLESS_LOGIN_TOML.replace(
        'password_command = "helper"', 'password = "secret-password"',
    ))
    with pytest.raises(click.ClickException, match="Remove it") as excinfo:
        te.load_config(path)
    assert "secret-password" not in str(excinfo.value)


def password_helper(tmp_path, source):
    path = tmp_path / "password helper.py"
    path.write_text(source)
    return shlex.join([sys.executable, str(path)])


@pytest.mark.parametrize("ending", ["", "\n", "\r\nextra metadata\n"])
def test_password_command_preserves_spaces_and_reads_first_line(tmp_path, ending):
    value = "  secret $word  "
    command = password_helper(tmp_path, f"import sys; sys.stdout.write({value + ending!r})")
    assert te.resolve_password(te.LoginConfig(password_command=command)) == value


@pytest.mark.parametrize("source, message", [
    ("", "empty password"),
    ('print("\\nmetadata")', "empty password"),
    ('import sys; sys.stdout.buffer.write(bytes([255]))', "UTF-8"),
    ('import sys; print("secret"); print("secret", file=sys.stderr); sys.exit(7)', "exit status 7"),
])
def test_password_command_failure_hides_output(tmp_path, capsys, source, message):
    command = password_helper(tmp_path, source)
    with pytest.raises(click.ClickException, match=message) as excinfo:
        te.resolve_password(te.LoginConfig(password_command=command))
    assert "secret" not in str(excinfo.value)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("command, message", [
    ("", "must not be empty"),
    ("'secret", "Invalid quoting"),
    ("/nonexistent/time-entry-password-helper", "Unable to execute"),
])
def test_password_command_invalid_execution(command, message):
    with pytest.raises(click.ClickException, match=message):
        te.resolve_password(te.LoginConfig(password_command=command))


@pytest.mark.parametrize("override", [False, True])
def test_login_password_command_cli(tmp_path, xdg, monkeypatch, override):
    command = password_helper(tmp_path, 'print("command-password")')
    config_path = tmp_path / "config.toml"
    config_path.write_text(HEADLESS_LOGIN_TOML.replace(
        '"helper"', '"unused-command"' if override else json.dumps(command),
    ))
    calls = []

    async def fake_headless(home_url, auth, username, password):
        calls.append(password)

    monkeypatch.setattr(te, "_do_login_headless", fake_headless)
    args = ["--config", str(config_path)]
    if override:
        args += ["--password-command", command]
    result = CliRunner().invoke(te.main, [*args, "login"])
    assert result.exit_code == 0, result.output
    assert calls == ["command-password"]
    assert "command-password" not in result.output


def test_headed_login_does_not_run_password_command(tmp_path, config, monkeypatch):
    config.login.password_command = "/nonexistent/helper"
    calls = []

    async def fake_headed(*args):
        calls.append(True)

    monkeypatch.setattr(te, "_do_login", fake_headed)
    te.cmd_login(tmp_path / "auth.json", config)
    assert calls == [True]


def test_workflow_password_command_override(tmp_path, xdg, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(HEADLESS_LOGIN_TOML)
    calls = []

    def fake_workflow(month, auth, config, *args, **kwargs):
        calls.append(config.login.password_command)

    monkeypatch.setattr(te, "cmd_workflow", fake_workflow)
    result = CliRunner().invoke(te.main, [
        "--config", str(path), "--password-command", "override-helper", "workflow",
    ])
    assert result.exit_code == 0, result.output
    assert calls == ["override-helper"]


# ---------------------------------------------------------------------------
# Laboratory holidays
# ---------------------------------------------------------------------------

def _holiday_table(year, rows):
    body = "".join(
        f"<tr>\n<td style='width:50%;'>{name}</td>\n<td style='width:50%;'>{when}</td>\n</tr>\n"
        for name, when in rows
    )
    return (
        f"<table class='HolidayTable noBorders'>\n<tr>\n<th colspan='2'>{year} Holidays</th>\n"
        f"</tr>\n{body}</table>\n"
    )


HOLIDAYS_2026 = _holiday_table(2026, [
    ("New Year's Day", "Thursday, January 1, 2026"),
    ("Juneteenth", "Friday, June 19, 2026"),
    ("Christmas Eve (floating holiday)", "Thursday, December 24, 2026"),
])
HOLIDAYS_2027 = _holiday_table(2027, [
    ("New Year&#039;s Day", "Friday, January 1, 2027"),
    ("Juneteenth (observed)", "Friday, June 18, 2027"),
])


def _holidays_page(*tables):
    return (
        "<html><body><h1>Calendar Year 2026</h1><p>Twelve days, January 1, 2026.</p>"
        + "".join(tables) + "</body></html>"
    )


def test_parse_bnl_holidays_single_year():
    got = te.parse_bnl_holidays(_holidays_page(HOLIDAYS_2026))
    assert got == [
        (date(2026, 1, 1), "New Year's Day"),
        (date(2026, 6, 19), "Juneteenth"),
        (date(2026, 12, 24), "Christmas Eve (floating holiday)"),
    ]


def test_parse_bnl_holidays_two_years():
    got = te.parse_bnl_holidays(_holidays_page(HOLIDAYS_2027, HOLIDAYS_2026))
    assert [d for d, _ in got] == [
        date(2026, 1, 1), date(2026, 6, 19), date(2026, 12, 24),
        date(2027, 1, 1), date(2027, 6, 18),
    ]
    assert got[3][1] == "New Year's Day"


def test_parse_bnl_holidays_none():
    assert te.parse_bnl_holidays("<html><table><tr><td>x</td></tr></table></html>") == []


HOLIDAYS = [(date(2026, 6, 19), "Juneteenth"), (date(2026, 7, 3), "Independence Day")]


def test_update_days_off_appends_and_keeps_comments():
    text = 'fiscal_year = 2026\ndays_off = [\n  "2026-07-03",  # vacation\n  "2026-08-10"  # trip\n]\n\n[[projects]]\ncode = "A"\n'
    new, added = te.update_days_off_text(text, HOLIDAYS)
    assert added == [(date(2026, 6, 19), "Juneteenth")]
    assert '"2026-07-03",  # vacation\n  "2026-08-10",  # trip\n  "2026-06-19",  # Juneteenth\n]\n' in new
    assert new.endswith('[[projects]]\ncode = "A"\n')


@pytest.mark.parametrize("array", ['["2026-07-03"]', '[ "2026-07-03", ]', "[]", "[\n]"])
def test_update_days_off_array_shapes(array):
    text = f"fiscal_year = 2026\ndays_off = {array}\n[workday]\nhome_url = 'x'\n"
    new, _ = te.update_days_off_text(text, HOLIDAYS)
    raw = tomllib.loads(new)
    assert set(raw["days_off"]) == {"2026-06-19", "2026-07-03"}
    assert raw["workday"] == {"home_url": "x"}


def test_update_days_off_inserts_missing_array_before_headers():
    text = "fiscal_year = 2026\n\n[workday]\nhome_url = 'x'\n"
    new, added = te.update_days_off_text(text, HOLIDAYS)
    assert len(added) == 2
    assert new.index("days_off") < new.index("[workday]")


def test_update_days_off_no_change_when_present():
    text = 'days_off = ["2026-06-19", "2026-07-03"]\n'
    assert te.update_days_off_text(text, HOLIDAYS) == (text, [])


def test_update_days_off_works_with_template_placeholders():
    new, added = te.update_days_off_text(te.TEMPLATE_TOML, HOLIDAYS)
    assert added == [(date(2026, 6, 19), "Juneteenth")]
    assert new.count('"2026-07-03"') == 1
    assert "pct  = XX" in new


def test_holidays_cli_dry_run_and_apply(tmp_path, xdg):
    page = tmp_path / "holidays.html"
    page.write_text(_holidays_page(HOLIDAYS_2026, HOLIDAYS_2027))
    cfg = tmp_path / "config.toml"
    cfg.write_text(GOOD_TOML)
    base = ["--config", str(cfg)]

    result = CliRunner().invoke(te.main, [*base, "--dry-run", "holidays", "--file", str(page)])
    assert result.exit_code == 0, result.output
    assert "Found 5 holidays for 2026, 2027" in result.output
    assert cfg.read_text() == GOOD_TOML

    result = CliRunner().invoke(
        te.main, [*base, "holidays", "--file", str(page), "--year", "2027"])
    assert result.exit_code == 0, result.output
    config = te.load_config(cfg)
    assert config.days_off == {date(2026, 7, 3), date(2027, 1, 1), date(2027, 6, 18)}

    result = CliRunner().invoke(te.main, [*base, "holidays", "--file", str(page)])
    assert result.exit_code == 0, result.output
    assert len(te.load_config(cfg).days_off) == 6


def test_holidays_cli_requires_config(tmp_path, xdg):
    page = tmp_path / "holidays.html"
    page.write_text(_holidays_page(HOLIDAYS_2026))
    result = CliRunner().invoke(
        te.main, ["--config", str(tmp_path / "none.toml"), "holidays", "--file", str(page)])
    assert result.exit_code != 0
    assert "time-entry init" in result.output
