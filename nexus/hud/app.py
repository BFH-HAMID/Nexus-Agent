"""Optional FastAPI/WebSocket transport: mandatory authentication, same-origin controls."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
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
    from .world.core import ASSETS as world_assets
    from .world.core import World
    world = World(studio.root)
    app.state.world = world
    slots = asyncio.Semaphore(2)
    public_slots = asyncio.Semaphore(3)
    connections = set()
    requests = {'public': deque(maxlen=120), 'private': deque(maxlen=120)}
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

    # Browser caps persistent cookies at ~400 days. Active sessions renew on API use.
    cookie_name = 'nexus_hud_session'
    lifetime = 400 * 86400
    scope = str(studio.root.resolve())

    def cookie_allowed(request):
        return request.url.scheme in {'https', 'wss'} or request.url.hostname in {'localhost', '127.0.0.1', '::1'}

    def signature(request, value):
        message = (scope + '|' + request.headers.get('host', '') + '|' + value).encode()
        return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()

    def cookie_ok(request):
        if not cookie_allowed(request):
            return False
        value = request.cookies.get(cookie_name, '')
        if len(value) > 256:
            return False
        try:
            expires, nonce, signed = value.split('.')
            return (time.time() < int(expires) <= time.time() + lifetime + 60
                    and len(nonce) == 32 and hmac.compare_digest(signed, signature(request, expires + '.' + nonce)))
        except (ValueError, TypeError):
            return False

    def remember(response, request):
        value = str(int(time.time()) + lifetime) + '.' + secrets.token_hex(16)
        response.set_cookie(cookie_name, value + '.' + signature(request, value), max_age=lifetime,
                            httponly=True, secure=request.url.scheme == 'https', samesite='strict', path='/')

    async def snapshot():
        async with slots:
            value = await run_in_threadpool(service.snapshot)
            value['world'] = world.revisions()
            return value

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        if not origin_ok(request):
            return JSONResponse({'error': 'Host/origin refused'}, status_code=403)
        if request.url.path.startswith('/api/'):
            authorization = request.headers.get('authorization', '')
            bearer = authorization.startswith('Bearer ') and token_ok(authorization[7:])
            remembered = cookie_ok(request)
            if (authorization and not bearer) or (not bearer and not remembered):
                return JSONResponse({'error': 'Authentication required'}, status_code=401)
            if remembered and not bearer and request.method not in {'GET', 'HEAD'} and not origin_ok(request, require=True):
                return JSONResponse({'error': 'Same-origin request required'}, status_code=403)
            # Public-feed refreshes must never consume the operational control budget.
            public_path = request.url.path in {'/api/countries', '/api/news', '/api/crypto', '/api/weather', '/api/quakes', '/api/cities'}
            bucket = requests['public' if public_path else 'private']
            now = time.monotonic()
            while bucket and now - bucket[0] > 60:
                bucket.popleft()
            if len(bucket) >= 120 and request.url.path != '/api/vision/off':
                return JSONResponse({'error': 'Rate limit; retry later'}, status_code=429)
            bucket.append(now)
        response = await call_next(request)
        if request.url.path.startswith('/api/') and cookie_ok(request) and request.method != 'DELETE':
            remember(response, request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'"
        return response

    @app.get('/api/session')
    async def session():
        return {'authenticated': True}

    @app.post('/api/session')
    async def save_session(request: Request):
        if not origin_ok(request, require=True) or not cookie_allowed(request):
            raise HTTPException(403, 'Remember requires same-origin localhost or HTTPS')
        response = JSONResponse({'authenticated': True})
        remember(response, request)
        return response

    @app.delete('/api/session')
    async def forget_session():
        response = JSONResponse({'authenticated': False})
        response.delete_cookie(cookie_name, path='/', httponly=True, samesite='strict')
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

    @app.get('/world/{name}')
    async def world_asset(name: str):
        # Flat, explicit shipped files; never expose an arbitrary path.
        permitted = {'world.js', 'world.js.LEGAL.txt', 'world.css', 'world.vendor.css', 'THIRD-PARTY.txt', 'NotoSansBengali-Regular.ttf', 'OFL-Bengali.txt', 'countries-detail.geojson',
                     'countries-low.geojson', 'earth-night.jpg', 'provenance.json'}
        if name not in permitted or not (world_assets / name).is_file():
            raise HTTPException(404, 'Unknown bundled asset')
        return FileResponse(world_assets / name)

    @app.get('/api/countries')
    async def countries():
        return {'items': world.countries, 'config': world.config, 'source': 'Bundled Natural Earth admin-0; borders are not political endorsement'}

    async def public_data(method, *args):
        if not world.config['enabled']:
            raise HTTPException(503, 'World widget disabled')
        async with public_slots:
            return await run_in_threadpool(method, *args)

    @app.get('/api/news')
    async def world_news():
        return await public_data(world.news)

    @app.get('/api/crypto')
    async def world_crypto():
        return await public_data(world.crypto)

    @app.get('/api/quakes')
    async def world_quakes():
        return await public_data(world.quakes)

    @app.get('/api/weather')
    async def world_weather(latitude: float | None = Query(None, ge=-90, le=90),
                            longitude: float | None = Query(None, ge=-180, le=180)):
        if (latitude is None) != (longitude is None):
            raise HTTPException(422, 'Both coordinates required')
        return await public_data(world.weather, latitude, longitude)

    @app.get('/api/cities')
    async def world_cities(q: str = Query(..., min_length=2, max_length=80)):
        return await public_data(world.cities, q)

    @app.get('/api/state')
    async def state():
        try:
            return await snapshot()
        except (ValueError, OSError):
            raise HTTPException(409, 'State unavailable; no fabricated fallback') from None

    from ..vision.api import MAX_BODY as VISION_MAX_BODY
    from ..vision.api import VisionAPI
    from ..vision.state import status as vision_status
    vision = VisionAPI(studio.root)

    @app.get('/api/vision/status')
    async def capture_status():
        return await run_in_threadpool(vision_status, studio.root)

    @app.post('/api/vision/{operation}')
    async def vision_action(operation: str, request: Request):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > VISION_MAX_BODY:
                raise HTTPException(413, 'Vision request too large')
            data.extend(chunk)
        try:
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise ValueError('Object required')
            if operation == 'off':
                return await run_in_threadpool(vision.post, operation, payload, 'hud')
            async with slots:
                return await run_in_threadpool(vision.post, operation, payload, 'hud')
        except (ValueError, OSError, TimeoutError, RecursionError):
            raise HTTPException(409, 'Vision refused; check operator policy and fresh review') from None

    @app.get('/api/monitoring')
    async def monitoring():
        try:
            async with slots:
                return await run_in_threadpool(service.monitoring)
        except (ValueError, OSError):
            raise HTTPException(409, 'Monitoring telemetry unavailable') from None

    @app.get('/api/schedules')
    async def schedules_list():
        try:
            async with slots:
                return await run_in_threadpool(service.schedules)
        except (ValueError, OSError):
            raise HTTPException(409, 'Schedules unavailable') from None

    @app.post('/api/schedules')
    async def schedules_action(request: Request):
        from ..scheduler import Scheduler
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 16000:
                raise HTTPException(413, 'Request too large')
            data.extend(chunk)
        try:
            payload = json.loads(data or b'{}')
            if not isinstance(payload, dict) or token in json.dumps(payload, ensure_ascii=False):
                raise ValueError('Invalid schedule payload')
            def _do():
                sched = Scheduler(studio.root, studio=studio)
                action_name = str(payload.get('action') or 'add')
                if action_name == 'add':
                    entry = sched.add(
                        str(payload.get('expression') or ''),
                        task=str(payload.get('task') or ''),
                        timezone=payload.get('timezone') or None,
                        target=payload.get('target') or None,
                        priority=int(payload.get('priority', 5)),
                    )
                    return service.clean(entry.to_dict())
                if action_name in {'pause', 'resume'}:
                    entry = sched.set_enabled(str(payload.get('id') or ''), action_name == 'resume')
                    return service.clean(entry.to_dict())
                if action_name == 'remove':
                    removed = sched.remove(str(payload.get('id') or ''))
                    return {'removed': removed, 'id': str(payload.get('id') or '')}
                raise ValueError('Unsupported schedule action')
            async with slots:
                return await run_in_threadpool(_do)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from None

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

    @app.get('/api/chief/missions')
    async def chief_missions():
        from ..chief import chief_hud_summary
        async with slots:
            return await run_in_threadpool(lambda: service.clean(chief_hud_summary(studio.root)))

    @app.get('/api/chief/history')
    async def chief_history(status: str | None = None, limit: int = 50):
        from ..chief import ChiefOfStaff
        async with slots:
            return await run_in_threadpool(lambda: service.clean({'items': ChiefOfStaff(studio.root, studio=studio).history(status=status, limit=limit)}))

    @app.get('/api/chief/missions/{identifier}/replay')
    async def chief_mission_replay(identifier: str, step_id: str | None = None, trace_step: int | None = None, mode: str = 'inspect'):
        from ..chief import ChiefError, ChiefOfStaff
        if mode not in {'inspect', 'why'}:
            raise HTTPException(400, 'HTTP replay supports read-only inspect or why; use CLI for reexecute')
        try:
            async with slots:
                return await run_in_threadpool(lambda: service.clean(ChiefOfStaff(studio.root, studio=studio).replay(identifier, step_id=step_id, trace_step=trace_step, mode=mode)))
        except ChiefError:
            raise HTTPException(404, 'Mission unavailable in this workspace') from None

    @app.get('/api/proactive/status')
    async def proactive_status():
        try:
            async with slots:
                return await run_in_threadpool(service.proactive_status)
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post('/api/proactive/action')
    async def proactive_action(request: Request):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 16000:
                raise HTTPException(413, 'Request too large')
            data.extend(chunk)
        try:
            payload = json.loads(data or b'{}')
            if not isinstance(payload, dict) or token in json.dumps(payload, ensure_ascii=False):
                raise ValueError('Invalid proactive payload')
            async with slots:
                return await run_in_threadpool(service.proactive_action, payload)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get('/api/chief/missions/{identifier}')
    async def chief_mission_detail(identifier: str):
        from ..chief import ChiefError, load_mission
        try:
            async with slots:
                return await run_in_threadpool(lambda: service.clean(load_mission(studio.root, identifier).to_dict()))
        except ChiefError:
            raise HTTPException(404, 'Mission unavailable in this workspace') from None

    @app.post('/api/chief/missions')
    async def chief_mission_create(request: Request):
        from ..chief import ChiefDisabledError, ChiefError, ChiefOfStaff
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 32000:
                raise HTTPException(413, 'Request too large')
            data.extend(chunk)
        try:
            payload = json.loads(data)
            if not isinstance(payload, dict) or token in json.dumps(payload, ensure_ascii=False):
                raise ValueError('Invalid mission payload')
            def _create():
                chief = ChiefOfStaff(studio.root, studio=studio)
                mission = chief.plan(
                    str(payload.get('goal') or ''),
                    pipeline=payload.get('pipeline') or None,
                    cost_cap_usd=payload.get('cost_cap_usd'),
                    concurrency=payload.get('concurrency'),
                    max_retries=payload.get('max_retries'),
                    critic_enabled=payload.get('critic_enabled'),
                    source='hud',
                )
                auto_ok = ChiefOfStaff.can_auto_start(mission, chief.chief_config(), chief.autonomy_policy())
                if payload.get('start') and (auto_ok or str(payload.get('confirm') or '').strip().upper() == 'GO'):
                    mission = chief.start_background(mission.id, auto_approve=True)
                elif not auto_ok:
                    from ..chief import save_mission
                    mission.status = 'awaiting_approval'
                    save_mission(studio.root, mission)
                return service.clean(mission.to_dict())
            async with slots:
                return await run_in_threadpool(_create)
        except ChiefDisabledError as exc:
            raise HTTPException(403, str(exc)) from None
        except (ChiefError, ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post('/api/chief/missions/{identifier}/{verb}')
    async def chief_mission_control(identifier: str, verb: str, request: Request):
        from ..chief import ChiefDisabledError, ChiefError, ChiefOfStaff
        if verb not in {'approve', 'pause', 'resume', 'cancel'}:
            raise HTTPException(404, 'Unknown Chief-of-Staff action')
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 8000:
                raise HTTPException(413, 'Request too large')
            data.extend(chunk)
        try:
            payload = json.loads(data or b'{}')
            if not isinstance(payload, dict):
                raise ValueError('Object required')
            def _act():
                chief = ChiefOfStaff(studio.root, studio=studio)
                if verb == 'approve':
                    mission = chief.approve(
                        identifier,
                        approved=bool(payload.get('approved', True)),
                        confirm=str(payload.get('confirm') or ''),
                        background=bool(payload.get('background', True)),
                    )
                elif verb == 'pause':
                    mission = chief.pause(identifier)
                elif verb == 'resume':
                    mission = chief.resume(identifier, background=bool(payload.get('background', True)))
                else:
                    mission = chief.cancel(identifier)
                return service.clean(mission.to_dict())
            async with slots:
                return await run_in_threadpool(_act)
        except ChiefDisabledError as exc:
            raise HTTPException(403, str(exc)) from None
        except (ChiefError, ValueError, TypeError) as exc:
            raise HTTPException(409, str(exc)) from None

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
            # First frame supports bearer clients; browser sessions use the HttpOnly cookie.
            message = await asyncio.wait_for(socket.receive_text(), timeout=5)
            if len(message) > 1024 or not (cookie_ok(socket) or token_ok(json.loads(message).get('token'))):
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
