from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from club_app import create_app, db
from club_app.models import Appointment, AppointmentVote, Member
from club_app.services.appointments import run_maintenance
from club_app.services.settings_store import set_setting


NAMES = [
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
    "Mina",
    "Noah",
]


def main() -> None:
    app = create_app()
    with app.app_context():
        # Remove non-admin members for a clean QA sandbox run.
        for member in db.session.query(Member).filter(Member.name != "Admin").all():
            db.session.delete(member)
        db.session.commit()

        random.shuffle(NAMES)
        for name in NAMES[:10]:
            db.session.add(
                Member(
                    name=name,
                    email=f"{name.lower()}@test.local",
                    phone="",
                    skill_rating=random.randint(850, 1250),
                    active=True,
                    notes="qa-seeded",
                )
            )
        db.session.commit()

        current = datetime(2022, 1, 1, 18, 0, 0)

        for _ in range(25):
            set_setting(db.session, "qa_now_iso", current.isoformat())
            run_maintenance(db.session)

            future_appointments = (
                db.session.query(Appointment)
                .filter(Appointment.event_start > current)
                .order_by(Appointment.event_start.asc())
                .all()
            )
            if future_appointments:
                target = future_appointments[0]
                active_members = db.session.query(Member).filter(Member.active.is_(True), Member.name != "Admin").all()
                for member in active_members:
                    join = random.random() > 0.35
                    vote = (
                        db.session.query(AppointmentVote)
                        .filter_by(appointment_id=target.id, member_id=member.id)
                        .one_or_none()
                    )
                    if vote is None:
                        vote = AppointmentVote(
                            appointment_id=target.id,
                            member_id=member.id,
                            will_join=join,
                        )
                        db.session.add(vote)
                    else:
                        vote.will_join = join

            db.session.commit()
            current += timedelta(days=1)

        print("QA seed simulation complete.")


if __name__ == "__main__":
    main()
