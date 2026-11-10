"""Statistics for the dashboard, from history.jsonl (bolos.history).

Every command logged since the history began: how many, how many worked, how fast, who decided (System 1 on
this computer, System 2, or a learned habit), in which language, triggered how, and the latest ones.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from math import isfinite
from statistics import median

from bolos.overlay import OUTCOME_STATES

# Before the history recorded "ok" (8 Nov 2026), an outcome key says whether a command worked. Declining a
# send ("not_sent") or stopping a guide is the user's choice, not a failure.
FAILED = {"not_understood", "error", "play_failed", "contact_not_found", "protected_field", "nothing_playing",
          "guide_unavailable", "guide_form", "focus_changed", "control_not_found", "app_not_found",
          "form_not_found", "form_not_finished", "guide_lost", "guide_cannot"}
SOURCES = {"system1": "System 1", "system2": "System 2", "system2_fields": "System 2", "habit": "Habit",
           None: "Not understood"}
LOCAL_SOURCES = {"system1", "habit"}  # decided on this computer, without asking System 2
TRIGGERS = {"hotkey": "Hotkey", "tap": "Tap", "wake": "Wake word", "typed": "Typed", "followup": "Follow-up"}
RECENT = 30


def worked(entry: dict) -> bool:
    if isinstance(entry.get("ok"), bool):
        return entry["ok"] and not entry.get("corrected")
    return _text(entry, "outcome") not in FAILED and not entry.get("corrected")


def _text(entry: dict, key: str) -> str:
    value = entry.get(key)
    return value if isinstance(value, str) else ""


def _when(entry: dict) -> datetime | None:
    try:
        when = datetime.fromisoformat(_text(entry, "time"))
        return when.astimezone() if when.tzinfo is None else when
    except (OSError, OverflowError, ValueError):
        return None


def _ms(entry: dict) -> float | None:
    timings = entry.get("timings")
    value = timings.get("total") if isinstance(timings, dict) else None
    try:
        return float(value) if type(value) in (int, float) and isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def _counts(values, top: int | None = None) -> list[dict]:
    return [{"name": name, "count": count} for name, count in Counter(values).most_common(top)]


def summarize(entries: list[dict], today: date | None = None, days: int = 14) -> dict:
    """The dashboard's numbers. `today` (local date) is a parameter for tests. The daily chart covers at most
    `days` days, and fewer (at least a week) when the history is younger, so it is not mostly empty."""
    dated = [(e, when) for e in entries if (when := _when(e)) is not None]
    today = today or date.today()
    if dated:
        age = (today - min(when for _, when in dated).date()).days + 1
        days = max(7, min(days, age))
    times = [ms for e, _ in dated if (ms := _ms(e)) is not None]
    ok = [e for e, _ in dated if worked(e)]
    understood = [e for e, _ in dated if _text(e, "source")]

    per_day = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        those = [e for e, when in dated if when.date() == day]
        good = sum(worked(e) for e in those)
        per_day.append({"date": day.isoformat(), "ok": good, "failed": len(those) - good})

    by_source: dict[str, list[float]] = {}
    for e, _ in dated:
        by_source.setdefault(SOURCES.get(_text(e, "source") or None, "Other"), []).append(_ms(e))
    sources = []
    for name, values in sorted(by_source.items(), key=lambda item: -len(item[1])):
        measured = [v for v in values if v is not None]
        sources.append({"name": name, "count": len(values),
                        "median_ms": round(median(measured)) if measured else None})

    recent = []
    for e, when in sorted(dated, key=lambda item: item[1], reverse=True)[:RECENT]:
        outcome = _text(e, "outcome")
        recent.append({"time": when.isoformat(timespec="seconds"), "transcript": _text(e, "transcript"),
                       "action": _text(e, "action"), "source": SOURCES.get(_text(e, "source") or None, "Other"),
                       "outcome": outcome, "ok": worked(e), "ms": _ms(e),
                       # failures read as the status card says them ("App not found")
                       "result": OUTCOME_STATES.get(outcome, (None,))[0],
                       "language": _text(e, "language").split("-")[0], "corrected": bool(e.get("corrected"))})

    return {
        "total": len(dated),
        "today": sum(when.date() == today for _, when in dated),
        "success_rate": round(len(ok) / len(dated), 3) if dated else None,
        "understood_rate": round(len(understood) / len(dated), 3) if dated else None,
        "local_rate": (round(sum(e.get("source") in LOCAL_SOURCES for e in understood) / len(understood), 3)
                       if understood else None),
        "median_ms": round(median(times)) if times else None,
        "p90_ms": round(_percentile(times, 0.9)) if times else None,
        "corrected": sum(bool(e.get("corrected")) for e, _ in dated),
        "first": min(when for _, when in dated).isoformat(timespec="seconds") if dated else None,
        "per_day": per_day,
        "sources": sources,
        "actions": _counts((_text(e, "action") or "not understood" for e, _ in dated), 8),
        "languages": _counts(((_text(e, "language") or "?").split("-")[0] for e, _ in dated)),
        "triggers": _counts(TRIGGERS.get(trigger, trigger or "?")
                            for trigger in (_text(e, "trigger") for e, _ in dated)),
        "failures": _counts((_text(e, "outcome") or "?" for e, _ in dated if not worked(e)), 6),
        "recent": recent,
    }
