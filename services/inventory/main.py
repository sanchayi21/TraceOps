from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, HTTPException

from services.common.chaos import register_chaos
from services.common.telemetry import setup_telemetry
from services.inventory import database

# A port where nothing listens. The "db_down" fault connects here so the
# failure is a real connection error, not a faked exception.
UNREACHABLE_DB_PORT = 5999


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        database.init_schema()
        logger.info("Inventory schema ready")
    except psycopg.OperationalError:
        logger.error("Database unavailable at startup; schema not initialised")
    yield


app = FastAPI(title="Inventory Service", lifespan=lifespan)
logger = setup_telemetry(app, "inventory")
chaos = register_chaos(app, logger)


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "inventory"}


@app.get("/inventory/{product_id}")
def get_inventory(product_id: int):
    if chaos.should_fail():
        logger.error("Injected fault: inventory internal error")
        raise HTTPException(status_code=500, detail="Injected inventory error")

    port = UNREACHABLE_DB_PORT if chaos.mode == "db_down" else None
    try:
        with database.get_connection(port_override=port) as connection:
            if chaos.mode == "slow_db":
                database.run_query(
                    connection,
                    "SELECT pg_sleep(%s)",
                    (chaos.latency_ms / 1000,),
                    "SELECT pg_sleep",
                )
            product = database.run_query(
                connection,
                """
                SELECT id AS product_id, name, quantity
                FROM products
                WHERE id = %s
                """,
                (product_id,),
                "SELECT products",
            ).fetchone()
    except psycopg.OperationalError as error:
        logger.error(
            "Inventory database unavailable",
            extra={"ctx_error_type": type(error).__name__},
        )
        raise HTTPException(
            status_code=503,
            detail="Inventory database unavailable",
        )

    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    return product
