from typing import Any, Protocol

from asyncer import asyncify
from databricks.sdk import WorkspaceClient


class DatabaseCredentialError(RuntimeError):
    """Raised when Lakebase cannot provide a usable database credential."""


class DatabaseCredentialProvider(Protocol):
    async def get_credential(self) -> str: ...


class PostgresApiProtocol(Protocol):
    def generate_database_credential(self, *, endpoint: str) -> Any: ...


class WorkspaceClientProtocol(Protocol):
    postgres: PostgresApiProtocol


class LakebaseCredentialProvider:
    def __init__(
        self,
        endpoint: str,
        workspace_client: WorkspaceClientProtocol | None = None,
        profile: str | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._workspace_client = workspace_client or WorkspaceClient(profile=profile)

    def get_credential_sync(self) -> str:
        try:
            credential = self._workspace_client.postgres.generate_database_credential(
                endpoint=self._endpoint
            )
        except Exception as exc:
            raise DatabaseCredentialError(
                "Unable to generate a Lakebase database credential"
            ) from exc

        token = credential.token
        if not token:
            raise DatabaseCredentialError("Lakebase returned an empty database credential")
        return token

    async def get_credential(self) -> str:
        return await asyncify(self.get_credential_sync)()
