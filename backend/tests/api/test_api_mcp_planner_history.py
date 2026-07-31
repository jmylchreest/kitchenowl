import pytest

from app.jobs.recipe_suggestions import computeRecipeSuggestions
from app.models import Recipe


def _call(client, name, arguments):
    body = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    ).get_json()
    assert "error" not in body, body
    assert not body["result"].get("isError"), body["result"]
    return body["result"]["structuredContent"]


@pytest.fixture
def recipe_id(user_client_with_household, household_id):
    return _call(
        user_client_with_household,
        "create_recipe",
        {"household_id": household_id, "name": "Chilli"},
    )["id"]


def _history(household_id):
    from app.models import RecipeHistory

    return RecipeHistory.query.filter(
        RecipeHistory.household_id == household_id
    ).all()


def test_planning_a_meal_records_history(
    user_client_with_household, household_id, recipe_id
):
    _call(
        user_client_with_household,
        "add_planner_entry",
        {
            "household_id": household_id,
            "recipe_id": recipe_id,
            "cooking_date": "2026-08-01T00:00:00",
        },
    )

    rows = _history(household_id)
    assert len(rows) == 1
    assert rows[0].recipe_id == recipe_id


def test_unplanning_a_meal_records_history(
    user_client_with_household, household_id, recipe_id
):
    args = {
        "household_id": household_id,
        "recipe_id": recipe_id,
        "cooking_date": "2026-08-01T00:00:00",
    }
    _call(user_client_with_household, "add_planner_entry", args)
    _call(user_client_with_household, "remove_planner_entry", args)

    statuses = [r.status for r in _history(household_id)]
    assert len(statuses) == 2
    assert len(set(statuses)) == 2


def test_meals_planned_through_mcp_feed_the_suggestion_ranking(
    user_client_with_household, household_id, recipe_id
):
    """The whole point: an agent's planning has to reach suggest_recipes.

    Without history the nightly job scores every recipe zero, find_suggestions
    filters on rank > 0, and the agent's own planning is invisible to it.
    """
    for day in ("2026-08-01", "2026-08-08", "2026-08-15"):
        _call(
            user_client_with_household,
            "add_planner_entry",
            {
                "household_id": household_id,
                "recipe_id": recipe_id,
                "cooking_date": f"{day}T00:00:00",
            },
        )
        _call(
            user_client_with_household,
            "remove_planner_entry",
            {
                "household_id": household_id,
                "recipe_id": recipe_id,
                "cooking_date": f"{day}T00:00:00",
            },
        )

    # The job ignores anything cooked in the last week, so that a recipe just
    # made is not immediately suggested again. Age the rows past that cooldown.
    from datetime import datetime, timedelta, timezone

    from app import db
    from app.models import RecipeHistory

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    for row in _history(household_id):
        row.created_at = cutoff
        db.session.add(row)
    db.session.commit()

    # The nightly job scores, then ranks; find_suggestions filters on rank.
    computeRecipeSuggestions(household_id)
    Recipe.compute_suggestion_ranking(household_id)

    recipe = Recipe.find_by_id(recipe_id)
    assert recipe.suggestion_score == 3
    assert recipe.suggestion_rank > 0

    # Not on the planner any more, so it is eligible to be suggested.
    result = _call(
        user_client_with_household, "suggest_recipes", {"household_id": household_id}
    )
    assert [r["name"] for r in result["items"]] == ["Chilli"]
    assert "reason" not in result
