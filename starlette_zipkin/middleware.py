import asyncio
import socket
import traceback
import urllib
from typing import Any, Callable
from urllib.parse import urlunparse

import aiozipkin as az
from aiozipkin.span import SpanAbc
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Scope

from .config import ZipkinConfig
from .trace import get_tracer, init_tracer, install_root_span, reset_root_span


class ZipkinMiddleware(BaseHTTPMiddleware):
    def __init__(
        self, app: Starlette, dispatch: Callable = None, config: ZipkinConfig = None
    ):
        self.app = app
        self.dispatch_func = self.dispatch if dispatch is None else dispatch
        self.config = config or ZipkinConfig()
        self.validate_config()
        self.tracer = None

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        tracer = get_tracer()
        if tracer is None:
            tracer = await init_tracer(self.config)

        if self.has_trace_id(request) and not self.config.force_new_trace:
            kw = {"context": self.config.header_formatter.make_context(request.headers)}
            function = tracer.new_child
        else:
            kw = {}
            function = tracer.new_trace

        with function(**kw) as span:
            # set root span using context variable
            root_span = install_root_span(span)
            try:
                self.before(span, request.scope)
                response = await call_next(request)
                self.after(span, response)
                asyncio.get_event_loop().create_task(tracer.close())
                return response

            except Exception as error:
                self.error(span, error)
                raise error from None

            finally:
                reset_root_span(root_span)

    def validate_config(self) -> None:
        if not isinstance(self.config, ZipkinConfig):
            raise ValueError("Config needs to be ZipkinConfig instance")

    def has_trace_id(self, request: Request) -> bool:
        if self.config.header_formatter.TRACE_ID_HEADER in request.headers:
            return True
        else:
            return False

    def before(self, span: SpanAbc, scope: Scope) -> None:
        name = f'{scope["scheme"].upper()} {scope["method"]} {scope["path"]}'
        span.name(name)
        span.tag("component", "asgi")
        span.tag("ip", get_ip())
        span.kind(az.SERVER)

        if scope["type"] in {"http", "websocket"}:
            span.tag("http.method", scope["method"])
            span.tag("http.url", self.get_url(scope))
            span.tag("http.route", scope["path"])
            span.tag("http.headers", self.get_headers(scope))
        query = self.get_query(scope)
        if query:
            span.tag("query", query)
        if scope.get("client"):
            span.tag("remote_address", scope["client"][0])
        if scope.get("endpoint"):
            span.tag("transaction", self.get_transaction(scope))

    def after(self, span: SpanAbc, response: Response) -> None:
        """
        If context header not filled in by other function,
        add tracing info.
        """
        if self.config.inject_response_headers:
            self.config.header_formatter.update_headers(span, response)

        span.tag("http.status_code", response.status_code)
        if response.status_code >= 400:
            span.tag("error", True)
        span.tag(
            "http.response.headers",
            self.config.json_encoder(dict(response.headers)),
        )
        # getting body after request was evaluated due to:
        # https://github.com/encode/starlette/issues/495
        # body = await request.body()
        # if body:
        #     span.tag(
        #         "http.body",
        #         self.config.json_encoder(await request.json()),
        #     )

    def error(self, span: SpanAbc, error: Exception) -> None:
        span.tag("error", True)
        span.tag("error.object", type(error).__name__)
        span.tag("error.stack", traceback.format_exc())

    def get_url(self, scope: Scope) -> str:
        host, port = scope["server"]
        url = urlunparse(
            (
                scope["scheme"],
                f"{host}:{port}",
                scope["path"],
                "",
                scope["query_string"].decode("utf-8"),
                "",
            )
        )
        return url

    def get_headers(self, scope: Scope) -> dict:
        """
        Extract headers from the ASGI scope.
        """
        headers: dict = {}
        for raw_key, raw_value in scope["headers"]:
            key = raw_key.decode("latin-1")
            value = raw_value.decode("latin-1")
            if key in headers:
                headers[key] = headers[key] + ", " + value
            else:
                headers[key] = value
        return self.config.json_encoder(headers)

    def get_query(self, scope: Scope) -> str:
        """
        Extract querystring from the ASGI scope.
        """
        return urllib.parse.unquote(scope["query_string"].decode("latin-1"))

    def get_transaction(self, scope: Scope) -> str:
        """
        Return a transaction string to identify the routed endpoint.
        """
        endpoint = scope["endpoint"]
        qualname = (
            getattr(endpoint, "__qualname__", None)
            or getattr(endpoint, "__name__", None)
            or None
        )
        if not qualname:
            return ""
        return f"{endpoint.__module__}.{qualname}"


def get_ip() -> Any:
    hostname = socket.gethostname()
    return socket.gethostbyname(hostname)
