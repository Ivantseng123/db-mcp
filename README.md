# db-mcp

Standalone read-only SQL MCP server. Replaces the IntelliJ coding-toolkit "database MCP" so IDEA does not need to run, and several databases are reachable at once.

- One connection = two tools: `run_select_<name>(sql)` and `run_select_more_<name>(sql, offset)`, plus `list_connections()`.
- Output mimics Oracle `DBMS_XMLGEN.GETXML` (`<ROWSET><ROW>…`), 4000 chars per call, `offset` is 1-based (4001, 8001, …).
- Only one `SELECT` / `WITH` statement is accepted; DML/DDL/EXEC keywords are refused before hitting the DB.
- Oracle via `oracledb` thin mode (no Oracle client needed). SQL Server via pure-Python `python-tds`. PostgreSQL via `psycopg`.

## Setup

```sh
cd ~/local_file/db-mcp
uv venv --python 3.13 .venv && uv pip install -p .venv/bin/python -e .
cp connections.example.yaml ~/.config/db-mcp/connections.yaml && chmod 600 ~/.config/db-mcp/connections.yaml
# fill in hosts / users / passwords, then:
.venv/bin/db-mcp --check        # tries SELECT 1 on every connection
```

Register with Claude Code (user scope):

```sh
claude mcp add -s user database-mcp -- ~/local_file/db-mcp/.venv/bin/db-mcp
```

Register with Codex CLI (global, written to `~/.codex/config.toml`):

```sh
codex mcp add database-mcp -- ~/local_file/db-mcp/.venv/bin/db-mcp
```

Both point at the same server and the same `connections.yaml`, so every connection is available in both tools at once.

## Drivers

No native client or ODBC setup is needed. Each `type` just picks a pip-installed Python driver inside the venv: `oracle` → `oracledb` (thin mode), `mssql` → `python-tds` (pure Python), `postgres` → `psycopg[binary]` (bundles libpq).

Config path override: `DB_MCP_CONFIG=/path/to/connections.yaml`. Passwords may come from env via `password_env`.
After editing the config, `/mcp` in Claude Code to reconnect (tools are built at startup).
