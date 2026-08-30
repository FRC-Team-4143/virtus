"""Team goals and student goals — including that they stay two independent lists."""
import pytest
from datetime import date

from app.models import GoalCategory, GoalStatus, GoalTeam, StudentGoal, TeamGoal
from app.services import goals as goal_service


async def test_a_goal_needs_a_title(db, make_member):
    member = await make_member("Sara Student")
    with pytest.raises(goal_service.GoalError):
        await goal_service.create_student_goal(db, member, title="   ")
    with pytest.raises(goal_service.GoalError):
        await goal_service.create_team_goal(db, title="")


async def test_student_goals_have_no_link_to_team_goals(db, make_member):
    """The lists are deliberately independent — assert the schema keeps them that way, so
    an accidental FK doesn't quietly reintroduce roll-up."""
    assert not any(c.foreign_keys for c in StudentGoal.__table__.columns if c.name != "member_id")
    assert "team_goal_id" not in StudentGoal.__table__.columns


async def test_get_student_goal_for_enforces_ownership(db, make_member):
    mine = await make_member("Sara Student")
    theirs = await make_member("Sam Student")
    goal = await goal_service.create_student_goal(db, mine, title="Learn the CNC")
    await db.commit()

    assert await goal_service.get_student_goal_for(db, goal.id, mine.id) is not None
    assert await goal_service.get_student_goal_for(db, goal.id, theirs.id) is None


async def test_student_goals_sort_done_to_the_bottom(db, make_member):
    member = await make_member("Sara Student")
    await goal_service.create_student_goal(
        db, member, title="Finished", status=GoalStatus.done, target_date=date(2026, 1, 1)
    )
    await goal_service.create_student_goal(
        db, member, title="Still going", status=GoalStatus.on_track, target_date=date(2026, 6, 1)
    )
    await db.commit()

    titles = [g.title for g in await goal_service.list_student_goals(db, member.id)]
    assert titles == ["Still going", "Finished"]


async def test_personal_goal_shortfall_counts_down_to_zero(db, make_member):
    member = await make_member("Sara Student")
    season = goal_service.current_season()
    assert goal_service.required_personal_goals() == 2  # the default

    assert await goal_service.personal_goal_shortfall(db, member.id, season=season) == 2
    await goal_service.create_student_goal(db, member, title="One")
    await db.commit()
    assert await goal_service.personal_goal_shortfall(db, member.id, season=season) == 1
    await goal_service.create_student_goal(db, member, title="Two")
    await goal_service.create_student_goal(db, member, title="Three")
    await db.commit()
    assert await goal_service.personal_goal_shortfall(db, member.id, season=season) == 0


async def test_personal_goal_shortfall_is_per_season(db, make_member):
    member = await make_member("Sara Student")
    await goal_service.create_student_goal(db, member, title="Old one", season="2024")
    await goal_service.create_student_goal(db, member, title="Old two", season="2024")
    await db.commit()

    assert await goal_service.personal_goal_shortfall(db, member.id, season="2024") == 0
    assert await goal_service.personal_goal_shortfall(db, member.id, season="2026") == 2


async def test_the_goal_requirement_can_be_switched_off(db, make_member, monkeypatch):
    monkeypatch.setattr(goal_service.settings, "required_personal_goals", 0)
    member = await make_member("Sara Student")
    assert goal_service.required_personal_goals() == 0
    assert await goal_service.personal_goal_shortfall(db, member.id) == 0


async def test_team_goals_group_by_category_in_canonical_order(db):
    await goal_service.create_team_goal(
        db, title="Onboard every rookie", season="2026",
        category=GoalCategory.learning_and_culture,
    )
    await goal_service.create_team_goal(
        db, title="Auto that scores", season="2026",
        category=GoalCategory.robot_performance,
    )
    await goal_service.create_team_goal(
        db, title="Win Chairman's", season="2026", category=GoalCategory.award,
    )
    await db.commit()

    groups = await goal_service.group_team_goals_by_category(db, season="2026")
    # Fixed order regardless of insertion order.
    assert [heading for heading, _ in groups] == [
        "Robot Performance", "Award", "Learning and Culture",
    ]
    assert groups[0][1][0].title == "Auto that scores"


async def test_group_by_category_keeps_empty_buckets_once_any_goal_exists(db):
    """Only one category has a goal, but the board still gets all three headings so it
    reads as a scorecard. With no goals at all it's [] so the board shows its empty state."""
    assert await goal_service.group_team_goals_by_category(db, season="2026") == []

    await goal_service.create_team_goal(db, title="Just one", season="2026")
    await db.commit()

    groups = await goal_service.group_team_goals_by_category(db, season="2026")
    assert [h for h, _ in groups] == ["Robot Performance", "Award", "Learning and Culture"]
    assert [len(g) for _, g in groups] == [1, 0, 0]


async def test_a_new_team_goal_defaults_to_robot_performance_and_organization(db):
    await goal_service.create_team_goal(db, title="Unclassified", season="2026")
    await db.commit()
    goal = (await goal_service.list_team_goals(db, season="2026"))[0]
    assert goal.category == GoalCategory.robot_performance
    assert goal.team == GoalTeam.organization


async def test_team_goals_are_filterable_by_team(db):
    await goal_service.create_team_goal(db, title="4143 only", season="2026", team=GoalTeam.team_4143)
    await goal_service.create_team_goal(db, title="4423 only", season="2026", team=GoalTeam.team_4423)
    await goal_service.create_team_goal(db, title="Whole org", season="2026", team=GoalTeam.organization)
    await db.commit()

    only_4143 = await goal_service.list_team_goals(db, season="2026", team=GoalTeam.team_4143)
    assert [g.title for g in only_4143] == ["4143 only"]

    groups = await goal_service.group_team_goals_by_category(db, season="2026", team=GoalTeam.team_4423)
    assert [g.title for _, goals in groups for g in goals] == ["4423 only"]


async def test_seasons_lists_every_season_plus_the_current_one(db, make_member):
    member = await make_member("Sara Student")
    await goal_service.create_team_goal(db, title="Old", season="2024")
    await goal_service.create_student_goal(db, member, title="Older", season="2023")
    await db.commit()

    assert await goal_service.seasons(db) == ["2026", "2024", "2023"]


async def test_parse_status_falls_back_for_junk(db):
    assert goal_service.parse_status("on_track") == GoalStatus.on_track
    assert goal_service.parse_status("nonsense") == GoalStatus.not_started
    assert goal_service.parse_status(None) == GoalStatus.not_started


async def test_parse_category_falls_back_for_junk(db):
    assert goal_service.parse_category("award") == GoalCategory.award
    assert goal_service.parse_category("nonsense") == GoalCategory.robot_performance
    assert goal_service.parse_category(None) == GoalCategory.robot_performance


async def test_parse_team_is_none_for_blank_or_junk(db):
    assert goal_service.parse_team("4143") == GoalTeam.team_4143
    assert goal_service.parse_team("organization") == GoalTeam.organization
    assert goal_service.parse_team("") is None
    assert goal_service.parse_team(None) is None
