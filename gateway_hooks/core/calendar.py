"""Peak / off-peak classification and fixed-window boundaries, in the configured local timezone."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .config import WEEKDAY_NAMES, Period, PeakHours, WindowSpec

_MAX_SCAN_DAYS = 60


@dataclass(frozen=True)
class PeriodInfo:
    period: Period
    start: dt.datetime
    end: dt.datetime


@dataclass(frozen=True)
class FixedWindow:
    start: dt.datetime
    end: dt.datetime


class Calendar:
    def __init__(self, peak: PeakHours):
        self._peak = peak
        self.tz = ZoneInfo(peak.timezone)
        self._weekdays = frozenset(WEEKDAY_NAMES.index(d) for d in peak.weekdays)

    def is_workday(self, day: dt.date) -> bool:
        if day in self._peak.extra_workdays:
            return True
        if day in self._peak.holidays:
            return False
        return day.weekday() in self._weekdays

    def _at(self, day: dt.date, t: dt.time) -> dt.datetime:
        return dt.datetime.combine(day, t, tzinfo=self.tz)

    def local(self, ts: float) -> dt.datetime:
        return dt.datetime.fromtimestamp(ts, tz=self.tz)

    def period_at(self, ts: float) -> PeriodInfo:
        now = self.local(ts)
        today = now.date()
        peak_start, peak_end = self._at(today, self._peak.start), self._at(today, self._peak.end)
        if self.is_workday(today) and peak_start <= now < peak_end:
            return PeriodInfo("peak", peak_start, peak_end)
        return PeriodInfo("off_peak", self._off_peak_start(now), self._off_peak_end(now))

    def _off_peak_start(self, now: dt.datetime) -> dt.datetime:
        day = now.date()
        for _ in range(_MAX_SCAN_DAYS):
            end_of_peak = self._at(day, self._peak.end)
            if self.is_workday(day) and end_of_peak <= now:
                return end_of_peak
            day -= dt.timedelta(days=1)
        return self._at(day, dt.time(0, 0))

    def _off_peak_end(self, now: dt.datetime) -> dt.datetime:
        day = now.date()
        for _ in range(_MAX_SCAN_DAYS):
            start_of_peak = self._at(day, self._peak.start)
            if self.is_workday(day) and start_of_peak > now:
                return start_of_peak
            day += dt.timedelta(days=1)
        return self._at(day, dt.time(0, 0))

    def fixed_window(self, ts: float, spec: WindowSpec, period: PeriodInfo) -> FixedWindow:
        """The fixed window containing `ts`, never extending past the current period or local day."""
        now = self.local(ts)
        length = dt.timedelta(seconds=spec.seconds)
        if spec.anchor == "period_start":
            anchor, hard_end = period.start, period.end
        else:
            anchor = self._at(now.date(), dt.time(0, 0))
            hard_end = self._at(now.date() + dt.timedelta(days=1), dt.time(0, 0))
        n = int((now - anchor) / length)
        start = anchor + n * length
        return FixedWindow(start=start, end=min(start + length, hard_end))
