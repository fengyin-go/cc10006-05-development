"""可控时钟：业务时间统一从这里取，测试里可以冻结或拨快时间验证委托到期等场景。"""
from __future__ import annotations

from datetime import datetime


class Clock:
    def __init__(self) -> None:
        self._frozen: datetime | None = None

    def now(self) -> datetime:
        return self._frozen or datetime.now()

    def now_iso(self) -> str:
        return self.now().isoformat(timespec="seconds")

    def freeze(self, value: datetime | str) -> None:
        self._frozen = value if isinstance(value, datetime) else self.parse(value)

    def reset(self) -> None:
        self._frozen = None

    @staticmethod
    def parse(value: str) -> datetime:
        return datetime.fromisoformat(value)


clock = Clock()
