import httpx
from fastapi import FastAPI, HTTPException

app = FastAPI(title="Order Service")


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "orders"}


@app.get("/orders/check/{product_id}")
async def check_product(product_id: int):
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(
                f"http://127.0.0.1:8001/inventory/{product_id}"
            )
    except httpx.RequestError:
        raise HTTPException(
            status_code=503,
            detail="Cannot reach Inventory Service",
        )

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Product not found")

    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="Inventory request failed")

    return {"inventory": response.json()}