"""Leaf Connect (M6): other ways to reach the same intelligence core.

- `api_v1`: the versioned REST API (OpenAPI at /api/v1/openapi.json), authenticated by a
  per-garden API key with scopes and limits (`auth`).
- `mcp`: a Model Context Protocol server over Streamable HTTP at /mcp (same keys).
- `manage`: the garden's own controls for all of it (keys, sensor readings).
- Home Assistant sends soil moisture to POST /api/v1/readings; it replaces the water balance.

Every surface calls the same functions (`tools`); none has logic of its own.
"""
