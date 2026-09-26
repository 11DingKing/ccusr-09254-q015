"""服务端业务模块。"""

from __future__ import annotations

from fastapi import FastAPI

from .routers import router
from .routers_interruption import router as interruption_router

app = FastAPI(
    title="Practice Hours Guard",
    version="0.1.0",
    description=(
        "Event-sourced practice-hours compliance service. Check-ins, mentor "
        "confirmations and leave corrections are append-only; compliance is "
        "derived by replay and can be frozen into an immutable snapshot. "
        "Natural-disaster or production-halt interruptions register an impact "
        "list, generate skill-matched compensation plans students confirm to "
        "lock, and settle with credit-source preservation when activities "
        "resume."
    ),
)

app.include_router(router)
app.include_router(interruption_router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
