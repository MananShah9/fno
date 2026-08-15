import os
import re
import logging
from datetime import datetime, time, timezone, timedelta
from typing import Optional, Tuple, Union, Any

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

logger = logging.getLogger("time_filter")

# Fixed offset fallback for Indian Standard Time (UTC+05:30)
IST_TZ = timezone(timedelta(hours=5, minutes=30), name="IST")

def get_configured_timezone(tz_name: Optional[str] = None) -> Union[timezone, Any]:
    """
    Returns tzinfo object for given timezone name, defaulting to Asia/Kolkata (IST).
    Supports standard IANA names (e.g. 'Asia/Kolkata', 'UTC'), abbreviations ('IST'),
    and UTC offset strings (e.g. '+05:30', 'GMT+5:30').
    """
    if not tz_name:
        tz_name = os.getenv("TELEGRAM_TIMEZONE", "Asia/Kolkata")
    
    tz_clean = str(tz_name).strip()
    tz_upper = tz_clean.upper()

    # Fast path for common IST representations
    if tz_upper in ("IST", "ASIA/KOLKATA", "ASIA/CALCUTTA", "+05:30", "+5:30", "GMT+5:30", "UTC+5:30"):
        if ZoneInfo:
            try:
                return ZoneInfo("Asia/Kolkata")
            except Exception:
                pass
        return IST_TZ

    if tz_upper in ("UTC", "GMT", "Z", "+00:00", "+0", "-0"):
        return timezone.utc

    # Try standard IANA name via ZoneInfo
    if ZoneInfo:
        try:
            return ZoneInfo(tz_clean)
        except Exception:
            pass

    # Try parsing +/-HH:MM or +/-HHMM offset
    offset_match = re.match(r"^([+-])(\d{1,2}):?(\d{2})?$", tz_clean)
    if offset_match:
        sign, hours, mins = offset_match.groups()
        mins_val = int(mins) if mins else 0
        total_mins = int(hours) * 60 + mins_val
        if sign == "-":
            total_mins = -total_mins
        return timezone(timedelta(minutes=total_mins))

    return IST_TZ


def parse_time_str(time_str: Optional[str], default: time) -> time:
    """
    Parses various time string formats into a datetime.time object.
    Supports:
      - 24-hour: '08:30', '8:30', '16:30', '16:30:00'
      - 12-hour AM/PM: '8:30 AM', '08:30am', '4:30 PM', '04:30pm', '4pm', '8am'
      - Suffixes like 'IST', 'UTC'
    """
    if not time_str:
        return default

    s = str(time_str).strip()
    if not s:
        return default

    # Strip extraneous timezone labels if user wrote e.g. "8:30 ist" or "4:30 pm IST"
    s = re.sub(r"\s*(IST|UTC|GMT|[+-]\d{1,2}:?\d{2})\s*$", "", s, flags=re.IGNORECASE).strip()

    # If seconds were not explicitly provided in string, we can track that
    has_explicit_seconds = (len(re.findall(r":", s)) >= 2)

    # 1. Try 12-hour AM/PM formats
    for fmt in ("%I:%M:%S %p", "%I:%M:%S%p", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p"):
        try:
            parsed = datetime.strptime(s.upper(), fmt).time()
            if not has_explicit_seconds and "default_end" in repr(default):
                # Will be handled if caller requested end time
                pass
            return parsed
        except ValueError:
            pass

    # 2. Try 24-hour formats
    for fmt in ("%H:%M:%S", "%H:%M", "%H"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            pass

    # 3. Fallback regex for H:M or HH:MM or HH:MM:SS
    m = re.match(r"^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$", s)
    if m:
        h, m_val, s_val = m.groups()
        hour = int(h)
        minute = int(m_val)
        second = int(s_val) if s_val is not None else 0
        if 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59:
            return time(hour, minute, second)

    # Single hour fallback e.g. "8" or "16"
    if s.isdigit():
        hour = int(s)
        if 0 <= hour <= 23:
            return time(hour, 0, 0)

    logger.warning(f"Could not parse time string '{time_str}'. Defaulting to {default}.")
    return default


def parse_end_time_str(time_str: Optional[str], default: time = time(16, 30, 59)) -> time:
    """
    Parses end time string. If seconds are not specified (e.g. '16:30' or '4:30 PM'),
    defaults seconds to 59 and microseconds to 999999 so that the entire end minute is inclusive.
    """
    if not time_str:
        return default

    s = str(time_str).strip()
    has_explicit_seconds = (len(re.findall(r":", s)) >= 2)
    parsed = parse_time_str(time_str, default=default)

    if not has_explicit_seconds and parsed.second == 0 and parsed.microsecond == 0:
        return parsed.replace(second=59, microsecond=999999)

    return parsed


def is_telegram_time_active(
    dt: Optional[datetime] = None,
    config_dict: Optional[dict] = None
) -> Tuple[bool, str]:
    """
    Checks if a given datetime (or the current time if dt is None) falls within
    the configured Telegram allowed time window and weekday schedule.

    Returns:
        (is_active: bool, reason: str)
    """
    if config_dict is None:
        filter_enabled = os.getenv("TELEGRAM_TIME_FILTER_ENABLED", "false").lower() in ("true", "1", "t", "yes")
        start_str = os.getenv("TELEGRAM_START_TIME", "08:30")
        end_str = os.getenv("TELEGRAM_END_TIME", "16:30")
        weekdays_only = os.getenv("TELEGRAM_WEEKDAYS_ONLY", "true").lower() in ("true", "1", "t", "yes")
        tz_name = os.getenv("TELEGRAM_TIMEZONE", "Asia/Kolkata")
    else:
        raw_enabled = config_dict.get("TELEGRAM_TIME_FILTER_ENABLED", False)
        if isinstance(raw_enabled, str):
            filter_enabled = raw_enabled.lower() in ("true", "1", "t", "yes")
        else:
            filter_enabled = bool(raw_enabled)

        start_str = config_dict.get("TELEGRAM_START_TIME", "08:30")
        end_str = config_dict.get("TELEGRAM_END_TIME", "16:30")

        raw_weekdays = config_dict.get("TELEGRAM_WEEKDAYS_ONLY", True)
        if isinstance(raw_weekdays, str):
            weekdays_only = raw_weekdays.lower() in ("true", "1", "t", "yes")
        else:
            weekdays_only = bool(raw_weekdays)

        tz_name = config_dict.get("TELEGRAM_TIMEZONE", "Asia/Kolkata")

    # If time restriction is not enabled, allow all messages 24/7
    if not filter_enabled:
        return True, "Telegram time filter is disabled (active 24/7)"

    tz = get_configured_timezone(tz_name)
    tz_label = tz_name if tz_name else "IST"

    # Convert dt to target timezone
    if dt is None:
        target_dt = datetime.now(tz)
    else:
        if dt.tzinfo is None:
            # Assume UTC if naive datetime was passed
            target_dt = dt.replace(tzinfo=timezone.utc).astimezone(tz)
        else:
            target_dt = dt.astimezone(tz)

    day_name = target_dt.strftime("%A")

    # 1. Weekdays Check (Monday=0, Tuesday=1, ..., Friday=4, Saturday=5, Sunday=6)
    if weekdays_only and target_dt.weekday() >= 5:
        return False, f"Outside active weekdays: today is {day_name} (Weekdays only Mon-Fri enabled)"

    # 2. Time Window Check
    start_t = parse_time_str(start_str, default=time(8, 30, 0))
    end_t = parse_end_time_str(end_str, default=time(16, 30, 59))
    current_t = target_dt.time()

    if start_t <= end_t:
        in_window = (start_t <= current_t <= end_t)
    else:
        # Crosses midnight (e.g. 22:00 to 06:00)
        in_window = (current_t >= start_t or current_t <= end_t)

    if not in_window:
        return False, (
            f"Outside active hours: current time {target_dt.strftime('%H:%M:%S')} {tz_label} "
            f"is outside allowed window {start_t.strftime('%H:%M')} - {end_t.strftime('%H:%M')} {tz_label}"
        )

    return True, f"Within active schedule ({day_name} {target_dt.strftime('%H:%M:%S')} {tz_label})"


def get_schedule_description(config_dict: Optional[dict] = None) -> str:
    """
    Returns a human-readable summary of the active schedule configuration.
    """
    if config_dict is None:
        filter_enabled = os.getenv("TELEGRAM_TIME_FILTER_ENABLED", "false").lower() in ("true", "1", "t", "yes")
        start_str = os.getenv("TELEGRAM_START_TIME", "08:30")
        end_str = os.getenv("TELEGRAM_END_TIME", "16:30")
        weekdays_only = os.getenv("TELEGRAM_WEEKDAYS_ONLY", "true").lower() in ("true", "1", "t", "yes")
        tz_name = os.getenv("TELEGRAM_TIMEZONE", "Asia/Kolkata")
    else:
        raw_enabled = config_dict.get("TELEGRAM_TIME_FILTER_ENABLED", False)
        filter_enabled = raw_enabled.lower() in ("true", "1", "t", "yes") if isinstance(raw_enabled, str) else bool(raw_enabled)
        start_str = config_dict.get("TELEGRAM_START_TIME", "08:30")
        end_str = config_dict.get("TELEGRAM_END_TIME", "16:30")
        raw_weekdays = config_dict.get("TELEGRAM_WEEKDAYS_ONLY", True)
        weekdays_only = raw_weekdays.lower() in ("true", "1", "t", "yes") if isinstance(raw_weekdays, str) else bool(raw_weekdays)
        tz_name = config_dict.get("TELEGRAM_TIMEZONE", "Asia/Kolkata")

    if not filter_enabled:
        return "Disabled (All days, 24/7)"

    days_desc = "Mon-Fri" if weekdays_only else "All Days"
    return f"Active [{days_desc} {start_str} - {end_str} {tz_name}]"
