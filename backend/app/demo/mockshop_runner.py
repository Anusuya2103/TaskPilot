"""Dependency-free loader for MockShop.

``demo-apps/mockshop`` cannot be imported as a package (hyphen), so tests and the
eval harness load ``app.py`` by path. Keeping this in one place means there is
exactly one way MockShop is imported in the whole repo.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[2]   # .../TaskPilot/backend
REPO_ROOT = BACKEND_ROOT.parent                        # .../TaskPilot
MOCKSHOP_PATH = REPO_ROOT / "demo-apps" / "mockshop" / "app.py"


def load_mockshop_module() -> ModuleType:
    """Import ``demo-apps/mockshop/app.py`` as ``mockshop_app``."""
    if "mockshop_app" in sys.modules:
        return sys.modules["mockshop_app"]
    spec = importlib.util.spec_from_file_location("mockshop_app", MOCKSHOP_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"cannot load MockShop from {MOCKSHOP_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["mockshop_app"] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class RunningMockShop:
    """A uvicorn server running MockShop on a real socket, in a thread."""

    base_url: str
    port: int
    server: Any
    thread: threading.Thread
    module: ModuleType

    def reset(self) -> None:
        self.module.store.reset()

    def state(self) -> dict[str, Any]:
        store = self.module.store
        return {
            "orders": len(store.orders),
            "lab_reports": len(store.lab_reports),
            "cart_items": sum(i["qty"] for i in store.cart),
            "cart_total": store.cart_total,
        }

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def start_mockshop(port: int = 0, host: str = "127.0.0.1") -> RunningMockShop:
    """Start MockShop on ``port`` (0 = pick a free one) and wait until ready."""
    import uvicorn

    module = load_mockshop_module()
    app = module.app

    config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="mockshop", daemon=True)
    thread.start()

    deadline = time.time() + 25
    while time.time() < deadline:
        if server.started:
            break
        time.sleep(0.05)
    else:  # pragma: no cover
        raise RuntimeError("MockShop did not start within 25s")

    bound_port = server.servers[0].sockets[0].getsockname()[1]
    return RunningMockShop(
        base_url=f"http://{host}:{bound_port}",
        port=bound_port,
        server=server,
        thread=thread,
        module=module,
    )


__all__ = ["MOCKSHOP_PATH", "REPO_ROOT", "RunningMockShop", "load_mockshop_module", "start_mockshop"]
