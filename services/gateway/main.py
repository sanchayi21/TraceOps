import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI(title="API Gateway")


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "gateway"}


@app.get("/orders/check/{product_id}")
async def check_product(product_id: int):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"http://127.0.0.1:8002/orders/check/{product_id}"
            )
    except httpx.RequestError:
        raise HTTPException(
            status_code=503,
            detail="Cannot reach Order Service",
        )

    return JSONResponse(
        status_code=response.status_code,
        content=response.json(),
    )