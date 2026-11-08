import os

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from services.common.telemetry import setup_telemetry

ORDERS_URL = os.getenv("ORDERS_URL", "http://127.0.0.1:8002")
# The outermost timeout is the longest, so inner services report first.
ORDERS_TIMEOUT_SECONDS = 6.0

app = FastAPI(title="API Gateway")
logger = setup_telemetry(app, "gateway")


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "gateway"}


@app.get("/orders/check/{product_id}")
async def check_product(product_id: int):
    try:
        async with httpx.AsyncClient(timeout=ORDERS_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{ORDERS_URL}/orders/check/{product_id}")
    except (httpx.ConnectError, httpx.ConnectTimeout) as error:
        # Checked BEFORE TimeoutException: ConnectTimeout is a subclass of it.
        logger.error(
            "Cannot reach Order Service",
            extra={"ctx_error_type": type(error).__name__},
        )
        raise HTTPException(status_code=503, detail="Cannot reach Order Service")
    except httpx.TimeoutException:
        logger.error("Order Service timed out")
        raise HTTPException(status_code=504, detail="Order Service timed out")
    except httpx.RequestError as error:
        logger.error(
            "Cannot reach Order Service",
            extra={"ctx_error_type": type(error).__name__},
        )
        raise HTTPException(status_code=503, detail="Cannot reach Order Service")

    if response.status_code >= 500:
        logger.error(
            "Order Service returned an error",
            extra={"ctx_upstream_status": response.status_code},
        )

    return JSONResponse(
        status_code=response.status_code,
        content=response.json(),
    )
