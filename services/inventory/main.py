import psycopg
from fastapi import FastAPI, HTTPException

from services.inventory.database import get_connection

app = FastAPI(title="Inventory Service")


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "inventory"}


@app.get("/inventory/{product_id}")
def get_inventory(product_id: int):
    try:
        with get_connection() as connection:
            product = connection.execute(
                """
                SELECT id AS product_id, name, quantity
                FROM products
                WHERE id = %s
                """,
                (product_id,),
            ).fetchone()
    except psycopg.OperationalError:
        raise HTTPException(
            status_code=503,
            detail="Inventory database unavailable",
        )

    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    return product