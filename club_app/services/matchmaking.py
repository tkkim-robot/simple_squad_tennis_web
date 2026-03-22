from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import combinations
from typing import Iterable

from ortools.sat.python import cp_model

from ..models import (
    Appointment,
    MatchPlan,
    MatchPlanGame,
    MatchPlanParticipant,
    MatchResultInput,
    Member,
    PracticeAssignment,
    RatingEvent,
)
from .appointments import get_appointment_participation
from .settings_store import app_now


@dataclass(frozen=True)
class PlannerParticipant:
    idx: int
    display_name: str
    skill_rating: float
    max_games: int
    member_id: int | None
    is_guest: bool


@dataclass
class CandidateMatch:
    p1: int
    p2: int
    p3: int
    p4: int
    score: int


def _pair_key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _member_pair_key(a: int | None, b: int | None) -> tuple[int, int] | None:
    if a is None or b is None:
        return None
    return _pair_key(a, b)


def _load_history_penalties(session, lookback_days: int = 120) -> tuple[dict[tuple[int, int], int], dict[tuple[int, int], int]]:
    team_counts: dict[tuple[int, int], int] = {}
    opp_counts: dict[tuple[int, int], int] = {}

    cutoff = app_now(session).date() - timedelta(days=lookback_days)
    plans = session.query(MatchPlan).filter(MatchPlan.event_date >= cutoff).all()

    participant_member_map: dict[int, int | None] = {}
    for plan in plans:
        for participant in plan.participants:
            participant_member_map[participant.id] = participant.member_id

        for game in plan.games:
            a1 = participant_member_map.get(game.team_a_p1_id)
            a2 = participant_member_map.get(game.team_a_p2_id)
            b1 = participant_member_map.get(game.team_b_p1_id)
            b2 = participant_member_map.get(game.team_b_p2_id)

            for pair in [_member_pair_key(a1, a2), _member_pair_key(b1, b2)]:
                if pair:
                    team_counts[pair] = team_counts.get(pair, 0) + 1

            for left in [a1, a2]:
                for right in [b1, b2]:
                    pair = _member_pair_key(left, right)
                    if pair:
                        opp_counts[pair] = opp_counts.get(pair, 0) + 1

    return team_counts, opp_counts


def _candidate_score(
    candidate: tuple[PlannerParticipant, PlannerParticipant, PlannerParticipant, PlannerParticipant],
    team_history: dict[tuple[int, int], int],
    opp_history: dict[tuple[int, int], int],
    same_day_team_counts: dict[tuple[int, int], int],
    same_day_opp_counts: dict[tuple[int, int], int],
    play_counts: dict[int, int],
) -> tuple[int, list[tuple[int, int]], list[tuple[int, int]]]:
    a, b, c, d = candidate

    team_pairs = [_pair_key(a.idx, b.idx), _pair_key(c.idx, d.idx)]
    opp_pairs = [
        _pair_key(a.idx, c.idx),
        _pair_key(a.idx, d.idx),
        _pair_key(b.idx, c.idx),
        _pair_key(b.idx, d.idx),
    ]

    # Balance is intentionally loose to encourage exploration.
    skill_gap = abs((a.skill_rating + b.skill_rating) - (c.skill_rating + d.skill_rating))
    teammate_gap = abs(a.skill_rating - b.skill_rating) + abs(c.skill_rating - d.skill_rating)

    fairness_bonus = sum(max(0, p.max_games - play_counts[p.idx]) for p in [a, b, c, d]) * 16

    history_team_penalty = 0
    history_opp_penalty = 0
    for left, right in [(a, b), (c, d)]:
        key = _member_pair_key(left.member_id, right.member_id)
        if key:
            history_team_penalty += team_history.get(key, 0) * 14

    for left in [a, b]:
        for right in [c, d]:
            key = _member_pair_key(left.member_id, right.member_id)
            if key:
                history_opp_penalty += opp_history.get(key, 0) * 6

    same_day_team_penalty = sum(same_day_team_counts.get(pair, 0) * 70 for pair in team_pairs)
    same_day_opp_penalty = sum(same_day_opp_counts.get(pair, 0) * 28 for pair in opp_pairs)

    # Small deterministic jitter prevents repetitive ties.
    jitter = ((a.idx + 3 * b.idx + 5 * c.idx + 7 * d.idx) % 11) - 5

    score = int(
        400
        + fairness_bonus
        - 0.4 * skill_gap
        - 0.15 * teammate_gap
        - history_team_penalty
        - history_opp_penalty
        - same_day_team_penalty
        - same_day_opp_penalty
        + jitter
    )

    return score, team_pairs, opp_pairs


def _generate_candidates(
    participants: list[PlannerParticipant],
    team_history: dict[tuple[int, int], int],
    opp_history: dict[tuple[int, int], int],
    same_day_team_counts: dict[tuple[int, int], int],
    same_day_opp_counts: dict[tuple[int, int], int],
    play_counts: dict[int, int],
) -> list[CandidateMatch]:
    output: list[CandidateMatch] = []

    for q in combinations(participants, 4):
        p0, p1, p2, p3 = q
        partitions = [
            (p0, p1, p2, p3),
            (p0, p2, p1, p3),
            (p0, p3, p1, p2),
        ]
        for cand in partitions:
            score, _, _ = _candidate_score(
                cand,
                team_history,
                opp_history,
                same_day_team_counts,
                same_day_opp_counts,
                play_counts,
            )
            output.append(
                CandidateMatch(
                    p1=cand[0].idx,
                    p2=cand[1].idx,
                    p3=cand[2].idx,
                    p4=cand[3].idx,
                    score=score,
                )
            )

    output.sort(key=lambda c: c.score, reverse=True)
    return output[:40000]


def _select_round_matches(
    candidates: list[CandidateMatch],
    participant_ids: list[int],
    match_count: int,
) -> list[CandidateMatch]:
    if match_count <= 0 or not candidates:
        return []

    model = cp_model.CpModel()
    vars_by_idx = [model.NewBoolVar(f"c{i}") for i in range(len(candidates))]

    model.Add(sum(vars_by_idx) == match_count)

    for pid in participant_ids:
        involved = [
            vars_by_idx[i]
            for i, c in enumerate(candidates)
            if pid in (c.p1, c.p2, c.p3, c.p4)
        ]
        if involved:
            model.Add(sum(involved) <= 1)

    model.Maximize(sum(vars_by_idx[i] * c.score for i, c in enumerate(candidates)))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 4.0
    solver.parameters.num_search_workers = 8
    result = solver.Solve(model)

    if result not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return []

    selected: list[CandidateMatch] = []
    for i, c in enumerate(candidates):
        if solver.BooleanValue(vars_by_idx[i]):
            selected.append(c)
    return selected


def generate_match_plan(
    session,
    appointment_id: int,
    created_by_user_id: int,
    rounds: int = 3,
) -> MatchPlan:
    appointment = session.get(Appointment, appointment_id)
    if appointment is None:
        raise ValueError("Appointment not found")

    participation = get_appointment_participation(appointment)
    joined_members = participation.confirmed_members
    guests = participation.confirmed_guests

    if len(joined_members) + len(guests) < 4:
        raise ValueError("Need at least 4 participants to create doubles matches")

    plan = MatchPlan(
        appointment_id=appointment.id,
        event_date=appointment.event_start.date(),
        created_by_user_id=created_by_user_id,
        notes="Auto-generated with OR-Tools CP-SAT",
    )
    session.add(plan)
    session.flush()

    planner: list[PlannerParticipant] = []

    for member in joined_members:
        participant = MatchPlanParticipant(
            match_plan_id=plan.id,
            member_id=member.id,
            display_name=member.name,
            is_guest=False,
            skill_rating=member.skill_rating,
            max_games=3,
        )
        session.add(participant)
    session.flush()

    for p in plan.participants:
        planner.append(
            PlannerParticipant(
                idx=p.id,
                display_name=p.display_name,
                skill_rating=p.skill_rating,
                max_games=p.max_games,
                member_id=p.member_id,
                is_guest=False,
            )
        )

    for guest in guests:
        participant = MatchPlanParticipant(
            match_plan_id=plan.id,
            member_id=None,
            display_name=f"Guest {guest.name}",
            is_guest=True,
            guest_name=guest.name,
            anchor_member_id=guest.closest_member_id,
            skill_rating=guest.closest_member.skill_rating,
            max_games=1,
        )
        session.add(participant)
    session.flush()

    new_guest_participants = [p for p in plan.participants if p.is_guest]
    for p in new_guest_participants:
        planner.append(
            PlannerParticipant(
                idx=p.id,
                display_name=p.display_name,
                skill_rating=p.skill_rating,
                max_games=1,
                member_id=None,
                is_guest=True,
            )
        )

    team_history, opp_history = _load_history_penalties(session)

    same_day_team_counts: dict[tuple[int, int], int] = {}
    same_day_opp_counts: dict[tuple[int, int], int] = {}
    play_counts = {p.idx: 0 for p in planner}

    for game_idx in range(1, rounds + 1):
        eligible = [p for p in planner if play_counts[p.idx] < p.max_games]
        match_count = len(eligible) // 4
        if match_count <= 0:
            for p in planner:
                session.add(
                    PracticeAssignment(
                        match_plan_id=plan.id,
                        game_index=game_idx,
                        participant_id=p.idx,
                    )
                )
            continue

        candidates = _generate_candidates(
            eligible,
            team_history,
            opp_history,
            same_day_team_counts,
            same_day_opp_counts,
            play_counts,
        )

        selected = _select_round_matches(candidates, [p.idx for p in eligible], match_count)
        if not selected:
            selected = candidates[:match_count]

        used: set[int] = set()
        for court_idx, match in enumerate(selected, start=1):
            session.add(
                MatchPlanGame(
                    match_plan_id=plan.id,
                    game_index=game_idx,
                    court_index=court_idx,
                    team_a_p1_id=match.p1,
                    team_a_p2_id=match.p2,
                    team_b_p1_id=match.p3,
                    team_b_p2_id=match.p4,
                )
            )

            used.update([match.p1, match.p2, match.p3, match.p4])

            for pair in [_pair_key(match.p1, match.p2), _pair_key(match.p3, match.p4)]:
                same_day_team_counts[pair] = same_day_team_counts.get(pair, 0) + 1

            for pair in [
                _pair_key(match.p1, match.p3),
                _pair_key(match.p1, match.p4),
                _pair_key(match.p2, match.p3),
                _pair_key(match.p2, match.p4),
            ]:
                same_day_opp_counts[pair] = same_day_opp_counts.get(pair, 0) + 1

        for pid in used:
            play_counts[pid] += 1

        for participant in planner:
            if participant.idx not in used:
                session.add(
                    PracticeAssignment(
                        match_plan_id=plan.id,
                        game_index=game_idx,
                        participant_id=participant.idx,
                    )
                )

    return plan


def get_or_create_match_plan(
    session,
    appointment_id: int,
    created_by_user_id: int,
    rounds: int = 3,
) -> MatchPlan:
    existing = (
        session.query(MatchPlan)
        .filter(MatchPlan.appointment_id == appointment_id)
        .order_by(MatchPlan.created_at.desc())
        .first()
    )
    if existing is not None:
        return existing
    return generate_match_plan(session, appointment_id, created_by_user_id, rounds=rounds)


def _appointment_roster_signature(appointment: Appointment) -> tuple[tuple, ...]:
    participation = get_appointment_participation(appointment)
    entries: list[tuple] = []
    for member in participation.confirmed_members:
        entries.append(("M", member.id))
    for guest in participation.confirmed_guests:
        entries.append(("G", guest.name, guest.closest_member_id))
    return tuple(sorted(entries))


def _plan_roster_signature(plan: MatchPlan) -> tuple[tuple, ...]:
    entries: list[tuple] = []
    for participant in plan.participants:
        if participant.member_id is not None:
            entries.append(("M", participant.member_id))
        elif participant.is_guest:
            entries.append(("G", participant.guest_name or participant.display_name, participant.anchor_member_id))
    return tuple(sorted(entries))


def sync_match_plan_for_appointment(
    session,
    appointment_id: int,
    created_by_user_id: int,
    rounds: int = 3,
) -> MatchPlan | None:
    appointment = session.get(Appointment, appointment_id)
    if appointment is None:
        raise ValueError("Appointment not found")

    existing_plans = (
        session.query(MatchPlan)
        .filter(MatchPlan.appointment_id == appointment_id)
        .order_by(MatchPlan.created_at.desc(), MatchPlan.id.desc())
        .all()
    )
    protected_plan_ids = {
        plan.id
        for plan in existing_plans
        if session.query(RatingEvent).filter(RatingEvent.match_plan_id == plan.id).count() > 0
    }

    participation = get_appointment_participation(appointment)
    if participation.confirmed_count < 4:
        for plan in existing_plans:
            if plan.id not in protected_plan_ids:
                session.delete(plan)
        return None

    desired_signature = _appointment_roster_signature(appointment)
    for plan in existing_plans:
        if plan.id in protected_plan_ids:
            if _plan_roster_signature(plan) == desired_signature:
                return plan
            continue
        if _plan_roster_signature(plan) == desired_signature:
            return plan

    for plan in existing_plans:
        if plan.id not in protected_plan_ids:
            session.delete(plan)
    session.flush()

    return generate_match_plan(session, appointment_id, created_by_user_id, rounds=rounds)


def result_window_bounds(event_start: datetime) -> tuple[datetime, datetime]:
    open_at = event_start
    close_at = event_start + timedelta(days=1)
    return open_at, close_at


def is_result_window_open(appointment: Appointment, now: datetime) -> bool:
    open_at, close_at = result_window_bounds(appointment.event_start)
    return open_at <= now < close_at


def save_user_result_inputs(
    session,
    plan_id: int,
    user_id: int,
    scores: dict[int, tuple[int, int]],
) -> None:
    plan = session.get(MatchPlan, plan_id)
    if plan is None:
        raise ValueError("Match plan not found")

    valid_game_ids = {g.id for g in plan.games}
    for game_id, (a_score, b_score) in scores.items():
        if game_id not in valid_game_ids:
            continue
        row = (
            session.query(MatchResultInput)
            .filter_by(match_game_id=game_id, user_id=user_id)
            .one_or_none()
        )
        if row is None:
            session.add(
                MatchResultInput(
                    match_game_id=game_id,
                    user_id=user_id,
                    team_a_score=a_score,
                    team_b_score=b_score,
                )
            )
        else:
            row.team_a_score = a_score
            row.team_b_score = b_score


def _select_final_score_from_inputs(inputs: list[MatchResultInput]) -> tuple[int, int] | None:
    if not inputs:
        return None

    counts: dict[tuple[int, int], tuple[int, datetime]] = {}
    for item in inputs:
        key = (item.team_a_score, item.team_b_score)
        prev_count, prev_latest = counts.get(key, (0, datetime(1970, 1, 1)))
        latest = item.updated_at if item.updated_at and item.updated_at > prev_latest else prev_latest
        counts[key] = (prev_count + 1, latest)

    ordered = sorted(
        counts.items(),
        key=lambda kv: (
            -kv[1][0],  # highest vote count
            -kv[1][1].timestamp(),  # latest update tie-break
            kv[0][0],
            kv[0][1],
        ),
    )
    return ordered[0][0]


def finalize_due_match_results(session, now: datetime) -> list[int]:
    applied_plan_ids: list[int] = []
    plans = session.query(MatchPlan).join(Appointment, MatchPlan.appointment_id == Appointment.id).all()

    for plan in plans:
        if plan.appointment is None:
            continue

        _, close_at = result_window_bounds(plan.appointment.event_start)
        if now < close_at:
            continue

        already_applied = session.query(RatingEvent).filter(RatingEvent.match_plan_id == plan.id).count() > 0
        if already_applied:
            continue

        selected_scores: dict[int, tuple[int, int]] = {}
        for game in plan.games:
            inputs = (
                session.query(MatchResultInput)
                .filter(MatchResultInput.match_game_id == game.id)
                .order_by(MatchResultInput.updated_at.desc())
                .all()
            )
            selected = _select_final_score_from_inputs(inputs)
            if selected is not None:
                selected_scores[game.id] = selected

        if not selected_scores:
            continue

        submit_results(session, plan.id, selected_scores)
        applied_plan_ids.append(plan.id)

    return applied_plan_ids


def submit_results(session, plan_id: int, scores: dict[int, tuple[int, int]]) -> None:
    plan = session.get(MatchPlan, plan_id)
    if plan is None:
        raise ValueError("Match plan not found")

    existing = session.query(RatingEvent).filter(RatingEvent.match_plan_id == plan.id).count()
    if existing:
        raise ValueError("Results already submitted for this plan")

    participant_map = {p.id: p for p in plan.participants}

    k_factor = 24.0

    for game in plan.games:
        if game.id not in scores:
            continue
        a_score, b_score = scores[game.id]
        game.team_a_score = a_score
        game.team_b_score = b_score

        pa1 = participant_map[game.team_a_p1_id]
        pa2 = participant_map[game.team_a_p2_id]
        pb1 = participant_map[game.team_b_p1_id]
        pb2 = participant_map[game.team_b_p2_id]

        rating_a = (pa1.skill_rating + pa2.skill_rating) / 2.0
        rating_b = (pb1.skill_rating + pb2.skill_rating) / 2.0

        expected_a = 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))

        if a_score > b_score:
            actual_a = 1.0
        elif a_score < b_score:
            actual_a = 0.0
        else:
            actual_a = 0.5

        delta_a = k_factor * (actual_a - expected_a)
        delta_b = -delta_a

        for participant, delta in [
            (pa1, delta_a),
            (pa2, delta_a),
            (pb1, delta_b),
            (pb2, delta_b),
        ]:
            if participant.member_id is None:
                continue
            member = session.get(Member, participant.member_id)
            if member is None:
                continue
            member.skill_rating += delta
            session.add(
                RatingEvent(
                    member_id=member.id,
                    match_plan_id=plan.id,
                    match_game_id=game.id,
                    delta=delta,
                    new_rating=member.skill_rating,
                    note=f"Game {game.game_index} Court {game.court_index}",
                )
            )
