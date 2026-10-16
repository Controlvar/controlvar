"""Controlvar's optional modules.

Everything under this directory is Apache-2.0 like the rest of the repository.
A core build deletes the directory whole (``scripts/core_build.sh``);
``backend/app/modules_loader.py`` then finds no ``modules`` package and the
API runs the core profile.

The package exposes one thing to the core application: ``register``, the entry
point ``modules_loader.load_modules()`` calls with
:mod:`backend.app.core.hooks` to install the modules' routers, model modules,
audit signer, OpenAPI tags, capabilities and names.  It lives in
``modules/backend/app/register.py``; this re-export is what the loader looks
at first, and the module path is what it looks at second.
"""

from modules.backend.app.register import register

__all__ = ["register"]
