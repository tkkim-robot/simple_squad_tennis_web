from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..models import Setting

DETROIT_TZ = ZoneInfo("America/Detroit")


DEFAULTS: dict[str, str] = {
    "auto_backfill_weeks": "10",
    "auto_lookahead_weeks": "4",
    "creation_weekday": "5",  # Saturday (Mon=0)
    "creation_hour": "18",
    "creation_minute": "0",
    "target_weekday": "1",  # Tuesday
    "target_occurrence": "2",  # second upcoming Tuesday
    "event_start_hour": "20",
    "event_start_minute": "0",
    "event_duration_minutes": "120",
    "vote_close_weekday": "0",  # Monday
    "vote_close_hour": "18",
    "vote_close_minute": "0",
    "min_players_to_run": "3",
    "court_rules_json": json.dumps(
        [
            {"min": 3, "max": 5, "courts": 1},
            {"min": 6, "max": 9, "courts": 2},
            {"min": 10, "max": 999, "courts": 3},
        ]
    ),
    "ball_carriers_per_court": "1",
    "reservers_per_court": "1",
    "notify_channel": "LOG",
    "notify_target": "",
    "discord_webhook": "",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "qa_now_iso": "",
}


def ensure_default_settings(session) -> None:
    existing = {s.key for s in session.query(Setting).all()}
    for key, value in DEFAULTS.items():
        if key not in existing:
            session.add(Setting(key=key, value=value))


def get_setting(session, key: str, default: Any = None) -> str:
    obj = session.get(Setting, key)
    if obj is None:
        if key in DEFAULTS:
            val = DEFAULTS[key]
            session.add(Setting(key=key, value=val))
            return val
        return default
    return obj.value


def get_setting_int(session, key: str, default: int) -> int:
    raw = get_setting(session, key, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def get_setting_json(session, key: str, default: Any) -> Any:
    raw = get_setting(session, key, None)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def set_setting(session, key: str, value: Any) -> None:
    text = value if isinstance(value, str) else json.dumps(value)
    obj = session.get(Setting, key)
    if obj is None:
        obj = Setting(key=key, value=text)
        session.add(obj)
    else:
        obj.value = text


def app_now(session) -> datetime:
    raw = get_setting(session, "qa_now_iso", "")
    if raw:
        try:
            normalized = raw.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is not None:
                return parsed.astimezone(DETROIT_TZ).replace(tzinfo=None)
            return parsed
        except ValueError:
            pass
    return datetime.now(DETROIT_TZ).replace(tzinfo=None)


def all_settings_dict(session) -> dict[str, str]:
    ensure_default_settings(session)
    return {s.key: s.value for s in session.query(Setting).all()}
