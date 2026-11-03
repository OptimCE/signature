"""Global fixtures for the signature service.

Infrastructure:
  pytest-docker starts tests/docker-compose.test.yml (Postgres on 5433); in CI
  a service container provides it. scripts/sql/schema.sql (the LOCAL schema)
  and tests/sql/crm_test_schema.sql (the CRM tables this service reads) are
  applied once, in the same database.

The seal: ``pki`` builds a disposable PKI (devpki, Certomancer) and serves its
TSA, OCSP and CRLs on 127.0.0.1; every PDF the suite seals is validated by
pyHanko against it, inside the seal operations themselves.

Sessions: each test runs in a connection-level transaction rolled back at the
end (``join_transaction_mode="create_savepoint"`` turns the services' commits
into savepoint releases). The same session serves as the LOCAL and the CRM
session, as both schemas live in one database. Tests needing real concurrent
transactions use ``committed`` instead and clean up with TRUNCATE.
"""

import os
import socket
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path

os.environ.setdefault("ENV", "test")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from api.deps import get_bus, get_store
from core.database.database import get_crm_session, get_local_session
from main import app
from ports.bus import RecordingBus
from ports.storage import MemoryStore

TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5433/test_db_be"
_REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = _REPO_ROOT / "scripts" / "sql" / "schema.sql"
CRM_TEST_SCHEMA_SQL = Path(__file__).resolve().parent / "sql" / "crm_test_schema.sql"


@pytest.fixture(scope="session")
def docker_compose_file(pytestconfig):
    return os.path.join(str(pytestconfig.rootdir), "tests", "docker-compose.test.yml")


@pytest.fixture(scope="session")
def docker_compose_project_name():
    return "signature-test"


def _is_pg_ready(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def test_db_ready(request):
    if os.getenv("CI"):
        import time

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _is_pg_ready("localhost", 5433):
                return 5433
            time.sleep(0.5)
        raise RuntimeError("Postgres not ready on port 5433 after 30 s")
    docker_services = request.getfixturevalue("docker_services")
    docker_ip = request.getfixturevalue("docker_ip")
    port = docker_services.port_for("db-test", 5432)
    docker_services.wait_until_responsive(
        timeout=30.0, pause=0.5, check=lambda: _is_pg_ready(docker_ip, port)
    )
    return port


@pytest.fixture(scope="session", autouse=True)
def apply_schema(test_db_ready):
    import asyncio

    import asyncpg

    async def _connect():
        # The port answers before Postgres accepts connections (Docker Desktop
        # forwards it as soon as the container starts): retry for a while.
        for _ in range(60):
            try:
                return await asyncpg.connect(
                    "postgresql://postgres:postgres@localhost:5433/test_db_be"
                )
            except (OSError, asyncpg.PostgresError):
                await asyncio.sleep(0.5)
        return await asyncpg.connect("postgresql://postgres:postgres@localhost:5433/test_db_be")

    async def _apply():
        conn = await _connect()
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        await conn.execute(CRM_TEST_SCHEMA_SQL.read_text(encoding="utf-8"))
        await conn.close()

    asyncio.run(_apply())
    return


@pytest.fixture(scope="session")
def test_engine(apply_schema):
    import asyncio

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    yield engine
    asyncio.run(engine.dispose())


@pytest_asyncio.fixture
async def db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    async with test_engine.connect() as conn:
        await conn.begin()
        factory = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        session = factory()
        yield session
        await session.close()
        await conn.rollback()


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def bus() -> RecordingBus:
    return RecordingBus()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession, store: MemoryStore, bus: RecordingBus):
    async def _session():
        yield db_session

    async def _bus():
        yield bus

    app.dependency_overrides[get_crm_session] = _session
    app.dependency_overrides[get_local_session] = _session
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_bus] = _bus
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# The test PKI
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def pki():
    import devpki

    port = _free_port()
    directory = Path(tempfile.mkdtemp(prefix="signature-pki-"))
    built = devpki.build(directory, f"http://127.0.0.1:{port}")
    server = devpki.serve_in_thread(built, "127.0.0.1", port)
    yield built
    server.shutdown()


@pytest.fixture(scope="session")
def material(pki):
    import devpki
    from crypto.pki import load_seal_material

    return load_seal_material(
        pkcs12_path=str(pki.seal_p12),
        passphrase=devpki.SEAL_PASSPHRASE,
        ca_chain_paths="",
        trust_root_paths=str(pki.root_cert),
        tsa_urls=pki.tsa_url,
        tsa_timeout=5,
    )


@pytest.fixture(scope="session")
def annex8_pdf() -> bytes:
    """CWaPE annexe 8 as document-generation renders it (fake participant data)."""
    return (Path(__file__).resolve().parent / "fixtures" / "annex8-rendered.pdf").read_bytes()
