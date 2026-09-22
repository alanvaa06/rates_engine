"""Use cases: a configuration in, a JSON-ready payload out (layer 8).

``config`` reads and checks a configuration document, ``builders`` turns its
blocks into domain objects, and ``commands`` holds one function per use case
plus :data:`~rates_engine.app.commands.COMMANDS`, the table every interface
dispatches through. The CLI and the MCP server are adapters over that table
and do not know about each other, so a third interface is one more adapter
rather than an import of the CLI. Depends on :mod:`rates_engine.reporting`
and below.
"""
