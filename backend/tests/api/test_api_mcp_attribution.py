import pytest


def _mint(client, device, scope=None):
    body = {"device": device}
    if scope:
        body["scope"] = scope
    return client.post("/api/auth/llt", json=body).get_json()["longlived_token"]


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
def list_id(user_client_with_household, household_id):
    res = user_client_with_household.get(f"/api/household/{household_id}/shoppinglist")
    return res.get_json()[0]["id"]


def _items(client, list_id):
    return {i["name"]: i for i in client.get(f"/api/shoppinglist/{list_id}/items").get_json()}


def test_a_session_added_item_names_no_token(user_client_with_household, list_id):
    user_client_with_household.post(
        f"/api/shoppinglist/{list_id}/add-item-by-name", json={"name": "milk"}
    )
    item = _items(user_client_with_household, list_id)["milk"]
    assert item["created_by"] is not None
    assert item["created_by_token"] is None


def test_an_agent_added_item_names_its_token(user_client_with_household, list_id):
    token = _mint(user_client_with_household, "Shopping Assistant")
    user_client_with_household.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {token}"

    _call(user_client_with_household, "add_item_by_name", {"list_id": list_id, "name": "milk"})

    item = _items(user_client_with_household, list_id)["milk"]
    assert item["created_by"] is not None
    assert item["created_by_token"] == "Shopping Assistant"


def test_rest_calls_made_with_a_token_are_attributed_too(user_client_with_household, list_id):
    # The REST API accepts a long-lived token as readily as MCP does, so the
    # attribution cannot live only in the MCP controller.
    token = _mint(user_client_with_household, "Kitchen Robot")
    user_client_with_household.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {token}"

    user_client_with_household.post(
        f"/api/shoppinglist/{list_id}/add-item-by-name", json={"name": "bread"}
    )
    assert _items(user_client_with_household, list_id)["bread"]["created_by_token"] == "Kitchen Robot"


def test_two_tokens_are_told_apart(user_client_with_household, list_id):
    first = _mint(user_client_with_household, "Assistant A")
    second = _mint(user_client_with_household, "Assistant B")

    user_client_with_household.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {first}"
    _call(user_client_with_household, "add_item_by_name", {"list_id": list_id, "name": "milk"})
    user_client_with_household.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {second}"
    _call(user_client_with_household, "add_item_by_name", {"list_id": list_id, "name": "bread"})

    items = _items(user_client_with_household, list_id)
    assert items["milk"]["created_by_token"] == "Assistant A"
    assert items["bread"]["created_by_token"] == "Assistant B"


def test_attribution_outlives_revoking_the_token(user_client_with_household, list_id):
    session_auth = user_client_with_household.environ_base["HTTP_AUTHORIZATION"]
    token = _mint(user_client_with_household, "Shopping Assistant")

    user_client_with_household.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    _call(user_client_with_household, "add_item_by_name", {"list_id": list_id, "name": "milk"})

    from app import db
    from app.models import Token

    db.session.delete(Token.query.filter(Token.name == "Shopping Assistant").one())
    db.session.commit()

    # Revoking an agent must not erase the record of what it did.
    user_client_with_household.environ_base["HTTP_AUTHORIZATION"] = session_auth
    item = _items(user_client_with_household, list_id)["milk"]
    assert item["created_by_token"] == "Shopping Assistant"
