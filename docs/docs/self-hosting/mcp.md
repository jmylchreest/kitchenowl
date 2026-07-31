# MCP Server

KitchenOwl can expose its household data to a Large Language Model through a
[Model Context Protocol](https://modelcontextprotocol.io) server, letting an assistant read
your shopping lists and recipes, plan meals, and put a recipe's ingredients on a list.

The server is **disabled by default**. Enable it by setting:

- `KITCHENOWL_MCP_ENABLED`: `true`

It is then served from your instance under `/mcp`.

## Authentication

Every request needs a bearer token. Create a long-lived token and pass it as an
`Authorization` header. The app can create one for you under **Settings → Sessions**, or you
can ask the API directly:

```bash
curl -X POST https://kitchenowl.example.com/api/auth/llt \
  -H "Authorization: Bearer <your session token>" \
  -H "Content-Type: application/json" \
  -d '{"device": "Claude", "scope": "write", "household_id": 1}'
```

The response contains a `longlived_token`. It does not expire, so treat it like a password.
Tokens are revoked from the same settings page, or with
`DELETE /api/auth/llt/<id>`.

### Scopes

A token acts as the user who created it and can never do more than that user could. Narrow
it further with `scope`:

| Scope | What it permits |
| --- | --- |
| `read` | Only tools that read. The assistant cannot change anything. |
| `write` | Everything except deleting a recipe, shopping list or expense. Adding items and ticking them off are both allowed. |
| `full` | Everything, including deletions. |

Omitting `scope` leaves the token unrestricted, which is how tokens behaved before scopes
existed.

Tools outside a token's scope are hidden from `tools/list` and refused if called anyway.

### Restricting to one household

Pass `household_id` to confine a token to a single household. The assistant will only see
that household, and any attempt to reach another one is refused — including when it is
reached indirectly through a list or recipe id. The household must be one you belong to.

!!! tip
    For an assistant that does your shopping, `"scope": "write"` with a `household_id` is a
    good default. It can build and tick off a list but cannot delete your recipes, and it
    cannot see your other households.

## Connecting a client

Two transports are available.

**Streamable HTTP** is stateless and works behind any number of workers:

```
https://kitchenowl.example.com/mcp
```

**HTTP+SSE** is the older transport, for clients that require it:

```
https://kitchenowl.example.com/mcp/sse
```

Both need the `Authorization: Bearer <longlived_token>` header.

For [Claude Code](https://docs.claude.com/en/docs/claude-code):

```bash
claude mcp add --transport http kitchenowl \
  https://kitchenowl.example.com/mcp \
  --header "Authorization: Bearer <longlived_token>"
```

!!! warning "SSE and multiple workers"
    The SSE transport holds a session in the worker that opened the stream, so the stream
    and the requests that feed it must reach the same process. If you run more than one
    backend worker, either use sticky sessions or prefer the Streamable HTTP transport,
    which holds no session at all.

## Reverse proxies

The SSE transport is a long-lived streaming response. Proxies that buffer responses will
make it appear to hang. The server sends `X-Accel-Buffering: no` for nginx; for other
proxies you may need to disable response buffering and raise the read timeout on `/mcp`
yourself.

## What the assistant can do

Tools cover households, shopping lists, items, categories, recipes, tags, the meal planner
and expenses. Some worth knowing about:

- `add_recipe_items_to_list` copies a recipe's ingredients onto a shopping list, merging
  quantities with anything already there. Cooking two recipes that both need mince gives
  you one entry with the total. Optional ingredients are left out unless asked for.
- `add_items` adds several things at once, merging quantities, rather than one call each.
- `scrape_recipe` reads a recipe from a URL and returns it parsed without saving it.
- `list_categories` returns the household's categories in shop order, so a list can be
  grouped by aisle.
- `suggest_recipes` ranks the recipes the household actually cooks, drawn from the
  suggestion scores computed nightly and skipping anything already planned. A household
  that has not cooked from its recipes yet has no ranking, and the tool says so rather
  than returning an empty list.

Every tool declares whether it reads, writes or deletes, so a client can ask for
confirmation before anything destructive.

## Seeing what the assistant did

Items added with a long-lived token record that token's name, and the app shows it beside
the person who owns the token — "John (Claude)" rather than just "John". The name is kept
even after the token is revoked, so revoking an assistant does not erase the record of what
it did.
