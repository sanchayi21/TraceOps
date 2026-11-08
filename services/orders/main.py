import os

import httpx
from fastapi import FastAPI, HTTPException

from services.common.telemetry import setup_telemetry

INVENTORY_URL = os.getenv("INVENTORY_URL", "http://127.0.0.1:8001")
# Longer than Inventory's database timeout (2s), shorter than the Gateway's (6s).
INVENTORY_TIMEOUT_SECONDS = 4.0

app = FastAPI(title="Order Service")
logger = setup_telemetry(app, "orders")


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "orders"}


@app.get("/orders/check/{product_id}")
async def check_product(product_id: int):
    try:
        async with httpx.AsyncClient(timeout=INVENTORY_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{INVENTORY_URL}/inventory/{product_id}")
    except (httpx.ConnectError, httpx.ConnectTimeout) as error:
        # Checked BEFORE TimeoutException: ConnectTimeout is a subclass of it,
        # but "could not connect" is a different problem from "too slow".
        logger.error(
            "Cannot reach Inventory Service",
            extra={"ctx_error_type": type(error).__name__},
        )
        raise HTTPException(status_code=503, detail="Cannot reach Inventory Service")
    except httpx.TimeoutException:
        logger.error("Inventory Service timed out")
        raise HTTPException(status_code=504, detail="Inventory Service timed out")
    except httpx.RequestError as error:
        logger.error(
            "Cannot reach Inventory Service",
            extra={"ctx_error_type": type(error).__name__},
        )
        raise HTTPException(status_code=503, detail="Cannot reach Inventory Service")

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Product not found")

    if response.status_code != 200:
        logger.error(
            "Inventory request failed",
            extra={"ctx_upstream_status": response.status_code},
        )
        raise HTTPException(
            status_code=502,
            detail=f"Inventory request failed ({response.status_code})",
        )

    return {"inventory": response.json()}
