"""MockShop — synthetic e-commerce target used by TaskPilot's demo and eval.

Run standalone:  ``python demo-apps/mockshop/app.py``  →  http://localhost:9000
In compose:      ``http://mockshop:9000``

The FastAPI application object lives in :mod:`app` next to this file. Because the
directory name contains a hyphen it is not importable as a package, so callers
load it explicitly::

    import importlib.util
    spec = importlib.util.spec_from_file_location("mockshop_app", "demo-apps/mockshop/app.py")

The test-suite helper :func:`conftest.mockshop_server` does exactly that.
"""

__all__: list[str] = []
