"""Validate Kanban runtime caps without silently shortening them."""

from decimal import Decimal, DecimalException


_MAX_SECONDS = 2**63 - 1


def normalize_runtime_cap(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("max_runtime_seconds must be a positive whole number; omit it for no cap")
    try:
        seconds = int(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("max_runtime_seconds must be a positive whole number; omit it for no cap") from exc
    if (not isinstance(value, (str, bytes)) and value != seconds) or not 0 < seconds <= _MAX_SECONDS:
        raise ValueError("max_runtime_seconds must be a positive whole number; omit it for no cap")
    return seconds


def parse_duration(value):
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    if len(text) > 64:
        raise ValueError("duration is too long")
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text and text[-1] in units:
        try:
            seconds = Decimal(text[:-1]) * units[text[-1]]
        except DecimalException as exc:
            raise ValueError(f"malformed duration {value!r}") from exc
        if not seconds.is_finite() or seconds != seconds.to_integral_value():
            raise ValueError("duration must resolve to whole seconds")
        if not 0 < seconds <= _MAX_SECONDS:
            raise ValueError("duration must be positive; omit it for no cap")
        return int(seconds)
    return normalize_runtime_cap(text)
