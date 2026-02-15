from __future__ import annotations

import json
from datetime import datetime
from functools import wraps

from flask import (
    Blueprint,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from . import db
from .models import (
    Appointment,
    AppointmentGuest,
    AppointmentVote,
    MatchPlan,
    MatchResultInput,
    Member,
    NotificationOutbox,
    RatingEvent,
    RoleAssignment,
    User,
)
from .services.appointments import create_missing_vote_rows, recompute_appointments, run_maintenance
from .services.matchmaking import (
    finalize_due_match_results,
    get_or_create_match_plan,
    is_result_window_open,
    result_window_bounds,
    save_user_result_inputs,
    submit_results,
)
from .services.notifications import queue_notification
from .services.settings_store import all_settings_dict, app_now, set_setting


bp = Blueprint("web", __name__)


def _login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("web.login"))
        return fn(*args, **kwargs)

    return wrapper


def _admin_required(fn):
    @wraps(fn)
    @_login_required
    def wrapper(*args, **kwargs):
        if not g.user.is_admin:
            flash("Admin access is required.", "error")
            return redirect(url_for("web.index"))
        return fn(*args, **kwargs)

    return wrapper


@bp.before_app_request
def load_user_and_run_maintenance() -> None:
    user_id = session.get("user_id")
    g.user = db.session.get(User, user_id) if user_id else None

    if request.endpoint and request.endpoint.startswith("static"):
        return

    try:
        run_maintenance(db.session)
        finalize_due_match_results(db.session, app_now(db.session))
        db.session.commit()
    except Exception:
        db.session.rollback()


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        display_name = (request.form.get("display_name") or username).strip()

        if not username or not password:
            flash("Username and password are required.", "error")
            return redirect(url_for("web.signup"))

        existing = db.session.query(User).filter_by(username=username).one_or_none()
        if existing:
            flash("Username already exists.", "error")
            return redirect(url_for("web.signup"))

        user = User(
            username=username,
            password_hash=generate_password_hash(password, method="pbkdf2:sha256"),
            is_admin=False,
        )
        db.session.add(user)
        db.session.flush()

        member = Member(
            user_id=user.id,
            name=display_name,
            email="",
            phone="",
            skill_rating=1000.0,
            active=True,
            notes="",
        )
        db.session.add(member)
        db.session.commit()

        flash("Account created. Please sign in.", "ok")
        return redirect(url_for("web.login"))

    return render_template("auth.html", mode="signup")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        user = db.session.query(User).filter_by(username=username).one_or_none()
        if user is None or not check_password_hash(user.password_hash, password):
            flash("Invalid credentials.", "error")
            return redirect(url_for("web.login"))

        session.clear()
        session["user_id"] = user.id
        return redirect(url_for("web.index"))

    return render_template("auth.html", mode="login")


@bp.route("/logout", methods=["POST"])
@_login_required
def logout():
    session.clear()
    return redirect(url_for("web.login"))


def _snapshot_ball_assignments(now_dt: datetime) -> dict[int, tuple[int, ...]]:
    result: dict[int, tuple[int, ...]] = {}
    appointments = (
        db.session.query(Appointment)
        .filter(Appointment.event_start >= now_dt)
        .order_by(Appointment.event_start.asc())
        .all()
    )
    for appt in appointments:
        ids = [
            ra.member_id
            for ra in appt.role_assignments
            if ra.role_type == "BALL_CARRIER"
        ]
        result[appt.id] = tuple(sorted(ids))
    return result


@bp.route("/")
@_login_required
def index():
    now = app_now(db.session)

    members = db.session.query(Member).order_by(Member.name.asc()).all()
    appointments = db.session.query(Appointment).order_by(Appointment.event_start.asc()).all()
    for appt in appointments:
        create_missing_vote_rows(db.session, appt)
    db.session.commit()

    open_appointment = next((a for a in appointments if a.status == "OPEN"), None)
    upcoming_appointment = next((a for a in appointments if a.event_start >= now), None)
    upcoming_appointment_id = upcoming_appointment.id if upcoming_appointment else None

    match_plans = db.session.query(MatchPlan).order_by(MatchPlan.created_at.desc()).limit(12).all()
    ratings_applied_plan_ids = {
        pid
        for (pid,) in db.session.query(RatingEvent.match_plan_id)
        .distinct()
        .filter(RatingEvent.match_plan_id.isnot(None))
        .all()
    }

    active_result_plan = None
    active_result_open_at = None
    active_result_close_at = None
    for plan in match_plans:
        if plan.appointment is None:
            continue
        if plan.id in ratings_applied_plan_ids:
            continue
        open_at, close_at = result_window_bounds(plan.appointment.event_start)
        if open_at <= now < close_at:
            active_result_plan = plan
            active_result_open_at = open_at
            active_result_close_at = close_at
            break

    active_result_user_inputs: dict[int, tuple[int, int]] = {}
    if active_result_plan and g.user:
        existing_inputs = (
            db.session.query(MatchResultInput)
            .filter(
                MatchResultInput.user_id == g.user.id,
                MatchResultInput.match_game_id.in_([g.id for g in active_result_plan.games]),
            )
            .all()
        )
        for row in existing_inputs:
            active_result_user_inputs[row.match_game_id] = (row.team_a_score, row.team_b_score)
    notifications = (
        db.session.query(NotificationOutbox)
        .order_by(NotificationOutbox.created_at.desc())
        .limit(20)
        .all()
    )
    leaderboard = db.session.query(Member).order_by(Member.skill_rating.desc(), Member.name.asc()).all()
    recent_rating_events = (
        db.session.query(RatingEvent)
        .order_by(RatingEvent.created_at.desc())
        .limit(50)
        .all()
    )

    settings = all_settings_dict(db.session)
    settings["pretty_json_court_rules"] = settings.get("court_rules_json", "[]")

    active_tab = request.args.get("tab")
    if not active_tab:
        active_tab = "appointments" if open_appointment else "squad"

    return render_template(
        "dashboard.html",
        now=now,
        user=g.user,
        members=members,
        appointments=appointments,
        open_appointment=open_appointment,
        upcoming_appointment=upcoming_appointment,
        upcoming_appointment_id=upcoming_appointment_id,
        active_tab=active_tab,
        active_result_plan=active_result_plan,
        active_result_open_at=active_result_open_at,
        active_result_close_at=active_result_close_at,
        active_result_user_inputs=active_result_user_inputs,
        ratings_applied_plan_ids=ratings_applied_plan_ids,
        match_plans=match_plans,
        notifications=notifications,
        leaderboard=leaderboard,
        recent_rating_events=recent_rating_events,
        settings=settings,
    )


@bp.route("/squad/add", methods=["POST"])
@_admin_required
def add_member():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Name is required.", "error")
        return redirect(url_for("web.index", tab="squad"))

    member = Member(
        name=name,
        email=(request.form.get("email") or "").strip(),
        phone=(request.form.get("phone") or "").strip(),
        skill_rating=float(request.form.get("skill_rating") or 1000),
        active=request.form.get("active") == "on",
        notes=(request.form.get("notes") or "").strip(),
    )
    db.session.add(member)
    db.session.commit()

    flash(f"Added member {name}.", "ok")
    return redirect(url_for("web.index", tab="squad"))


@bp.route("/squad/<int:member_id>/update", methods=["POST"])
@_admin_required
def update_member(member_id: int):
    member = db.session.get(Member, member_id)
    if member is None:
        flash("Member not found.", "error")
        return redirect(url_for("web.index", tab="squad"))

    member.name = (request.form.get("name") or member.name).strip()
    member.email = (request.form.get("email") or "").strip()
    member.phone = (request.form.get("phone") or "").strip()
    member.notes = (request.form.get("notes") or "").strip()
    member.active = request.form.get("active") == "on"
    try:
        member.skill_rating = float(request.form.get("skill_rating") or member.skill_rating)
    except ValueError:
        pass

    db.session.commit()
    flash(f"Updated member {member.name}.", "ok")
    return redirect(url_for("web.index", tab="squad"))


@bp.route("/squad/<int:member_id>/delete", methods=["POST"])
@_admin_required
def delete_member(member_id: int):
    member = db.session.get(Member, member_id)
    if member is None:
        flash("Member not found.", "error")
        return redirect(url_for("web.index", tab="squad"))

    if member.user_id:
        member.active = False
        db.session.commit()
        flash("Linked account member cannot be deleted; marked inactive instead.", "error")
    else:
        db.session.delete(member)
        db.session.commit()
        flash("Member deleted.", "ok")

    return redirect(url_for("web.index", tab="squad"))


@bp.route("/appointments/<int:appointment_id>/vote", methods=["POST"])
@_login_required
def vote_appointment(appointment_id: int):
    appointment = db.session.get(Appointment, appointment_id)
    if appointment is None:
        flash("Appointment not found.", "error")
        return redirect(url_for("web.index", tab="appointments"))

    if g.user.member is None:
        flash("This account is not linked to a squad member.", "error")
        return redirect(url_for("web.index", tab="appointments"))

    now = app_now(db.session)
    if appointment.event_start <= now:
        flash("This appointment has already started.", "error")
        return redirect(url_for("web.index", tab="appointments"))
    if appointment.status != "OPEN" or not (appointment.vote_open_at <= now < appointment.vote_close_at):
        flash("Voting is currently closed for this appointment.", "error")
        return redirect(url_for("web.index", tab="appointments"))

    will_join = request.form.get("will_join") == "1"

    vote = (
        db.session.query(AppointmentVote)
        .filter_by(appointment_id=appointment.id, member_id=g.user.member.id)
        .one_or_none()
    )
    if vote is None:
        vote = AppointmentVote(
            appointment_id=appointment.id,
            member_id=g.user.member.id,
            will_join=will_join,
        )
        db.session.add(vote)
    else:
        vote.will_join = will_join

    was_ball_carrier = any(
        ra.role_type == "BALL_CARRIER" and ra.member_id == g.user.member.id
        for ra in appointment.role_assignments
    )

    before = _snapshot_ball_assignments(now)
    run_maintenance(db.session)
    after = _snapshot_ball_assignments(now)

    if was_ball_carrier and not will_join and before != after:
        queue_notification(
            db.session,
            f"Ball Carrier Change Needed ({appointment.event_start.date().isoformat()})",
            (
                f"{g.user.member.name} canceled participation after ball-carrier assignment. "
                "The system recalculated assignments; admin should review replacements."
            ),
        )

    db.session.commit()
    flash("Your appointment vote was updated.", "ok")
    return redirect(url_for("web.index", tab="appointments"))


@bp.route("/appointments/<int:appointment_id>/guest/add", methods=["POST"])
@_admin_required
def add_guest(appointment_id: int):
    appointment = db.session.get(Appointment, appointment_id)
    if appointment is None:
        flash("Appointment not found.", "error")
        return redirect(url_for("web.index", tab="appointments"))

    name = (request.form.get("name") or "").strip()
    closest_member_id = request.form.get("closest_member_id")
    if not name or not closest_member_id:
        flash("Guest name and closest member are required.", "error")
        return redirect(url_for("web.index", tab="appointments"))

    closest = db.session.get(Member, int(closest_member_id))
    if closest is None:
        flash("Closest member not found.", "error")
        return redirect(url_for("web.index", tab="appointments"))

    guest = AppointmentGuest(
        appointment_id=appointment.id,
        name=name,
        closest_member_id=closest.id,
        active=True,
    )
    db.session.add(guest)

    run_maintenance(db.session)
    db.session.commit()

    flash(f"Guest {name} added for appointment.", "ok")
    return redirect(url_for("web.index", tab="appointments"))


@bp.route("/appointments/<int:appointment_id>/guest/<int:guest_id>/toggle", methods=["POST"])
@_admin_required
def toggle_guest(appointment_id: int, guest_id: int):
    guest = db.session.get(AppointmentGuest, guest_id)
    if guest is None or guest.appointment_id != appointment_id:
        flash("Guest record not found.", "error")
        return redirect(url_for("web.index", tab="appointments"))

    guest.active = not guest.active
    run_maintenance(db.session)
    db.session.commit()

    flash(f"Guest {guest.name} {'enabled' if guest.active else 'disabled'}.", "ok")
    return redirect(url_for("web.index", tab="appointments"))


@bp.route("/admin/run-maintenance", methods=["POST"])
@_admin_required
def admin_run_maintenance():
    run_maintenance(db.session)
    db.session.commit()
    flash("Maintenance executed.", "ok")
    return redirect(url_for("web.index", tab="appointments"))


@bp.route("/admin/recompute", methods=["POST"])
@_admin_required
def admin_recompute():
    recompute_appointments(db.session, app_now(db.session))
    db.session.commit()
    flash("Appointment counters and role assignments recomputed.", "ok")
    return redirect(url_for("web.index", tab="appointments"))


@bp.route("/settings/update", methods=["POST"])
@_admin_required
def update_settings():
    int_keys = [
        "auto_backfill_weeks",
        "auto_lookahead_weeks",
        "creation_weekday",
        "creation_hour",
        "creation_minute",
        "target_weekday",
        "target_occurrence",
        "event_start_hour",
        "event_start_minute",
        "event_duration_minutes",
        "vote_close_weekday",
        "vote_close_hour",
        "vote_close_minute",
        "min_players_to_run",
        "ball_carriers_per_court",
        "reservers_per_court",
        "smtp_port",
    ]

    for key in int_keys:
        value = request.form.get(key)
        if value is None:
            continue
        try:
            int(value)
        except ValueError:
            flash(f"Invalid integer for {key}", "error")
            return redirect(url_for("web.index", tab="settings"))
        set_setting(db.session, key, value)

    for key in [
        "notify_channel",
        "notify_target",
        "discord_webhook",
        "smtp_host",
        "smtp_user",
        "smtp_password",
        "smtp_from",
        "qa_now_iso",
    ]:
        if key in request.form:
            set_setting(db.session, key, request.form.get(key) or "")

    rules_text = request.form.get("court_rules_json")
    if rules_text is not None:
        try:
            parsed = json.loads(rules_text)
            if not isinstance(parsed, list):
                raise ValueError("Court rules must be a list")
            set_setting(db.session, "court_rules_json", parsed)
        except Exception:
            flash("Invalid JSON for court rules.", "error")
            return redirect(url_for("web.index", tab="settings"))

    run_maintenance(db.session)
    db.session.commit()
    flash("Settings updated.", "ok")
    return redirect(url_for("web.index", tab="settings"))


@bp.route("/matchmaking/generate", methods=["POST"])
@_admin_required
def generate_matchmaking_plan():
    appointment_id = int(request.form.get("appointment_id") or 0)
    if appointment_id <= 0:
        flash("Choose an appointment first.", "error")
        return redirect(url_for("web.index", tab="matchmaking"))

    try:
        plan = get_or_create_match_plan(db.session, appointment_id, g.user.id, rounds=3)
        db.session.commit()
        flash(f"Match plan ready: #{plan.id}.", "ok")
    except Exception as exc:
        db.session.rollback()
        flash(str(exc), "error")

    return redirect(url_for("web.index", tab="matchmaking"))


@bp.route("/matchmaking/<int:plan_id>/input-results", methods=["POST"])
@_login_required
def input_match_results(plan_id: int):
    plan = db.session.get(MatchPlan, plan_id)
    if plan is None or plan.appointment is None:
        flash("Match plan not found.", "error")
        return redirect(url_for("web.index", tab="matchmaking"))

    now = app_now(db.session)
    if not is_result_window_open(plan.appointment, now):
        flash("Result input window is closed (Tue 8pm to Wed 9am).", "error")
        return redirect(url_for("web.index", tab="matchmaking"))

    already_applied = (
        db.session.query(RatingEvent)
        .filter(RatingEvent.match_plan_id == plan.id)
        .count()
        > 0
    )
    if already_applied:
        flash("Ranking has already been applied for this match plan.", "error")
        return redirect(url_for("web.index", tab="matchmaking"))

    scores: dict[int, tuple[int, int]] = {}
    for game in plan.games:
        a_val = request.form.get(f"game_{game.id}_a")
        b_val = request.form.get(f"game_{game.id}_b")
        if a_val is None or b_val is None or a_val == "" or b_val == "":
            continue
        try:
            a_score = int(a_val)
            b_score = int(b_val)
        except ValueError:
            flash("Scores must be integers.", "error")
            return redirect(url_for("web.index", tab="matchmaking"))
        if a_score < 0 or b_score < 0:
            flash("Scores must be non-negative.", "error")
            return redirect(url_for("web.index", tab="matchmaking"))
        scores[game.id] = (a_score, b_score)

    if not scores:
        flash("No result values were provided.", "error")
        return redirect(url_for("web.index", tab="matchmaking"))

    try:
        save_user_result_inputs(db.session, plan.id, g.user.id, scores)
        db.session.commit()
        flash("Results saved. Ranking updates will apply after Wed 9am.", "ok")
    except Exception as exc:
        db.session.rollback()
        flash(str(exc), "error")

    return redirect(url_for("web.index", tab="appointments"))


@bp.route("/matchmaking/<int:plan_id>/submit-results", methods=["POST"])
@_admin_required
def submit_match_results(plan_id: int):
    plan = db.session.get(MatchPlan, plan_id)
    if plan is None or plan.appointment is None:
        flash("Match plan not found.", "error")
        return redirect(url_for("web.index", tab="matchmaking"))

    now = app_now(db.session)
    _, close_at = result_window_bounds(plan.appointment.event_start)
    if now < close_at:
        flash("Ranking cannot be applied before Wed 9am. Use result input instead.", "error")
        return redirect(url_for("web.index", tab="matchmaking"))

    already_applied = (
        db.session.query(RatingEvent)
        .filter(RatingEvent.match_plan_id == plan.id)
        .count()
        > 0
    )
    if already_applied:
        flash("Ranking has already been applied for this match plan.", "error")
        return redirect(url_for("web.index", tab="matchmaking"))

    scores: dict[int, tuple[int, int]] = {}
    for game in plan.games:
        a_val = request.form.get(f"game_{game.id}_a")
        b_val = request.form.get(f"game_{game.id}_b")
        if a_val is None or b_val is None or a_val == "" or b_val == "":
            continue
        try:
            scores[game.id] = (int(a_val), int(b_val))
        except ValueError:
            flash("Scores must be integers.", "error")
            return redirect(url_for("web.index", tab="matchmaking"))

    try:
        submit_results(db.session, plan_id, scores)
        db.session.commit()
        flash("Results submitted and leaderboard updated.", "ok")
    except Exception as exc:
        db.session.rollback()
        flash(str(exc), "error")

    return redirect(url_for("web.index", tab="leaderboard"))
