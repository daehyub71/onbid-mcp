"""배치 건강 점검 (AC14·F8.1·F8.2).

**자동 배치는 조용히 멈춘다.** cron 이 걸러지거나 워크플로가 비활성화돼도 오류 하나 나지
않고 데이터만 서서히 낡는다. 실패 알림은 *실행됐는데 실패한* 경우만 알려주므로,
**실행 자체가 없었던 날**은 날짜를 세어야만 드러난다.

기대 배치는 요일로 갈린다 — **일요일은 전량, 나머지는 증분**이다. 뒤집어 판정하면 매주
일요일이 실패로 잡힌다. 회차는 매일이다.

판정 규칙 세 가지:

- `failed` 는 **돈 것이 아니다** — 상태를 안 보면 매일 실패해도 초록으로 보인다.
- `partial` 은 데이터를 남겼으니 빠짐은 아니지만 눈에 띄어야 한다.
- 평일의 전량 수집은 증분보다 **강한** 수집이라 빠짐으로 보지 않는다.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Final

SUNDAY: Final = 6
"""`date.weekday()` 기준 일요일."""

FULL: Final = "full"
DELTA: Final = "delta"
ROUNDS: Final = "rounds"

#: 전량은 증분을 대신할 수 있다. 반대는 안 된다 — tombstone 판정이 빠진다.
SATISFIES: Final = {DELTA: {DELTA, FULL}, FULL: {FULL}}

RAN_STATUSES: Final = frozenset({"ok", "partial"})
"""'돌았다' 로 치는 상태. `failed` 는 제외한다."""

WEEKDAY_NAMES: Final = ("월", "화", "수", "목", "금", "토", "일")


def expected_mode(day: date) -> str:
    """그 날 돌아야 할 물건 배치 모드.

    Args:
        day: KST 기준 날짜.

    Returns:
        ``full`` (일요일) 또는 ``delta``.
    """
    return FULL if day.weekday() == SUNDAY else DELTA


@dataclass(frozen=True, slots=True)
class DayCheck:
    """하루 판정 결과.

    Attributes:
        day: KST 날짜.
        expected: 그 날 기대한 물건 배치 모드.
        ran: 그 날 **성공 계열로** 실행된 모드들.
        statuses: 관측된 상태 값들.
    """

    day: date
    expected: str
    ran: set[str] = field(default_factory=set)
    statuses: set[str] = field(default_factory=set)

    @property
    def missing(self) -> bool:
        """기대한 물건 배치가 없었는지 여부."""
        return not (self.ran & SATISFIES[self.expected])

    @property
    def missing_rounds(self) -> bool:
        """회차 배치가 없었는지 여부. 빠지면 이력이 며칠씩 낡는다."""
        return ROUNDS not in self.ran

    @property
    def partial(self) -> bool:
        """중단된 배치가 섞여 있는지 여부."""
        return "partial" in self.statuses

    @property
    def ok(self) -> bool:
        """그 날이 온전한지 여부."""
        return not (self.missing or self.missing_rounds or self.partial)

    def label(self) -> str:
        """터미널 한 줄 — 매일 확인하려면 짧아야 한다."""
        mark = "✅" if self.ok else ("❌" if self.missing else "⚠️")
        notes = []
        if self.missing:
            notes.append(f"{self.expected} 없음")
        if self.missing_rounds:
            notes.append("회차 없음")
        if self.partial:
            notes.append("partial")
        detail = " · ".join(notes) if notes else " · ".join(sorted(self.ran))
        return f"{mark} {self.day} ({WEEKDAY_NAMES[self.day.weekday()]}) {detail}"


def check_window(
    runs: Iterable[Mapping[str, Any]], *, end: date, days: int = 7
) -> Sequence[DayCheck]:
    """최근 며칠의 실행 이력을 날짜별로 판정한다.

    Args:
        runs: `onbid_batch_run` 행들. ``kst_date``(YYYY-MM-DD)·``mode``·``status`` 를 갖는다.
        end: 마지막 날 (포함).
        days: 살펴볼 일수.

    Returns:
        오래된 날부터 정렬된 판정 결과.
    """
    window = [end - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    ran: dict[date, set[str]] = {day: set() for day in window}
    seen: dict[date, set[str]] = {day: set() for day in window}

    for row in runs:
        day = date.fromisoformat(str(row["kst_date"]))
        if day not in ran:
            continue
        status = str(row.get("status") or "")
        seen[day].add(status)
        if status in RAN_STATUSES:
            ran[day].add(str(row["mode"]))

    return [
        DayCheck(day=day, expected=expected_mode(day), ran=ran[day], statuses=seen[day])
        for day in window
    ]
