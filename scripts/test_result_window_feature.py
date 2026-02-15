from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from club_app import create_app, db
from club_app.models import Appointment, AppointmentVote, Member, RatingEvent, User
from club_app.services.appointments import run_maintenance
from club_app.services.matchmaking import (
    finalize_due_match_results,
    generate_match_plan,
    save_user_result_inputs,
)
from club_app.services.settings_store import set_setting


def _set_vote(session, appointment_id: int, member_id: int, will_join: bool) -> None:
    row = (
        session.query(AppointmentVote)
        .filter_by(appointment_id=appointment_id, member_id=member_id)
        .one_or_none()
    )
    if row is None:
        session.add(AppointmentVote(appointment_id=appointment_id, member_id=member_id, will_join=will_join))
    else:
        row.will_join = will_join


def main() -> None:
    temp_db = Path("/tmp/annarbortennis_result_window_test.db")
    if temp_db.exists():
        temp_db.unlink()

    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{temp_db}",
            "SECRET_KEY": "result-window-test",
        }
    )

    with app.app_context():
        for i in range(8):
            db.session.add(Member(name=f"T{i+1}", skill_rating=1000 + i * 5, active=True))
        db.session.commit()

        admin = db.session.query(User).filter_by(username="admin").one()
        set_setting(db.session, "qa_now_iso", "2022-01-01T18:30:00")
        run_maintenance(db.session)
        db.session.commit()

        appt = db.session.query(Appointment).filter(Appointment.event_start == datetime(2022, 1, 11, 20, 0, 0)).one()
        members = db.session.query(Member).filter(Member.name.like("T%")).all()
        for member in members:
            _set_vote(db.session, appt.id, member.id, True)
        db.session.commit()

        plan = generate_match_plan(db.session, appt.id, admin.id, rounds=3)
        db.session.commit()

        scores = {g.id: (6, 4) for g in plan.games}
        save_user_result_inputs(db.session, plan.id, admin.id, scores)
        db.session.commit()

        finalize_due_match_results(db.session, datetime(2022, 1, 12, 8, 30, 0))
        db.session.commit()
        pre_close_events = db.session.query(RatingEvent).filter(RatingEvent.match_plan_id == plan.id).count()

        finalize_due_match_results(db.session, datetime(2022, 1, 12, 9, 10, 0))
        db.session.commit()
        post_close_events = db.session.query(RatingEvent).filter(RatingEvent.match_plan_id == plan.id).count()

        print(f"pre_close_rating_events={pre_close_events}")
        print(f"post_close_rating_events={post_close_events}")
        print("result_window_test=PASS" if pre_close_events == 0 and post_close_events > 0 else "result_window_test=FAIL")

    if temp_db.exists():
        temp_db.unlink()


if __name__ == "__main__":
    main()
