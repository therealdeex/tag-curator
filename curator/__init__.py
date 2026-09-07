"""Stash Tag Curator - curator package.

Hybrid raw+UI StashApp plugin (Stash v0.31.1). The raw task entrypoint lives
in :mod:`curator.main`; the browser route ships under ``ui/``. Persistent
state is kept outside the package, under
``<server_connection.Dir>/stash-tag-curator-data/``.
"""

__version__ = "0.1.0"
