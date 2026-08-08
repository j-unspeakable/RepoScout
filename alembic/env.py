from logging.config import fileConfig

from sqlalchemy import create_engine, pool
from sqlalchemy.engine import URL

from alembic import context
from app.config import AppEnvironment, get_settings
from app.database.credentials import LakebaseCredentialProvider

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url="postgresql+psycopg://",
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    settings = get_settings()
    if settings.app_env is AppEnvironment.TEST:
        raise RuntimeError("Online Alembic migrations are disabled for APP_ENV=test")
    if not settings.lakebase_endpoint:
        raise RuntimeError("LAKEBASE_ENDPOINT is required for online migrations")

    credential_provider = LakebaseCredentialProvider(
        settings.lakebase_endpoint,
        profile=settings.databricks_config_profile,
    )
    password = credential_provider.get_credential_sync()
    url = URL.create(
        drivername="postgresql+psycopg",
        username=settings.pguser,
        password=password,
        host=settings.pghost,
        port=settings.pgport,
        database=settings.pgdatabase,
        query={"sslmode": settings.pgsslmode},
    )
    connectable = create_engine(url, poolclass=pool.NullPool)

    try:
        with connectable.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
