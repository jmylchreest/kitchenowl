import pytest

from app.controller.mcp_controller import TOOLS


def _mint(client, device, scope=None, household_id=None):
    body = {"device": device}
    if scope is not None:
        body["scope"] = scope
    if household_id is not None:
        body["household_id"] = household_id
    return client.post("/api/auth/llt", json=body)


def _scoped_client(client, token):
    client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    return client


def _rpc(client, method, params=None):
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def _tool_names(client):
    return {t["name"] for t in _rpc(client, "tools/list").get_json()["result"]["tools"]}


def _call(client, name, arguments):
    return _rpc(client, "tools/call", {"name": name, "arguments": arguments}).get_json()["result"]


@pytest.fixture
def list_id(user_client_with_household, household_id):
    res = user_client_with_household.get(f"/api/household/{household_id}/shoppinglist")
    return res.get_json()[0]["id"]


@pytest.fixture
def recipe_id(user_client_with_household, household_id):
    result = _call(
        user_client_with_household,
        "create_recipe",
        {"household_id": household_id, "name": "Chilli"},
    )
    return result["structuredContent"]["id"]


def test_minting_accepts_a_scope(user_client_with_household):
    res = _mint(user_client_with_household, "agent", scope="read")
    assert res.status_code == 200
    assert res.get_json()["longlived_token"]


def test_minting_rejects_an_unknown_scope(user_client_with_household):
    assert _mint(user_client_with_household, "agent", scope="root").status_code == 400


def test_cannot_pin_a_token_to_a_household_you_are_not_in(user_client_with_household):
    assert _mint(user_client_with_household, "agent", household_id=999999).status_code == 404


def test_existing_tokens_stay_unrestricted(user_client_with_household):
    # A token minted without a scope keeps the pre-scopes behaviour.
    token = _mint(user_client_with_household, "legacy").get_json()["longlived_token"]
    assert _tool_names(_scoped_client(user_client_with_household, token)) == set(TOOLS)


def test_read_scope_lists_only_read_only_tools(user_client_with_household):
    token = _mint(user_client_with_household, "agent", scope="read").get_json()["longlived_token"]
    names = _tool_names(_scoped_client(user_client_with_household, token))

    assert names == {n for n, t in TOOLS.items() if t.read_only}
    assert "list_recipes" in names
    assert "add_item_by_name" not in names
    assert "delete_recipe" not in names


def test_write_scope_lists_everything_except_deletes(user_client_with_household):
    token = _mint(user_client_with_household, "agent", scope="write").get_json()["longlived_token"]
    names = _tool_names(_scoped_client(user_client_with_household, token))

    assert names == {n for n, t in TOOLS.items() if not t.deletes}
    assert {"delete_recipe", "delete_shoppinglist", "delete_expense"}.isdisjoint(names)
    # Ticking an item off a list is ordinary shopping, not a delete.
    assert "remove_item_from_list" in names
    assert "add_recipe_items_to_list" in names


def test_full_scope_lists_everything(user_client_with_household):
    token = _mint(user_client_with_household, "agent", scope="full").get_json()["longlived_token"]
    assert _tool_names(_scoped_client(user_client_with_household, token)) == set(TOOLS)


def test_read_scope_cannot_call_a_write_tool(user_client_with_household, list_id):
    token = _mint(user_client_with_household, "agent", scope="read").get_json()["longlived_token"]
    scoped = _scoped_client(user_client_with_household, token)

    result = _call(scoped, "add_item_by_name", {"list_id": list_id, "name": "milk"})
    assert result["isError"] is True
    assert "scope" in result["content"][0]["text"]

    # And the write really did not happen.
    listing = _call(scoped, "list_shoppinglist_items", {"list_id": list_id})
    assert listing["structuredContent"]["items"] == []


def test_write_scope_cannot_delete_but_can_shop(user_client_with_household, list_id, recipe_id):
    token = _mint(user_client_with_household, "agent", scope="write").get_json()["longlived_token"]
    scoped = _scoped_client(user_client_with_household, token)

    assert _call(scoped, "delete_recipe", {"recipe_id": recipe_id})["isError"] is True

    assert not _call(scoped, "add_item_by_name", {"list_id": list_id, "name": "milk"}).get("isError")
    assert not _call(scoped, "remove_item_from_list", {"list_id": list_id, "name": "milk"}).get("isError")


def test_hiding_a_tool_is_not_the_only_defence(user_client_with_household, recipe_id):
    """A client that calls a tool missing from tools/list is still refused."""
    token = _mint(user_client_with_household, "agent", scope="read").get_json()["longlived_token"]
    scoped = _scoped_client(user_client_with_household, token)

    assert "delete_recipe" not in _tool_names(scoped)
    assert _call(scoped, "delete_recipe", {"recipe_id": recipe_id})["isError"] is True
    assert not _call(scoped, "get_recipe", {"recipe_id": recipe_id}).get("isError")


def test_pinned_token_sees_only_its_household(
    user_client_with_household, household_id, admin_client, username
):
    second = user_client_with_household.post("/api/household", json={"name": "Second"}).get_json()
    token = _mint(
        user_client_with_household, "agent", scope="full", household_id=household_id
    ).get_json()["longlived_token"]
    scoped = _scoped_client(user_client_with_household, token)

    listed = _call(scoped, "list_households", {})["structuredContent"]
    assert [h["id"] for h in listed["items"]] == [household_id]

    blocked = _call(scoped, "list_recipes", {"household_id": second["id"]})
    assert blocked["isError"] is True
    assert "restricted" in blocked["content"][0]["text"]


def test_pin_is_enforced_when_reached_via_a_list_id(user_client_with_household, household_id):
    second = user_client_with_household.post("/api/household", json={"name": "Second"}).get_json()
    second_list = second["default_shopping_list"]["id"]

    token = _mint(
        user_client_with_household, "agent", scope="full", household_id=household_id
    ).get_json()["longlived_token"]
    scoped = _scoped_client(user_client_with_household, token)

    # The pinned household is never named in these arguments, so the check has
    # to happen where the list is resolved.
    assert _call(scoped, "list_shoppinglist_items", {"list_id": second_list})["isError"] is True
    assert _call(scoped, "add_item_by_name", {"list_id": second_list, "name": "milk"})["isError"] is True


def test_pin_is_enforced_when_reached_via_a_recipe_id(
    user_client_with_household, household_id, recipe_id
):
    second = user_client_with_household.post("/api/household", json={"name": "Second"}).get_json()
    token = _mint(
        user_client_with_household, "agent", scope="full", household_id=second["id"]
    ).get_json()["longlived_token"]
    scoped = _scoped_client(user_client_with_household, token)

    assert _call(scoped, "get_recipe", {"recipe_id": recipe_id})["isError"] is True
    assert _call(scoped, "update_recipe", {"recipe_id": recipe_id, "name": "x"})["isError"] is True
