import datetime as dt

from gateway_hooks import GatewayConfig
from gateway_hooks.core.calendar import Calendar
from gateway_hooks.core.config import WindowSpec
from helpers import *  # noqa: F403  # shared fixtures-as-functions, see tests/helpers.py


def test_calendar_peak_holiday_and_makeup_day():
    cal = Calendar(GatewayConfig.model_validate({
        "peak_hours": {"holidays": ["2026-03-03"], "extra_workdays": ["2026-03-07"]}}).peak_hours)
    assert cal.period_at(ts(2026, 3, 2, 9)).period == "peak"
    assert cal.period_at(ts(2026, 3, 2, 8, 59, 59)).period == "off_peak"
    assert cal.period_at(ts(2026, 3, 2, 18)).period == "off_peak"
    assert cal.period_at(ts(2026, 3, 3, 12)).period == "off_peak"  # holiday
    assert cal.period_at(ts(2026, 3, 7, 12)).period == "peak"      # make-up Saturday
    # off-peak from Monday 18:00 through the Tuesday holiday until Wednesday 09:00
    p = cal.period_at(ts(2026, 3, 3, 12))
    assert (p.start, p.end) == (dt.datetime(2026, 3, 2, 18, tzinfo=TPE), dt.datetime(2026, 3, 4, 9, tzinfo=TPE))


def test_weekend_off_peak_spans_friday_evening_to_monday_morning():
    cal = Calendar(GatewayConfig().peak_hours)
    p = cal.period_at(ts(2026, 3, 7, 12))
    assert (p.start, p.end) == (dt.datetime(2026, 3, 6, 18, tzinfo=TPE), dt.datetime(2026, 3, 9, 9, tzinfo=TPE))


def test_fixed_window_anchors():
    cfg = GatewayConfig()
    cal = Calendar(cfg.peak_hours)
    four_h = WindowSpec(seconds=14400, mode="fixed", anchor="period_start")
    t = ts(2026, 3, 2, 17, 30)
    w = cal.fixed_window(t, four_h, cal.period_at(t))
    assert (w.start.hour, w.end.hour) == (17, 18)  # 09-13, 13-17, 17-18: cut at period end
    t = ts(2026, 3, 3, 2)
    w = cal.fixed_window(t, four_h, cal.period_at(t))
    assert (w.start, w.end) == (dt.datetime(2026, 3, 3, 2, tzinfo=TPE), dt.datetime(2026, 3, 3, 6, tzinfo=TPE))
    w1h = cal.fixed_window(ts(2026, 3, 2, 10, 40), cfg.windows["1h"], cal.period_at(ts(2026, 3, 2, 10, 40)))
    assert (w1h.start.hour, w1h.end.hour) == (10, 11)
    w1d = cal.fixed_window(t, cfg.windows["1d"], cal.period_at(t))
    assert w1d.start == dt.datetime(2026, 3, 3, tzinfo=TPE)
