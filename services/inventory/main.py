from fastapi import FastAPI, HTTPException

app = FastAPI(title="Inventory Service")

products = {
    101: {"name": "Keyboard", "quantity": 10},
    102: {"name": "Mouse", "quantity": 5},
}


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "inventory"}


@app.get("/inventory/{product_id}")
def get_inventory(product_id: int):
    product = products.get(product_id)

    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    return {"product_id": product_id, **product}