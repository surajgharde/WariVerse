"""Request middleware: trace ids, access logs, Prometheus metrics, security headers."""

from __future__ import annotations

import time

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core.config import settings
from app.core.logging import get_logger, set_trace_id

logger = get_logger(__name__)

REQUEST_COUNT = Counter(
    "wariverse_http_requests_total",
    "HTTP requests handled",
    ["method", "path", "status"],
)
REQUEST_LATENCY = Histogram(
    "wariverse_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "path"],
    # Buckets chosen around the p95 < 300ms target in Section 11.
    buckets=(0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0, 5.0),
)


class TraceMiddleware(BaseHTTPMiddleware):
    """Assign or adopt a trace id and echo it back on the response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("x-trace-id")
        trace_id = set_trace_id(incoming if incoming and len(incoming) <= 64 else None)
        request.state.trace_id = trace_id

        started = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - started

        # Use the route template, not the raw path, or every pass id becomes
        # its own Prometheus time series.
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)

        REQUEST_COUNT.labels(request.method, path, str(response.status_code)).inc()
        REQUEST_LATENCY.labels(request.method, path).observe(elapsed)

        response.headers["x-trace-id"] = trace_id
        response.headers["server-timing"] = f"app;dur={elapsed * 1000:.1f}"

        if not path.startswith("/health") and path != "/metrics":
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": path,
                    "status": response.status_code,
                    "duration_ms": round(elapsed * 1000, 2),
                },
            )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """CSP, HSTS and friends (Section 12)."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers.setdefault("x-content-type-options", "nosniff")
        response.headers.setdefault("x-frame-options", "DENY")
        response.headers.setdefault("referrer-policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("permissions-policy", "geolocation=(self), camera=(), microphone=(self)")
        if settings.is_production:
            response.headers.setdefault(
                "strict-transport-security", "max-age=31536000; includeSubDomains; preload"
            )
        # The API serves JSON, not documents — the only thing it should ever be
        # allowed to load is nothing.  Swagger UI is exempt in development.
        if not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
            response.headers.setdefault("content-security-policy", "default-src 'none'; frame-ancestors 'none'")
        return response


def register_middleware(app: FastAPI) -> None:
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(TraceMiddleware)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
