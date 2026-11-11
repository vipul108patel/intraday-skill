"""NSE market calendar and IST helpers (Indian adaptation of day-trader-skill clocks)."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# NSE holidays 2025–2026 (common list; update yearly from NSE circulars)
NSE_HOLIDAYS = {
    "2025-02-26": "Mahashivratri",
    "2025-03-14": "Holi",
    "2025-03-31": "Id-Ul-Fitr",
    "2025-04-10": "Mahavir Jayanti",
    "2025-04-14": "Dr. Ambedkar Jayanti",
    "2025-04-18": "Good Friday",
    "2025-05-01": "Maharashtra Day",
    "2025-08-15": "Independence Day",
    "2025-08-27": "Ganesh Chaturthi",
    "2025-10-02": "Mahatma Gandhi Jayanti",
    "2025-10-21": "Dussehra",
    "2025-10-22": "Diwali Laxmi Pujan",
    "2025-11-05": "Guru Nanak Jayanti",
    "2025-12-25": "Christmas",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Ram Navami",
    "2026-03-31": "Id-Ul-Fitr",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-08-15": "Independence Day",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-20": "Dussehra",
    # 2026-11-08 Diwali Laxmi Pujan is Sunday — special Muhurat session only (not a full holiday block for Mon)
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}

OPEN = dt.time(9, 15)
CLOSE = dt.time(15, 30)


def now_ist(pin: dt.datetime | None = None) -> dt.datetime:
    if pin is not None:
        if pin.tzinfo is None:
            return pin.replace(tzinfo=IST)
        return pin.astimezone(IST)
    return dt.datetime.now(IST).replace(microsecond=0)


def parse_ist(text: str) -> dt.datetime:
    """Parse 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD HH:MM:SS' as IST."""
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            naive = dt.datetime.strptime(text, fmt)
            return naive.replace(tzinfo=IST)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse IST datetime: {text}")


def parse_hhmm(text: str) -> dt.time:
    h, m = text.split(":")
    return dt.time(int(h), int(m))


def is_holiday(day: dt.date) -> bool:
    return day.isoformat() in NSE_HOLIDAYS


def holiday_name(day: dt.date) -> str | None:
    return NSE_HOLIDAYS.get(day.isoformat())


def is_trading_day(day: dt.date) -> bool:
    if day.weekday() >= 5:
        return False
    return not is_holiday(day)


def session_times(cfg: dict) -> tuple[dt.time, dt.time]:
    return parse_hhmm(cfg.get("session_open", "09:15")), parse_hhmm(
        cfg.get("session_close", "15:30")
    )


def is_market_open(cfg: dict, when: dt.datetime | None = None) -> bool:
    when = now_ist(when)
    if not is_trading_day(when.date()):
        return False
    open_t, close_t = session_times(cfg)
    return open_t <= when.time() <= close_t


def in_entry_window(cfg: dict, when: dt.datetime | None = None) -> bool:
    when = now_ist(when)
    if not is_trading_day(when.date()):
        return False
    before = parse_hhmm(cfg.get("no_entries_before", "09:30"))
    after = parse_hhmm(cfg.get("no_entries_after", "14:45"))
    return before <= when.time() <= after


def opening_range_end(cfg: dict) -> dt.time:
    open_t = parse_hhmm(cfg.get("session_open", "09:15"))
    minutes = int(cfg.get("opening_range_minutes", 15))
    start = dt.datetime.combine(dt.date.today(), open_t)
    return (start + dt.timedelta(minutes=minutes)).time()


def bars_per_session(cfg: dict, interval_minutes: int = 5) -> int:
    open_t, close_t = session_times(cfg)
    start = dt.datetime.combine(dt.date.today(), open_t)
    end = dt.datetime.combine(dt.date.today(), close_t)
    return int((end - start).total_seconds() // (interval_minutes * 60))


def previous_trading_days(end: dt.date, n: int) -> list[dt.date]:
    days: list[dt.date] = []
    cur = end
    while len(days) < n:
        cur -= dt.timedelta(days=1)
        if is_trading_day(cur):
            days.append(cur)
    return list(reversed(days))