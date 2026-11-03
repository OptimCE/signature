import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from starlette.middleware.cors import CORSMiddleware

from api.admin.routes import admin_routes
from api.health.routes import health_router
from api.signer.routes import signer_routes
from core.config import Environment, settings
from core.errors.errors import ErrorException
from core.errors.handlers import error_exception_handler, unhandled_exception_handler
from core.logging import configure_logging
from core.middleware.correlation_id import CorrelationIdMiddleware
from core.middleware.locale_middleware import LocaleMiddleware
from core.middleware.request_limits import RequestLimitsMiddleware
from core.middleware.set_auth_context import GatewayScopeMiddleware
from core.queue.init import close_nats, init_nats
from core.realtime import log_realtime_state
from core.tracing import enrich_span, setup_tracer_provider

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Absence of this line means the image predates the realtime feature —
    # see core/realtime/bus.py. Must come after configure_logging().
    log_realtime_state("signature api")
    setup_tracer_provider()
    # The API publishes the sign jobs: it declares the streams too.
    await init_nats()
    yield
    await close_nats()


protected_deps = [Depends(enrich_span)]
app = FastAPI(
    lifespan=lifespan,
    docs_url="/docs" if settings.ENV == Environment.LOCAL else None,
    redoc_url="/redoc" if settings.ENV == Environment.LOCAL else None,
    openapi_url="/openapi.json" if settings.ENV == Environment.LOCAL else None,
)

# --- Middleware (executed bottom to top — last registered = outermost = executes first) ---
app.add_middleware(LocaleMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.ALLOW_ORIGIN.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept-Language", "X-Request-ID"],
    expose_headers=[
        "Content-Disposition",
        "X-Document-Revision",
        "X-Document-SHA256",
        "X-Request-ID",
    ],
)
app.add_middleware(RequestLimitsMiddleware)
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(GatewayScopeMiddleware)

# --- Exception handlers ---
app.add_exception_handler(ErrorException, error_exception_handler)  # type: ignore[arg-type]
app.add_exception_handler(Exception, unhandled_exception_handler)

# --- Routers ---
app.include_router(signer_routes, tags=["Signature"], dependencies=protected_deps)
app.include_router(admin_routes, tags=["Signature follow-up"], dependencies=protected_deps)
app.include_router(health_router, prefix="/health", tags=["Health"])

FastAPIInstrumentor.instrument_app(app)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104  # container-internal API behind KrakenD gateway
