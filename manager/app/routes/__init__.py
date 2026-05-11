"""Manager API v1 route collection."""
from __future__ import annotations

from fastapi.routing import APIRouter

from manager.app.routes.agents import router as agents_router
from manager.app.routes.agent_ws import router as agent_ws_router
from manager.app.routes.analytics import router as analytics_router
from manager.app.routes.auth import router as auth_router
from manager.app.routes.bots import router as bots_router
from manager.app.routes.debug import router as debug_router
from manager.app.routes.market import router as market_router
from manager.app.routes.settings import router as settings_router
from manager.app.routes.stream import router as stream_router
from manager.app.routes.ui_ws import router as ui_ws_router
from manager.app.routes.users import router as users_router

v1 = APIRouter(prefix="/api/v1")
v1.include_router(auth_router)
v1.include_router(users_router)
v1.include_router(agents_router)
v1.include_router(agent_ws_router)
v1.include_router(bots_router)
v1.include_router(debug_router)
v1.include_router(market_router)
v1.include_router(settings_router)
v1.include_router(analytics_router)
v1.include_router(stream_router)
v1.include_router(ui_ws_router)

__all__ = ["v1"]
