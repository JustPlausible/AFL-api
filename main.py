# Run the FastAPI app
from fastapi import FastAPI
from db.migration_runner import migrate_database
from api.routes import router as api_router
from health import router as health_router

migrate_database()

app = FastAPI()
app.include_router(health_router)
app.include_router(api_router)
