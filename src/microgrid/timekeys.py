"""Lossless time-label parsing for CUMCM 2026 C inputs and templates.

Parsing a label is an engineering operation.  Deciding what energy interval
the label maps to is a model/data decision (D-TIME) and is intentionally not
encoded here.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import asdict, dataclass
from typing import Any

from .schemas import IntervalLabel, TimeLabelError

_HHMM_RE = re.compile(r"^(?P<h>\d{1,2}):(?P<m>\d{2})(?P<plus>\+1)?$")
_DATE_RE = re.compile(r"^(?P<y>\d{4})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})$")
_INTERVAL_RE = re.compile(r"^(?P<start>\d{1,2}:\d{2}(?:\+1)?)-(?P<end>\d{1,2}:\d{2}(?:\+1)?)$")


@dataclass(frozen=True)
class ParsedTime:
    """A clock/date label normalised to an explicit date offset + minute.

    `+1` and `24:00` are normalised to the next day at 00:00.  The raw label is
    always preserved so callers can detect and investigate changes.
    """

    raw_label: str
    base_date: _dt.date | None
    day_offset: int
    minute_of_day: int
    second: int = 0
    microsecond: int = 0
    kind: str = "time"

    @property
    def total_minutes(self) -> int:
        return self.day_offset * 24 * 60 + self.minute_of_day

    def as_datetime(self) -> _dt.datetime | None:
        if self.base_date is None:
            return None
        return _dt.datetime.combine(self.base_date, _dt.time()) + _dt.timedelta(
            days=self.day_offset,
            minutes=self.minute_of_day,
            seconds=self.second,
            microseconds=self.microsecond,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["base_date"] = self.base_date.isoformat() if self.base_date else None
        return data


def _make(
    raw: str,
    *,
    base_date: _dt.date | None,
    day_offset: int,
    minute_of_day: int,
    second: int = 0,
    microsecond: int = 0,
    kind: str = "time",
) -> ParsedTime:
    if minute_of_day < 0 or minute_of_day >= 24 * 60:
        raise TimeLabelError(f"minute-of-day out of range in {raw!r}")
    if second < 0 or second > 59 or microsecond < 0 or microsecond > 999999:
        raise TimeLabelError(f"seconds out of range in {raw!r}")
    return ParsedTime(
        raw_label=raw,
        base_date=base_date,
        day_offset=day_offset,
        minute_of_day=minute_of_day,
        second=second,
        microsecond=microsecond,
        kind=kind,
    )


def parse_time_label(
    value: Any,
    *,
    base_date: _dt.date | None = None,
    date_system: str = "1900",
) -> ParsedTime:
    """Parse one date/time label without guessing a physical interval mapping."""

    raw = "" if value is None else str(value)

    if value is None:
        raise TimeLabelError("time label is None")

    if isinstance(value, bool):
        raise TimeLabelError(f"boolean is not a time label: {value!r}")

    if isinstance(value, _dt.datetime):
        return _make(
            raw,
            base_date=value.date(),
            day_offset=0,
            minute_of_day=value.hour * 60 + value.minute,
            second=value.second,
            microsecond=value.microsecond,
            kind="datetime",
        )

    if isinstance(value, _dt.date):
        return _make(raw, base_date=value, day_offset=0, minute_of_day=0, kind="date")

    if isinstance(value, _dt.time):
        return _make(
            raw,
            base_date=base_date,
            day_offset=0,
            minute_of_day=value.hour * 60 + value.minute,
            second=value.second,
            microsecond=value.microsecond,
            kind="time",
        )

    if isinstance(value, (int, float)):
        number = float(value)
        if number < 0:
            raise TimeLabelError(f"negative numeric time label: {raw!r}")
        if number < 1.0:
            minute_float = number * 24 * 60
            minute = int(round(minute_float))
            if abs(minute_float - minute) > 1e-6:
                raise TimeLabelError(f"fractional minute in numeric time label: {raw!r}")
            if minute == 24 * 60:
                return _make(raw, base_date=base_date, day_offset=1, minute_of_day=0, kind="time")
            return _make(raw, base_date=base_date, day_offset=0, minute_of_day=minute, kind="time")

        if float(number).is_integer():
            parsed_date = excel_serial_to_date(int(number), date_system=date_system)
            return _make(
                raw,
                base_date=parsed_date,
                day_offset=0,
                minute_of_day=0,
                kind="date",
            )

        whole = int(number)
        fraction = number - whole
        parsed_date = excel_serial_to_date(whole, date_system=date_system)
        minute = int(round(fraction * 24 * 60))
        if abs(fraction * 24 * 60 - minute) > 1e-6:
            raise TimeLabelError(f"unsupported Excel datetime fraction: {raw!r}")
        return _make(
            raw,
            base_date=parsed_date,
            day_offset=0,
            minute_of_day=minute,
            kind="datetime",
        )

    if not isinstance(value, str):
        raise TimeLabelError(f"unsupported time label type: {type(value)!r}")

    text = value.strip()
    if not text:
        raise TimeLabelError("empty time label")

    date_match = _DATE_RE.match(text)
    if date_match:
        year = int(date_match.group("y"))
        month = int(date_match.group("m"))
        day = int(date_match.group("d"))
        try:
            parsed_date = _dt.date(year, month, day)
        except ValueError as exc:
            raise TimeLabelError(f"invalid date label: {value!r}") from exc
        return _make(text, base_date=parsed_date, day_offset=0, minute_of_day=0, kind="date")

    match = _HHMM_RE.match(text)
    if match:
        hour = int(match.group("h"))
        minute = int(match.group("m"))
        if minute > 59 or hour > 24:
            raise TimeLabelError(f"invalid time label: {value!r}")
        if hour == 24:
            if minute != 0:
                raise TimeLabelError(f"24:xx must have zero minutes: {value!r}")
            if match.group("plus"):
                raise TimeLabelError(f"cannot combine 24:00 with +1: {value!r}")
            return _make(text, base_date=base_date, day_offset=1, minute_of_day=0)
        offset = 1 if match.group("plus") else 0
        return _make(
            text,
            base_date=base_date,
            day_offset=offset,
            minute_of_day=hour * 60 + minute,
        )

    if text == "0":
        return _make(text, base_date=base_date, day_offset=0, minute_of_day=0)

    raise TimeLabelError(f"unrecognised time label: {value!r}")


def parse_interval(
    label: str,
    *,
    date_system: str = "1900",
) -> IntervalLabel:
    """Parse an `HH:MM[-HH:MM+1]` label as two literal endpoints.

    Literal inconsistencies (e.g. `0:00-0:10+1`) are reported via
    `suspicious=True`; they are never silently repaired.
    """

    raw = "" if label is None else str(label)
    match = _INTERVAL_RE.match(raw.strip())
    if not match:
        raise TimeLabelError(f"unrecognised interval label: {label!r}")
    start = parse_time_label(match.group("start"), date_system=date_system)
    end = parse_time_label(match.group("end"), date_system=date_system)
    span = end.total_minutes - start.total_minutes
    suspicious = False
    reason = ""
    if span <= 0:
        suspicious = True
        reason = f"non-positive interval span ({span} minutes)"
    elif span > 24 * 60:
        suspicious = True
        reason = f"literal interval span exceeds 24 hours ({span} minutes)"
    return IntervalLabel(
        raw_label=raw,
        start_day_offset=start.day_offset,
        start_minute_of_day=start.minute_of_day,
        end_day_offset=end.day_offset,
        end_minute_of_day=end.minute_of_day,
        span_minutes=span,
        suspicious=suspicious,
        reason=reason,
    )


def excel_serial_to_date(serial: int, *, date_system: str = "1900") -> _dt.date:
    """Convert an Excel serial date to a Python date using the workbook epoch."""

    if serial < 0:
        raise TimeLabelError(f"negative Excel serial date: {serial}")
    if date_system == "1904":
        return _dt.date(1904, 1, 1) + _dt.timedelta(days=serial)
    if date_system != "1900":
        raise TimeLabelError(f"unsupported Excel date system: {date_system!r}")
    # Preserve Excel's fictitious 1900-02-29 for serial 60; modern dates are
    # unambiguous after serial 60.
    if 1 <= serial < 60:
        return _dt.date(1899, 12, 31) + _dt.timedelta(days=serial - 1)
    return _dt.date(1899, 12, 30) + _dt.timedelta(days=serial)


def issue_time_from_forecast_label(date_label: Any, clock_label: Any) -> _dt.datetime:
    """Return the raw forecast issue time from its date + clock labels."""

    date_part = parse_time_label(date_label)
    if date_part.base_date is None:
        raise TimeLabelError(f"forecast date has no date: {date_label!r}")
    clock = parse_time_label(clock_label, base_date=date_part.base_date)
    issue = _dt.datetime.combine(date_part.base_date, _dt.time()) + _dt.timedelta(
        days=clock.day_offset, minutes=clock.minute_of_day
    )
    return issue


def valid_time(issue_time: _dt.datetime, lead_hours: int) -> _dt.datetime:
    """Restore the raw forecast index only; no interpolation is performed."""

    if lead_hours < 1:
        raise TimeLabelError(f"forecast lead must be >= 1 hour, got {lead_hours}")
    return issue_time + _dt.timedelta(hours=lead_hours)


def is_available(issue_time: _dt.datetime, decision_time: _dt.datetime) -> bool:
    """Explicit visibility rule for forecasts whose issue time is known."""

    return issue_time <= decision_time
