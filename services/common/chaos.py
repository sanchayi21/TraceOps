"""Controlled fault injection for the local demo services.

Each service keeps an in-memory `ChaosState` describing which fault (if any)
is active. The service code checks this state at the point where the real
failure would happen, so the telemetry is genuine: a "slow database" fault
runs a real `pg_sleep` query, a "database down" fault really connects to a
port with nothing listening.

Endpoints (only registered when CHAOS_ENABLED=true):
    GET    /chaos   -> current fault
    PUT    /chaos   -> set a fault, e.g. {"mode": "slow_db", "latency_ms": 1500}
    DELETE /chaos   -> clear the fault
"""

import os
import random
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field

FaultMode = Literal["none", "slow_db", "db_down", "error"]


class ChaosState(BaseModel):
    mode: FaultMode = "none"
    latency_ms: int = Field(default=0, ge=0, le=30_000)
    error_rate: float = Field(default=1.0, ge=0.0, le=1.0)

    def should_fail(self) -> bool:
        return self.mode == "error" and random.random() < self.error_rate


def chaos_enabled() -> bool:
    return os.getenv("CHAOS_ENABLED", "false").lower() == "true"


def register_chaos(app: FastAPI, logger) -> ChaosState:
    state = ChaosState()
    if not chaos_enabled():
        return state

    @app.get("/chaos", tags=["chaos"])
    def get_chaos() -> ChaosState:
        return state

    @app.put("/chaos", tags=["chaos"])
    def set_chaos(new_state: ChaosState) -> ChaosState:
        for field in ChaosState.model_fields:
            setattr(state, field, getattr(new_state, field))
        logger.warning(f"Chaos fault activated: {state.model_dump()}")
        return state

    @app.delete("/chaos", tags=["chaos"])
    def clear_chaos() -> ChaosState:
        state.mode, state.latency_ms, state.error_rate = "none", 0, 1.0
        logger.warning("Chaos fault cleared")
        return state

    return state
