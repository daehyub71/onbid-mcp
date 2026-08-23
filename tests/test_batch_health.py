"""배치 건강 점검 테스트 (AC14).

**자동 배치는 조용히 멈춘다.** cron 이 걸러지거나 워크플로가 비활성화돼도 아무 일도 일어나지
않고, 데이터만 서서히 낡는다. 그래서 "며칠에 빠짐이 있는가" 를 세는 규칙이 필요하다.

기대 배치는 요일로 갈린다 — **일요일은 전량, 나머지는 증분**이다 (F8.1·F8.2). 이걸 뒤집어
판정하면 매주 일요일이 실패로 잡힌다.
"""

from datetime import date

from core.ops.health import DayCheck, check_window, expected_mode


def run(day: str, mode: str, status: str = "ok") -> dict[str, str]:
    return {"kst_date": day, "mode": mode, "status": status}


# ── 요일별 기대 배치 ───────────────────────────────────────────────────


def test_sunday_expects_a_full_scan() -> None:
    """tombstone 판정 기회가 일요일뿐이라 이 날은 전량이어야 한다 (F8.2)."""
    assert expected_mode(date(2026, 8, 23)) == "full"


def test_weekdays_expect_a_delta_scan() -> None:
    assert expected_mode(date(2026, 8, 24)) == "delta"
    assert expected_mode(date(2026, 8, 29)) == "delta"


# ── 정상 ───────────────────────────────────────────────────────────────


def test_window_is_clean_when_every_day_ran() -> None:
    rows = [run("2026-08-24", "delta"), run("2026-08-24", "rounds"),
            run("2026-08-25", "delta"), run("2026-08-25", "rounds")]

    checks = check_window(rows, end=date(2026, 8, 25), days=2)

    assert [c.missing for c in checks] == [False, False]
    assert all(c.ok for c in checks)


def test_rounds_are_expected_every_day() -> None:
    """회차를 빠뜨리면 이력이 며칠씩 낡는다 — 물건만 돌았다고 정상이 아니다."""
    checks = check_window([run("2026-08-24", "delta")], end=date(2026, 8, 24), days=1)

    assert checks[0].missing_rounds is True
    assert checks[0].ok is False


# ── 빠짐·실패 ──────────────────────────────────────────────────────────


def test_missing_day_is_reported() -> None:
    """cron 이 걸러진 날. 아무 흔적이 없으므로 날짜를 세어야만 드러난다."""
    rows = [run("2026-08-24", "delta"), run("2026-08-24", "rounds")]

    checks = check_window(rows, end=date(2026, 8, 25), days=2)

    assert checks[0].day == date(2026, 8, 24) and checks[0].ok
    assert checks[1].day == date(2026, 8, 25) and checks[1].missing


def test_failed_run_is_not_counted_as_ran() -> None:
    """`failed` 는 돈 것이 아니다. 상태를 안 보면 매일 실패해도 초록으로 보인다."""
    rows = [run("2026-08-24", "delta", "failed"), run("2026-08-24", "rounds")]

    checks = check_window(rows, end=date(2026, 8, 24), days=1)

    assert checks[0].ok is False
    assert checks[0].missing is True


def test_partial_run_counts_as_ran_but_is_flagged() -> None:
    """`partial` 은 데이터를 남겼으니 빠짐은 아니다 — 다만 눈에 띄어야 한다."""
    rows = [run("2026-08-24", "delta", "partial"), run("2026-08-24", "rounds")]

    checks = check_window(rows, end=date(2026, 8, 24), days=1)

    assert checks[0].missing is False
    assert checks[0].partial is True
    assert checks[0].ok is False


def test_wrong_mode_on_sunday_is_missing() -> None:
    """일요일에 증분만 돌면 그 주의 tombstone 판정이 통째로 빠진다."""
    rows = [run("2026-08-23", "delta"), run("2026-08-23", "rounds")]

    checks = check_window(rows, end=date(2026, 8, 23), days=1)

    assert checks[0].missing is True


def test_manual_full_scan_on_a_weekday_is_acceptable() -> None:
    """평일에 전량을 돌리는 것은 증분보다 강한 수집이다 — 빠짐으로 보지 않는다."""
    rows = [run("2026-08-24", "full"), run("2026-08-24", "rounds")]

    assert check_window(rows, end=date(2026, 8, 24), days=1)[0].ok


# ── 요약 ───────────────────────────────────────────────────────────────


def test_window_covers_the_requested_number_of_days() -> None:
    checks = check_window([], end=date(2026, 8, 25), days=7)

    assert len(checks) == 7
    assert checks[0].day == date(2026, 8, 19)
    assert checks[-1].day == date(2026, 8, 25)


def test_check_renders_a_short_label() -> None:
    """터미널에서 한 줄로 읽혀야 매일 확인하게 된다."""
    check = DayCheck(day=date(2026, 8, 23), expected="full",
                     ran={"full", "rounds"}, statuses={"ok"})

    assert "2026-08-23" in check.label()
    assert "일" in check.label()
