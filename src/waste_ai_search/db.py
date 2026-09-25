from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Sequence
from urllib.parse import quote


@dataclass
class DatabaseConfig:
    host: str = ""
    port: int = 5432
    dbname: str = "postgres"
    user: str = ""
    password: str = ""
    sslmode: str = "require"
    connect_timeout: int = 30

    @classmethod
    def from_env(cls, prefix: str = "WASTEMAP_DB") -> "DatabaseConfig":
        try:
            from dotenv import load_dotenv  # type: ignore

            load_dotenv()
        except ImportError:
            pass

        def get(key: str, default: str = "") -> str:
            return os.environ.get(f"{prefix}_{key}", default).strip()

        # Passwords are used verbatim: leading/trailing whitespace can be significant.
        password = os.environ.get(f"{prefix}_PASSWORD", "")

        return cls(
            host=get("HOST"),
            port=int(get("PORT", "5432") or "5432"),
            dbname=get("NAME", "postgres") or "postgres",
            user=get("USER"),
            password=password,
            sslmode=get("SSLMODE", "require") or "require",
            connect_timeout=int(get("CONNECT_TIMEOUT", "30") or "30"),
        )

    def validate(self) -> None:
        missing = [name for name in ("host", "user", "password") if not getattr(self, name)]
        if missing:
            keys = ", ".join(f"WASTEMAP_DB_{name.upper()}" for name in missing)
            raise ValueError(f"Missing database settings: {keys}. Copy .env.example to .env and fill them in.")

    def conninfo(self) -> dict[str, Any]:
        """Keyword connection parameters. Preferred over a URL: no escaping of special characters."""
        self.validate()
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.user,
            "password": self.password,
            "sslmode": self.sslmode,
            "connect_timeout": self.connect_timeout,
        }

    def url(self, hide_password: bool = False) -> str:
        """SQLAlchemy/psql-style URL. Only needed by tools that insist on a DSN string."""
        self.validate()
        password = "***" if hide_password else quote(self.password, safe="")
        return (
            f"postgresql://{quote(self.user, safe='')}:{password}"
            f"@{self.host}:{self.port}/{self.dbname}?sslmode={self.sslmode}"
        )

    def describe(self) -> str:
        return f"{self.user}@{self.host}:{self.port}/{self.dbname} (sslmode={self.sslmode})"


@contextmanager
def connect(config: DatabaseConfig | None = None, autocommit: bool = False) -> Iterator[Any]:
    """Open a single connection, committing on clean exit and rolling back on error."""
    config = config or DatabaseConfig.from_env()
    try:
        import psycopg  # type: ignore
    except ImportError as exc:
        raise ImportError("Database access requires psycopg. Run `uv sync` first.") from exc

    conn = psycopg.connect(**config.conninfo(), autocommit=autocommit)
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def fetch_all(
    sql: str,
    params: Sequence[Any] | dict[str, Any] | None = None,
    config: DatabaseConfig | None = None,
) -> list[dict[str, Any]]:
    """Run a read query and return rows as dicts."""
    from psycopg.rows import dict_row  # type: ignore

    with connect(config, autocommit=True) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())


def fetch_one(
    sql: str,
    params: Sequence[Any] | dict[str, Any] | None = None,
    config: DatabaseConfig | None = None,
) -> dict[str, Any] | None:
    rows = fetch_all(sql, params, config)
    return rows[0] if rows else None


def execute(
    sql: str,
    params: Sequence[Any] | dict[str, Any] | None = None,
    config: DatabaseConfig | None = None,
) -> int:
    """Run a write statement and return the affected row count."""
    with connect(config) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount


def check_connection(config: DatabaseConfig | None = None) -> dict[str, Any]:
    """Round-trip the server to confirm credentials, TLS, and database access."""
    config = config or DatabaseConfig.from_env()
    row = fetch_one(
        "SELECT current_database() AS database, current_user AS user, version() AS version",
        config=config,
    )
    return row or {}


def list_tables(schema: str = "public", config: DatabaseConfig | None = None) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema = %s AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """,
        (schema,),
        config=config,
    )
