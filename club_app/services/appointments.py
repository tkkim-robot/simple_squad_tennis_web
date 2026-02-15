from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable

from sqlalchemy import and_

from ..models import (
    Appointment,
    AppointmentVote,
    MatchPlan,
    MatchPlanGame,
    MemberCounterSeed,
    Member,
    PracticeAssignment,
    RoleAssignment,
    User,
)
from .notifications import dispatch_pending_notifications, queue_notification
from .settings_store import app_now, get_setting_int, get_setting_json


@dataclass
class RoleSelection:
    members: list[Member]
    slots: int


def _nth_weekday_after(start_date: date, target_weekday: int, n: int) -> date:
    d = start_date
    found = 0
    while found < n:
        d += timedelta(days=1)
        if d.weekday() == target_weekday:
            found += 1
    return d


def _next_weekday(start_date: date, target_weekday: int, include_today: bool = False) -> date:
    d = start_date
    if include_today and d.weekday() == target_weekday:
        return d
    while True:
        d += timedelta(days=1)
        if d.weekday() == target_weekday:
            return d


def _start_of_week(d: date) -> date:
    return d - timedelta(days=d.weekday())


def compute_courts_for_count(session, total_players: int) -> int:
    min_players = get_setting_int(session, "min_players_to_run", 3)
    if total_players < min_players:
        return 0

    rules = get_setting_json(session, "court_rules_json", []) or []
    for rule in rules:
        r_min = int(rule.get("min", 0))
        r_max = int(rule.get("max", 999))
        courts = int(rule.get("courts", 0))
        if r_min <= total_players <= r_max:
            return max(0, courts)
    return 0


def ensure_auto_appointments(session, now: datetime) -> list[Appointment]:
    created: list[Appointment] = []

    creation_weekday = get_setting_int(session, "creation_weekday", 5)
    creation_hour = get_setting_int(session, "creation_hour", 18)
    creation_minute = get_setting_int(session, "creation_minute", 0)
    target_weekday = get_setting_int(session, "target_weekday", 1)
    target_occurrence = get_setting_int(session, "target_occurrence", 2)
    event_hour = get_setting_int(session, "event_start_hour", 20)
    event_minute = get_setting_int(session, "event_start_minute", 0)
    duration_minutes = get_setting_int(session, "event_duration_minutes", 120)
    vote_close_weekday = get_setting_int(session, "vote_close_weekday", 0)
    vote_close_hour = get_setting_int(session, "vote_close_hour", 18)
    vote_close_minute = get_setting_int(session, "vote_close_minute", 0)
    backfill_weeks = max(0, get_setting_int(session, "auto_backfill_weeks", 10))
    lookahead_weeks = max(1, get_setting_int(session, "auto_lookahead_weeks", 4))

    today = now.date()
    start_week = _start_of_week(today - timedelta(weeks=backfill_weeks))
    end_week = _start_of_week(today + timedelta(weeks=lookahead_weeks))

    week = start_week
    while week <= end_week:
        trigger_date = week + timedelta(days=creation_weekday)
        trigger_dt = datetime.combine(trigger_date, time(creation_hour, creation_minute))
        if trigger_dt <= now:
            target_date = _nth_weekday_after(trigger_date, target_weekday, max(1, target_occurrence))
            event_start = datetime.combine(target_date, time(event_hour, event_minute))
            event_end = event_start + timedelta(minutes=max(30, duration_minutes))
            vote_close_date = _next_weekday(trigger_date, vote_close_weekday)
            vote_close = datetime.combine(vote_close_date, time(vote_close_hour, vote_close_minute))

            exists = (
                session.query(Appointment)
                .filter(Appointment.event_start == event_start)
                .one_or_none()
            )
            if exists is None:
                item = Appointment(
                    title=f"Club Play {target_date.isoformat()}",
                    event_start=event_start,
                    event_end=event_end,
                    vote_open_at=trigger_dt,
                    vote_close_at=vote_close,
                    created_trigger_at=trigger_dt,
                    status="OPEN",
                    auto_generated=True,
                )
                session.add(item)
                created.append(item)
        week += timedelta(weeks=1)

    return created


def _confirmed_member_list(appointment: Appointment) -> list[Member]:
    seen: dict[int, Member] = {}
    for vote in appointment.votes:
        if vote.will_join and vote.member and vote.member.active:
            seen[vote.member.id] = vote.member
    return list(seen.values())


def _active_guest_count(appointment: Appointment) -> int:
    return len([g for g in appointment.guests if g.active])


def _select_for_role(
    members: list[Member],
    slots: int,
    role: str,
) -> RoleSelection:
    if slots <= 0 or not members:
        return RoleSelection(members=[], slots=0)

    if role == "BALL_CARRIER":
        key_fn = lambda m: (
            -m.ball_wait_count,
            m.last_joined_at or datetime(1970, 1, 1),
            m.last_ball_assigned_at or datetime(1970, 1, 1),
            m.id,
        )
    else:
        key_fn = lambda m: (
            -m.reserver_wait_count,
            m.last_joined_at or datetime(1970, 1, 1),
            m.last_reserver_assigned_at or datetime(1970, 1, 1),
            m.id,
        )

    ordered = sorted(members, key=key_fn)
    selected = ordered[: min(slots, len(ordered))]
    return RoleSelection(members=selected, slots=len(selected))


def _upsert_role_assignment(
    session,
    appointment_id: int,
    role_type: str,
    slot_index: int,
    member_id: int,
) -> None:
    row = (
        session.query(RoleAssignment)
        .filter_by(
            appointment_id=appointment_id,
            role_type=role_type,
            slot_index=slot_index,
        )
        .one_or_none()
    )
    if row is None:
        session.add(
            RoleAssignment(
                appointment_id=appointment_id,
                role_type=role_type,
                slot_index=slot_index,
                member_id=member_id,
                source="AUTO",
            )
        )
    else:
        row.member_id = member_id
        row.source = "AUTO"


def recompute_appointments(session, now: datetime | None = None) -> None:
    now = now or app_now(session)

    seed_by_member_id = {
        row.member_id: row for row in session.query(MemberCounterSeed).all()
    }
    members = session.query(Member).all()
    for m in members:
        seed = seed_by_member_id.get(m.id)
        m.ball_wait_count = seed.ball_seed if seed else 0
        m.reserver_wait_count = seed.reserver_seed if seed else 0
        m.last_joined_at = None

    session.query(RoleAssignment).delete()

    appointments = session.query(Appointment).order_by(Appointment.vote_close_at.asc()).all()
    appointment_by_event_date = {a.event_start.date(): a for a in appointments}

    ball_slots_per_court = max(1, get_setting_int(session, "ball_carriers_per_court", 1))
    reserver_slots_per_court = max(1, get_setting_int(session, "reservers_per_court", 1))

    for appt in appointments:
        if now < appt.vote_open_at:
            appt.status = "PLANNED"
            appt.finalized_at = None
            appt.joined_count = len(_confirmed_member_list(appt)) + _active_guest_count(appt)
            appt.courts_reserved = compute_courts_for_count(session, appt.joined_count)
            continue

        if appt.vote_open_at <= now < appt.vote_close_at:
            appt.status = "OPEN"
            appt.finalized_at = None
            appt.joined_count = len(_confirmed_member_list(appt)) + _active_guest_count(appt)
            appt.courts_reserved = compute_courts_for_count(session, appt.joined_count)
            continue

        previous = appointment_by_event_date.get(appt.event_start.date() - timedelta(days=7))
        if previous and previous.status != "CANCELED":
            has_ball = (
                session.query(RoleAssignment)
                .filter_by(appointment_id=previous.id, role_type="BALL_CARRIER")
                .count()
                > 0
            )
            if not has_ball:
                prev_members = _confirmed_member_list(previous)
                prev_total = len(prev_members) + _active_guest_count(previous)
                prev_courts = compute_courts_for_count(session, prev_total)
                slots = prev_courts * ball_slots_per_court
                selection = _select_for_role(prev_members, slots, "BALL_CARRIER")
                for idx, member in enumerate(selection.members):
                    _upsert_role_assignment(
                        session,
                        previous.id,
                        "BALL_CARRIER",
                        idx,
                        member.id,
                    )
                    member.ball_wait_count = 0
                    member.last_ball_assigned_at = appt.vote_close_at

        joined_members = _confirmed_member_list(appt)
        total_joined = len(joined_members) + _active_guest_count(appt)
        appt.joined_count = total_joined
        courts = compute_courts_for_count(session, total_joined)
        appt.courts_reserved = courts

        if courts <= 0:
            appt.status = "CANCELED"
            appt.finalized_at = appt.vote_close_at
            continue

        for member in joined_members:
            member.ball_wait_count += 1
            member.reserver_wait_count += 1
            member.last_joined_at = appt.event_start

        reserve_slots = courts * reserver_slots_per_court
        selection = _select_for_role(joined_members, reserve_slots, "RESERVER")
        for idx, member in enumerate(selection.members):
            _upsert_role_assignment(session, appt.id, "RESERVER", idx, member.id)
            member.reserver_wait_count = 0
            member.last_reserver_assigned_at = appt.vote_close_at

        appt.status = "COMPLETED" if appt.event_end <= now else "CLOSED"
        appt.finalized_at = appt.vote_close_at

    # Safety rail: keep at most one OPEN appointment even if data is malformed.
    open_rows = [a for a in appointments if a.status == "OPEN"]
    if len(open_rows) > 1:
        open_rows.sort(key=lambda a: a.vote_close_at)
        for extra in open_rows[1:]:
            extra.status = "PLANNED"


def _list_role_names(appointment: Appointment, role_type: str) -> list[str]:
    rows = [r for r in appointment.role_assignments if r.role_type == role_type]
    rows.sort(key=lambda r: r.slot_index)
    return [r.member.name for r in rows if r.member]


def _match_plan_summary(session, plan: MatchPlan | None) -> list[str]:
    if plan is None:
        return ["- Not available (canceled or insufficient players)."]

    lines: list[str] = []
    games = (
        session.query(MatchPlanGame)
        .filter(MatchPlanGame.match_plan_id == plan.id)
        .order_by(MatchPlanGame.game_index.asc(), MatchPlanGame.court_index.asc())
        .all()
    )
    practices = (
        session.query(PracticeAssignment)
        .filter(PracticeAssignment.match_plan_id == plan.id)
        .order_by(PracticeAssignment.game_index.asc(), PracticeAssignment.id.asc())
        .all()
    )
    games_by_round: dict[int, list] = {}
    for game in games:
        games_by_round.setdefault(game.game_index, []).append(game)

    for game_index in sorted(games_by_round):
        for game in sorted(games_by_round[game_index], key=lambda g: g.court_index):
            lines.append(
                (
                    f"- G{game.game_index} C{game.court_index}: "
                    f"{game.team_a_p1.display_name}/{game.team_a_p2.display_name} vs "
                    f"{game.team_b_p1.display_name}/{game.team_b_p2.display_name}"
                )
            )
        practice = [p.participant.display_name for p in practices if p.game_index == game_index]
        if practice:
            lines.append(f"- G{game_index} Practice: {', '.join(practice)}")
    return lines


def build_appointment_notification(
    session,
    appointment: Appointment,
    coming_appointment: Appointment | None = None,
    coming_plan: MatchPlan | None = None,
    match_plan_error: str | None = None,
) -> tuple[str, str]:
    joined_members = _confirmed_member_list(appointment)
    guest_names = [g.name for g in appointment.guests if g.active]
    joined_names = sorted([m.name for m in joined_members]) + sorted(guest_names)
    reserver_names = _list_role_names(appointment, "RESERVER")

    previous = coming_appointment
    if previous is None:
        previous = (
            session.query(Appointment)
            .filter(Appointment.event_start == appointment.event_start - timedelta(days=7))
            .one_or_none()
        )
    ball_names: list[str] = []
    if previous:
        ball_names = _list_role_names(previous, "BALL_CARRIER")
        if coming_plan is None:
            coming_plan = (
                session.query(MatchPlan)
                .filter(MatchPlan.appointment_id == previous.id)
                .order_by(MatchPlan.created_at.desc())
                .first()
            )

    title = f"Vote Closed Summary {appointment.event_start.date().isoformat()}"
    coming_date_text = previous.event_start.date().isoformat() if previous else "N/A"
    body_lines = [
        f"Who ({len(joined_names)}): {', '.join(joined_names) if joined_names else 'None'}",
        "",
        f"How many courts to reserve: {appointment.courts_reserved}",
        "",
        f"Reservers: {', '.join(reserver_names) if reserver_names else 'TBD'}",
        f"Ball Carriers: {', '.join(ball_names) if ball_names else 'TBD'}",
        "",
        f"Coming Tuesday Match Making ({coming_date_text}):",
    ]
    body_lines.extend(_match_plan_summary(session, coming_plan))
    if match_plan_error:
        body_lines.extend(["", f"Match making generation error: {match_plan_error}"])
    return title, "\n".join(body_lines)


def create_missing_vote_rows(session, appointment: Appointment) -> None:
    existing_member_ids = {v.member_id for v in appointment.votes}
    active_members = session.query(Member).filter(Member.active.is_(True)).all()
    for member in active_members:
        if member.id not in existing_member_ids:
            session.add(
                AppointmentVote(
                    appointment_id=appointment.id,
                    member_id=member.id,
                    will_join=False,
                )
            )


def run_maintenance(session) -> None:
    now = app_now(session)
    ensure_auto_appointments(session, now)
    session.flush()

    appointments = session.query(Appointment).all()
    for appt in appointments:
        create_missing_vote_rows(session, appt)

    recompute_appointments(session, now)
    session.flush()

    ready_to_notify = (
        session.query(Appointment)
        .filter(
            and_(
                Appointment.vote_close_at <= now,
                Appointment.notification_sent_at.is_(None),
            )
        )
        .order_by(Appointment.vote_close_at.asc())
        .all()
    )

    for appt in ready_to_notify:
        current_plan = (
            session.query(MatchPlan)
            .filter(MatchPlan.appointment_id == appt.id)
            .order_by(MatchPlan.created_at.desc())
            .first()
        )
        match_plan_error = None
        if current_plan is None and appt.courts_reserved > 0 and appt.joined_count >= 4:
            admin = (
                session.query(User)
                .filter(User.is_admin.is_(True))
                .order_by(User.id.asc())
                .first()
            )
            if admin is None:
                match_plan_error = "No admin account available to own generated plan."
            else:
                try:
                    from .matchmaking import get_or_create_match_plan

                    current_plan = get_or_create_match_plan(session, appt.id, admin.id, rounds=3)
                    session.flush()
                except Exception as exc:
                    match_plan_error = str(exc)

        coming_appointment = (
            session.query(Appointment)
            .filter(Appointment.event_start == appt.event_start - timedelta(days=7))
            .one_or_none()
        )
        coming_plan = None
        if coming_appointment is not None:
            coming_plan = (
                session.query(MatchPlan)
                .filter(MatchPlan.appointment_id == coming_appointment.id)
                .order_by(MatchPlan.created_at.desc())
                .first()
            )
            if coming_plan is None and coming_appointment.courts_reserved > 0 and coming_appointment.joined_count >= 4:
                admin = (
                    session.query(User)
                    .filter(User.is_admin.is_(True))
                    .order_by(User.id.asc())
                    .first()
                )
                if admin is not None:
                    try:
                        from .matchmaking import get_or_create_match_plan

                        coming_plan = get_or_create_match_plan(session, coming_appointment.id, admin.id, rounds=3)
                        session.flush()
                    except Exception as exc:
                        match_plan_error = str(exc)

        title, body = build_appointment_notification(
            session,
            appt,
            coming_appointment,
            coming_plan,
            match_plan_error,
        )
        queue_notification(session, title, body)
        appt.notification_sent_at = now

    dispatch_pending_notifications(session)
