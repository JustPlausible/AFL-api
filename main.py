# Run the FastAPI app
from fastapi import FastAPI
from db.migration_runner import migrate_database
from analytics.middleware import analytics_http_middleware
from api.routes import router as api_router
from api.routes_v1 import router as api_v1_router
from health import router as health_router
from version import __version__

migrate_database()

app = FastAPI(title="AFL-api", version=__version__)
app.middleware("http")(analytics_http_middleware)
app.include_router(health_router)
app.include_router(api_router)
app.include_router(api_v1_router)
