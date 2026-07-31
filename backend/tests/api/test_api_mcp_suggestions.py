import pytest

from app import db
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
def recipes(user_client_with_household, household_id):
    ids = {}
    for name in ("Chilli", "Bolognese", "Pancakes"):
        ids[name] = _call(
            user_client_with_household,
            "create_recipe",
            {"household_id": household_id, "name": name},
        )["id"]
    return ids


def _rank(recipe_id, score, rank):
    r = Recipe.find_by_id(recipe_id)
    r.suggestion_score = score
    r.suggestion_rank = rank
    db.session.add(r)
    db.session.commit()


def test_an_unranked_household_explains_itself(
    user_client_with_household, household_id, recipes
):
    result = _call(
        user_client_with_household, "suggest_recipes", {"household_id": household_id}
    )
    # An empty ranking is not an empty household; without the reason the model
    # would conclude there is nothing to cook.
    assert result["items"] == []
    assert "list_recipes" in result["reason"]


def test_suggestions_come_back_in_rank_order(
    user_client_with_household, household_id, recipes
):
    _rank(recipes["Chilli"], score=5, rank=2)
    _rank(recipes["Bolognese"], score=9, rank=1)

    result = _call(
        user_client_with_household, "suggest_recipes", {"household_id": household_id}
    )
    assert [r["name"] for r in result["items"]] == ["Bolognese", "Chilli"]
    assert "reason" not in result


def test_a_suggestion_carries_how_often_it_is_cooked(
    user_client_with_household, household_id, recipes
):
    _rank(recipes["Chilli"], score=7, rank=1)

    suggested = _call(
        user_client_with_household, "suggest_recipes", {"household_id": household_id}
    )["items"][0]
    # The score is the whole point: it tells the model which recipes are real
    # to this household rather than merely present in it.
    assert suggested["suggestion_score"] == 7


def test_list_recipes_carries_the_score_too(
    user_client_with_household, household_id, recipes
):
    _rank(recipes["Chilli"], score=4, rank=1)
    listed = _call(
        user_client_with_household, "list_recipes", {"household_id": household_id}
    )["items"]
    assert {r["name"]: r["suggestion_score"] for r in listed}["Chilli"] == 4


def test_already_planned_recipes_are_not_suggested(
    user_client_with_household, household_id, recipes
):
    _rank(recipes["Chilli"], score=5, rank=1)
    _rank(recipes["Bolognese"], score=5, rank=2)

    _call(
        user_client_with_household,
        "add_planner_entry",
        {
            "household_id": household_id,
            "recipe_id": recipes["Chilli"],
            "cooking_date": "2026-08-01T18:00:00Z",
        },
    )

    result = _call(
        user_client_with_household, "suggest_recipes", {"household_id": household_id}
    )
    assert [r["name"] for r in result["items"]] == ["Bolognese"]


def test_suggestions_are_scoped_to_the_household(
    user_client_with_household, household_id, recipes
):
    _rank(recipes["Chilli"], score=5, rank=1)
    other = user_client_with_household.post(
        "/api/household", json={"name": "Other"}
    ).get_json()

    result = _call(
        user_client_with_household, "suggest_recipes", {"household_id": other["id"]}
    )
    assert result["items"] == []
