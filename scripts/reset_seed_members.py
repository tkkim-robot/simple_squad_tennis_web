from __future__ import annotations

import sys
from pathlib import Path

from werkzeug.security import generate_password_hash

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
    MatchResultInput,
    MemberCounterSeed,
    Member,
    NotificationOutbox,
    PracticeAssignment,
    RatingEvent,
    RoleAssignment,
    User,
    ensure_admin_user,
)
from club_app.services.appointments import run_maintenance
from club_app.services.settings_store import set_setting


SEED_MEMBERS = [
    ("김태경", 3, 3),
    ("이수연", 1, 1),
    ("석원준", 1, 1),
    ("심태현", 4, 1),
    ("오혁근", 1, 1),
    ("조운수", 4, 0),
    ("김지연", 1, 3),
    ("최준석", 2, 0),
    ("우호철", 1, 1),
    ("김태호", 1, 4),
    ("한동헌", 0, 0),
    ("윤지영", 3, 1),
    ("민진홍", 0, 1),
    ("윤정현", 3, 0),
    ("정정윤", 3, 3),
    ("조세영", 3, 1),
    ("용현", 1, 2),
    ("양태원", 0, 0),
    ("한상현", 1, 1),
    ("이수환", 0, 3),
    ("김도훈", 2, 3),
    ("변주성", 10, 0),
    ("이권민", 1, 1),
    ("전명환", 1, 1),
]


def main() -> None:
    with app.app_context():
        ensure_admin_user(db.session)
        db.session.flush()

        admin = db.session.query(User).filter(User.username == "admin").one()
        admin_member_id = admin.member.id if admin.member else None

        # Clear operational data.
        db.session.query(RatingEvent).delete()
        db.session.query(MatchResultInput).delete()
        db.session.query(MemberCounterSeed).delete()
        db.session.query(PracticeAssignment).delete()
        db.session.query(MatchPlanGame).delete()
        db.session.query(MatchPlanParticipant).delete()
        db.session.query(MatchPlan).delete()
        db.session.query(AppointmentGuest).delete()
        db.session.query(RoleAssignment).delete()
        db.session.query(AppointmentVote).delete()
        db.session.query(Appointment).delete()
        db.session.query(NotificationOutbox).delete()

        # Remove all non-admin users and all non-admin members.
        non_admin_users = db.session.query(User).filter(User.id != admin.id).all()
        for user in non_admin_users:
            db.session.delete(user)

        members = db.session.query(Member).all()
        for member in members:
            if admin_member_id is not None and member.id == admin_member_id:
                member.name = "Admin"
                member.ball_wait_count = 0
                member.reserver_wait_count = 0
                member.active = True
                member.skill_rating = 1000.0
                member.notes = "System admin account"
                continue
            db.session.delete(member)

        db.session.flush()

        # Create member accounts.
        default_password = "0000"
        for name, ball_count, reserve_count in SEED_MEMBERS:
            username = name.strip()
            user = User(
                username=username,
                password_hash=generate_password_hash(default_password, method="pbkdf2:sha256"),
                is_admin=False,
            )
            db.session.add(user)
            db.session.flush()

            member = Member(
                user_id=user.id,
                name=name.strip(),
                email="",
                phone="",
                skill_rating=1000.0,
                active=True,
                notes="seeded",
                ball_wait_count=ball_count,
                reserver_wait_count=reserve_count,
            )
            db.session.add(member)
            db.session.flush()
            db.session.add(
                MemberCounterSeed(
                    member_id=member.id,
                    ball_seed=ball_count,
                    reserver_seed=reserve_count,
                )
            )

        # Use real Detroit/Eastern time by default.
        set_setting(db.session, "qa_now_iso", "")
        set_setting(db.session, "auto_backfill_weeks", "0")
        set_setting(db.session, "auto_lookahead_weeks", "4")
        set_setting(db.session, "notify_channel", "DISCORD")

        db.session.flush()
        run_maintenance(db.session)
        db.session.commit()

        print("reset_complete=yes")
        print("admin_account=admin / annarbor")
        print("member_default_password=0000")
        print("accounts_created=24")
        print("username,name,ball_counter,reserve_counter")
        for name, ball_count, reserve_count in SEED_MEMBERS:
            print(f"{name.strip()},{name.strip()},{ball_count},{reserve_count}")


if __name__ == "__main__":
    main()
