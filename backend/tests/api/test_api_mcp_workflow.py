import pytest


def _call(client, name, arguments):
    res = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    body = res.get_json()
    assert "error" not in body, body
    return body["result"]


def _structured(client, name, arguments):
    result = _call(client, name, arguments)
    assert not result.get("isError"), result
    return result["structuredContent"]


@pytest.fixture
def list_id(user_client_with_household, household_id):
    response = user_client_with_household.get(f"/api/household/{household_id}/shoppinglist")
    assert response.status_code == 200
    return response.get_json()[0]["id"]


@pytest.fixture
def recipe_id(user_client_with_household, household_id):
    recipe = _structured(
        user_client_with_household,
        "create_recipe",
        {
            "household_id": household_id,
            "name": "Pancakes",
            "items": [
                {"name": "flour", "description": "200 g"},
                {"name": "milk", "description": "300 ml"},
                {"name": "maple syrup", "optional": True},
            ],
        },
    )
    return recipe["id"]


def _names_on(client, list_id):
    listing = _structured(client, "list_shoppinglist_items", {"list_id": list_id})
    return {i["name"]: i["description"] for i in listing["items"]}


def test_add_items_adds_a_batch(user_client_with_household, list_id):
    result = _structured(
        user_client_with_household,
        "add_items",
        {"list_id": list_id, "items": ["milk", {"name": "bread", "description": "1 x"}]},
    )
    # Neither name is known to a fresh household, so both mint an item.
    assert [r["outcome"] for r in result["results"]] == ["created", "created"]
    assert _names_on(user_client_with_household, list_id) == {"milk": "", "bread": "1 x"}


def test_add_items_merges_quantities(user_client_with_household, list_id):
    args = {"list_id": list_id, "items": [{"name": "milk", "description": "1 l"}]}
    _structured(user_client_with_household, "add_items", args)
    result = _structured(user_client_with_household, "add_items", args)

    assert result["results"][0]["outcome"] == "merged"
    assert _names_on(user_client_with_household, list_id) == {"milk": "2L"}


def test_add_items_can_skip_merging(user_client_with_household, list_id):
    args = {"list_id": list_id, "items": [{"name": "milk", "description": "1 l"}]}
    _structured(user_client_with_household, "add_items", args)
    result = _structured(
        user_client_with_household, "add_items", {**args, "merge_quantities": False}
    )

    assert result["results"][0]["outcome"] == "unchanged"
    assert _names_on(user_client_with_household, list_id) == {"milk": "1 l"}


def test_recipe_items_land_on_the_list(user_client_with_household, list_id, recipe_id):
    result = _structured(
        user_client_with_household,
        "add_recipe_items_to_list",
        {"recipe_id": recipe_id, "list_id": list_id},
    )

    assert result["recipe"]["name"] == "Pancakes"
    # The optional ingredient is left out by default.
    assert _names_on(user_client_with_household, list_id) == {
        "flour": "200 g",
        "milk": "300 ml",
    }


def test_recipe_items_can_include_optional(user_client_with_household, list_id, recipe_id):
    _structured(
        user_client_with_household,
        "add_recipe_items_to_list",
        {"recipe_id": recipe_id, "list_id": list_id, "include_optional": True},
    )
    assert "maple syrup" in _names_on(user_client_with_household, list_id)


def test_recipe_items_can_be_filtered(user_client_with_household, list_id, recipe_id):
    _structured(
        user_client_with_household,
        "add_recipe_items_to_list",
        {"recipe_id": recipe_id, "list_id": list_id, "only_items": ["flour"]},
    )
    assert set(_names_on(user_client_with_household, list_id)) == {"flour"}


def test_cooking_a_recipe_twice_accumulates(user_client_with_household, list_id, recipe_id):
    args = {"recipe_id": recipe_id, "list_id": list_id}
    _structured(user_client_with_household, "add_recipe_items_to_list", args)
    _structured(user_client_with_household, "add_recipe_items_to_list", args)

    assert _names_on(user_client_with_household, list_id)["flour"] == "400g"


def test_update_item_description_overwrites(user_client_with_household, list_id):
    _structured(
        user_client_with_household,
        "add_items",
        {"list_id": list_id, "items": [{"name": "milk", "description": "1 l"}]},
    )
    _structured(
        user_client_with_household,
        "update_item_description",
        {"list_id": list_id, "name": "milk", "description": "500 ml"},
    )
    assert _names_on(user_client_with_household, list_id) == {"milk": "500 ml"}


def test_update_item_description_reports_items_not_on_the_list(
    user_client_with_household, list_id
):
    result = _structured(
        user_client_with_household,
        "update_item_description",
        {"list_id": list_id, "name": "caviar", "description": "1 x"},
    )
    assert result == {"updated": False, "reason": "not_on_list"}


def test_list_categories_returns_household_categories(
    user_client_with_household, household_id
):
    user_client_with_household.post(
        f"/api/household/{household_id}/category", json={"name": "Produce"}
    )
    result = _structured(
        user_client_with_household, "list_categories", {"household_id": household_id}
    )
    assert "Produce" in [c["name"] for c in result["items"]]


def test_recipe_and_list_must_share_a_household(
    user_client_with_household, recipe_id, admin_client
):
    other = admin_client.post("/api/household", json={"name": "Other"}).get_json()
    other_list = other["default_shopping_list"]["id"]

    result = _call(
        user_client_with_household,
        "add_recipe_items_to_list",
        {"recipe_id": recipe_id, "list_id": other_list},
    )
    assert result["isError"] is True


def test_a_known_item_is_reused_rather_than_created(user_client_with_household, list_id):
    args = {"list_id": list_id, "items": ["milk"]}
    assert _structured(user_client_with_household, "add_items", args)["results"][0][
        "outcome"
    ] == "created"

    _structured(
        user_client_with_household,
        "remove_item_from_list",
        {"list_id": list_id, "name": "milk"},
    )
    # The item survives removal, so putting it back is a reuse, not a creation.
    again = _structured(user_client_with_household, "add_items", args)
    assert again["results"][0]["outcome"] == "added"
    assert "similar_existing" not in again["results"][0]


def test_creating_a_near_duplicate_names_what_it_resembles(
    user_client_with_household, list_id
):
    _structured(user_client_with_household, "add_items", {"list_id": list_id, "items": ["Ice cream"]})

    result = _structured(
        user_client_with_household,
        "add_items",
        {"list_id": list_id, "items": ["Salted caramel ice cream"]},
    )["results"][0]

    # The model needs to see that it invented a product, and what it resembles,
    # otherwise the household quietly accumulates near-duplicates.
    assert result["outcome"] == "created"
    assert "Ice cream" in result["similar_existing"]
    assert "description" in result["hint"]


def test_an_unrelated_name_reports_no_lookalikes(user_client_with_household, list_id):
    result = _structured(
        user_client_with_household,
        "add_items",
        {"list_id": list_id, "items": ["Dishwasher tablets"]},
    )["results"][0]
    assert result["outcome"] == "created"
    assert "similar_existing" not in result


def test_two_recipes_sharing_an_item_accumulate(
    user_client_with_household, household_id, list_id
):
    def recipe(name, flour, extra):
        return _structured(
            user_client_with_household,
            "create_recipe",
            {
                "household_id": household_id,
                "name": name,
                "items": [
                    {"name": "flour", "description": flour},
                    {"name": extra, "description": "1 x"},
                ],
            },
        )["id"]

    bread = recipe("Bread", "200 g", "yeast")
    cake = recipe("Cake", "300 g", "sugar")

    for r in (bread, cake):
        _structured(
            user_client_with_household,
            "add_recipe_items_to_list",
            {"recipe_id": r, "list_id": list_id},
        )

    names = _names_on(user_client_with_household, list_id)
    # One row for flour carrying the total, not two rows or the last value.
    assert names["flour"] == "500g"
    assert set(names) == {"flour", "yeast", "sugar"}


def test_quantities_that_cannot_convert_are_kept_side_by_side(
    user_client_with_household, household_id, list_id
):
    def recipe(name, tomato_qty):
        return _structured(
            user_client_with_household,
            "create_recipe",
            {
                "household_id": household_id,
                "name": name,
                "items": [{"name": "tomatoes", "description": tomato_qty}],
            },
        )["id"]

    for r in (recipe("Soup", "400 g"), recipe("Sauce", "2 tins")):
        _structured(
            user_client_with_household,
            "add_recipe_items_to_list",
            {"recipe_id": r, "list_id": list_id},
        )

    # Grams and tins have no conversion, so both are shown rather than one
    # silently winning or a bogus total being invented.
    assert _names_on(user_client_with_household, list_id)["tomatoes"] == "400g, 2 tins"
