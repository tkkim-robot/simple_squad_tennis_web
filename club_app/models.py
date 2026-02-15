from __future__ import annotations

from datetime import datetime
from typing import Optional

from werkzeug.security import generate_password_hash

from . import db


UTC_NOW = datetime.utcnow


class TimestampMixin:
    created_at = db.Column(db.DateTime, default=UTC_NOW, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=UTC_NOW,
        onupdate=UTC_NOW,
        nullable=False,
    )


class User(TimestampMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)

    member = db.relationship("Member", back_populates="user", uselist=False)


class Member(TimestampMixin, db.Model):
    __tablename__ = "members"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120))
    phone = db.Column(db.String(40))
    skill_rating = db.Column(db.Float, default=1000.0, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    notes = db.Column(db.Text)

    ball_wait_count = db.Column(db.Integer, default=0, nullable=False)
    reserver_wait_count = db.Column(db.Integer, default=0, nullable=False)
    last_joined_at = db.Column(db.DateTime)
    last_ball_assigned_at = db.Column(db.DateTime)
    last_reserver_assigned_at = db.Column(db.DateTime)

    user = db.relationship("User", back_populates="member")
    votes = db.relationship("AppointmentVote", back_populates="member", cascade="all,delete")


class MemberCounterSeed(TimestampMixin, db.Model):
    __tablename__ = "member_counter_seeds"
    __table_args__ = (
        db.UniqueConstraint("member_id", name="uq_member_counter_seed_member"),
    )

    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False, index=True)
    ball_seed = db.Column(db.Integer, default=0, nullable=False)
    reserver_seed = db.Column(db.Integer, default=0, nullable=False)

    member = db.relationship("Member")


class Setting(db.Model):
    __tablename__ = "settings"

    key = db.Column(db.String(128), primary_key=True)
    value = db.Column(db.Text, nullable=False)


class Appointment(TimestampMixin, db.Model):
    __tablename__ = "appointments"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    event_start = db.Column(db.DateTime, nullable=False, index=True)
    event_end = db.Column(db.DateTime, nullable=False)
    vote_open_at = db.Column(db.DateTime, nullable=False)
    vote_close_at = db.Column(db.DateTime, nullable=False, index=True)
    created_trigger_at = db.Column(db.DateTime)

    status = db.Column(db.String(32), default="OPEN", nullable=False, index=True)
    joined_count = db.Column(db.Integer, default=0, nullable=False)
    courts_reserved = db.Column(db.Integer, default=0, nullable=False)
    finalized_at = db.Column(db.DateTime)
    notification_sent_at = db.Column(db.DateTime)
    auto_generated = db.Column(db.Boolean, default=True, nullable=False)

    votes = db.relationship("AppointmentVote", back_populates="appointment", cascade="all,delete")
    role_assignments = db.relationship(
        "RoleAssignment",
        back_populates="appointment",
        cascade="all,delete",
        order_by="RoleAssignment.slot_index",
    )
    guests = db.relationship("AppointmentGuest", back_populates="appointment", cascade="all,delete")
    match_plans = db.relationship("MatchPlan", back_populates="appointment")


class AppointmentVote(db.Model):
    __tablename__ = "appointment_votes"
    __table_args__ = (
        db.UniqueConstraint("appointment_id", "member_id", name="uq_appointment_member_vote"),
    )

    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointments.id"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False)
    will_join = db.Column(db.Boolean, default=True, nullable=False)
    updated_at = db.Column(db.DateTime, default=UTC_NOW, onupdate=UTC_NOW, nullable=False)

    appointment = db.relationship("Appointment", back_populates="votes")
    member = db.relationship("Member", back_populates="votes")


class RoleAssignment(TimestampMixin, db.Model):
    __tablename__ = "role_assignments"
    __table_args__ = (
        db.UniqueConstraint(
            "appointment_id",
            "role_type",
            "slot_index",
            name="uq_appointment_role_slot",
        ),
        {"sqlite_autoincrement": True},
    )

    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointments.id"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False)
    role_type = db.Column(db.String(32), nullable=False)  # BALL_CARRIER or RESERVER
    slot_index = db.Column(db.Integer, default=0, nullable=False)
    source = db.Column(db.String(32), default="AUTO", nullable=False)

    appointment = db.relationship("Appointment", back_populates="role_assignments")
    member = db.relationship("Member")


class AppointmentGuest(TimestampMixin, db.Model):
    __tablename__ = "appointment_guests"

    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointments.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    closest_member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)

    appointment = db.relationship("Appointment", back_populates="guests")
    closest_member = db.relationship("Member")


class NotificationOutbox(TimestampMixin, db.Model):
    __tablename__ = "notification_outbox"

    id = db.Column(db.Integer, primary_key=True)
    channel = db.Column(db.String(32), default="LOG", nullable=False)
    target = db.Column(db.String(256))
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(32), default="PENDING", nullable=False)
    sent_at = db.Column(db.DateTime)
    error = db.Column(db.Text)


class MatchPlan(TimestampMixin, db.Model):
    __tablename__ = "match_plans"

    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointments.id"))
    event_date = db.Column(db.Date, nullable=False, index=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    notes = db.Column(db.Text)

    appointment = db.relationship("Appointment", back_populates="match_plans")
    created_by = db.relationship("User")
    participants = db.relationship(
        "MatchPlanParticipant",
        back_populates="match_plan",
        cascade="all,delete",
        order_by="MatchPlanParticipant.id",
    )
    games = db.relationship(
        "MatchPlanGame",
        back_populates="match_plan",
        cascade="all,delete",
        order_by="MatchPlanGame.game_index,MatchPlanGame.court_index",
    )
    practices = db.relationship(
        "PracticeAssignment",
        back_populates="match_plan",
        cascade="all,delete",
        order_by="PracticeAssignment.game_index,PracticeAssignment.id",
    )


class MatchPlanParticipant(TimestampMixin, db.Model):
    __tablename__ = "match_plan_participants"

    id = db.Column(db.Integer, primary_key=True)
    match_plan_id = db.Column(db.Integer, db.ForeignKey("match_plans.id"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"))
    display_name = db.Column(db.String(120), nullable=False)
    is_guest = db.Column(db.Boolean, default=False, nullable=False)
    guest_name = db.Column(db.String(120))
    anchor_member_id = db.Column(db.Integer, db.ForeignKey("members.id"))
    skill_rating = db.Column(db.Float, nullable=False)
    max_games = db.Column(db.Integer, default=3, nullable=False)

    match_plan = db.relationship("MatchPlan", back_populates="participants")
    member = db.relationship("Member", foreign_keys=[member_id])
    anchor_member = db.relationship("Member", foreign_keys=[anchor_member_id])


class MatchPlanGame(TimestampMixin, db.Model):
    __tablename__ = "match_plan_games"
    __table_args__ = (
        db.UniqueConstraint("match_plan_id", "game_index", "court_index", name="uq_game_slot"),
    )

    id = db.Column(db.Integer, primary_key=True)
    match_plan_id = db.Column(db.Integer, db.ForeignKey("match_plans.id"), nullable=False)
    game_index = db.Column(db.Integer, nullable=False)
    court_index = db.Column(db.Integer, nullable=False)

    team_a_p1_id = db.Column(db.Integer, db.ForeignKey("match_plan_participants.id"), nullable=False)
    team_a_p2_id = db.Column(db.Integer, db.ForeignKey("match_plan_participants.id"), nullable=False)
    team_b_p1_id = db.Column(db.Integer, db.ForeignKey("match_plan_participants.id"), nullable=False)
    team_b_p2_id = db.Column(db.Integer, db.ForeignKey("match_plan_participants.id"), nullable=False)

    team_a_score = db.Column(db.Integer)
    team_b_score = db.Column(db.Integer)

    match_plan = db.relationship("MatchPlan", back_populates="games")
    team_a_p1 = db.relationship("MatchPlanParticipant", foreign_keys=[team_a_p1_id])
    team_a_p2 = db.relationship("MatchPlanParticipant", foreign_keys=[team_a_p2_id])
    team_b_p1 = db.relationship("MatchPlanParticipant", foreign_keys=[team_b_p1_id])
    team_b_p2 = db.relationship("MatchPlanParticipant", foreign_keys=[team_b_p2_id])
    result_inputs = db.relationship(
        "MatchResultInput",
        back_populates="match_game",
        cascade="all,delete",
        order_by="MatchResultInput.updated_at.desc()",
    )


class PracticeAssignment(TimestampMixin, db.Model):
    __tablename__ = "practice_assignments"
    __table_args__ = (
        db.UniqueConstraint(
            "match_plan_id",
            "game_index",
            "participant_id",
            name="uq_practice_assignment",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    match_plan_id = db.Column(db.Integer, db.ForeignKey("match_plans.id"), nullable=False)
    game_index = db.Column(db.Integer, nullable=False)
    participant_id = db.Column(db.Integer, db.ForeignKey("match_plan_participants.id"), nullable=False)

    match_plan = db.relationship("MatchPlan", back_populates="practices")
    participant = db.relationship("MatchPlanParticipant")


class MatchResultInput(TimestampMixin, db.Model):
    __tablename__ = "match_result_inputs"
    __table_args__ = (
        db.UniqueConstraint("match_game_id", "user_id", name="uq_match_result_game_user"),
    )

    id = db.Column(db.Integer, primary_key=True)
    match_game_id = db.Column(db.Integer, db.ForeignKey("match_plan_games.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    team_a_score = db.Column(db.Integer, nullable=False)
    team_b_score = db.Column(db.Integer, nullable=False)
    updated_at = db.Column(db.DateTime, default=UTC_NOW, onupdate=UTC_NOW, nullable=False)

    match_game = db.relationship("MatchPlanGame", back_populates="result_inputs")
    user = db.relationship("User")


class RatingEvent(TimestampMixin, db.Model):
    __tablename__ = "rating_events"

    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False, index=True)
    match_plan_id = db.Column(db.Integer, db.ForeignKey("match_plans.id"), nullable=False)
    match_game_id = db.Column(db.Integer, db.ForeignKey("match_plan_games.id"), nullable=False)
    delta = db.Column(db.Float, nullable=False)
    new_rating = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(200))

    member = db.relationship("Member")
    match_plan = db.relationship("MatchPlan")
    match_game = db.relationship("MatchPlanGame")


def ensure_admin_user(session) -> None:
    admin = session.query(User).filter_by(username="admin").one_or_none()
    if admin is None:
        admin = User(
            username="admin",
            password_hash=generate_password_hash("annarbor", method="pbkdf2:sha256"),
            is_admin=True,
        )
        session.add(admin)
        session.flush()
    elif not admin.is_admin:
        admin.is_admin = True

    if admin.member is None:
        member = Member(
            user=admin,
            name="Admin",
            email="",
            phone="",
            active=True,
            notes="System admin account",
        )
        session.add(member)
