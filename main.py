# Run the FastAPI app
from contextlib import asynccontextmanager

from fastapi import FastAPI

from analytics import record as analytics_record
from db.migration_runner import migrate_database
from analytics.middleware import analytics_http_middleware
from api.routes import router as api_router
from api.routes_v1 import router as api_v1_router
from health import router as health_router
from utils.log import log
from version import __version__

migrate_database()

# Matches scheduler/start.py's ANALYTICS_DRAIN_TIMEOUT_SECONDS -- best-effort
# only (Issue #205): a timed-out drain is logged, never raised, since
# analytics is observational and must never block a clean API shutdown.
ANALYTICS_DRAIN_TIMEOUT_SECONDS = 5.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    if not analytics_record.wait_until_idle(timeout=ANALYTICS_DRAIN_TIMEOUT_SECONDS):
        log("Analytics write queue did not drain before shutdown; some observations may be lost.", "WARN")


app = FastAPI(title="AFL-api", version=__version__, lifespan=lifespan)
app.middleware("http")(analytics_http_middleware)
app.include_router(health_router)
app.include_router(api_router)
app.include_router(api_v1_router)
