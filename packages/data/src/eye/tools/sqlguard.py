"""Parse contributor SQL before it can reach the metric engine."""

import json
from collections.abc import Collection, Iterator
from typing import Any

import duckdb


def _nodes(value: Any) -> Iterator[dict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nodes(child)


def check(sql: str, allowed_tables: Collection[str]) -> tuple[str, ...]:
    """Validate one SELECT and return its parsed physical source tables."""
    with duckdb.connect() as parser:
        try:
            statements = parser.extract_statements(sql)
            if (
                len(statements) != 1
                or statements[0].type != duckdb.StatementType.SELECT
            ):
                raise ValueError("SQL must contain exactly one SELECT")
            tree = json.loads(
                parser.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
            )
        except duckdb.Error as error:
            raise ValueError(f"Invalid SQL: {error}") from error
    if tree.get("error"):
        raise ValueError("SQL could not be parsed")
    allowed = {name.lower() for name in allowed_tables} - {"scenario_log"}
    sources: dict[str, None] = {}

    def tables(value: Any, ctes: frozenset[str] = frozenset()) -> None:
        if isinstance(value, list):
            for child in value:
                tables(child, ctes)
            return
        if not isinstance(value, dict):
            return
        node = value
        ctes = ctes | {
            entry["key"].lower() for entry in node.get("cte_map", {}).get("map", [])
        }
        if node.get("type") == "BASE_TABLE":
            parts = [node.get(k) for k in ("catalog_name", "schema_name", "table_name")]
            name = ".".join(p for p in parts if p).lower()
            if (
                node["table_name"].lower() == "scenario_log"
                or name not in allowed | ctes
            ):
                raise ValueError(f"Table is not allowed: {name}")
            if name not in ctes:
                sources[name] = None
        for child in node.values():
            tables(child, ctes)

    tables(tree)
    for node in _nodes(tree):
        if node.get("type") == "TABLE_FUNCTION":
            raise ValueError("Table functions are not allowed")
        if node.get("class") == "FUNCTION":
            function = node.get("function_name", "").lower()
            if function.startswith(("read_", "write_")) or function in {
                "query",
                "query_table",
                "getenv",
                "nextval",
                "setval",
                "set_config",
                "load_extension",
            }:
                raise ValueError(f"Function is not allowed: {function}")
    return tuple(sources)


def dry_run(
    con: duckdb.DuckDBPyConnection, sql: str, allowed_tables: Collection[str]
) -> list[str]:
    """Bind and sample guarded SQL in a read-only transaction with a row cap."""
    check(sql, allowed_tables)
    with con.cursor() as cursor:
        cursor.execute("BEGIN TRANSACTION READ ONLY")
        try:
            cursor.execute(
                f"SELECT * FROM ({sql.rstrip().rstrip(';')}) guarded LIMIT 1000"
            )
            columns = [item[0] for item in cursor.description]
            cursor.fetchall()
            return columns
        finally:
            cursor.execute("ROLLBACK")
