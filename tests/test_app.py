from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from club_app import create_app, db
from club_app.models import (
    Appointment,
    AppointmentGuest,
    AppointmentVote,
    MatchPlan,
    Member,
    NotificationOutbox,
    RatingEvent,
    RoleAssignment,
    User,
)
from club_app.services.appointments import (
    get_appointment_participation,
    is_voting_open_for_appointment,
    run_maintenance,
)
from club_app.services.matchmaking import (
    finalize_due_match_results,
    generate_match_plan,
    save_user_result_inputs,
    submit_results,
)
from club_app.services.settings_store import set_setting


@pytest.fixture()
def app():
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "SECRET_KEY": "test",
        }
    )
    with app.app_context():
        yield app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def session(app):
    with app.app_context():
        yield db.session


def _set_now(session, iso: str) -> None:
    set_setting(session, "qa_now_iso", iso)
    session.commit()


def _mk_member(session, name: str, rating: float = 1000.0) -> Member:
    m = Member(name=name, email="", phone="", skill_rating=rating, active=True, notes="")
    session.add(m)
    session.flush()
    return m


def _get_appointment_by_date(session, dt: datetime) -> Appointment:
    return session.query(Appointment).filter(Appointment.event_start == dt).one()


def _mk_account_member(session, username: str, display_name: str, *, is_admin: bool = False) -> User:
    user = User(username=username, password_hash="test", is_admin=is_admin)
    session.add(user)
    session.flush()
    member = Member(
        user_id=user.id,
        name=display_name,
        email="",
        phone="",
        skill_rating=1000.0,
        active=True,
        notes="",
    )
    session.add(member)
    session.flush()
    return user


def _set_vote(
    session,
    appointment_id: int,
    member_id: int,
    will_join: bool,
    when: datetime | None = None,
) -> None:
    vote = (
        session.query(AppointmentVote)
        .filter_by(appointment_id=appointment_id, member_id=member_id)
        .one_or_none()
    )
    if vote is None:
        vote = AppointmentVote(
            appointment_id=appointment_id,
            member_id=member_id,
            will_join=will_join,
        )
        session.add(vote)
    else:
        vote.will_join = will_join
    if when is not None:
        vote.updated_at = when


def test_admin_seeded(session):
    admin = session.query(User).filter_by(username="admin").one_or_none()
    assert admin is not None
    assert admin.is_admin is True


def test_auto_appointment_creation_second_upcoming_tuesday(session):
    _set_now(session, "2022-01-01T18:30:00")  # Saturday
    run_maintenance(session)
    session.commit()

    appt = _get_appointment_by_date(session, datetime(2022, 1, 11, 20, 0, 0))
    assert appt is not None
    assert appt.vote_close_at == datetime(2022, 1, 3, 18, 0, 0)


def test_only_one_open_appointment_window(session):
    _set_now(session, "2022-01-02T10:00:00")
    run_maintenance(session)
    session.commit()

    open_rows = session.query(Appointment).filter_by(status="OPEN").all()
    assert len(open_rows) <= 1
    assert len(open_rows) == 1
    assert open_rows[0].event_start == datetime(2022, 1, 11, 20, 0, 0)

    _set_now(session, "2022-01-04T12:00:00")
    run_maintenance(session)
    session.commit()
    open_rows_after = session.query(Appointment).filter_by(status="OPEN").all()
    assert len(open_rows_after) == 0


def test_vote_recompute_and_ball_role_shift_on_cancel(session):
    names = ["A", "B", "C", "D", "E", "F"]
    members = {name: _mk_member(session, name) for name in names}
    session.commit()

    _set_now(session, "2022-01-03T19:00:00")
    run_maintenance(session)

    jan4 = _get_appointment_by_date(session, datetime(2022, 1, 4, 20, 0, 0))
    jan11 = _get_appointment_by_date(session, datetime(2022, 1, 11, 20, 0, 0))

    for name in ["A", "B", "C", "D"]:
        _set_vote(session, jan4.id, members[name].id, True)
    for name in ["A", "B", "E", "F"]:
        _set_vote(session, jan11.id, members[name].id, True)
    session.commit()

    run_maintenance(session)
    session.commit()

    first_ball = (
        session.query(RoleAssignment)
        .filter_by(appointment_id=jan4.id, role_type="BALL_CARRIER")
        .order_by(RoleAssignment.slot_index.asc())
        .all()
    )
    assert first_ball
    previous_carrier_id = first_ball[0].member_id

    vote = (
        session.query(AppointmentVote)
        .filter_by(appointment_id=jan4.id, member_id=previous_carrier_id)
        .one()
    )
    vote.will_join = False
    session.commit()

    run_maintenance(session)
    session.commit()

    shifted_ball = (
        session.query(RoleAssignment)
        .filter_by(appointment_id=jan4.id, role_type="BALL_CARRIER")
        .order_by(RoleAssignment.slot_index.asc())
        .all()
    )
    assert shifted_ball
    assert shifted_ball[0].member_id != previous_carrier_id


def test_vote_waitlist_cuts_to_multiple_of_four_by_vote_time(session):
    members = [_mk_member(session, name) for name in ["A", "B", "C", "D", "E"]]
    creator_user = session.query(User).filter_by(username="admin").one()
    session.commit()

    _set_now(session, "2022-01-02T10:00:00")
    run_maintenance(session)
    session.commit()

    appt = _get_appointment_by_date(session, datetime(2022, 1, 11, 20, 0, 0))
    base_time = datetime(2022, 1, 2, 10, 0, 0)
    for idx, member in enumerate(members, start=1):
        _set_vote(session, appt.id, member.id, True, base_time + timedelta(minutes=idx))
    session.commit()

    _set_now(session, "2022-01-03T19:00:00")
    run_maintenance(session)
    session.commit()

    session.refresh(appt)
    participation = get_appointment_participation(appt)
    assert appt.joined_count == 4
    assert appt.courts_reserved == 1
    assert participation.confirmed_names == ["A", "B", "C", "D"]
    assert participation.waitlist_names == ["E"]

    plan = generate_match_plan(session, appt.id, creator_user.id)
    session.commit()
    participant_names = sorted(p.display_name for p in plan.participants)
    assert participant_names == ["A", "B", "C", "D"]


def test_late_cancel_promotes_waitlist_and_notifies_admin(session, client):
    actor = _mk_account_member(session, "alpha", "A")
    members = [actor.member] + [_mk_member(session, name) for name in ["B", "C", "D", "E"]]
    session.commit()

    _set_now(session, "2022-01-02T10:00:00")
    run_maintenance(session)
    session.commit()

    appt = _get_appointment_by_date(session, datetime(2022, 1, 11, 20, 0, 0))
    base_time = datetime(2022, 1, 2, 10, 0, 0)
    for idx, member in enumerate(members, start=1):
        _set_vote(session, appt.id, member.id, True, base_time + timedelta(minutes=idx))
    session.commit()

    _set_now(session, "2022-01-03T19:00:00")
    run_maintenance(session)
    session.commit()

    with client.session_transaction() as flask_session:
        flask_session["user_id"] = actor.id

    response = client.post(
        f"/appointments/{appt.id}/vote",
        data={"will_join": "0"},
        follow_redirects=False,
    )
    assert response.status_code == 302

    session.expire_all()
    appt = session.get(Appointment, appt.id)
    participation = get_appointment_participation(appt)
    assert appt.joined_count == 4
    assert appt.courts_reserved == 1
    assert participation.confirmed_names == ["B", "C", "D", "E"]
    assert participation.waitlist_names == []

    outbox = (
        session.query(NotificationOutbox)
        .filter(NotificationOutbox.title.like("Late Participation Update%"))
        .order_by(NotificationOutbox.created_at.desc())
        .first()
    )
    assert outbox is not None
    assert "Removed from confirmed: A" in outbox.body
    assert "Promoted from waitlist: E" in outbox.body


def test_admin_bulk_vote_edit_recomputes_closed_appointment(session, client):
    admin = session.query(User).filter_by(username="admin").one()
    members = [_mk_member(session, name) for name in ["A", "B", "C", "D", "E"]]
    session.commit()

    _set_now(session, "2022-01-02T10:00:00")
    run_maintenance(session)
    session.commit()

    appt = _get_appointment_by_date(session, datetime(2022, 1, 11, 20, 0, 0))
    base_time = datetime(2022, 1, 2, 10, 0, 0)
    for idx, member in enumerate(members[:4], start=1):
        _set_vote(session, appt.id, member.id, True, base_time + timedelta(minutes=idx))
    _set_vote(session, appt.id, members[4].id, False)
    session.commit()

    _set_now(session, "2022-01-03T19:00:00")
    run_maintenance(session)
    session.commit()

    with client.session_transaction() as flask_session:
        flask_session["user_id"] = admin.id

    response = client.post(
        f"/appointments/{appt.id}/admin-votes",
        data={
            f"vote_{members[3].id}": "0",
            f"vote_{members[4].id}": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    session.expire_all()
    appt = session.get(Appointment, appt.id)
    participation = get_appointment_participation(appt)
    assert participation.confirmed_names == ["A", "B", "C", "E"]
    assert appt.joined_count == 4
    assert appt.courts_reserved == 1

    outbox = (
        session.query(NotificationOutbox)
        .filter(NotificationOutbox.title.like("Late Participation Update%"))
        .order_by(NotificationOutbox.created_at.desc())
        .first()
    )
    assert outbox is not None
    assert "Admin confirmed vote edits" in outbox.body
    assert "Removed from confirmed: D" in outbox.body
    assert "Confirmed now (4): A, B, C, E" in outbox.body


def test_three_player_exception_keeps_one_court_and_allows_late_join(session):
    members = [_mk_member(session, name) for name in ["A", "B", "C", "D"]]
    session.commit()

    _set_now(session, "2022-01-02T10:00:00")
    run_maintenance(session)
    session.commit()

    appt = _get_appointment_by_date(session, datetime(2022, 1, 11, 20, 0, 0))
    base_time = datetime(2022, 1, 2, 10, 0, 0)
    for idx, member in enumerate(members[:3], start=1):
        _set_vote(session, appt.id, member.id, True, base_time + timedelta(minutes=idx))
    session.commit()

    _set_now(session, "2022-01-03T19:00:00")
    run_maintenance(session)
    session.commit()

    session.refresh(appt)
    assert appt.status == "EXTENDED"
    assert appt.joined_count == 3
    assert appt.courts_reserved == 1
    assert is_voting_open_for_appointment(appt, datetime(2022, 1, 4, 12, 0, 0)) is True

    _set_now(session, "2022-01-04T12:00:00")
    _set_vote(session, appt.id, members[3].id, True, datetime(2022, 1, 4, 12, 0, 0))
    session.commit()
    run_maintenance(session)
    session.commit()

    session.refresh(appt)
    participation = get_appointment_participation(appt)
    assert appt.status == "CLOSED"
    assert appt.joined_count == 4
    assert appt.courts_reserved == 1
    assert participation.confirmed_names == ["A", "B", "C", "D"]


def test_auto_match_plan_and_notification_on_vote_close(session):
    members = [_mk_member(session, f"N{i}", rating=900 + i * 25) for i in range(1, 9)]
    session.commit()

    _set_now(session, "2022-01-02T10:00:00")
    run_maintenance(session)
    session.commit()

    appt = _get_appointment_by_date(session, datetime(2022, 1, 11, 20, 0, 0))
    for m in members:
        _set_vote(session, appt.id, m.id, True)
    session.commit()

    _set_now(session, "2022-01-03T19:00:00")
    run_maintenance(session)
    session.commit()

    plan = (
        session.query(MatchPlan)
        .filter(MatchPlan.appointment_id == appt.id)
        .order_by(MatchPlan.created_at.desc())
        .first()
    )
    assert plan is not None
    assert len(plan.games) > 0

    outbox = (
        session.query(NotificationOutbox)
        .filter(NotificationOutbox.title.like("Vote Closed Summary%"))
        .order_by(NotificationOutbox.created_at.desc())
        .first()
    )
    assert outbox is not None
    assert "How many courts to reserve" in outbox.body
    assert "Coming Tuesday Match Making" in outbox.body
    assert ("G1 C1" in outbox.body) or ("Not available" in outbox.body)


def test_role_rotation_distribution_over_many_weeks(session):
    members = [_mk_member(session, f"M{i}", rating=1000 + i * 15) for i in range(1, 11)]
    session.commit()

    _set_now(session, "2022-03-25T20:00:00")
    run_maintenance(session)
    session.commit()

    all_appts = session.query(Appointment).all()
    for appt in all_appts:
        for m in members:
            _set_vote(session, appt.id, m.id, True)
    session.commit()

    run_maintenance(session)
    session.commit()

    ball_rows = session.query(RoleAssignment).filter_by(role_type="BALL_CARRIER").all()
    reserve_rows = session.query(RoleAssignment).filter_by(role_type="RESERVER").all()

    assert len(ball_rows) > 0
    assert len(reserve_rows) > 0

    unique_ball = {r.member_id for r in ball_rows}
    unique_reserve = {r.member_id for r in reserve_rows}
    assert len(unique_ball) >= 5
    assert len(unique_reserve) >= 5

    per_appt_role_slots: dict[tuple[int, str], list[int]] = {}
    for row in reserve_rows + ball_rows:
        key = (row.appointment_id, row.role_type)
        per_appt_role_slots.setdefault(key, [])
        per_appt_role_slots[key].append(row.member_id)
    for member_ids in per_appt_role_slots.values():
        assert len(member_ids) == len(set(member_ids))


def test_matchmaking_guest_once_and_three_games(session):
    members = [_mk_member(session, f"P{i}", rating=900 + i * 30) for i in range(1, 8)]
    creator_user = session.query(User).filter_by(username="admin").one()
    session.commit()

    _set_now(session, "2022-01-01T18:30:00")
    run_maintenance(session)
    session.commit()

    appt = _get_appointment_by_date(session, datetime(2022, 1, 11, 20, 0, 0))
    for m in members:
        _set_vote(session, appt.id, m.id, True)
    guest = AppointmentGuest(
        appointment_id=appt.id,
        name="GuestX",
        closest_member_id=members[0].id,
        active=True,
    )
    session.add(guest)
    session.commit()

    plan = generate_match_plan(session, appt.id, creator_user.id)
    session.commit()

    assert plan is not None
    rounds = {g.game_index for g in plan.games}
    assert rounds == {1, 2, 3}

    guest_participant = [p for p in plan.participants if p.is_guest][0]
    guest_games = [
        g
        for g in plan.games
        if guest_participant.id in [g.team_a_p1_id, g.team_a_p2_id, g.team_b_p1_id, g.team_b_p2_id]
    ]
    assert len(guest_games) <= 1


def test_submit_results_updates_leaderboard(session):
    members = [_mk_member(session, f"R{i}", rating=1000 + i * 20) for i in range(1, 9)]
    creator_user = session.query(User).filter_by(username="admin").one()
    session.commit()

    _set_now(session, "2022-01-01T18:30:00")
    run_maintenance(session)
    session.commit()

    appt = _get_appointment_by_date(session, datetime(2022, 1, 11, 20, 0, 0))
    for m in members:
        _set_vote(session, appt.id, m.id, True)
    session.commit()

    plan = generate_match_plan(session, appt.id, creator_user.id)
    session.commit()

    before = {m.id: m.skill_rating for m in members}

    scores = {g.id: (6, 4) for g in plan.games}
    submit_results(session, plan.id, scores)
    session.commit()

    changed = 0
    for m in members:
        session.refresh(m)
        if abs(m.skill_rating - before[m.id]) > 1e-6:
            changed += 1
    assert changed > 0


def test_delayed_rating_apply_after_result_window_close(session):
    members = [_mk_member(session, f"W{i}", rating=1000 + i * 10) for i in range(1, 9)]
    creator_user = session.query(User).filter_by(username="admin").one()
    session.commit()

    _set_now(session, "2022-01-01T18:30:00")
    run_maintenance(session)
    session.commit()

    appt = _get_appointment_by_date(session, datetime(2022, 1, 11, 20, 0, 0))
    for m in members:
        _set_vote(session, appt.id, m.id, True)
    session.commit()

    plan = generate_match_plan(session, appt.id, creator_user.id)
    session.commit()

    scores = {g.id: (6, 4) for g in plan.games}
    save_user_result_inputs(session, plan.id, creator_user.id, scores)
    session.commit()

    # Before the one-day result window closes, ratings must not apply.
    finalize_due_match_results(session, datetime(2022, 1, 12, 19, 30, 0))
    session.commit()
    before_events = session.query(RatingEvent).filter_by(match_plan_id=plan.id).count()
    assert before_events == 0

    # After the result window closes, ratings should apply.
    finalize_due_match_results(session, datetime(2022, 1, 12, 20, 5, 0))
    session.commit()
    after_events = session.query(RatingEvent).filter_by(match_plan_id=plan.id).count()
    assert after_events > 0
