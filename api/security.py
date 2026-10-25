"""Single-owner browser sessions. Credentials never enter the agent runtime."""

import asyncio
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import time
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from starlette.responses import JSONResponse

COOKIE = "ma_session"
MIGRATION_REQUIRED = "Security database migration required: stop the service and run python -m api.migrate_security"


@dataclass(frozen=True)
class SecuritySettings:
    origin: str
    authority: str
    secure: bool
    host: str
    port: int
    session_seconds: int

    @classmethod
    def load(cls):
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        origin = os.environ.get("APP_ORIGIN", "http://127.0.0.1:8000")
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or origin != f"{parsed.scheme}://{parsed.netloc}"
        ):
            raise RuntimeError("APP_ORIGIN must be one exact origin without a path or credentials")
        host = os.environ.get("APP_BIND_HOST", "127.0.0.1")
        port = int(os.environ.get("APP_BIND_PORT", "8000"))
        seconds = int(os.environ.get("APP_SESSION_SECONDS", "43200"))
        if not 1 <= port <= 65535 or not 300 <= seconds <= 86400:
            raise RuntimeError("Invalid APP_BIND_PORT or APP_SESSION_SECONDS")
        if not _loopback(host):
            raise RuntimeError("APP_BIND_HOST must be loopback; remote access uses a local HTTPS reverse proxy")
        if parsed.scheme == "http" and (not _loopback(parsed.hostname) or not _loopback(host)):
            raise RuntimeError("HTTP is allowed only for loopback; remote access requires HTTPS APP_ORIGIN")
        origin_port = parsed.port
        origin_host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        default_port = 443 if parsed.scheme == "https" else 80
        canonical = f"{parsed.scheme}://{origin_host}" + (
            f":{origin_port}" if origin_port not in {None, default_port} else ""
        )
        if origin != canonical:
            raise RuntimeError("APP_ORIGIN must use the canonical browser origin, without a default port")
        return cls(origin, parsed.netloc.lower(), parsed.scheme == "https", host, port, seconds)


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _derive(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)


class DataLease:
    """A process lease shared by the server and the offline migration command."""

    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.stream = (directory / "security.lock").open("a+b")
        self.stream.seek(0, 2)
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise RuntimeError("Stop the other service/migration process before opening these databases") from None

    def close(self):
        self.stream.close()


class AuthService:
    def __init__(self, store, settings, verifier):
        self.store, self.settings, self.verifier = store, settings, verifier
        self.changed = asyncio.Event()

    @classmethod
    async def open(cls, store, settings):
        password = os.environ.get("APP_ACCESS_PASSWORD", "")
        if len(password) < 32 or len(password) > 1024 or len(set(password)) < 12:
            raise RuntimeError("APP_ACCESS_PASSWORD must be a separate high-entropy password of 32–1024 characters")
        if any(
            password == value
            for key, value in os.environ.items()
            if key.endswith("_API_KEY") or key == "MYSQL_READONLY_PASSWORD"
        ):
            raise RuntimeError("APP_ACCESS_PASSWORD must not reuse a service credential")
        async with store._lock:
            async with store.connection.execute("SELECT password_salt,password_hash FROM security_identity") as cursor:
                row = await cursor.fetchone()
            salt = bytes.fromhex(row["password_salt"]) if row["password_salt"] else secrets.token_bytes(16)
            verifier = await asyncio.to_thread(_derive, password, salt)
            if not row["password_hash"] or not hmac.compare_digest(verifier.hex(), row["password_hash"]):
                await store.connection.execute(
                    "UPDATE security_identity SET password_salt=?,password_hash=? WHERE singleton=1",
                    (salt.hex(), verifier.hex()),
                )
                await store.connection.execute("DELETE FROM auth_sessions")
            await store.connection.commit()
        return cls(store, settings, (salt, verifier))

    async def login(self, password):
        # Persist a single-owner throttle before expensive password verification.
        now = int(time.time())
        async with self.store._lock:
            async with self.store.connection.execute(
                "SELECT started,attempts FROM login_throttle WHERE singleton=1"
            ) as cursor:
                row = await cursor.fetchone()
            started, attempts = (row["started"], row["attempts"]) if row and now < row["started"] + 60 else (now, 0)
            if attempts >= 5:
                return None, 429
            await self.store.connection.execute(
                "INSERT INTO login_throttle VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET started=excluded.started,attempts=excluded.attempts",
                (started, attempts + 1),
            )
            await self.store.connection.commit()
        candidate = await asyncio.to_thread(_derive, password, self.verifier[0])
        if not hmac.compare_digest(candidate, self.verifier[1]):
            return None, 401
        token = secrets.token_urlsafe(32)
        session = dict(
            token_hash=self.digest(token),
            owner_id=self.store.owner_id,
            csrf=secrets.token_urlsafe(32),
            expires_at=now + self.settings.session_seconds,
        )
        async with self.store._lock:
            await self.store.connection.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (now,))
            await self.store.connection.execute(
                "INSERT INTO auth_sessions(token_hash,owner_id,csrf,expires_at) VALUES(?,?,?,?)",
                tuple(session.values()),
            )
            await self.store.connection.commit()
        return (token, session), 200

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    async def session(self, token_hash):
        async with self.store._lock:
            async with self.store.connection.execute(
                "SELECT * FROM auth_sessions WHERE token_hash=? AND owner_id=? AND expires_at>?",
                (token_hash, self.store.owner_id, int(time.time())),
            ) as cursor:
                row = await cursor.fetchone()
        return dict(row) if row else None

    async def revoke(self, token_hash):
        async with self.store._lock:
            await self.store.connection.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_hash,))
            await self.store.connection.commit()
        previous = self.changed
        self.changed = asyncio.Event()
        previous.set()

    async def watch(self, session):
        while True:
            changed = self.changed
            if await self.session(session["token_hash"]) is None:
                return
            try:
                await asyncio.wait_for(changed.wait(), timeout=max(0.05, session["expires_at"] - time.time()))
            except TimeoutError:
                return


def session_cookie(headers):
    cookies = SimpleCookie()
    try:
        cookies.load(headers.get("cookie", ""))
    except CookieError:
        return None
    token = cookies[COOKIE].value if COOKIE in cookies else ""
    return token if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) else None


class SecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        state = scope["app"].state
        settings, auth = state.security_settings, state.auth
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope["headers"]}
        websocket = scope["type"] == "websocket"

        async def reject(status, detail):
            if websocket:
                await send({"type": "websocket.close", "code": 4401 if status == 401 else 4403})
            else:
                await JSONResponse({"detail": detail}, status_code=status, headers={"Cache-Control": "no-store"})(
                    scope, receive, send
                )

        async def guarded_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-security-policy", b"frame-ancestors 'none'; base-uri 'none'; object-src 'none'"),
                ]
            await send(message)

        if headers.get("host", "").lower() != settings.authority:
            return await reject(403, "Host rejected")
        path = scope["path"]
        protected = path.startswith("/api/") or websocket
        if not protected:
            return await self.app(scope, receive, guarded_send)
        unsafe = websocket or scope.get("method") not in {"GET", "HEAD", "OPTIONS"}
        if unsafe and headers.get("origin") != settings.origin:
            return await reject(403, "Origin rejected")
        if path == "/api/auth/login" and not websocket:
            if scope["method"] != "POST" or headers.get("x-login-intent") != "login":
                return await reject(403, "Login intent required")
            return await self.app(scope, receive, guarded_send)
        token = session_cookie(headers)
        session = await auth.session(auth.digest(token)) if token else None
        if session is None:
            return await reject(401, "Login required")
        if (
            unsafe
            and not websocket
            and not hmac.compare_digest(
                headers.get("x-csrf-token", "").encode("utf-8"), session["csrf"].encode("ascii")
            )
        ):
            return await reject(403, "CSRF token rejected")
        scope["state"] = dict(scope.get("state", {}), auth_session=session)

        if not websocket:
            return await self.app(scope, receive, guarded_send)
        # Check every event as well as idle connections; logout never cancels jobs.
        ws_lock = asyncio.Lock()
        ws_closed = False

        async def ws_send(message):
            nonlocal ws_closed
            async with ws_lock:
                if ws_closed:
                    return
                if message["type"] in {"websocket.accept", "websocket.send"}:
                    if await auth.session(session["token_hash"]) is None:
                        message = {"type": "websocket.close", "code": 4401}
                if message["type"] == "websocket.close":
                    ws_closed = True
                await send(message)

        handler = asyncio.create_task(self.app(scope, receive, ws_send))
        watcher = asyncio.create_task(auth.watch(session))
        try:
            done, _ = await asyncio.wait({handler, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if watcher in done and not handler.done():
                await ws_send({"type": "websocket.close", "code": 4401})
                handler.cancel()
            if handler in done:
                await handler
        finally:
            handler.cancel()
            watcher.cancel()
            await asyncio.gather(handler, watcher, return_exceptions=True)
