# SPDX-License-Identifier: Apache-2.0
"""What a task registry surface reaches of the store, read from its source.

The split between the registry's two writers is in what a surface calls
(``task-registry/DESIGN.md``, "Two writers, two modules"), so the tests holding each
surface to its side read the module itself: every name it binds to a store module or
to a name in one, under any alias, and every attribute chain it walks from that name,
to the chain's end. Reading only ``module.name`` pairs let ``import tasks as t`` and
``reads.tasks.update`` through.
"""
from __future__ import annotations

import ast

#: The task registry's store modules.
STORE = frozenset({"taskdb", "tasks", "claims", "notes", "reads", "agent_moves",
                   "operator_moves"})


def reached(source: str, roots: frozenset[str] = STORE,
            ) -> tuple[set[str], set[tuple[str, ...]]]:
    """The modules ``source`` imports, and each chain it walks from a module in
    ``roots``, as the names it passes through: ``reads.tasks.update`` is ``("reads",
    "tasks", "update")``. An alias reads as what it binds: ``import tasks as t`` then
    ``t.update`` is ``("tasks", "update")``, and ``from tasks import update`` used bare
    is the same chain."""
    tree = ast.parse(source)
    imported: set[str] = set()
    bound: dict[str, tuple[str, ...]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
                bound[alias.asname or alias.name] = (alias.name,)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            for alias in node.names:
                bound[alias.asname or alias.name] = (node.module, alias.name)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    chains = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and bound.get(node.id, ("",))[0] in roots):
            continue
        chain, at = list(bound[node.id]), node
        while isinstance(parents.get(at), ast.Attribute) and parents[at].value is at:
            at = parents[at]
            chain.append(at.attr)
        chains.add(tuple(chain))
    return imported, chains


def overreach(chains: set[tuple[str, ...]], callable_: frozenset[str],
              allowed: frozenset[tuple[str, str]] = frozenset()) -> list[tuple[str, ...]]:
    """The chains a surface that may call the modules in ``callable_`` must not walk:
    through one of them into another store module (``reads.tasks``), or into any other
    store module for anything but a constant or a name in ``allowed``."""
    wrong = []
    for chain in sorted(chains):
        module, rest = chain[0], chain[1:]
        if module in callable_:
            if rest and rest[0] in STORE:
                wrong.append(chain)
        elif not rest or not (rest[0].isupper() or (module, rest[0]) in allowed):
            wrong.append(chain)
    return wrong
