from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import app
from club_app import db
from club_app.models import (
    Appointment,
    AppointmentGuest,
    AppointmentVote,
    MatchPlan,
    MatchPlanGame,
    MatchPlanParticipant,
    Member,
    NotificationOutbox,
    PracticeAssignment,
    RatingEvent,
    RoleAssignment,
    User,
)
from club_app.services.appointments import (
    build_appointment_notification,
    create_missing_vote_rows,
    recompute_appointments,
)
from club_app.services.matchmaking import get_or_create_match_plan
from club_app.services.notifications import dispatch_pending_notifications, queue_notification
from club_app.services.settings_store import get_setting, set_setting


@dataclass
class WeekReport:
    week: int
    vote_closed_for: str
    joined_count: int
    courts: int
    reservers: list[str]
    ball_carriers: list[str]
    coming_tuesday: str
    match_preview: str
    notify_status: str
    notify_error: str


def _set_vote(session, appointment_id: int, member_id: int, will_join: bool) -> None:
    row = (
        session.query(AppointmentVote)
        .filter_by(appointment_id=appointment_id, member_id=member_id)
        .one_or_none()
    )
    if row is None:
        session.add(
            AppointmentVote(
                appointment_id=appointment_id,
                member_id=member_id,
                will_join=will_join,
            )
        )
    else:
        row.will_join = will_join


def _role_names(appt: Appointment | None, role_type: str) -> list[str]:
    if appt is None:
        return []
    rows = [r for r in appt.role_assignments if r.role_type == role_type]
    rows.sort(key=lambda r: r.slot_index)
    return [r.member.name for r in rows if r.member]


def _clear_previous_data(session) -> None:
    session.query(RatingEvent).delete()
    session.query(PracticeAssignment).delete()
    session.query(MatchPlanGame).delete()
    session.query(MatchPlanParticipant).delete()
    session.query(MatchPlan).delete()
    session.query(AppointmentGuest).delete()
    session.query(RoleAssignment).delete()
    session.query(AppointmentVote).delete()
    session.query(Appointment).delete()
    session.query(NotificationOutbox).delete()

    members = session.query(Member).all()
    for member in members:
        member.ball_wait_count = 0
        member.reserver_wait_count = 0
        member.last_joined_at = None
        member.last_ball_assigned_at = None
        member.last_reserver_assigned_at = None


def _ensure_test_members(session) -> list[Member]:
    names = [
        "QA_Alice",
        "QA_Ben",
        "QA_Chloe",
        "QA_Daniel",
        "QA_Evan",
        "QA_Fiona",
        "QA_Grace",
        "QA_Henry",
        "QA_Ivy",
        "QA_Jason",
        "QA_Kathy",
        "QA_Leo",
    ]
    output: list[Member] = []
    for idx, name in enumerate(names):
        member = session.query(Member).filter_by(name=name).one_or_none()
        if member is None:
            member = Member(
                name=name,
                email="",
                phone="",
                skill_rating=920.0 + idx * 22.0,
                active=True,
                notes="qa-10-week",
            )
            session.add(member)
            session.flush()
        else:
            member.active = True
            member.skill_rating = 920.0 + idx * 22.0
            member.notes = "qa-10-week"
        output.append(member)
    return output


def _create_appointments(session) -> list[Appointment]:
    # Warm-up trigger is one week before Feb 13, 2026, so first reported week has "coming Tuesday" data.
    start_trigger = datetime(2026, 2, 7, 18, 0, 0)  # Saturday 6pm
    rows: list[Appointment] = []
    for i in range(11):  # 1 warm-up + 10 reported weeks
        trigger = start_trigger + timedelta(days=7 * i)
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
        session.add(appt)
        rows.append(appt)
    session.flush()
    return rows


def _vote_pattern(member_names: list[str], week_index: int) -> set[str]:
    # index 0 is warm-up (not reported), indices 1..10 are reported tests.
    patterns = [
        member_names[:8],  # warm-up
        member_names[:5],  # 5 players
        member_names[1:9],  # 8 players
        member_names[:11],  # 11 players
        member_names[3:7],  # 4 players
        member_names[:2],  # 2 players -> canceled
        member_names[2:8],  # 6 players
        member_names[0:9],  # 9 players
        member_names[1:11],  # 10 players
        member_names[4:7],  # 3 players
        member_names[2:9],  # 7 players
    ]
    return set(patterns[week_index])


def main() -> None:
    with app.app_context():
        webhook = get_setting(db.session, "discord_webhook", "")
        if not webhook:
            raise RuntimeError("Discord webhook is not configured in settings.")

        set_setting(db.session, "notify_channel", "DISCORD")

        # Force deterministic schedule settings for this QA run.
        set_setting(db.session, "creation_weekday", "5")
        set_setting(db.session, "creation_hour", "18")
        set_setting(db.session, "creation_minute", "0")
        set_setting(db.session, "target_weekday", "1")
        set_setting(db.session, "target_occurrence", "2")
        set_setting(db.session, "event_start_hour", "20")
        set_setting(db.session, "event_start_minute", "0")
        set_setting(db.session, "event_duration_minutes", "120")
        set_setting(db.session, "vote_close_weekday", "0")
        set_setting(db.session, "vote_close_hour", "18")
        set_setting(db.session, "vote_close_minute", "0")
        db.session.commit()

        _clear_previous_data(db.session)
        members = _ensure_test_members(db.session)
        appointments = _create_appointments(db.session)
        db.session.commit()

        admin = db.session.query(User).filter(User.is_admin.is_(True)).order_by(User.id.asc()).first()
        if admin is None:
            raise RuntimeError("Admin account missing.")

        reports: list[WeekReport] = []
        member_names = [m.name for m in members]
        member_by_name = {m.name: m for m in members}

        for idx, appt in enumerate(appointments):
            open_now = appt.vote_open_at + timedelta(hours=16)  # Sunday morning
            close_now = appt.vote_close_at + timedelta(hours=1)  # Monday 7pm

            set_setting(db.session, "qa_now_iso", open_now.isoformat())
            create_missing_vote_rows(db.session, appt)
            joined = _vote_pattern(member_names, idx)
            for name, member in member_by_name.items():
                _set_vote(db.session, appt.id, member.id, name in joined)
            db.session.commit()

            set_setting(db.session, "qa_now_iso", close_now.isoformat())
            recompute_appointments(db.session, close_now)
            db.session.flush()

            current = db.session.get(Appointment, appt.id)
            assert current is not None

            if current.courts_reserved > 0 and current.joined_count >= 4:
                get_or_create_match_plan(db.session, current.id, admin.id, rounds=3)
                db.session.flush()

            previous = None
            previous_plan = None
            if idx > 0:
                previous = db.session.get(Appointment, appointments[idx - 1].id)
                if previous is not None:
                    previous_plan = (
                        db.session.query(MatchPlan)
                        .filter(MatchPlan.appointment_id == previous.id)
                        .order_by(MatchPlan.created_at.desc())
                        .first()
                    )
                    if previous_plan is None and previous.courts_reserved > 0 and previous.joined_count >= 4:
                        previous_plan = get_or_create_match_plan(db.session, previous.id, admin.id, rounds=3)
                        db.session.flush()

            if idx == 0:
                # warm-up week: no Discord push
                current.notification_sent_at = close_now
                db.session.commit()
                continue

            title, body = build_appointment_notification(
                db.session,
                current,
                previous,
                previous_plan,
                None,
            )
            out = queue_notification(db.session, title, body)
            current.notification_sent_at = close_now
            dispatch_pending_notifications(db.session)
            db.session.commit()

            out_final = db.session.get(NotificationOutbox, out.id)
            notify_status = out_final.status if out_final else "UNKNOWN"
            notify_error = (out_final.error or "") if out_final else "missing outbox row"

            match_preview = "Not available"
            if out_final:
                lines = [ln.strip() for ln in out_final.body.splitlines() if ln.strip()]
                match_lines = [ln for ln in lines if ln.startswith("- G")]
                if match_lines:
                    match_preview = "; ".join(match_lines[:3])

            reports.append(
                WeekReport(
                    week=idx,
                    vote_closed_for=current.event_start.date().isoformat(),
                    joined_count=current.joined_count,
                    courts=current.courts_reserved,
                    reservers=_role_names(current, "RESERVER"),
                    ball_carriers=_role_names(previous, "BALL_CARRIER") if previous else [],
                    coming_tuesday=previous.event_start.date().isoformat() if previous else "N/A",
                    match_preview=match_preview,
                    notify_status=notify_status,
                    notify_error=notify_error,
                )
            )

        print("clean_previous_data=done")
        print("start_reference_date=2026-02-13")
        print("tests_run=10")
        print(
            "week,vote_closed_for,joined,courts,reservers,ball_carriers,coming_tuesday,matchmaking_preview,notify_status,notify_error"
        )
        for row in reports:
            print(
                ",".join(
                    [
                        str(row.week),
                        row.vote_closed_for,
                        str(row.joined_count),
                        str(row.courts),
                        "|".join(row.reservers) if row.reservers else "-",
                        "|".join(row.ball_carriers) if row.ball_carriers else "-",
                        row.coming_tuesday,
                        row.match_preview.replace(",", ";"),
                        row.notify_status,
                        row.notify_error.replace(",", ";"),
                    ]
                )
            )


if __name__ == "__main__":
    main()
