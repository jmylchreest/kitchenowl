from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from queue import Empty, Queue
from typing import Any, Callable

from flask import Blueprint, Response, current_app, g, jsonify, request, url_for
from flask_jwt_extended import current_user, get_jwt, jwt_required

from app import db, socketio
from app.config import BACKEND_VERSION
from app.errors import (
    ForbiddenRequest,
    InvalidUsage,
    NotFoundRequest,
    UnauthorizedRequest,
)
from app.models import (
    Category,
    History,
    Household,
    HouseholdMember,
    Item,
    Recipe,
    RecipeItems,
    RecipeTags,
    Shoppinglist,
    ShoppinglistItems,
    Token,
    Expense,
    Planner,
    Tag,
)
from app.models.recipe import RecipeVisibility
from app.service.recipe_scraping import scrape
from app.util import description_merger

mcp = Blueprint("mcp", __name__)


DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def _as_tool_result(payload: Any):
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
    }


def _as_tool_error(message: str):
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _tool_error_message(error: Exception) -> str:
    if isinstance(error, (NotFoundRequest, ForbiddenRequest, UnauthorizedRequest, InvalidUsage)):
        return error.message
    if isinstance(error, KeyError):
        return f"Missing required argument: {error.args[0]}"
    if isinstance(error, (ValueError, TypeError)):
        return str(error)
    current_app.logger.exception("MCP tool failed")
    return "The tool failed unexpectedly. The error has been logged."


def _page_args(args: dict[str, Any]) -> tuple[int, int]:
    limit = min(max(int(args.get("limit", DEFAULT_PAGE_SIZE)), 1), MAX_PAGE_SIZE)
    return limit, max(int(args.get("offset", 0)), 0)


def _paginate_query(query, args: dict[str, Any], serialize) -> dict[str, Any]:
    limit, offset = _page_args(args)
    total = query.count()
    rows = query.limit(limit).offset(offset).all()
    return {
        "items": [serialize(r) for r in rows],
        "total": total,
        "offset": offset,
        "has_more": offset + len(rows) < total,
    }


def _paginate_list(rows: list, args: dict[str, Any], serialize) -> dict[str, Any]:
    limit, offset = _page_args(args)
    page = rows[offset : offset + limit]
    return {
        "items": [serialize(r) for r in page],
        "total": len(rows),
        "offset": offset,
        "has_more": offset + len(page) < len(rows),
    }


def _recipe_summary(recipe: Recipe) -> dict[str, Any]:
    # Deliberately omits description, items and the nested household that
    # obj_to_full_dict carries; get_recipe returns those on demand.
    return {
        "id": recipe.id,
        "name": recipe.name,
        "time": recipe.time,
        "cook_time": recipe.cook_time,
        "prep_time": recipe.prep_time,
        "yields": recipe.yields,
        "tags": [t.tag.name for t in recipe.tags],
        "suggestion_score": recipe.suggestion_score,
    }


def _current_token() -> Token | None:
    # Keyed on the jti rather than cached outright, so a second token in the
    # same app context is never answered from the first one's entry.
    jti = get_jwt().get("jti")
    if not jti:
        return None
    if g.get("mcp_token_jti") != jti:
        g.mcp_token_jti = jti
        g.mcp_token = Token.find_by_jti(jti)
    return g.mcp_token


def _tool_permitted(tool: Tool) -> bool:
    token = _current_token()
    scope = token.scope if token else None
    if scope == "read":
        return tool.read_only
    if scope == "write":
        return not tool.deletes
    return True


def _permitted_tools() -> dict[str, Tool]:
    return {name: tool for name, tool in TOOLS.items() if _tool_permitted(tool)}


def _check_household_pin(household_id: int):
    token = _current_token()
    pinned = token.household_id if token else None
    if pinned is not None and pinned != household_id:
        raise ForbiddenRequest("This token is restricted to a different household")


def _emit(event: str, household_id: int, payload: dict[str, Any]):
    socketio.emit(event, payload, to="household/" + str(household_id))


def _emit_item(event: str, shoppinglist: Shoppinglist, con: ShoppinglistItems):
    _emit(
        event,
        shoppinglist.household_id,
        {"item": con.obj_to_item_dict(), "shoppinglist": shoppinglist.obj_to_dict()},
    )


def _require_household_access(household_id: int):
    member = HouseholdMember.find_by_ids(household_id, current_user.id)
    if not member:
        raise NotFoundRequest()
    _check_household_pin(household_id)


def _tool_list_households(args: dict[str, Any]) -> Any:
    members = HouseholdMember.find_by_user(current_user.id)
    token = _current_token()
    if token and token.household_id is not None:
        members = [m for m in members if m.household_id == token.household_id]
    return _paginate_list(members, args, lambda m: m.household.obj_to_dict())


def _tool_list_shoppinglists(args: dict[str, Any]) -> Any:
    household_id = int(args["household_id"])
    _require_household_access(household_id)
    lists = Shoppinglist.all_from_household(household_id)
    return _paginate_list(lists, args, lambda e: e.obj_to_dict())


def _tool_list_shoppinglist_items(args: dict[str, Any]) -> Any:
    list_id = int(args["list_id"])
    shoppinglist = _authorized_shoppinglist(list_id)
    query = ShoppinglistItems.query.filter(
        ShoppinglistItems.shoppinglist_id == list_id
    ).join(ShoppinglistItems.item)
    return _paginate_query(query, args, lambda e: e.obj_to_item_dict())


def _authorized_shoppinglist(list_id: int) -> Shoppinglist:
    shoppinglist = Shoppinglist.find_by_id(list_id)
    if not shoppinglist:
        raise NotFoundRequest()
    shoppinglist.checkAuthorized()
    _check_household_pin(shoppinglist.household_id)
    return shoppinglist


def _authorized_recipe(recipe_id: int) -> Recipe:
    recipe = Recipe.find_by_id(recipe_id)
    if not recipe:
        raise NotFoundRequest()
    recipe.checkAuthorized()
    _check_household_pin(recipe.household_id)
    return recipe


def _authorized_expense(expense_id: int) -> Expense:
    expense = Expense.find_by_id(expense_id)
    if not expense:
        raise NotFoundRequest()
    expense.checkAuthorized()
    _check_household_pin(expense.household_id)
    return expense


def _similar_item_names(household_id: int, name: str, limit: int = 5) -> list[str]:
    """Existing items whose name contains, or is contained by, the given one."""
    lowered = name.strip().lower()
    if not lowered:
        return []
    rows = db.session.query(Item.name).filter(Item.household_id == household_id).all()
    similar = [
        existing
        for (existing,) in rows
        if existing.lower() != lowered
        and (existing.lower() in lowered or lowered in existing.lower())
    ]
    return sorted(similar, key=len)[:limit]


def _find_or_create_item(household_id: int, name: str) -> tuple[Item, list[str] | None]:
    """Resolve a name to an item. Returns the item and, when one had to be
    created, the existing names it resembles."""
    item = Item.find_by_name(household_id, name)
    if item:
        return item, None
    similar = _similar_item_names(household_id, name)
    return Item.create_by_name(household_id, name), similar


def _find_or_create_tag(household_id: int, name: str) -> tuple[Tag, bool]:
    tag = Tag.find_by_name(household_id, name)
    if tag:
        return tag, False
    return Tag.create_by_name(household_id, name), True


def _created_note(created_items: list[dict[str, Any]]) -> dict[str, Any]:
    if not created_items:
        return {}
    note: dict[str, Any] = {"created_items": created_items}
    if any(c.get("similar_existing") for c in created_items):
        note["hint"] = (
            "New household items were created. Where similar_existing names the "
            "same product, use that item and put the variant in description."
        )
    return note


def _put_item_on_list(
    shoppinglist: Shoppinglist, name: str, description: str, merge: bool
) -> dict[str, Any]:
    item, similar = _find_or_create_item(shoppinglist.household_id, name)
    # Minting a household item is a durable side effect, so it is reported
    # separately from merely putting a known item on a list.
    created = similar is not None

    con = ShoppinglistItems.find_by_ids(shoppinglist.id, item.id)
    if con and not created:
        if not merge or not description:
            return {"outcome": "unchanged"}
        con.description = description_merger.merge(con.description, description)
        con.save()
        History.create_added(shoppinglist, item, con.description)
        _emit_item("shoppinglist_item:add", shoppinglist, con)
        return {"outcome": "merged", "description": con.description}

    con = ShoppinglistItems(description=description)
    con.created_by = current_user.id
    con.created_by_token_name = Token.current_llt_name()
    con.item = item
    con.shoppinglist = shoppinglist
    con.save()
    History.create_added(shoppinglist, item, description)
    _emit_item("shoppinglist_item:add", shoppinglist, con)

    result: dict[str, Any] = {"outcome": "created" if created else "added"}
    if similar:
        result["similar_existing"] = similar
        result["hint"] = (
            "A new household item was created. If one of similar_existing is the "
            "same product, remove this one and use that item with the variant in "
            "description instead."
        )
    return result


def _tool_add_item_by_name(args: dict[str, Any]) -> Any:
    shoppinglist = _authorized_shoppinglist(int(args["list_id"]))
    name = str(args["name"]).strip()
    result = _put_item_on_list(
        shoppinglist, name, str(args.get("description", "")), merge=False
    )
    item = Item.find_by_name(shoppinglist.household_id, name)
    return {**item.obj_to_dict(), **result}


def _tool_add_items(args: dict[str, Any]) -> Any:
    shoppinglist = _authorized_shoppinglist(int(args["list_id"]))
    merge = bool(args.get("merge_quantities", True))

    results = []
    for raw in args.get("items") or []:
        if isinstance(raw, str):
            name, description = raw.strip(), ""
        else:
            name = str(raw.get("name", "")).strip()
            description = str(raw.get("description", ""))
        if not name:
            continue
        results.append(
            {"name": name, **_put_item_on_list(shoppinglist, name, description, merge)}
        )

    return {"list_id": shoppinglist.id, "results": results}


def _tool_add_recipe_items_to_list(args: dict[str, Any]) -> Any:
    recipe = _authorized_recipe(int(args["recipe_id"]))
    shoppinglist = _authorized_shoppinglist(int(args["list_id"]))
    if recipe.household_id != shoppinglist.household_id:
        raise InvalidUsage("Recipe and shopping list belong to different households")

    include_optional = bool(args.get("include_optional", False))
    wanted = {str(n).strip().lower() for n in (args.get("only_items") or [])}

    results = []
    for con in recipe.items:
        if con.optional and not include_optional:
            continue
        if wanted and con.item.name.lower() not in wanted:
            continue
        results.append(
            {
                "name": con.item.name,
                **_put_item_on_list(
                    shoppinglist, con.item.name, con.description or "", merge=True
                ),
            }
        )

    return {
        "recipe": {"id": recipe.id, "name": recipe.name},
        "list_id": shoppinglist.id,
        "results": results,
    }


def _tool_update_item_description(args: dict[str, Any]) -> Any:
    shoppinglist = _authorized_shoppinglist(int(args["list_id"]))
    name = str(args["name"]).strip()

    item = Item.find_by_name(shoppinglist.household_id, name)
    con = ShoppinglistItems.find_by_ids(shoppinglist.id, item.id) if item else None
    if not con:
        return {"updated": False, "reason": "not_on_list"}

    con.description = str(args.get("description", ""))
    con.save()
    _emit_item("shoppinglist_item:add", shoppinglist, con)
    return {"updated": True, **con.obj_to_item_dict()}


def _tool_list_categories(args: dict[str, Any]) -> Any:
    household_id = int(args["household_id"])
    _require_household_access(household_id)
    categories = Category.all_by_ordering(household_id)
    return _paginate_list(list(categories), args, lambda c: c.obj_to_dict())


def _tool_list_recipes(args: dict[str, Any]) -> Any:
    household_id = int(args["household_id"])
    _require_household_access(household_id)
    query = Recipe.query.filter(Recipe.household_id == household_id).order_by(Recipe.name)
    return _paginate_query(query, args, _recipe_summary)


def _tool_search_recipes(args: dict[str, Any]) -> Any:
    household_id = int(args["household_id"])
    query = str(args["query"]).strip()
    _require_household_access(household_id)
    matches = (
        Recipe.query.filter(Recipe.household_id == household_id)
        .filter(Recipe.name.ilike(f"%{query}%"))
        .order_by(Recipe.name)
    )
    return _paginate_query(matches, args, _recipe_summary)


def _tool_create_recipe(args: dict[str, Any]) -> Any:
    household_id = int(args["household_id"])
    _require_household_access(household_id)

    recipe = Recipe()
    recipe.name = str(args["name"]).strip()[:128]
    recipe.description = str(args.get("description", ""))
    recipe.household_id = household_id

    if "time" in args and args["time"] is not None:
        recipe.time = int(args["time"])
    if "cook_time" in args and args["cook_time"] is not None:
        recipe.cook_time = int(args["cook_time"])
    if "prep_time" in args and args["prep_time"] is not None:
        recipe.prep_time = int(args["prep_time"])
    if "yields" in args and args["yields"] is not None:
        recipe.yields = int(args["yields"])
    if "source" in args and args["source"] is not None:
        recipe.source = str(args["source"])
    if "visibility" in args and args["visibility"] is not None:
        recipe.visibility = RecipeVisibility(int(args["visibility"]))

    recipe.save()

    created_items: list[dict[str, Any]] = []
    created_tags: list[str] = []

    for recipe_item in (args.get("items") or []):
        if isinstance(recipe_item, str):
            item_name = recipe_item
            item_description = ""
            item_optional = False
        else:
            item_name = str(recipe_item.get("name", "")).strip()
            item_description = str(recipe_item.get("description", ""))
            item_optional = bool(recipe_item.get("optional", False))

        if not item_name:
            continue

        item, similar = _find_or_create_item(household_id, item_name)
        if similar is not None:
            created_items.append(
                {"name": item.name, **({"similar_existing": similar} if similar else {})}
            )

        con = RecipeItems(description=item_description, optional=item_optional)
        con.item = item
        con.recipe = recipe
        con.save()

    for tag_name in (args.get("tags") or []):
        name = str(tag_name).strip()
        if not name:
            continue
        tag, tag_created = _find_or_create_tag(household_id, name)
        if tag_created:
            created_tags.append(tag.name)
        con = RecipeTags()
        con.tag = tag
        con.recipe = recipe
        con.save()

    result = recipe.obj_to_full_dict() | _created_note(created_items)
    if created_tags:
        result["created_tags"] = created_tags
    return result


def _tool_get_recipe(args: dict[str, Any]) -> Any:
    recipe_id = int(args["recipe_id"])
    recipe = _authorized_recipe(recipe_id)
    return recipe.obj_to_full_dict()


def _tool_delete_recipe(args: dict[str, Any]) -> Any:
    recipe_id = int(args["recipe_id"])
    recipe = _authorized_recipe(recipe_id)
    name = recipe.name
    recipe.delete()
    return {"deleted": True, "id": recipe_id, "name": name}


def _tool_list_items(args: dict[str, Any]) -> Any:
    household_id = int(args["household_id"])
    _require_household_access(household_id)
    search = str(args.get("search", "")).strip()

    q = Item.query.filter(Item.household_id == household_id)
    if search:
        q = q.filter(Item.name.ilike(f"%{search}%"))
    return _paginate_query(q.order_by(Item.name), args, lambda i: i.obj_to_dict())


def _tool_list_tags(args: dict[str, Any]) -> Any:
    household_id = int(args["household_id"])
    _require_household_access(household_id)
    query = Tag.query.filter(Tag.household_id == household_id).order_by(Tag.name)
    return _paginate_query(query, args, lambda t: t.obj_to_full_dict())


def _tool_create_tag(args: dict[str, Any]) -> Any:
    household_id = int(args["household_id"])
    _require_household_access(household_id)
    name = str(args["name"]).strip()
    if not name:
        return {"created": False, "reason": "empty_name"}

    tag, created = _find_or_create_tag(household_id, name)
    return tag.obj_to_full_dict() | {"created": created}


def _tool_create_shoppinglist(args: dict[str, Any]) -> Any:
    household_id = int(args["household_id"])
    _require_household_access(household_id)
    name = str(args["name"]).strip()[:128]
    if not name:
        return {"created": False, "reason": "empty_name"}

    shoppinglist = Shoppinglist(name=name, household_id=household_id)
    shoppinglist.save()
    _emit("shoppinglist:add", household_id, {"shoppinglist": shoppinglist.obj_to_dict()})
    return shoppinglist.obj_to_dict()


def _tool_delete_shoppinglist(args: dict[str, Any]) -> Any:
    list_id = int(args["list_id"])
    shoppinglist = _authorized_shoppinglist(list_id)

    if shoppinglist.isDefault():
        return {"deleted": False, "reason": "default_list"}

    name = shoppinglist.name
    payload = {"shoppinglist": shoppinglist.obj_to_dict()}
    household_id = shoppinglist.household_id
    shoppinglist.delete()
    _emit("shoppinglist:delete", household_id, payload)
    return {"deleted": True, "id": list_id, "name": name}


def _tool_remove_item_from_list(args: dict[str, Any]) -> Any:
    list_id = int(args["list_id"])
    shoppinglist = _authorized_shoppinglist(list_id)

    item_id = args.get("item_id")
    item_name = str(args.get("name", "")).strip()

    con = None
    if item_id is not None:
        con = ShoppinglistItems.find_by_ids(list_id, int(item_id))
    elif item_name:
        item = Item.find_by_name(shoppinglist.household_id, item_name)
        if item:
            con = ShoppinglistItems.find_by_ids(list_id, item.id)

    if not con:
        return {"removed": False, "reason": "not_found"}

    removed_item = con.item.obj_to_dict()
    item = con.item
    description = con.description
    con.delete()
    History.create_dropped(shoppinglist, item, description)
    _emit_item("shoppinglist_item:remove", shoppinglist, con)
    return {"removed": True, "list_id": list_id, "item": removed_item}


def _tool_add_recipe_item(args: dict[str, Any]) -> Any:
    recipe_id = int(args["recipe_id"])
    recipe = _authorized_recipe(recipe_id)

    item_name = str(args["name"]).strip()
    if not item_name:
        return {"added": False, "reason": "empty_name"}

    item, similar = _find_or_create_item(recipe.household_id, item_name)
    created = (
        [{"name": item.name, **({"similar_existing": similar} if similar else {})}]
        if similar is not None
        else []
    )

    con = RecipeItems.find_by_ids(recipe.id, item.id)
    if not con:
        con = RecipeItems(
            description=str(args.get("description", "")),
            optional=bool(args.get("optional", False)),
        )
    else:
        if "description" in args:
            con.description = str(args.get("description", ""))
        if "optional" in args:
            con.optional = bool(args.get("optional", False))

    con.item = item
    con.recipe = recipe
    con.save()
    return recipe.obj_to_full_dict() | _created_note(created)


def _tool_remove_recipe_item(args: dict[str, Any]) -> Any:
    recipe_id = int(args["recipe_id"])
    item_id = int(args["item_id"])
    recipe = _authorized_recipe(recipe_id)

    con = RecipeItems.find_by_ids(recipe_id, item_id)
    if not con:
        return {"removed": False, "reason": "not_found"}
    con.delete()
    return recipe.obj_to_full_dict()


def _tool_add_recipe_tag(args: dict[str, Any]) -> Any:
    recipe_id = int(args["recipe_id"])
    recipe = _authorized_recipe(recipe_id)

    tag_name = str(args["name"]).strip()
    if not tag_name:
        return {"added": False, "reason": "empty_name"}

    tag, tag_created = _find_or_create_tag(recipe.household_id, tag_name)

    con = RecipeTags.find_by_ids(recipe.id, tag.id)
    if not con:
        con = RecipeTags()
        con.tag = tag
        con.recipe = recipe
        con.save()

    result = recipe.obj_to_full_dict()
    if tag_created:
        result["created_tags"] = [tag.name]
    return result


def _tool_remove_recipe_tag(args: dict[str, Any]) -> Any:
    recipe_id = int(args["recipe_id"])
    recipe = _authorized_recipe(recipe_id)

    tag_id = args.get("tag_id")
    tag_name = str(args.get("name", "")).strip()

    con = None
    if tag_id is not None:
        con = RecipeTags.find_by_ids(recipe.id, int(tag_id))
    elif tag_name:
        tag = Tag.find_by_name(recipe.household_id, tag_name)
        if tag:
            con = RecipeTags.find_by_ids(recipe.id, tag.id)

    if not con:
        return {"removed": False, "reason": "not_found"}

    con.delete()
    return recipe.obj_to_full_dict()


def _tool_update_recipe(args: dict[str, Any]) -> Any:
    recipe_id = int(args["recipe_id"])
    recipe = _authorized_recipe(recipe_id)

    if "name" in args:
        recipe.name = str(args["name"]).strip()[:128]
    if "description" in args:
        recipe.description = str(args.get("description", ""))
    if "time" in args:
        recipe.time = int(args["time"]) if args["time"] is not None else None
    if "cook_time" in args:
        recipe.cook_time = int(args["cook_time"]) if args["cook_time"] is not None else None
    if "prep_time" in args:
        recipe.prep_time = int(args["prep_time"]) if args["prep_time"] is not None else None
    if "yields" in args:
        recipe.yields = int(args["yields"]) if args["yields"] is not None else None
    if "source" in args:
        recipe.source = str(args["source"]) if args["source"] is not None else None
    if "visibility" in args and args["visibility"] is not None:
        recipe.visibility = RecipeVisibility(int(args["visibility"]))

    recipe.save()
    return recipe.obj_to_full_dict()


def _tool_list_expenses(args: dict[str, Any]) -> Any:
    household_id = int(args["household_id"])
    search = str(args.get("search", "")).strip()
    _require_household_access(household_id)

    q = Expense.query.filter(Expense.household_id == household_id)
    if search:
        q = q.filter(Expense.name.ilike(f"%{search}%"))
    return _paginate_query(
        q.order_by(Expense.date.desc()), args, lambda e: e.obj_to_full_dict()
    )


def _tool_create_expense(args: dict[str, Any]) -> Any:
    household_id = int(args["household_id"])
    _require_household_access(household_id)

    expense = Expense()
    expense.household_id = household_id
    expense.name = str(args["name"]).strip()[:128]
    expense.amount = float(args["amount"])
    expense.description = str(args.get("description", ""))
    expense.paid_by_id = current_user.id

    date_raw = args.get("date")
    if date_raw:
        expense.date = datetime.fromisoformat(str(date_raw).replace("Z", "+00:00"))

    expense.save()
    return expense.obj_to_full_dict()


def _tool_delete_expense(args: dict[str, Any]) -> Any:
    expense_id = int(args["expense_id"])
    expense = _authorized_expense(expense_id)
    name = expense.name
    expense.delete()
    return {"deleted": True, "id": expense_id, "name": name}


def _tool_add_planner_entry(args: dict[str, Any]) -> Any:
    household_id = int(args["household_id"])
    recipe_id = int(args["recipe_id"])
    _require_household_access(household_id)

    recipe = Recipe.find_by_id(recipe_id)
    if not recipe or recipe.household_id != household_id:
        raise NotFoundRequest()

    cooking_date = datetime.fromisoformat(str(args["cooking_date"]).replace("Z", "+00:00"))

    existing = Planner.query.filter(
        Planner.household_id == household_id,
        Planner.recipe_id == recipe_id,
        Planner.cooking_date == cooking_date,
    ).first()
    if existing:
        return existing.obj_to_full_dict()

    plan = Planner(
        household_id=household_id,
        recipe_id=recipe_id,
        cooking_date=cooking_date,
        yields=int(args.get("yields", 1)),
    )
    plan.save()
    return plan.obj_to_full_dict()


def _tool_remove_planner_entry(args: dict[str, Any]) -> Any:
    household_id = int(args["household_id"])
    recipe_id = int(args["recipe_id"])
    cooking_date = datetime.fromisoformat(str(args["cooking_date"]).replace("Z", "+00:00"))
    _require_household_access(household_id)

    plan = Planner.query.filter(
        Planner.household_id == household_id,
        Planner.recipe_id == recipe_id,
        Planner.cooking_date == cooking_date,
    ).first()
    if not plan:
        return {"removed": False, "reason": "not_found"}

    plan.delete()
    return {"removed": True, "household_id": household_id, "recipe_id": recipe_id, "cooking_date": cooking_date}


def _tool_list_planner(args: dict[str, Any]) -> Any:
    household_id = int(args["household_id"])
    _require_household_access(household_id)
    plans = Planner.all_from_household(household_id)
    return _paginate_list(plans, args, lambda p: p.obj_to_full_dict())


def _tool_suggest_recipes(args: dict[str, Any]) -> Any:
    household_id = int(args["household_id"])
    _require_household_access(household_id)
    page = max(int(args.get("page", 0)), 0)

    recipes = Recipe.find_suggestions(household_id, page)
    result: dict[str, Any] = {
        "items": [_recipe_summary(r) for r in recipes],
        "page": page,
        "has_more": len(recipes) == 10,
    }
    if not recipes and page == 0:
        # An empty ranking is not an empty household, and the difference
        # matters: without it the model concludes there is nothing to cook.
        result["reason"] = (
            "No ranking available. Suggestions are computed nightly from cooked "
            "recipe history, so a household that has not cooked from its recipes "
            "yet has none. Use list_recipes instead."
        )
    return result


def _tool_scrape_recipe(args: dict[str, Any]) -> Any:
    household_id = int(args["household_id"])
    url = str(args["url"]).strip()
    _require_household_access(household_id)

    household = Household.find_by_id(household_id)
    if not household:
        raise NotFoundRequest()

    res = scrape(url, household)
    if not res:
        raise ValueError("Unsupported website")
    return res


@dataclass(frozen=True)
class Tool:
    title: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False
    # Destroys a durable object, not just an entry in a collection.
    deletes: bool = False

    def annotations(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "idempotentHint": self.idempotent,
            "openWorldHint": self.open_world,
        }


def _schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required)}


def _paged(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        **properties,
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_PAGE_SIZE,
            "description": f"Rows to return, {1}-{MAX_PAGE_SIZE}, default {DEFAULT_PAGE_SIZE}.",
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": "Rows to skip. Use with the has_more flag to page through results.",
        },
    }


P_HOUSEHOLD = {
    "type": "integer",
    "description": "Household id, as returned by list_households.",
}
P_LIST = {
    "type": "integer",
    "description": "Shopping list id, as returned by list_shoppinglists.",
}
P_RECIPE = {"type": "integer", "description": "Recipe id, as returned by list_recipes."}
P_QUANTITY = {
    "type": "string",
    "description": (
        "Free text next to the item: a quantity like '2 kg' or '3 x', or a variant "
        "like 'salted caramel'. Variants belong here, not in the name."
    ),
}
P_COOKING_DATE = {
    "type": "string",
    "description": "Day the recipe is planned for, as an ISO-8601 date-time.",
}
P_VISIBILITY = {
    "type": "integer",
    "enum": [0, 1, 2],
    "description": "0 private to the household, 1 shared by link, 2 public.",
}

RECIPE_FIELDS = {
    "name": {"type": "string", "description": "Recipe name, truncated to 128 characters."},
    "description": {
        "type": "string",
        "description": "The method, as markdown. This is the body of the recipe.",
    },
    "time": {"type": "integer", "description": "Total time in minutes."},
    "cook_time": {"type": "integer", "description": "Cooking time in minutes."},
    "prep_time": {"type": "integer", "description": "Preparation time in minutes."},
    "yields": {"type": "integer", "description": "Number of servings the recipe makes."},
    "source": {"type": "string", "description": "URL or book the recipe came from."},
    "visibility": P_VISIBILITY,
}


TOOLS: dict[str, Tool] = {
    "list_households": Tool(
        title="List households",
        description=(
            "List the households you are a member of. Almost every other tool needs a "
            "household_id, so call this first and reuse the id."
        ),
        schema=_schema(_paged({})),
        handler=_tool_list_households,
        read_only=True,
        idempotent=True,
    ),
    "list_shoppinglists": Tool(
        title="List shopping lists",
        description=(
            "List the shopping lists belonging to a household. Most households have a "
            "single list named 'Default'."
        ),
        schema=_schema(_paged({"household_id": P_HOUSEHOLD}), ("household_id",)),
        handler=_tool_list_shoppinglists,
        read_only=True,
        idempotent=True,
    ),
    "list_shoppinglist_items": Tool(
        title="List items to buy",
        description=(
            "List what is currently on a shopping list, i.e. what still needs buying. "
            "Each entry carries the item name and a free-text description holding the "
            "quantity. To see the household's full item vocabulary instead, use list_items."
        ),
        schema=_schema(_paged({"list_id": P_LIST}), ("list_id",)),
        handler=_tool_list_shoppinglist_items,
        read_only=True,
        idempotent=True,
    ),
    "add_item_by_name": Tool(
        title="Add item to shopping list",
        description=(
            "Put an item on a shopping list. A name that matches no known item creates "
            "one in the household, which is durable and starts with no category, icon "
            "or history, so the result reports outcome 'created' rather than 'added'. "
            "Adding an item already on the list changes nothing, so this is safe to "
            "retry."
        ),
        schema=_schema(
            {
                "list_id": P_LIST,
                "name": {
                    "type": "string",
                    "description": "Item name on its own, e.g. 'milk'. Put quantities in description.",
                },
                "description": P_QUANTITY,
            },
            ("list_id", "name"),
        ),
        handler=_tool_add_item_by_name,
        idempotent=True,
    ),
    "add_items": Tool(
        title="Add several items to a shopping list",
        description=(
            "Put several items on a shopping list in one call. Prefer this over "
            "repeated add_item_by_name. By default an item already on the list has its "
            "quantity merged with the one you give, so asking for '1 l milk' twice "
            "leaves '2 l' rather than a duplicate row.\n\n"
            "Names that do not match a known item create one in the household, which "
            "is durable and starts with no category, icon or history. Each result "
            "reports outcome 'added' or 'created', and a created one lists the "
            "existing names it resembles so you can correct it."
        ),
        schema=_schema(
            {
                "list_id": P_LIST,
                "items": {
                    "type": "array",
                    "description": "Items to add, as names or {name, description} objects.",
                    "items": {
                        "oneOf": [
                            {"type": "string"},
                            {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "description": "Item name."},
                                    "description": P_QUANTITY,
                                },
                                "required": ["name"],
                            },
                        ]
                    },
                },
                "merge_quantities": {
                    "type": "boolean",
                    "description": "Merge quantities into items already listed. Default true.",
                },
            },
            ("list_id", "items"),
        ),
        handler=_tool_add_items,
    ),
    "add_recipe_items_to_list": Tool(
        title="Add a recipe's ingredients to a shopping list",
        description=(
            "Copy a recipe's ingredients onto a shopping list, merging quantities into "
            "anything already there. This is the tool for turning a meal plan into a "
            "shop. Optional ingredients are left out unless you ask for them, and the "
            "recipe and list must belong to the same household."
        ),
        schema=_schema(
            {
                "recipe_id": P_RECIPE,
                "list_id": P_LIST,
                "include_optional": {
                    "type": "boolean",
                    "description": "Include ingredients marked optional. Default false.",
                },
                "only_items": {
                    "type": "array",
                    "description": "Only add these ingredient names, e.g. to skip what is in stock.",
                    "items": {"type": "string"},
                },
            },
            ("recipe_id", "list_id"),
        ),
        handler=_tool_add_recipe_items_to_list,
    ),
    "update_item_description": Tool(
        title="Change an item's quantity",
        description=(
            "Replace the quantity text of an item already on a shopping list, e.g. to "
            "correct '1 kg' to '500 g'. Use add_items if you want to add to the "
            "existing quantity instead of overwriting it."
        ),
        schema=_schema(
            {
                "list_id": P_LIST,
                "name": {"type": "string", "description": "Name of the item on the list."},
                "description": P_QUANTITY,
            },
            ("list_id", "name"),
        ),
        handler=_tool_update_item_description,
        destructive=True,
        idempotent=True,
    ),
    "list_categories": Tool(
        title="List categories",
        description=(
            "List a household's item categories in shop order, e.g. 'Produce' or "
            "'Dairy'. Items carry a category_id, so this is how you group a shopping "
            "list by aisle."
        ),
        schema=_schema(_paged({"household_id": P_HOUSEHOLD}), ("household_id",)),
        handler=_tool_list_categories,
        read_only=True,
        idempotent=True,
    ),
    "remove_item_from_list": Tool(
        title="Remove item from shopping list",
        description=(
            "Take an item off a shopping list, typically once it has been bought. "
            "Identify it by item_id or by name. The item itself is kept in the "
            "household so it can be added again later."
        ),
        schema=_schema(
            {
                "list_id": P_LIST,
                "item_id": {"type": "integer", "description": "Item id. Preferred over name."},
                "name": {"type": "string", "description": "Item name, if the id is unknown."},
            },
            ("list_id",),
        ),
        handler=_tool_remove_item_from_list,
        destructive=True,
        idempotent=True,
    ),
    "create_shoppinglist": Tool(
        title="Create shopping list",
        description="Create an additional shopping list in a household.",
        schema=_schema(
            {
                "household_id": P_HOUSEHOLD,
                "name": {"type": "string", "description": "List name, e.g. 'Weekly shop'."},
            },
            ("household_id", "name"),
        ),
        handler=_tool_create_shoppinglist,
    ),
    "delete_shoppinglist": Tool(
        title="Delete shopping list",
        description=(
            "Delete a shopping list and everything on it. The household's default list "
            "cannot be deleted."
        ),
        schema=_schema({"list_id": P_LIST}, ("list_id",)),
        handler=_tool_delete_shoppinglist,
        destructive=True,
        deletes=True,
    ),
    "list_items": Tool(
        title="List known items",
        description=(
            "List the items a household knows about, whether or not they are on a list "
            "right now. Call this before adding anything whose name you have not seen "
            "in this session: matching an existing item keeps its category, icon and "
            "history, which a near-duplicate would lose. Use the search filter on a "
            "distinctive word, e.g. 'cream' rather than 'salted caramel ice cream'."
        ),
        schema=_schema(
            _paged(
                {
                    "household_id": P_HOUSEHOLD,
                    "search": {
                        "type": "string",
                        "description": "Case-insensitive substring filter on the item name.",
                    },
                }
            ),
            ("household_id",),
        ),
        handler=_tool_list_items,
        read_only=True,
        idempotent=True,
    ),
    "list_recipes": Tool(
        title="List recipes",
        description=(
            "List a household's recipes as summaries: id, name, times, yields and tags. "
            "Ingredients and method are omitted, so call get_recipe for a specific one."
        ),
        schema=_schema(_paged({"household_id": P_HOUSEHOLD}), ("household_id",)),
        handler=_tool_list_recipes,
        read_only=True,
        idempotent=True,
    ),
    "search_recipes": Tool(
        title="Search recipes",
        description=(
            "Find recipes whose name contains the query, case-insensitive. Returns the "
            "same summaries as list_recipes. Matches names only, not ingredients."
        ),
        schema=_schema(
            _paged(
                {
                    "household_id": P_HOUSEHOLD,
                    "query": {"type": "string", "description": "Substring to look for in recipe names."},
                }
            ),
            ("household_id", "query"),
        ),
        handler=_tool_search_recipes,
        read_only=True,
        idempotent=True,
    ),
    "suggest_recipes": Tool(
        title="Suggest recipes from history",
        description=(
            "Recipes this household actually cooks, ranked from the last six "
            "months and excluding anything already on the planner. Start here when "
            "asked to plan meals, so the plan follows what gets eaten rather than "
            "guesswork. Falls back with a reason when no ranking exists yet."
        ),
        schema=_schema(
            {
                "household_id": P_HOUSEHOLD,
                "page": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Page of ten, 0 for the first.",
                },
            },
            ("household_id",),
        ),
        handler=_tool_suggest_recipes,
        read_only=True,
        idempotent=True,
    ),
    "get_recipe": Tool(
        title="Get recipe",
        description="Get one recipe in full, including its method, ingredients and tags.",
        schema=_schema({"recipe_id": P_RECIPE}, ("recipe_id",)),
        handler=_tool_get_recipe,
        read_only=True,
        idempotent=True,
    ),
    "create_recipe": Tool(
        title="Create recipe",
        description=(
            "Create a recipe in a household. Ingredients may be given as plain names or "
            "as objects carrying a description and an optional flag.\n\n"
            "Ingredient names that match no known item create one in the household, and "
            "tags behave the same way. The result reports created_items and "
            "created_tags, with the existing names each new item resembles. Prefer an "
            "existing item with the variant in its description over a new near-"
            "duplicate: check list_items for names you have not seen this session."
        ),
        schema=_schema(
            {
                "household_id": P_HOUSEHOLD,
                **RECIPE_FIELDS,
                "items": {
                    "type": "array",
                    "description": "Ingredients, as names or {name, description, optional} objects.",
                    "items": {
                        "oneOf": [
                            {"type": "string"},
                            {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "description": "Ingredient name."},
                                    "description": P_QUANTITY,
                                    "optional": {
                                        "type": "boolean",
                                        "description": "True if the recipe works without it.",
                                    },
                                },
                                "required": ["name"],
                            },
                        ]
                    },
                },
                "tags": {
                    "type": "array",
                    "description": "Tag names, e.g. 'vegetarian'. Created if they do not exist.",
                    "items": {"type": "string"},
                },
            },
            ("household_id", "name"),
        ),
        handler=_tool_create_recipe,
    ),
    "update_recipe": Tool(
        title="Update recipe",
        description=(
            "Change fields on an existing recipe. Only the fields you pass are touched, "
            "and each one replaces the stored value outright. Ingredients and tags are "
            "managed with the add_recipe_item and add_recipe_tag tools."
        ),
        schema=_schema({"recipe_id": P_RECIPE, **RECIPE_FIELDS}, ("recipe_id",)),
        handler=_tool_update_recipe,
        destructive=True,
        idempotent=True,
    ),
    "delete_recipe": Tool(
        title="Delete recipe",
        description="Permanently delete a recipe and its ingredient and tag links.",
        schema=_schema({"recipe_id": P_RECIPE}, ("recipe_id",)),
        handler=_tool_delete_recipe,
        destructive=True,
        deletes=True,
    ),
    "add_recipe_item": Tool(
        title="Add ingredient to recipe",
        description=(
            "Add an ingredient to a recipe. Calling it for an ingredient already on the "
            "recipe updates that ingredient's description and optional flag instead of "
            "duplicating it.\n\n"
            "A name matching no known item creates one in the household, which is "
            "durable and starts with no category, icon or history; the result then "
            "reports created_items with the existing names it resembles."
        ),
        schema=_schema(
            {
                "recipe_id": P_RECIPE,
                "name": {"type": "string", "description": "Ingredient name, e.g. 'plain flour'."},
                "description": P_QUANTITY,
                "optional": {"type": "boolean", "description": "True if the recipe works without it."},
            },
            ("recipe_id", "name"),
        ),
        handler=_tool_add_recipe_item,
        idempotent=True,
    ),
    "remove_recipe_item": Tool(
        title="Remove ingredient from recipe",
        description="Remove an ingredient from a recipe. The item itself is kept in the household.",
        schema=_schema(
            {
                "recipe_id": P_RECIPE,
                "item_id": {"type": "integer", "description": "Item id, from the recipe's items."},
            },
            ("recipe_id", "item_id"),
        ),
        handler=_tool_remove_recipe_item,
        destructive=True,
        idempotent=True,
    ),
    "list_tags": Tool(
        title="List tags",
        description=(
            "List a household's recipe tags, e.g. 'vegetarian' or 'quick'. Call this "
            "before tagging with a name you are unsure of, so you reuse a tag rather "
            "than creating a near-duplicate of it."
        ),
        schema=_schema(_paged({"household_id": P_HOUSEHOLD}), ("household_id",)),
        handler=_tool_list_tags,
        read_only=True,
        idempotent=True,
    ),
    "create_tag": Tool(
        title="Create tag",
        description=(
            "Create a recipe tag in a household. Returns the existing tag if one with "
            "that name is already there, with created telling you which happened."
        ),
        schema=_schema(
            {
                "household_id": P_HOUSEHOLD,
                "name": {"type": "string", "description": "Tag name, e.g. 'vegetarian'."},
            },
            ("household_id", "name"),
        ),
        handler=_tool_create_tag,
        idempotent=True,
    ),
    "add_recipe_tag": Tool(
        title="Tag a recipe",
        description=(
            "Attach a tag to a recipe. A tag name that does not exist yet is created in "
            "the household and reported as created_tags, so check list_tags first if "
            "you are guessing at a name."
        ),
        schema=_schema(
            {
                "recipe_id": P_RECIPE,
                "name": {"type": "string", "description": "Tag name to attach."},
            },
            ("recipe_id", "name"),
        ),
        handler=_tool_add_recipe_tag,
        idempotent=True,
    ),
    "remove_recipe_tag": Tool(
        title="Untag a recipe",
        description=(
            "Detach a tag from a recipe, by tag_id or name. The tag itself stays in the "
            "household."
        ),
        schema=_schema(
            {
                "recipe_id": P_RECIPE,
                "tag_id": {"type": "integer", "description": "Tag id. Preferred over name."},
                "name": {"type": "string", "description": "Tag name, if the id is unknown."},
            },
            ("recipe_id",),
        ),
        handler=_tool_remove_recipe_tag,
        destructive=True,
        idempotent=True,
    ),
    "list_planner": Tool(
        title="List meal plan",
        description="List the recipes planned in a household, with the day each is planned for.",
        schema=_schema(_paged({"household_id": P_HOUSEHOLD}), ("household_id",)),
        handler=_tool_list_planner,
        read_only=True,
        idempotent=True,
    ),
    "add_planner_entry": Tool(
        title="Plan a meal",
        description=(
            "Plan a recipe for a given day. Planning the same recipe on the same day "
            "twice changes nothing. The recipe must belong to the household."
        ),
        schema=_schema(
            {
                "household_id": P_HOUSEHOLD,
                "recipe_id": P_RECIPE,
                "cooking_date": P_COOKING_DATE,
                "yields": {"type": "integer", "description": "Servings to cook, default 1."},
            },
            ("household_id", "recipe_id", "cooking_date"),
        ),
        handler=_tool_add_planner_entry,
        idempotent=True,
    ),
    "remove_planner_entry": Tool(
        title="Unplan a meal",
        description="Remove a planned recipe from a given day. The recipe itself is kept.",
        schema=_schema(
            {
                "household_id": P_HOUSEHOLD,
                "recipe_id": P_RECIPE,
                "cooking_date": P_COOKING_DATE,
            },
            ("household_id", "recipe_id", "cooking_date"),
        ),
        handler=_tool_remove_planner_entry,
        destructive=True,
        idempotent=True,
    ),
    "list_expenses": Tool(
        title="List expenses",
        description="List a household's expenses, most recent first, with who paid and the split.",
        schema=_schema(
            _paged(
                {
                    "household_id": P_HOUSEHOLD,
                    "search": {
                        "type": "string",
                        "description": "Case-insensitive substring filter on the expense name.",
                    },
                }
            ),
            ("household_id",),
        ),
        handler=_tool_list_expenses,
        read_only=True,
        idempotent=True,
    ),
    "create_expense": Tool(
        title="Record expense",
        description=(
            "Record an expense paid by you, in the household's currency. Defaults to now "
            "unless a date is given."
        ),
        schema=_schema(
            {
                "household_id": P_HOUSEHOLD,
                "name": {"type": "string", "description": "What the money was spent on."},
                "amount": {"type": "number", "description": "Amount paid, in the household currency."},
                "description": {"type": "string", "description": "Optional longer note."},
                "date": {"type": "string", "description": "When it was paid, ISO-8601. Defaults to now."},
            },
            ("household_id", "name", "amount"),
        ),
        handler=_tool_create_expense,
    ),
    "delete_expense": Tool(
        title="Delete expense",
        description="Permanently delete an expense and rebalance the household accordingly.",
        schema=_schema(
            {"expense_id": {"type": "integer", "description": "Expense id, from list_expenses."}},
            ("expense_id",),
        ),
        handler=_tool_delete_expense,
        destructive=True,
        deletes=True,
    ),
    "scrape_recipe": Tool(
        title="Import recipe from a URL",
        description=(
            "Fetch a recipe from a web page and return it parsed. Nothing is saved, so "
            "keeping it is a separate step. Fails on sites that publish no "
            "recognisable recipe data."
        ),
        schema=_schema(
            {
                "household_id": P_HOUSEHOLD,
                "url": {"type": "string", "description": "Public URL of the recipe page."},
            },
            ("household_id", "url"),
        ),
        handler=_tool_scrape_recipe,
        read_only=True,
        open_world=True,
    ),
}


SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

SERVER_INSTRUCTIONS = (
    "KitchenOwl manages households, each of which owns shopping lists, items, "
    "recipes, tags, meal plans and expenses. Nearly every tool is scoped to a "
    "household, so call list_households first and reuse the id you get back.\n\n"
    "A household keeps one item per product, and each item carries a category, an "
    "icon and the history the suggestion features learn from. Naming a variant as "
    "a new item throws all of that away, so prefer an existing item with the "
    "variant in its description: 'Ice cream' described as 'salted caramel', not a "
    "new 'Salted caramel ice cream'. Search list_items before adding something you "
    "have not seen in this session. Tools report outcome 'created' when they had "
    "to mint a new item, along with the existing names it resembles."
)

SSE_KEEPALIVE_SECONDS = 15


def _dispatch(body: Any) -> Any:
    # Returns the response to send back, or None for notifications, which the
    # protocol requires be left unanswered.
    if isinstance(body, list):
        responses = [r for r in (_dispatch(item) for item in body) if r is not None]
        return responses or None

    if not isinstance(body, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }

    id_value = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}
    is_notification = "id" not in body

    try:
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = (
                requested
                if requested in SUPPORTED_PROTOCOL_VERSIONS
                else LATEST_PROTOCOL_VERSION
            )
            result = {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "kitchenowl-mcp",
                    "version": str(BACKEND_VERSION),
                },
                "instructions": SERVER_INSTRUCTIONS,
            }
        elif method is not None and method.startswith("notifications/"):
            return None
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": name,
                        "description": tool.description,
                        "inputSchema": tool.schema,
                        "annotations": tool.annotations(),
                    }
                    for name, tool in _permitted_tools().items()
                ]
            }
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name not in TOOLS:
                return None if is_notification else _rpc_error(
                    id_value, -32602, f"Unknown tool: {name}"
                )
            if not _tool_permitted(TOOLS[name]):
                return None if is_notification else _rpc_ok(
                    id_value,
                    _as_tool_error(
                        f"This token's scope does not permit {name}. Mint a token with a "
                        "wider scope if you need it."
                    ),
                )
            try:
                result = _as_tool_result(TOOLS[name].handler(args))
                db.session.commit()
            except Exception as e:
                # A tool that fails is a result the model can react to, not a
                # protocol fault that should abort the call.
                db.session.rollback()
                result = _as_tool_error(_tool_error_message(e))
        else:
            return None if is_notification else _rpc_error(
                id_value, -32601, f"Method not found: {method}"
            )
    except Exception as e:
        db.session.rollback()
        return None if is_notification else _rpc_error(id_value, -32000, str(e))

    return None if is_notification else {"jsonrpc": "2.0", "id": id_value, "result": result}


def _rpc_error(id_value: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_value, "error": {"code": code, "message": message}}


def _rpc_ok(id_value: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_value, "result": result}


# Streamable HTTP. Stateless: no session id is issued, so this transport keeps
# working across multiple uWSGI workers.


@mcp.route("", methods=["POST"])
@jwt_required()
def mcp_post():
    response = _dispatch(request.get_json(silent=True))
    if response is None:
        return "", 202
    return jsonify(response)


@mcp.route("", methods=["GET", "DELETE"])
@jwt_required()
def mcp_stream_unsupported():
    # Nothing to push outside a request and no session to tear down; the spec
    # allows 405 for both.
    return Response(status=405, headers={"Allow": "POST"})


# HTTP+SSE. Both halves of a session must be served by the same process, so this
# registry is deliberately per-worker.


@dataclass
class _SseSession:
    user_id: int
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    outbox: Queue = field(default_factory=Queue)


_sse_sessions: dict[str, _SseSession] = {}


@mcp.route("/sse", methods=["GET"])
@jwt_required()
def mcp_sse():
    session = _SseSession(user_id=current_user.id)
    _sse_sessions[session.id] = session

    # Relative, so it survives a reverse proxy that request.url_root would not.
    endpoint = f"{url_for('mcp.mcp_messages')}?session_id={session.id}"

    def generate():
        try:
            yield f"event: endpoint\ndata: {endpoint}\n\n"
            while True:
                try:
                    message = session.outbox.get(timeout=SSE_KEEPALIVE_SECONDS)
                except Empty:
                    yield ": keep-alive\n\n"
                    continue
                yield "event: message\ndata: {}\n\n".format(
                    json.dumps(message, ensure_ascii=False, default=str)
                )
        finally:
            _sse_sessions.pop(session.id, None)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@mcp.route("/messages", methods=["POST"])
@mcp.route("/messages/<session_id>", methods=["POST"])
@jwt_required()
def mcp_messages(session_id: str | None = None):
    session_id = session_id or request.args.get("session_id")
    session = _sse_sessions.get(session_id) if session_id else None

    if session is None:
        return jsonify({"error": "Unknown or expired session"}), 404
    if session.user_id != current_user.id:
        return jsonify({"error": "Session belongs to a different user"}), 403

    response = _dispatch(request.get_json(silent=True))
    if response is not None:
        session.outbox.put(response)

    # The reply travels over the SSE stream, not this response.
    return "", 202
