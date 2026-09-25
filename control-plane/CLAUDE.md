# control-plane

How to work in this component. Its design is in `control-plane/DESIGN.md`, and what it
shares with the rest of the system is in the root `DESIGN.md`. The root `CLAUDE.md`
still applies, and the module docstring in `app.py` is the map: which module serves
which surface, and the order they import in. Each line below points at where a rule
lives rather than restating it.

## Adding things

- **A module:** a `COPY` line in `control-plane/Dockerfile`
  (`ControlPlaneModulesAreShippedTests` fails without it), the `PYFILES` and
  `REFFILES` lists in the `Makefile`, the layout block in `README.md`, and the module
  list in the `app.py` docstring. The `api_*` modules never import each other.
- **A route:** in the `api_*` module for its surface. The "what each listener serves"
  block in `app.py` mounts it, and the comment above each `FastAPI(...)` call asks the
  question for that listener. `ApiSurfaceSplitTests` holds the two enforcer listeners'
  rosters exactly and pins the grant routes to management. If the UI calls the route,
  add it to `_RELAY_ROUTES` in `control-plane-ui/app.py`:
  `test_every_endpoint_the_page_calls_is_served_or_relayed` and
  `test_every_relayed_route_is_actually_called` check both directions.
- **A column:** a migration step, never the DDL alone — see "Schema note (read before
  adding a column)" in `control-plane/DESIGN.md`, then the NOTE below `_init_db` in `store.py`. A
  comment inside a DDL string is schema text a fresh store records.
- **An audit word** (the first argument to `store._audit`): add it to `audit.KINDS`,
  which the filters and the UI share; `test_every_word_the_control_plane_writes_is_filterable`
  fails otherwise.
- **An error message served to a caller:** only through a typed exception whose message
  holds the caller's own input and module constants (`audit.FilterError`,
  `inventory.InventoryError`); `test_an_exception_reaches_a_response_only_through_a_named_type`.
- **A bound or cap:** an env var, because it fails closed — see "Hold bounds are
  fail-closed, so their values stay env vars" in
  `control-plane/DESIGN.md`.

## Testing

- `tests/_loader.py` loads `app.py` with stub `fastapi` and `pydantic`, and exposes every
  module as `cp.<module>`. Call a handler as `cp.<module>.<name>`, and rebind a tunable
  on the module that READS it.
- The stubs record only the routes this code declares. Anything FastAPI does by itself
  (its docs routes, #93; how an included router resolves) is checked in a venv with
  `control-plane/requirements.txt`, using `fastapi.testclient`.
- One uvicorn worker, by design: the hold registry is in-process (the concurrency
  paragraph of the `app.py` docstring), so nothing here may assume a second process.
