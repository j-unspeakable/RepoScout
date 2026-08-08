from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.database.credentials import DatabaseCredentialProvider


class ConnectionProvider(Protocol):
    def connection(self) -> AbstractAsyncContextManager[AsyncConnection[Any]]: ...


class LakebasePool:
    def __init__(
        self,
        settings: Settings,
        credential_provider: DatabaseCredentialProvider,
    ) -> None:
        self._settings = settings
        self._credential_provider = credential_provider
        self._pool: AsyncConnectionPool[AsyncConnection[Any]] = AsyncConnectionPool(
            conninfo="",
            kwargs=self._connection_parameters,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            max_lifetime=settings.db_pool_max_lifetime_seconds,
            timeout=settings.db_pool_timeout_seconds,
            open=False,
            name="reposcout-lakebase",
        )

    @property
    def pool(self) -> AsyncConnectionPool[AsyncConnection[Any]]:
        return self._pool

    async def _connection_parameters(self) -> dict[str, Any]:
        password = await self._credential_provider.get_credential()
        return {
            "host": self._settings.pghost,
            "port": self._settings.pgport,
            "dbname": self._settings.pgdatabase,
            "user": self._settings.pguser,
            "sslmode": self._settings.pgsslmode,
            "password": password,
        }

    async def open(self) -> None:
        await self._pool.open(wait=True)

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection[Any]]:
        async with self._pool.connection() as connection:
            yield connection
