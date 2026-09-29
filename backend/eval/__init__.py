"""TaskPilot evaluation harness.

Run it with ``python -m eval.run``.

This package deliberately has **no eager imports**. An empty ``__init__`` keeps
the parent package fully initialised before ``run.py`` reaches for
``eval.tasks``; importing a submodule from inside the package's own
``__init__`` can resolve against the builtin ``eval`` *function* instead.
"""

__all__: list[str] = []
