import sqlite3
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import Settings
from app.database import Database
from app.models import NotificationAccepted, NotificationCreate, NotificationStatus


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    database = Database(settings)

    @asynccontextmanager
    async def lifespan(app):
        database.initialize()
        yield

    application = FastAPI(lifespan=lifespan)
    application.state.database = database

    @application.exception_handler(sqlite3.Error)
    async def database_error(request: Request, exc: sqlite3.Error):
        return JSONResponse(status_code=503, content={"detail": "Database unavailable"})

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Default validation responses may echo credentials and request bodies.
        # Nested keys and extra-field names are also user-controlled input.
        errors = []
        for error in exc.errors():
            location = error["loc"]
            safe_location = ["body"]
            if len(location) > 1 and location[0] == "body" and location[1] in {
                "url", "method", "headers", "body"
            }:
                safe_location.append(location[1])
            errors.append({"loc": safe_location, "type": error["type"]})
        return JSONResponse(status_code=422, content={"detail": errors})

    @application.post("/notifications", status_code=202, response_model=NotificationAccepted)
    def create_notification(payload: NotificationCreate):
        if urlsplit(payload.url).hostname not in settings.allowed_hosts:
            raise HTTPException(status_code=422, detail="Destination hostname is not allowed")
        return database.create(payload)

    @application.get("/notifications/{task_id}", response_model=NotificationStatus)
    def get_notification(task_id: str):
        task = database.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Notification not found")
        return task

    return application


app = create_app()
