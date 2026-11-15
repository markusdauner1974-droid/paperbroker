"""HTTP routes, split by family out of ``server.create_app``.

Convention for every module in this package:

- one module per route family (health, chain, earnings, gap, page);
- the module exposes ``register(app)`` and registers its rules through
  ``app.add_url_rule`` with an **explicit** ``endpoint``;
- the endpoint names are the ones the monolith used before the split, so
  ``url_for``, bookmarks and tests keep working unchanged;
- no behaviour moves with the route: a move is a move.

``add_url_rule`` instead of ``Blueprint`` is deliberate. A blueprint prefixes
every endpoint with its name (``health.healthz``), which would rename all ten
existing endpoints; Flask allows an empty blueprint name for exactly one
blueprint per app, so six families cannot keep their names that way.
"""
