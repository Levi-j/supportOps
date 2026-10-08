from collections.abc import Callable

from fastapi import FastAPI

from billing_api.database import DatabaseStatus

FAKE_DB_PASSWORD = "UnitTestDbPw"

AppFactory = Callable[..., FastAPI]

HEALTHY = DatabaseStatus(up=True, latency_ms=2)
