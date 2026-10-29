"""Optional FastAPI/WebSocket transport: mandatory bearer auth, same-origin controls."""
from __future__ import annotations

import asyncio
import hmac
import json
import re
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import HUD, settings


def create_app(studio, token, *, allowed_hosts=None):
    if not isinstance(token, str) or not 32 <= len(token) <= 256 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError('Set a private 32–256 character NEXUS_HUD_TOKEN; never use GitHub credentials')
    cfg = settings(studio.root)
    hosts = set(allowed_hosts or ['127.0.0.1', 'localhost', '::1'])
    if not hosts or any(not isinstance(h, str) or not re.fullmatch(r'[A-Za-z0-9.:-]{1,253}', h) for h in hosts):
        raise ValueError('Explicit hostnames required; no wildcards')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    service = HUD(studio, secret_values=[token])
    slots = asyncio.Semaphore(2)
    connections = set()
    requests = deque(maxlen=120)
    assets = Path(__file__).parent / 'assets'

    def origin_ok(scope, require=False):
        host = scope.headers.get('host', '')
        try:
            hostname = urlsplit('http://' + host).hostname
            origin = scope.headers.get('origin')
            if hostname not in hosts:
                return False
            return ((not require and origin is None) or origin in ('http://' + host, 'https://' + host))
        except ValueError:
            return False

    def token_ok(value):
        return isinstance(value, str) and len(value) <= 512 and hmac.compare_digest(value.encode(), token.encode())

    async def snapshot():
        async with slots:
            return await run_in_threadpool(service.snapshot)

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        if not origin_ok(request):
            return JSONResponse({'error': 'Host/origin refused'}, status_code=403)
        if request.url.path.startswith('/api/'):
            authorization = request.headers.get('authorization', '')
            if not authorization.startswith('Bearer ') or not token_ok(authorization[7:]):
                return JSONResponse({'error': 'Authentication required'}, status_code=401)
            now = time.monotonic()
            while requests and now - requests[0] > 60:
                requests.popleft()
            if len(requests) >= 120:
                return JSONResponse({'error': 'Rate limit; retry later'}, status_code=429)
            requests.append(now)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'"
        return response

    @app.get('/')
    async def page():
        return FileResponse(assets / 'index.html')

    @app.get('/hud.js')
    async def script():
        return FileResponse(assets / 'hud.js', media_type='text/javascript')

    @app.get('/hud.css')
    async def styles():
        return FileResponse(assets / 'hud.css', media_type='text/css')

    @app.get('/api/state')
    async def state():
        try:
            return await snapshot()
        except (ValueError, OSError):
            raise HTTPException(409, 'State unavailable; no fabricated fallback') from None

    @app.get('/api/trace/{identifier}')
    async def trace(identifier: str, operation: str = 'inspect', step: int | None = None):
        if operation not in {'inspect', 'why', 'replay'}:
            raise HTTPException(400, 'Inspection only; re-execution requires the local CLI and fresh gates')
        try:
            async with slots:
                return await run_in_threadpool(service.trace, identifier, operation, step)
        except (ValueError, OSError, KeyError):
            raise HTTPException(404, 'Trace/step unavailable in this workspace') from None

    @app.get('/api/job/{identifier}')
    async def job(identifier: str):
        try:
            async with slots:
                return await run_in_threadpool(service.job, identifier)
        except (ValueError, OSError, KeyError):
            raise HTTPException(404, 'Job unavailable in this workspace') from None

    @app.get('/api/approval/{identifier}')
    async def approval(identifier: str):
        try:
            async with slots:
                return await run_in_threadpool(service.approval, identifier)
        except (ValueError, OSError):
            raise HTTPException(404, 'Approval unavailable') from None

    @app.post('/api/action/{operation}')
    async def action(operation: str, request: Request):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 16000:
                raise HTTPException(413, 'Request too large')
            data.extend(chunk)
        try:
            payload = json.loads(data)
            if token in json.dumps(payload, ensure_ascii=False):
                raise ValueError('Authentication material is not task content')
            async with slots:
                return await run_in_threadpool(service.action, operation, payload)
        except (ValueError, OSError, KeyError, TypeError, TimeoutError):
            raise HTTPException(409, 'Action refused; inspect exact confirmation, policy, limits and current state') from None

    @app.websocket('/ws')
    async def websocket(socket: WebSocket):
        if not origin_ok(socket, require=True) or len(connections) >= cfg['max_connections']:
            await socket.close(code=1008)
            return
        connections.add(socket)
        try:
            await socket.accept()
            # Authentication is never placed in URL/query, cookies or logs.
            message = await asyncio.wait_for(socket.receive_text(), timeout=5)
            if len(message) > 1024 or not token_ok(json.loads(message).get('token')):
                await socket.close(code=1008)
                return
            while True:
                await socket.send_json(await snapshot())
                try:
                    incoming = await asyncio.wait_for(socket.receive(), timeout=cfg['refresh_seconds'])
                except asyncio.TimeoutError:
                    continue
                if incoming['type'] != 'websocket.disconnect':
                    await socket.close(code=1008)  # Snapshot-only protocol, no inbound commands.
                break  # Release the slot immediately instead of waiting for the next refresh.
        except (WebSocketDisconnect, asyncio.TimeoutError, ValueError, OSError, RuntimeError, AttributeError, KeyError, TypeError):
            try:
                await socket.close(code=1008)
            except RuntimeError:
                pass
        finally:
            connections.discard(socket)

    return app
