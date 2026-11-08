"""Demo controls: check service health, inject faults, generate traffic.

These endpoints just call the demo services' own /health and /chaos
endpoints and send ordinary requests to the gateway, so the dashboard can
drive a whole demonstration. They are for the local demo only.
"""

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from traceops.config import settings

router = APIRouter(prefix="/api/demo", tags=["demo"])

PRODUCT_IDS = [101, 102, 103]


class FaultRequest(BaseModel):
    mode: str = Field(pattern="^(slow_db|db_down|error)$")
    latency_ms: int = Field(default=1500, ge=0, le=30_000)
    error_rate: float = Field(default=1.0, ge=0.0, le=1.0)


class TrafficRequest(BaseModel):
    duration_seconds: int = Field(default=90, ge=5, le=600)
    requests_per_second: float = Field(default=4, gt=0, le=20)


class _TrafficGenerator:
    def __init__(self):
        self.lock = threading.Lock()
        self.running_until = 0.0
        self.sent = 0
        self.status_counts: dict[str, int] = {}

    @property
    def active(self) -> bool:
        return time.time() < self.running_until

    def start(self, duration: int, rps: float):
        with self.lock:
            already_running = self.active
            self.running_until = time.time() + duration
            if not already_running:
                self.sent, self.status_counts = 0, {}
                threading.Thread(target=self._run, args=(rps,), daemon=True).start()

    def stop(self):
        self.running_until = 0.0

    def _one_request(self):
        product = random.choice(PRODUCT_IDS)
        try:
            response = httpx.get(
                f"{settings.services['gateway']}/orders/check/{product}", timeout=10
            )
            key = str(response.status_code)
        except httpx.HTTPError as error:
            key = type(error).__name__
        with self.lock:
            self.sent += 1
            self.status_counts[key] = self.status_counts.get(key, 0) + 1

    def _run(self, rps: float):
        with ThreadPoolExecutor(max_workers=16) as pool:
            while self.active:
                pool.submit(self._one_request)
                time.sleep(1 / rps)


traffic = _TrafficGenerator()


def _service_status(name: str, url: str) -> dict:
    status = {"name": name, "url": url, "healthy": False, "fault": None}
    try:
        status["healthy"] = httpx.get(f"{url}/health", timeout=2).status_code == 200
        chaos = httpx.get(f"{url}/chaos", timeout=2)
        if chaos.status_code == 200:
            status["fault"] = chaos.json()
    except httpx.HTTPError:
        pass
    return status


@router.get("/status")
def demo_status():
    return {
        "services": [_service_status(n, u) for n, u in settings.services.items()],
        "traffic": {
            "active": traffic.active,
            "seconds_left": max(0, round(traffic.running_until - time.time())),
            "sent": traffic.sent,
            "status_counts": traffic.status_counts,
        },
    }


@router.put("/fault")
def inject_fault(fault: FaultRequest):
    try:
        response = httpx.put(
            f"{settings.services['inventory']}/chaos",
            json=fault.model_dump(), timeout=3,
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise HTTPException(502, f"Could not reach inventory: {error}")
    return response.json()


@router.delete("/fault")
def clear_fault():
    try:
        response = httpx.delete(f"{settings.services['inventory']}/chaos", timeout=3)
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise HTTPException(502, f"Could not reach inventory: {error}")
    return response.json()


@router.post("/traffic")
def start_traffic(request: TrafficRequest):
    traffic.start(request.duration_seconds, request.requests_per_second)
    return {"started": True, **request.model_dump()}


@router.delete("/traffic")
def stop_traffic():
    traffic.stop()
    return {"stopped": True}
