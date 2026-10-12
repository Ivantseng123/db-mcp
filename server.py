"""Standalone read-only SQL MCP server.

One pair of tools per connection in the config file:
  run_select_<name>(sql)              -> XML (ROWSET/ROW), first 4000 chars
  run_select_more_<name>(sql, offset) -> same XML, 4000 chars starting at `offset` (1-based)
plus list_connections().

Config: $DB_MCP_CONFIG or ~/.config/db-mcp/connections.yaml (see connections.example.yaml).
Only a single SELECT / WITH statement is accepted; anything else is refused before it reaches the DB.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import os
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

import yaml
from mcp.server.fastmcp import FastMCP

CHUNK = 4000
DEFAULT_MAX_ROWS = 200
CONFIG_PATH = Path(os.environ.get("DB_MCP_CONFIG", "~/.config/db-mcp/connections.yaml")).expanduser()

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|truncate|create|grant|revoke|exec|execute|call|begin|declare|commit|rollback)\b",
    re.I,
)


def load_config() -> dict[str, dict]:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"db-mcp: config not found: {CONFIG_PATH} (copy connections.example.yaml there)")
    raw = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    conns = raw.get("connections") or {}
    for name, c in conns.items():
        if not re.fullmatch(r"[a-z0-9_]+", name):
            raise SystemExit(f"db-mcp: connection name '{name}' must match [a-z0-9_]+")
        if c.get("type") not in ("oracle", "mssql", "postgres"):
            raise SystemExit(f"db-mcp: connection '{name}': type must be oracle, mssql or postgres")
        if "password_env" in c and "password" not in c:
            c["password"] = os.environ.get(c["password_env"], "")
    return conns


def check_select(sql: str) -> str:
    """Return the cleaned statement or raise ValueError."""
    s = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    s = re.sub(r"--[^\n]*", " ", s)
    s = s.strip().rstrip(";").strip()
    if not s:
        raise ValueError("empty SQL")
    if ";" in s:
        raise ValueError("only one statement is allowed")
    if not re.match(r"(select|with)\b", s, re.I):
        raise ValueError("only SELECT / WITH statements are allowed")
    m = _FORBIDDEN.search(s)
    if m:
        raise ValueError(f"forbidden keyword in read-only tool: {m.group(0)}")
    return s


def _cell(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat(sep=" ") if isinstance(v, _dt.datetime) else v.isoformat()
    if isinstance(v, decimal.Decimal):
        return format(v, "f")
    if hasattr(v, "read"):  # LOB
        v = v.read()
        return v.hex() if isinstance(v, bytes) else str(v)
    return str(v)


def to_xml(columns: list[str], rows: list[tuple]) -> str:
    """Mimic Oracle DBMS_XMLGEN.GETXML output so existing habits keep working."""
    out = ['<?xml version="1.0"?>', "<ROWSET>"]
    for r in rows:
        out.append(" <ROW>")
        for c, v in zip(columns, r):
            s = _cell(v)
            if s is not None:
                out.append(f"  <{c}>{escape(s)}</{c}>")
        out.append(" </ROW>")
    out.append("</ROWSET>")
    return "\n".join(out) + "\n"


def run_query(conn_cfg: dict, sql: str, max_rows: int) -> tuple[list[str], list[tuple], bool]:
    t = conn_cfg["type"]
    if t == "oracle":
        import oracledb

        kw = dict(user=conn_cfg["user"], password=conn_cfg["password"])
        if conn_cfg.get("dsn"):
            kw["dsn"] = conn_cfg["dsn"]
        elif conn_cfg.get("sid"):
            kw["dsn"] = oracledb.makedsn(conn_cfg["host"], int(conn_cfg.get("port", 1521)), sid=conn_cfg["sid"])
        else:
            kw["dsn"] = f"{conn_cfg['host']}:{conn_cfg.get('port', 1521)}/{conn_cfg['service_name']}"
        conn = oracledb.connect(**kw)
    elif t == "postgres":
        import psycopg

        conn = psycopg.connect(
            host=conn_cfg["host"],
            port=int(conn_cfg.get("port", 5432)),
            dbname=conn_cfg["database"],
            user=conn_cfg["user"],
            password=conn_cfg["password"],
            connect_timeout=int(conn_cfg.get("timeout", 30)),
        )
    else:
        import pytds

        conn = pytds.connect(
            server=conn_cfg["host"],
            port=int(conn_cfg.get("port", 1433)),
            database=conn_cfg.get("database"),
            user=conn_cfg["user"],
            password=conn_cfg["password"],
            login_timeout=int(conn_cfg.get("timeout", 30)),
            as_dict=False,
        )
    try:
        cur = conn.cursor()
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        return cols, rows[:max_rows], truncated
    finally:
        conn.close()


def build_server() -> FastMCP:
    conns = load_config()
    mcp = FastMCP("db-mcp")

    @mcp.tool()
    def list_connections() -> str:
        """List configured database connections and their types."""
        return "\n".join(f"{n}: {c['type']} {c.get('description', '')}".rstrip() for n, c in conns.items()) or "(none)"

    for name, cfg in conns.items():
        _register(mcp, name, cfg)
    return mcp


def _register(mcp: FastMCP, name: str, cfg: dict) -> None:
    max_rows = int(cfg.get("max_rows", DEFAULT_MAX_ROWS))
    dialect_hint = {
        "oracle": "Oracle: limit with WHERE ROWNUM <= 50.",
        "mssql": "SQL Server: limit with SELECT TOP 50.",
        "postgres": "PostgreSQL: limit with LIMIT 50; quote mixed-case identifiers.",
    }[cfg["type"]]
    desc = cfg.get("description", "")

    def _run(sql: str, offset: int) -> str:
        try:
            stmt = check_select(sql)
            cols, rows, truncated = run_query(cfg, stmt, max_rows)
        except Exception as e:  # noqa: BLE001 - surface DB/guard errors to the caller as text
            return f"ERROR: {type(e).__name__}: {e}"
        xml = to_xml(cols, rows)
        start = max(offset, 1) - 1
        chunk = xml[start : start + CHUNK]
        tail = []
        if start + CHUNK < len(xml):
            tail.append(f"[truncated: total {len(xml)} chars, call run_select_more_{name} with offset={start + CHUNK + 1}]")
        if truncated:
            tail.append(f"[rows capped at {max_rows}; add a tighter WHERE or ROWNUM/TOP]")
        return chunk + ("\n" + "\n".join(tail) if tail else "")

    def run_select(sql: str) -> str:
        return _run(sql, 1)

    def run_select_more(sql: str, offset: int) -> str:
        return _run(sql, offset)

    run_select.__name__ = f"run_select_{name}"
    run_select.__doc__ = (
        f"[{name}] {desc}\nRun one read-only SELECT (joins/subqueries OK) on connection '{name}' ({cfg['type']}). "
        f"Returns XML <ROWSET><ROW>..., first {CHUNK} chars. {dialect_hint}"
    )
    run_select_more.__name__ = f"run_select_more_{name}"
    run_select_more.__doc__ = (
        f"[{name}] Same as run_select_{name}, but returns {CHUNK} chars starting at 1-based `offset` "
        f"(4001, 8001, ...) for long results."
    )
    mcp.tool(name=run_select.__name__)(run_select)
    mcp.tool(name=run_select_more.__name__)(run_select_more)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        conns = load_config()
        for n, c in conns.items():
            try:
                cols, rows, _ = run_query(c, "SELECT 1 AS OK FROM DUAL" if c["type"] == "oracle" else "SELECT 1 AS ok", 1)
                print(f"{n}: OK {dict(zip(cols, rows[0]))}")
            except Exception as e:  # noqa: BLE001
                print(f"{n}: FAIL {type(e).__name__}: {e}")
        return
    build_server().run()


if __name__ == "__main__":
    main()
