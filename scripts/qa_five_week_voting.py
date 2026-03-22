from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from club_app import create_app, db
from club_app.models import (
    Appointment,
    AppointmentVote,
    MatchPlan,
    Member,
    NotificationOutbox,
    RoleAssignment,
    User,
)
from club_app.services.appointments import build_appointment_notification, create_missing_vote_rows, recompute_appointments
from club_app.services.matchmaking import get_or_create_match_plan
from club_app.services.notifications import dispatch_pending_notifications, queue_notification
from club_app.services.settings_store import set_setting


TMP_DB = Path("/tmp/annarbortennis_qa_5weeks.db")
BASE_SATURDAY = datetime(2022, 1, 1, 18, 0, 0)


@dataclass
class WeekResult:
    week: int
    event_date: str
    status: str
    joined_count: int
    courts: int
    ball_carriers: list[str]
    reservers: list[str]
    match_plan_id: int | None
    notification_status: str
    notification_error: str


def _read_live_webhook() -> str:
    live_db = ROOT / "instance" / "club.db"
    if not live_db.exists():
        return ""
    conn = sqlite3.connect(str(live_db))
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = 'discord_webhook'").fetchone()
        return row[0] if row and row[0] else ""
    finally:
        conn.close()


def _role_names(appt: Appointment, role_type: str) -> list[str]:
    rows = [r for r in appt.role_assignments if r.role_type == role_type]
    rows.sort(key=lambda r: r.slot_index)
    return [r.member.name for r in rows if r.member]


def _set_vote(session, appointment_id: int, member_id: int, will_join: bool) -> None:
    vote = (
        session.query(AppointmentVote)
        .filter_by(appointment_id=appointment_id, member_id=member_id)
        .one_or_none()
    )
    if vote is None:
        session.add(AppointmentVote(appointment_id=appointment_id, member_id=member_id, will_join=will_join))
    else:
        vote.will_join = will_join


def main() -> None:
    if TMP_DB.exists():
        TMP_DB.unlink()

    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{TMP_DB}",
            "SECRET_KEY": "qa-five-week",
        }
    )

    webhook = _read_live_webhook()
    notify_channel = "DISCORD" if webhook else "LOG"

    with app.app_context():
        set_setting(db.session, "notify_channel", notify_channel)
        set_setting(db.session, "discord_webhook", webhook)
        set_setting(db.session, "notify_target", "")
        db.session.commit()

        names = [
            "Alice",
            "Ben",
            "Chloe",
            "Daniel",
            "Evan",
            "Fiona",
            "Grace",
            "Henry",
            "Ivy",
            "Jason",
            "Kathy",
            "Leo",
        ]
        for name in names:
            db.session.add(Member(name=name, email="", phone="", skill_rating=1000.0, active=True, notes="qa"))
        db.session.commit()

        # Build 5 appointments that follow Saturday creation -> second upcoming Tuesday, vote closes Monday 6pm.
        created_appts: list[Appointment] = []
        for week in range(5):
            trigger = BASE_SATURDAY + timedelta(days=7 * week)
            event_start = trigger + timedelta(days=10, hours=2)  # Tuesday 8pm
            event_end = event_start + timedelta(hours=2)
            vote_close = trigger + timedelta(days=2)  # Monday 6pm
            appt = Appointment(
                title=f"Club Play {event_start.date().isoformat()}",
                event_start=event_start,
                event_end=event_end,
                vote_open_at=trigger,
                vote_close_at=vote_close,
                created_trigger_at=trigger,
                status="PLANNED",
                auto_generated=True,
            )
            db.session.add(appt)
            created_appts.append(appt)
        db.session.flush()

        for appt in created_appts:
            create_missing_vote_rows(db.session, appt)
        db.session.commit()

        member_by_name = {m.name: m for m in db.session.query(Member).filter(Member.name != "Admin").all()}
        admin = db.session.query(User).filter(User.is_admin.is_(True)).order_by(User.id.asc()).first()
        assert admin is not None

        vote_plan = [
            ["Alice", "Ben", "Chloe", "Daniel", "Evan"],  # 5 joined -> 4 confirmed + 1 waitlist
            ["Alice", "Ben", "Fiona", "Grace", "Henry", "Ivy", "Jason", "Kathy"],  # 8 -> 2 courts
            ["Alice", "Ben", "Chloe", "Daniel", "Evan", "Fiona", "Grace", "Henry", "Ivy", "Jason", "Leo"],  # 11 joined -> 8 confirmed + 3 waitlist
            ["Daniel", "Evan", "Fiona", "Grace"],  # 4 -> 1 court
            ["Alice", "Ben"],  # 2 -> canceled
        ]

        results: list[WeekResult] = []

        for idx, appt in enumerate(created_appts, start=1):
            open_time = appt.vote_open_at + timedelta(hours=18)  # Sunday noon
            close_time = appt.vote_close_at + timedelta(hours=1)  # Monday 7pm

            set_setting(db.session, "qa_now_iso", open_time.isoformat())
            create_missing_vote_rows(db.session, appt)

            joined_set = set(vote_plan[idx - 1])
            for name, member in member_by_name.items():
                _set_vote(db.session, appt.id, member.id, name in joined_set)

            # Add one cancellation toggle scenario in week 3 before close.
            if idx == 3:
                _set_vote(db.session, appt.id, member_by_name["Jason"].id, False)
                _set_vote(db.session, appt.id, member_by_name["Jason"].id, True)

            db.session.commit()

            set_setting(db.session, "qa_now_iso", close_time.isoformat())
            recompute_appointments(db.session, close_time)
            db.session.flush()

            current = db.session.get(Appointment, appt.id)
            plan = None
            match_error = None
            if current and current.courts_reserved > 0 and current.joined_count >= 4:
                try:
                    plan = get_or_create_match_plan(db.session, current.id, admin.id, rounds=3)
                except Exception as exc:  # pragma: no cover - operational reporting
                    match_error = str(exc)

            notif_item = None
            if current and current.notification_sent_at is None:
                prev = (
                    db.session.query(Appointment)
                    .filter(Appointment.event_start == current.event_start - timedelta(days=7))
                    .one_or_none()
                )
                prev_plan = None
                if prev is not None:
                    prev_plan = (
                        db.session.query(MatchPlan)
                        .filter(MatchPlan.appointment_id == prev.id)
                        .order_by(MatchPlan.created_at.desc())
                        .first()
                    )
                title, body = build_appointment_notification(db.session, current, prev, prev_plan, match_error)
                notif_item = queue_notification(db.session, title, body)
                current.notification_sent_at = close_time

            dispatch_pending_notifications(db.session)
            db.session.commit()

            current = db.session.get(Appointment, appt.id)
            assert current is not None
            prev = (
                db.session.query(Appointment)
                .filter(Appointment.event_start == current.event_start - timedelta(days=7))
                .one_or_none()
            )
            ball_names = _role_names(prev, "BALL_CARRIER") if prev else []
            reserve_names = _role_names(current, "RESERVER")

            notif_status = "NONE"
            notif_err = ""
            if notif_item is not None:
                out = db.session.get(NotificationOutbox, notif_item.id)
                if out:
                    notif_status = out.status
                    notif_err = out.error or ""

            plan_id = plan.id if plan else None
            results.append(
                WeekResult(
                    week=idx,
                    event_date=current.event_start.date().isoformat(),
                    status=current.status,
                    joined_count=current.joined_count,
                    courts=current.courts_reserved,
                    ball_carriers=ball_names,
                    reservers=reserve_names,
                    match_plan_id=plan_id,
                    notification_status=notif_status,
                    notification_error=notif_err,
                )
            )

        print(f"notification_channel={notify_channel}")
        if notify_channel == "DISCORD":
            print("discord_webhook=SET")
        else:
            print("discord_webhook=NOT_SET")
        print("week,event_date,status,joined,courts,ball_carriers,reservers,match_plan,notification,notification_error")
        for r in results:
            print(
                ",".join(
                    [
                        str(r.week),
                        r.event_date,
                        r.status,
                        str(r.joined_count),
                        str(r.courts),
                        "|".join(r.ball_carriers) if r.ball_carriers else "-",
                        "|".join(r.reservers) if r.reservers else "-",
                        str(r.match_plan_id) if r.match_plan_id else "-",
                        r.notification_status,
                        r.notification_error.replace(",", ";"),
                    ]
                )
            )


if __name__ == "__main__":
    main()
