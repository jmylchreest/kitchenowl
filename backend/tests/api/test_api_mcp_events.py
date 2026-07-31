from unittest.mock import patch

import pytest


def _call(client, name, arguments):
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


@pytest.fixture
def list_id(user_client_with_household, household_id):
    response = user_client_with_household.get(f"/api/household/{household_id}/shoppinglist")
    assert response.status_code == 200
    return response.get_json()[0]["id"]


@pytest.fixture
def emitted():
    with patch("app.controller.mcp_controller.socketio.emit") as mock:
        yield mock


def _events(mock):
    return [c.args[0] for c in mock.call_args_list]


def _rooms(mock):
    return [c.kwargs["to"] for c in mock.call_args_list]


def test_adding_an_item_notifies_the_household(
    user_client_with_household, list_id, household_id, emitted
):
    _call(user_client_with_household, "add_item_by_name", {"list_id": list_id, "name": "milk"})

    assert _events(emitted) == ["shoppinglist_item:add"]
    assert _rooms(emitted) == [f"household/{household_id}"]
    payload = emitted.call_args.args[1]
    assert payload["item"]["name"] == "milk"
    assert payload["shoppinglist"]["id"] == list_id


def test_adding_a_duplicate_item_notifies_nobody(
    user_client_with_household, list_id, emitted
):
    args = {"list_id": list_id, "name": "milk"}
    _call(user_client_with_household, "add_item_by_name", args)
    emitted.reset_mock()

    _call(user_client_with_household, "add_item_by_name", args)
    assert _events(emitted) == []


def test_removing_an_item_notifies_the_household(
    user_client_with_household, list_id, emitted
):
    _call(user_client_with_household, "add_item_by_name", {"list_id": list_id, "name": "milk"})
    emitted.reset_mock()

    _call(user_client_with_household, "remove_item_from_list", {"list_id": list_id, "name": "milk"})
    assert _events(emitted) == ["shoppinglist_item:remove"]


def test_removing_an_absent_item_notifies_nobody(
    user_client_with_household, list_id, emitted
):
    _call(user_client_with_household, "remove_item_from_list", {"list_id": list_id, "name": "nope"})
    assert _events(emitted) == []


def test_creating_and_deleting_a_list_notifies_the_household(
    user_client_with_household, household_id, emitted
):
    created = _call(
        user_client_with_household,
        "create_shoppinglist",
        {"household_id": household_id, "name": "Weekly shop"},
    ).get_json()["result"]["structuredContent"]

    _call(user_client_with_household, "delete_shoppinglist", {"list_id": created["id"]})

    assert _events(emitted) == ["shoppinglist:add", "shoppinglist:delete"]


def test_removing_an_item_records_history(user_client_with_household, list_id, household_id):
    _call(user_client_with_household, "add_item_by_name", {"list_id": list_id, "name": "milk"})
    _call(user_client_with_household, "remove_item_from_list", {"list_id": list_id, "name": "milk"})

    from app.models import History

    states = [h.status for h in History.query.all()]
    # An agent dropping an item must leave the same trail a person would, so
    # the suggestion jobs keep working.
    assert len(states) == 2
    assert len(set(states)) == 2
