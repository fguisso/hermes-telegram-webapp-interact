"""PluginInteract — Hermes plugin entry point (the implementation lives in ``interact/``)."""

if __package__:  # loaded by Hermes as the plugin package
    from .interact.plugin import register  # noqa: F401
else:  # imported as a bare module (e.g. pytest collecting the repo root)
    from interact.plugin import register  # noqa: F401
