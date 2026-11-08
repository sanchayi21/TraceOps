import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from psycopg.rows import dict_row

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

tracer = trace.get_tracer("inventory.database")

# Kept SHORTER than the Orders -> Inventory HTTP timeout. If the database
# timeout were longer, Orders would give up first and blame Inventory instead
# of the database (this exact bug existed when both were 3 seconds).
CONNECT_TIMEOUT_SECONDS = 2

SEED_PRODUCTS = [(101, "Keyboard", 10), (102, "Mouse", 5), (103, "Monitor", 3)]


def _db_attributes(port: int) -> dict:
    return {
        "db.system": "postgresql",
        "db.name": os.environ["POSTGRES_DB"],
        "server.address": os.getenv("POSTGRES_HOST", "127.0.0.1"),
        "server.port": port,
        "peer.service": "postgresql",
    }


def get_connection(port_override: int | None = None):
    """Open a connection inside a CLIENT span so failures show up in traces."""
    port = port_override or int(os.getenv("POSTGRES_PORT", "5432"))
    with tracer.start_as_current_span(
        "postgresql connect", kind=SpanKind.CLIENT, attributes=_db_attributes(port)
    ):
        return psycopg.connect(
            host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
            port=port,
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            row_factory=dict_row,
        )


def run_query(connection, sql: str, params: tuple, operation: str):
    """Execute one statement inside a CLIENT span named like 'SELECT products'."""
    port = connection.info.port
    attributes = _db_attributes(port) | {
        "db.operation": operation.split()[0],
        "db.statement": " ".join(sql.split()),
    }
    with tracer.start_as_current_span(
        operation, kind=SpanKind.CLIENT, attributes=attributes
    ):
        return connection.execute(sql, params)


def init_schema() -> None:
    """Create and seed the products table. Safe to run on every startup."""
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity >= 0)
            )
            """
        )
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO products (id, name, quantity) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                SEED_PRODUCTS,
            )
