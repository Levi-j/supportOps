from collections.abc import Iterator
from typing import Annotated, cast

from fastapi import Depends, Request

from billing_api import database
from billing_api.config import BillingSettings
from billing_api.database import Connection


def get_settings(request: Request) -> BillingSettings:
    return cast(BillingSettings, request.app.state.settings)


def get_connection(request: Request) -> Iterator[Connection]:
    with database.connect(get_settings(request)) as connection:
        yield connection


Settings = Annotated[BillingSettings, Depends(get_settings)]
DbConnection = Annotated[Connection, Depends(get_connection, scope="function")]
