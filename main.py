import logging
import os
import hmac
import hashlib
from fastapi import FastAPI, Depends, HTTPException, Request, Form, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy.sql import func
from datetime import datetime, timedelta
import json
import asyncio
import secrets

logger = logging.getLogger(__name__)

_CSRF_SECRET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".csrf_secret")


def _load_csrf_secret() -> str:
    env_secret = os.environ.get("CSRF_SECRET")
    if env_secret:
        return env_secret
    if os.path.exists(_CSRF_SECRET_FILE):
        try:
            with open(_CSRF_SECRET_FILE, "r") as f:
                data = f.read().strip()
                if data:
                    return data
        except Exception as e:
            logger.warning(f"Could not read CSRF secret file: {e}")
    secret = secrets.token_hex(32)
    try:
        with open(_CSRF_SECRET_FILE, "w") as f:
            f.write(secret)
    except Exception as e:
        logger.warning(f"Could not write CSRF secret file: {e}")
    return secret


def rotate_csrf_secret():
    global CSRF_SECRET
    secret = secrets.token_hex(32)
    CSRF_SECRET = secret
    try:
        with open(_CSRF_SECRET_FILE, "w") as f:
            f.write(secret)
    except Exception as e:
        logger.warning(f"Could not write CSRF secret file: {e}")


CSRF_SECRET = _load_csrf_secret()

PATH_SYSTEM_DOWN = "/system-down"
PATH_DASHBOARD = "/dashboard"
PATH_ADMIN = "/admin"
MSG_RESERVATION_NOT_FOUND = "Reservation not found"
MSG_NOT_AN_EDITOR_SESSION = "Not an editor session"
MSG_NOT_A_TERMINAL_SESSION = "Not a terminal session"


def generate_csrf_token() -> str:
    token = secrets.token_urlsafe(32)
    signature = hmac.new(CSRF_SECRET.encode(), token.encode(), hashlib.sha256).hexdigest()
    return f"{token}.{signature}"


def validate_csrf_token(token: str) -> bool:
    if not token or "." not in token:
        return False
    token_part, signature_part = token.rsplit(".", 1)
    expected_signature = hmac.new(CSRF_SECRET.encode(), token_part.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_part, expected_signature)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "font-src 'self' https://cdn.jsdelivr.net; "
            "img-src 'self' data:; "
            "connect-src 'self' ws: wss:;"
        )
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            cookie_token = request.cookies.get("csrf_token")
            if cookie_token and validate_csrf_token(cookie_token):
                token = cookie_token
            else:
                token = generate_csrf_token()
            request.state.csrf_token = token
        response = await call_next(request)
        if request.method in ("GET", "HEAD", "OPTIONS"):
            response.set_cookie(
                key="csrf_token",
                value=request.state.csrf_token,
                httponly=False,
                secure=False,
                samesite="Strict",
                path="/",
            )
        return response


async def csrf_protect(request: Request):
    cookie_token = request.cookies.get("csrf_token")
    form_data = await request.form()
    field_token = form_data.get("csrf_token")
    if not cookie_token or not field_token or not validate_csrf_token(field_token) or field_token != cookie_token:
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")

from database import engine, SessionLocal, get_db
from models import Base, User, Reservation, ClusterResources, ResourceAllocation, EditorRun
from auth import authenticate_ldap, get_or_create_user, create_jwt, get_current_user, get_current_user_optional, require_admin, decode_jwt
from slurm_client import submit_slurm_job
from vm_monitor import get_health, get_downtime, format_downtime, read_slurm_output, _run_ssh
from slurm_resources import get_node_resources
from code_runner import run_code
from terminal_session import TerminalSession
from config import Config

Base.metadata.create_all(bind=engine)


def _add_reservation_columns_sqlite(conn, cols):
    for col in ["language", "mode", "code", "session_expires_at", "slurm_allocation", "output", "terminal_pid"]:
        if col not in cols:
            sql = f"ALTER TABLE reservations ADD COLUMN {col} TEXT"
            conn.execute(text(sql))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text - col is from hardcoded whitelist


def _create_sqlite_tables(conn, tables):
    if "cluster_resources" not in tables:
        conn.execute(text("""
            CREATE TABLE cluster_resources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                total_cpu INTEGER NOT NULL,
                free_cpu INTEGER NOT NULL,
                total_ram_gb INTEGER NOT NULL,
                free_ram_gb INTEGER NOT NULL,
                raw TEXT,
                recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """))
    if "resource_allocations" not in tables:
        conn.execute(text("""
            CREATE TABLE resource_allocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reservation_id INTEGER NOT NULL,
                cpu INTEGER NOT NULL,
                ram_gb INTEGER NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'allocated',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                released_at DATETIME NULL
            )
        """))
    if "editor_runs" not in tables:
        conn.execute(text("""
            CREATE TABLE editor_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reservation_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                code_input TEXT NOT NULL,
                output TEXT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """))


def _add_reservation_columns_mysql(conn, cols):
    if "language" not in cols:
        conn.execute(text("ALTER TABLE reservations ADD COLUMN language VARCHAR(20) DEFAULT 'python'"))
    if "mode" not in cols:
        conn.execute(text("ALTER TABLE reservations ADD COLUMN mode VARCHAR(20) DEFAULT 'batch'"))
    if "code" not in cols:
        conn.execute(text("ALTER TABLE reservations ADD COLUMN code TEXT"))
    if "session_expires_at" not in cols:
        conn.execute(text("ALTER TABLE reservations ADD COLUMN session_expires_at DATETIME NULL"))
    if "slurm_allocation" not in cols:
        conn.execute(text("ALTER TABLE reservations ADD COLUMN slurm_allocation VARCHAR(100) NULL"))
    if "output" not in cols:
        conn.execute(text("ALTER TABLE reservations ADD COLUMN output TEXT NULL"))
    if "terminal_pid" not in cols:
        conn.execute(text("ALTER TABLE reservations ADD COLUMN terminal_pid VARCHAR(50) NULL"))


def _create_mysql_tables(conn, tables):
    if "editor_runs" not in tables:
        conn.execute(text("""
            CREATE TABLE editor_runs (
                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                reservation_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                code_input TEXT NOT NULL,
                output TEXT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (reservation_id) REFERENCES reservations(id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """))


def migrate_db():
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    dialect = engine.dialect.name
    if dialect == "sqlite":
        with engine.connect() as conn:
            cols = [c["name"] for c in insp.get_columns("reservations")]
            _add_reservation_columns_sqlite(conn, cols)
            conn.commit()
        with engine.connect() as conn:
            tables = insp.get_table_names()
            _create_sqlite_tables(conn, tables)
            conn.commit()
    elif dialect == "mysql":
        with engine.connect() as conn:
            cols = [c["name"] for c in insp.get_columns("reservations")]
            _add_reservation_columns_mysql(conn, cols)
            conn.commit()
        with engine.connect() as conn:
            tables = insp.get_table_names()
            _create_mysql_tables(conn, tables)
            conn.commit()


def _get_or_create_cluster_resources(db: Session) -> ClusterResources:
    resources = db.query(ClusterResources).first()
    if not resources:
        resources = ClusterResources(
            total_cpu=Config.MAX_CPU,
            free_cpu=Config.MAX_CPU,
            total_ram_gb=Config.MAX_RAM_GB,
            free_ram_gb=Config.MAX_RAM_GB,
        )
        db.add(resources)
        db.commit()
        db.refresh(resources)
    return resources


def _allocate_resources(db: Session, reservation_id: int, cpu: int, ram_gb: int) -> bool:
    resources = _get_or_create_cluster_resources(db)
    if resources.free_cpu < cpu or resources.free_ram_gb < ram_gb:
        return False
    resources.free_cpu -= cpu
    resources.free_ram_gb -= ram_gb
    allocation = ResourceAllocation(
        reservation_id=reservation_id,
        cpu=cpu,
        ram_gb=ram_gb,
        status="allocated",
    )
    db.add(allocation)
    db.commit()
    return True


def _release_resources(db: Session, reservation_id: int) -> None:
    resources = _get_or_create_cluster_resources(db)
    allocation = db.query(ResourceAllocation).filter(
        ResourceAllocation.reservation_id == reservation_id,
        ResourceAllocation.status == "allocated",
    ).first()
    if allocation:
        resources.free_cpu += allocation.cpu
        resources.free_ram_gb += allocation.ram_gb
        allocation.status = "released"
        allocation.released_at = func.now()
        db.commit()


def _release_all_orphaned_resources(db: Session) -> None:
    resources = _get_or_create_cluster_resources(db)
    allocations = db.query(ResourceAllocation).filter(ResourceAllocation.status == "allocated").all()
    released = 0
    for allocation in allocations:
        reservation = db.query(Reservation).filter(Reservation.id == allocation.reservation_id).first()
        if not reservation or reservation.status in ("completed", "failed", "cancelled"):
            resources.free_cpu += allocation.cpu
            resources.free_ram_gb += allocation.ram_gb
            allocation.status = "released"
            allocation.released_at = func.now()
            released += 1
    if released > 0:
        db.commit()


def _process_queued_terminal(db: Session, reservation: Reservation):
    if not _allocate_resources(db, reservation.id, reservation.cpu, reservation.ram):
        return
    from datetime import datetime, timedelta
    reservation.session_expires_at = datetime.utcnow() + timedelta(minutes=Config.EDITOR_SESSION_MINUTES)
    reservation.status = "approved"
    db.commit()
    db.refresh(reservation)


def _process_queued_editor(db: Session, reservation: Reservation):
    if not _allocate_resources(db, reservation.id, reservation.cpu, reservation.ram):
        return
    from datetime import datetime, timedelta
    reservation.session_expires_at = datetime.utcnow() + timedelta(minutes=Config.EDITOR_SESSION_MINUTES)
    reservation.status = "approved"
    db.commit()
    db.refresh(reservation)


def _process_queued_batch(db: Session, reservation: Reservation):
    from code_runner import run_code
    result = run_code(reservation.language, reservation.code or reservation.script, reservation.cpu, reservation.ram, reservation.duration)
    reservation.slurm_job_id = result.get("job_id")
    if result.get("success"):
        reservation.status = "running"
    else:
        reservation.status = "failed"
        _release_resources(db, reservation.id)
    raw_output = result.get("output") or result.get("error") or "Job submitted"
    if reservation.slurm_job_id and reservation.status == "running":
        actual = read_slurm_output(reservation.slurm_job_id)
        reservation.output = actual or raw_output
    else:
        reservation.output = raw_output
    db.commit()
    db.refresh(reservation)


def _process_queue(db: Session) -> None:
    queued = db.query(Reservation).filter(Reservation.status == "queued").order_by(Reservation.created_at.asc()).all()
    for reservation in queued:
        if reservation.mode == "terminal":
            _process_queued_terminal(db, reservation)
            continue
        if reservation.mode == "editor":
            _process_queued_editor(db, reservation)
            continue
        _process_queued_batch(db, reservation)


app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


def _render_csrf_token(request: Request = None) -> str:
    if request is not None and hasattr(request, "state") and request.state.csrf_token:
        return request.state.csrf_token
    return generate_csrf_token()


templates.env.globals["csrf_token"] = _render_csrf_token


@app.on_event("startup")
def startup():
    migrate_db()
    db = SessionLocal()
    try:
        _release_all_orphaned_resources(db)
        _cleanup_orphaned_editor_containers(db)
    except Exception as e:
        logger.error(f"Failed to release orphaned resources on startup: {e}", exc_info=True)
    finally:
        db.close()
    print("[STARTUP] Server started with DinD terminal support")


PUBLIC_PATHS = {"/", "/login", "/system-down"}


class HealthGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path in PUBLIC_PATHS or path.startswith("/static"):
            return await call_next(request)

        health = get_health()
        if not health["overall_up"]:
            return RedirectResponse(url=PATH_SYSTEM_DOWN, status_code=302)

        return await call_next(request)


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CSRFMiddleware)


def get_user_reservations(db: Session, user_id: int, search: str = None):
    query = db.query(Reservation).filter(Reservation.user_id == user_id)
    if search:
        query = query.filter(Reservation.job_name.ilike(f"%{search}%"))
    return query.order_by(Reservation.created_at.desc()).all()


def enforce_resources(cpu: int, ram: int, duration: int):
    if cpu > Config.MAX_CPU or ram > Config.MAX_RAM_GB or duration > Config.MAX_WALLTIME_HOURS:
        raise HTTPException(400, f"Max allowed: {Config.MAX_CPU} CPU / {Config.MAX_RAM_GB} GB RAM / {Config.MAX_WALLTIME_HOURS}h")


# ---------- Routes ----------
@app.get("/", response_class=HTMLResponse)
async def root(request: Request, user: User = Depends(get_current_user_optional)):
    if user:
        health = get_health()
        if not health["overall_up"]:
            return RedirectResponse(url=PATH_SYSTEM_DOWN, status_code=302)
        return RedirectResponse(url=PATH_DASHBOARD, status_code=302)

    health = get_health()
    downtime = get_downtime()
    return templates.TemplateResponse(request, "landing.html", {
        "request": request,
        "overall_up": health["overall_up"],
        "downtime_message": format_downtime(downtime),
    })


@app.get("/system-down", response_class=HTMLResponse)
async def system_down(request: Request, user: User = Depends(get_current_user_optional)):
    if not user:
        return RedirectResponse(url="/", status_code=302)

    health = get_health()
    downtime = get_downtime()
    return templates.TemplateResponse(request, "system_down.html", {
        "request": request,
        "user": user,
        "services": health["services"],
        "overall_up": health["overall_up"],
        "downtime_message": format_downtime(downtime),
    })


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request})


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db), csrf: None = Depends(csrf_protect)):
    if authenticate_ldap(username, password):
        rotate_csrf_secret()
        user = get_or_create_user(db, username)
        token = create_jwt(user.id, user.role)
        health = get_health()
        target = PATH_SYSTEM_DOWN if not health["overall_up"] else PATH_DASHBOARD
        response = RedirectResponse(url=target, status_code=302)
        response.set_cookie(
            key="access_token",
            value=token,
            httponly=True,
            secure=True,
            samesite="Strict",
            max_age=3600,
            path="/",
        )
        return response
    else:
        return templates.TemplateResponse(request, "login.html", {"request": request, "error": "Invalid credentials"})


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, search: str = Query(None), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from sanitize import sanitize_text
    _release_all_orphaned_resources(db)
    _cleanup_orphaned_editor_containers(db)
    safe_search = sanitize_text(search or "", max_length=100) if search else None
    reservations = get_user_reservations(db, user.id, safe_search)
    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request,
        "reservations": reservations,
        "search": safe_search,
        "user": user
    })


@app.get("/request", response_class=HTMLResponse)
async def request_form(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _release_all_orphaned_resources(db)
    resources = _get_or_create_cluster_resources(db)
    return templates.TemplateResponse(request, "request_form.html", {
        "request": request,
        "user": user,
        "resources": resources,
        "limits": {"cpu": Config.MAX_CPU, "ram": Config.MAX_RAM_GB, "hours": Config.MAX_WALLTIME_HOURS},
    })


@app.post("/request", response_class=HTMLResponse)
async def create_reservation(
    request: Request,
    job_name: str = Form(...),
    cpu: int = Form(...),
    ram: int = Form(...),
    duration: int = Form(...),
    language: str = Form("python"),
    mode: str = Form("batch"),
    code: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    csrf: None = Depends(csrf_protect),
):
    from code_runner import build_script
    from sanitize import validate_job_name, validate_code_input
    import uuid

    try:
        job_name = validate_job_name(job_name)
        code = validate_code_input(code)
    except ValueError as e:
        return templates.TemplateResponse(request, "request_form.html", {
            "request": request,
            "user": user,
            "resources": _get_or_create_cluster_resources(db),
            "limits": {"cpu": Config.MAX_CPU, "ram": Config.MAX_RAM_GB, "hours": Config.MAX_WALLTIME_HOURS},
            "error": str(e),
        })

    enforce_resources(cpu, ram, duration)
    job_id = uuid.uuid4().hex[:8]
    script = build_script(language, cpu, ram, duration, job_id) if mode == "batch" else code
    if mode == "terminal":
        language = "docker"
    reservation = Reservation(
        user_id=user.id,
        job_name=job_name,
        cpu=cpu,
        ram=ram,
        duration=duration,
        script=script,
        status="pending",
        language=language,
        mode=mode,
        code=code,
    )
    db.add(reservation)
    db.commit()
    db.refresh(reservation)

    return RedirectResponse(url=PATH_DASHBOARD, status_code=302)


@app.get("/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(key="access_token")
    return response


@app.get("/reservation/{reservation_id}", response_class=HTMLResponse)
async def reservation_detail(
    request: Request,
    reservation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not reservation:
        raise HTTPException(status_code=404, detail=MSG_RESERVATION_NOT_FOUND)
    if reservation.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    if reservation.mode == "terminal":
        raise HTTPException(status_code=404, detail="Use the terminal page for this session")
    return templates.TemplateResponse(request, "reservation_detail.html", {
        "request": request,
        "reservation": reservation,
        "user": user
    })


def _require_editor_access(reservation: Reservation, user: User) -> None:
    if not reservation or (reservation.user_id != user.id and user.role != "admin"):
        raise HTTPException(404)
    if reservation.mode != "editor":
        raise HTTPException(400, "This reservation is not an editor session")
    if reservation.status in ("completed", "failed"):
        raise HTTPException(403, "This editor session has ended")


def _ensure_editor_session_active(reservation: Reservation, reservation_id: int, db: Session) -> EditorSession:
    now = datetime.utcnow()
    if reservation.session_expires_at and now > reservation.session_expires_at:
        raise HTTPException(403, "This editor session has expired")

    session = _get_editor_session(reservation_id)
    if session and session.active:
        return session

    if reservation.status != "approved":
        raise HTTPException(403, "Editor session is not ready")

    from editor_session import EditorSession
    session = EditorSession(
        reservation_id=reservation.id,
        cpu=reservation.cpu,
        ram_gb=reservation.ram,
        duration_hours=reservation.duration,
        language=reservation.language,
    )
    result = session.start_slurm_allocation()
    if not result.get("success"):
        raise HTTPException(500, f"Failed to start editor session: {result.get('error')}")
    _active_editor_sessions[reservation.id] = session
    reservation.status = "running"
    reservation.slurm_job_id = session.job_id
    db.commit()
    db.refresh(reservation)
    return session


@app.get("/editor/{reservation_id}", response_class=HTMLResponse)
async def editor_page(request: Request, reservation_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _cleanup_orphaned_editor_containers(db)
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    _require_editor_access(reservation, user)
    _ensure_editor_session_active(reservation, reservation_id, db)
    now = datetime.utcnow()
    remaining = max(0, int((reservation.session_expires_at - now).total_seconds())) if reservation.session_expires_at else 0
    return templates.TemplateResponse(request, "editor.html", {
        "request": request,
        "user": user,
        "reservation": reservation,
        "remaining_seconds": remaining,
    })


@app.post("/editor/{reservation_id}/run", response_class=HTMLResponse)
async def editor_run(
    request: Request,
    reservation_id: int,
    code: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    csrf: None = Depends(csrf_protect),
):
    from sanitize import validate_code_input, sanitize_code_output
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not reservation or (reservation.user_id != user.id and user.role != "admin"):
        raise HTTPException(404, "Not found")
    if reservation.mode != "editor":
        raise HTTPException(400, MSG_NOT_AN_EDITOR_SESSION)
    if reservation.status in ("completed", "failed"):
        raise HTTPException(403, "Session has ended")

    now = datetime.utcnow()
    if reservation.session_expires_at and now > reservation.session_expires_at:
        raise HTTPException(403, "Session expired")

    session = _get_editor_session(reservation_id)
    if not session or not session.active:
        raise HTTPException(403, "Editor session is not active")

    code = validate_code_input(code or reservation.code)
    reservation.code = code
    db.commit()
    db.refresh(reservation)

    result = session.execute_code(code)
    output = sanitize_code_output(result.get("output", ""))
    reservation.output = output
    reservation.slurm_job_id = session.job_id
    db.commit()
    db.refresh(reservation)

    run_record = EditorRun(
        reservation_id=reservation.id,
        user_id=user.id,
        code_input=code,
        output=output,
    )
    db.add(run_record)
    db.commit()

    return RedirectResponse(url=f"/editor/{reservation_id}", status_code=303)


@app.get("/editor/{reservation_id}/status")
async def editor_status(reservation_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not reservation or (reservation.user_id != user.id and user.role != "admin"):
        raise HTTPException(404)
    if reservation.mode != "editor":
        raise HTTPException(400, MSG_NOT_AN_EDITOR_SESSION)

    now = datetime.utcnow()
    expired = reservation.session_expires_at and now > reservation.session_expires_at
    session = _get_editor_session(reservation_id)
    active = bool(session and session.active)

    if expired or (not active and reservation.status == "running"):
        _cleanup_editor_session(reservation_id, db)
        active = False

    status = reservation.status
    if active and status != "running":
        status = "running"
    elif not active and status == "running":
        status = "completed"

    return JSONResponse({
        "status": status,
        "active": active,
        "remaining_seconds": max(0, int((reservation.session_expires_at - now).total_seconds())) if reservation.session_expires_at else 0,
        "output": reservation.output or "",
    })


@app.post("/editor/{reservation_id}/stop")
async def editor_stop(reservation_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user), csrf: None = Depends(csrf_protect)):
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not reservation or reservation.user_id != user.id:
        raise HTTPException(404)
    if reservation.mode != "editor":
        raise HTTPException(400, MSG_NOT_AN_EDITOR_SESSION)

    _cleanup_editor_session(reservation_id, db)
    reservation.status = "completed"
    reservation.session_expires_at = datetime.utcnow()
    db.commit()
    db.refresh(reservation)
    return RedirectResponse(url=PATH_DASHBOARD, status_code=302)


@app.get("/editor/{reservation_id}/stop")
async def editor_stop_get(reservation_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return await editor_stop(reservation_id, db, user)


@app.get("/editor/{reservation_id}/runs", response_class=HTMLResponse)
async def editor_runs_page(request: Request, reservation_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    print(f"[RUNS DEBUG] Hit route for reservation_id={reservation_id} user={user.id} role={user.role}")
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    print(f"[RUNS DEBUG] reservation found={reservation is not None} mode={reservation.mode if reservation else None} status={reservation.status if reservation else None}")
    if not reservation or (reservation.user_id != user.id and user.role != "admin"):
        print(f"[RUNS DEBUG] Raising 404: reservation={reservation is not None} owner={reservation.user_id if reservation else None} user={user.id} role={user.role}")
        raise HTTPException(404)
    if reservation.mode != "editor":
        raise HTTPException(400, MSG_NOT_AN_EDITOR_SESSION)
    if reservation.status not in ("completed", "failed"):
        raise HTTPException(403, "Execution history is only available after the session is completed or failed")
    runs = db.query(EditorRun).filter(EditorRun.reservation_id == reservation_id).order_by(EditorRun.created_at.asc()).all()
    return templates.TemplateResponse(request, "editor_runs.html", {
        "request": request,
        "user": user,
        "reservation": reservation,
        "runs": runs,
    })


@app.get("/reservation/{reservation_id}/status")
async def reservation_status(reservation_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not reservation or reservation.user_id != user.id:
        raise HTTPException(404)
    return _poll_job_status(reservation, db)


def _poll_job_status(reservation: Reservation, db: Session) -> dict:
    from sanitize import sanitize_code_output
    data = {
        "status": reservation.status,
        "job_id": reservation.slurm_job_id,
        "output": sanitize_code_output(reservation.output or ""),
    }
    if reservation.mode == "terminal":
        return data
    if not reservation.slurm_job_id:
        return data

    actual_status = _get_slurm_job_status(reservation.slurm_job_id)
    if actual_status in ("completed", "failed", "cancelled"):
        actual_output = read_slurm_output(reservation.slurm_job_id) or reservation.output or ""
        reservation.output = sanitize_code_output(actual_output)
        reservation.status = actual_status
        reservation.session_expires_at = datetime.utcnow()
        _release_resources(db, reservation.id)
        db.commit()
        db.refresh(reservation)
        _process_queue(db)
        data["status"] = reservation.status
        data["output"] = sanitize_code_output(reservation.output or "")
    elif actual_status == "running" and reservation.status != "running":
        reservation.status = "running"
        db.commit()
        db.refresh(reservation)
        data["status"] = reservation.status
    return data


def _get_slurm_job_status(slurm_job_id: str) -> str:
    from vm_monitor import _run_ssh
    ok, out = _run_ssh(f"sacct -j {slurm_job_id} --format=JobID,State 2>/dev/null | tail -n +3 | head -n 1 | awk '{{print $2}}'")
    if ok and out:
        state = out.strip().lower()
        if state in ("completed", "failed", "cancelled", "pending", "running", "completing", "configuring", "failed", "node_fail", "preempted", "resizing", "reverting", "signaling", "special_exit", "stage_out", "stopped", "suspended", "timeout"):
            return state
    ok2, out2 = _run_ssh(f"scontrol show job {slurm_job_id} 2>/dev/null | grep -oP 'JobState=\\K[A-Z_]+'")
    if ok2 and out2:
        return out2.strip().lower()
    return "unknown"


# ---------- Terminal session management ----------
_active_terminal_sessions: dict[int, TerminalSession] = {}


def _get_terminal_session(reservation_id: int) -> TerminalSession | None:
    return _active_terminal_sessions.get(reservation_id)


def _cancel_slurm_job_if_exists(reservation_id: int):
    try:
        if db is not None:
            res = db.query(Reservation).filter(Reservation.id == reservation_id).first()
            if res and res.slurm_job_id:
                from slurm_client import cancel_slurm_job
                cancel_slurm_job(res.slurm_job_id)
    except Exception as e:
        logger.warning(f"Could not cancel Slurm job for reservation {reservation_id}: {e}")


def _remove_terminal_container(reservation_id: int):
    container_name = f"terminal_{reservation_id}"
    try:
        _run_ssh(f"docker rm -f {container_name} 2>/dev/null || true")
    except Exception as e:
        logger.warning(f"Could not remove docker container {container_name}: {e}")


def _mark_reservation_completed(reservation_id: int, db: Session = None):
    if db is not None:
        _release_resources(db, reservation_id)
    try:
        res = db.query(Reservation).filter(Reservation.id == reservation_id).first() if db else None
        if res and res.status == "running":
            res.status = "completed"
            res.session_expires_at = datetime.utcnow()
            try:
                res.terminal_pid = None
            except Exception as e:
                logger.warning(f"Could not clear terminal_pid for reservation {reservation_id}: {e}")
            if db:
                db.commit()
                db.refresh(res)
    except Exception as e:
        logger.error(f"Failed to update reservation {reservation_id} status during cleanup: {e}", exc_info=True)


def _cleanup_terminal_session(reservation_id: int, db: Session = None):
    print(f"[TRACE] _cleanup_terminal_session reservation_id={reservation_id}")
    session = _active_terminal_sessions.pop(reservation_id, None)
    if session:
        print(f"[TRACE] cleanup session found for {reservation_id}")
        try:
            session.cleanup()
        except Exception as e:
            logger.error(f"Failed to cleanup session for reservation {reservation_id}: {e}", exc_info=True)
    else:
        print(f"[TRACE] no active session for {reservation_id}, cleaning container directly")
        _cancel_slurm_job_if_exists(reservation_id)
        _remove_terminal_container(reservation_id)
    _mark_reservation_completed(reservation_id, db)


# ---------- Editor session management ----------
_active_editor_sessions: dict[int, "EditorSession"] = {}


def _get_editor_session(reservation_id: int) -> "EditorSession | None":
    return _active_editor_sessions.get(reservation_id)


def _cancel_editor_slurm_job_if_needed(reservation_id: int, db: Session = None):
    try:
        if db is not None:
            res = db.query(Reservation).filter(Reservation.id == reservation_id).first()
            if res and res.slurm_job_id:
                from slurm_client import cancel_slurm_job
                cancel_slurm_job(res.slurm_job_id)
    except Exception as e:
        logger.warning(f"Could not cancel Slurm job for reservation {reservation_id}: {e}")


def _remove_editor_container(reservation_id: int):
    container_name = f"editor_{reservation_id}"
    try:
        _run_ssh(f"docker rm -f {container_name} 2>/dev/null || true")
    except Exception as e:
        logger.warning(f"Could not remove docker container {container_name}: {e}")


def _cleanup_editor_session(reservation_id: int, db: Session = None):
    session = _active_editor_sessions.pop(reservation_id, None)
    if session:
        try:
            session.cleanup()
        except Exception as e:
            logger.error(f"Failed to cleanup editor session for reservation {reservation_id}: {e}", exc_info=True)
    else:
        _cancel_editor_slurm_job_if_needed(reservation_id, db)
        _remove_editor_container(reservation_id)
    if db is not None:
        _release_resources(db, reservation_id)
    try:
        res = db.query(Reservation).filter(Reservation.id == reservation_id).first() if db else None
        if res and res.status == "running":
            res.status = "completed"
            res.session_expires_at = datetime.utcnow()
            db.commit()
            db.refresh(res)
    except Exception as e:
        logger.error(f"Failed to update reservation {reservation_id} status during editor cleanup: {e}", exc_info=True)


def _cleanup_orphaned_editor_containers(db: Session = None):
    try:
        ok, out = _run_ssh("docker ps --filter label=editor_session --format '{{.Names}} {{.ID}}' 2>/dev/null || true")
        if not ok or not out:
            return
        for line in out.splitlines():
            parts = line.strip().split()
            if not parts:
                continue
            container_name = parts[0]
            try:
                reservation_id = int(container_name.split("_")[-1])
            except (ValueError, IndexError):
                continue
            session = _active_editor_sessions.get(reservation_id)
            if session and session.active:
                continue
            _cleanup_editor_session(reservation_id, db)
    except Exception as e:
        logger.error(f"Failed to cleanup orphaned editor containers: {e}", exc_info=True)


def _editor_is_active(reservation: Reservation) -> bool:
    if not reservation.session_expires_at:
        return False
    now = datetime.utcnow()
    if now > reservation.session_expires_at:
        return False
    session = _get_editor_session(reservation.id)
    if session and session.active:
        return True
    container_name = f"editor_{reservation.id}"
    ok, out = _run_ssh(f"docker ps --filter name={container_name} --format '{{{{.Names}}}}' 2>/dev/null || true")
    if ok and container_name in out:
        return True
    if reservation.slurm_job_id:
        ok2, out2 = _run_ssh(f"sacct -j {reservation.slurm_job_id} --noheader --format=State 2>/dev/null || true")
        if ok2 and out2.strip():
            state = out2.strip().split()[0].lower()
            if state in ("pending", "running", "configuring", "completing"):
                return True
    return False


# ---------- Terminal session management ----------
def _terminal_is_active(reservation: Reservation) -> bool:
    if not reservation.session_expires_at:
        return False
    now = datetime.utcnow()
    if now > reservation.session_expires_at:
        return False
    session = _get_terminal_session(reservation.id)
    if session and session.active:
        return True
    container_name = f"terminal_{reservation.id}"
    ok, out = _run_ssh(f"docker ps --filter name={container_name} --format '{{{{.Names}}}}' 2>/dev/null || true")
    if ok and container_name in out:
        return True
    if reservation.slurm_job_id:
        ok2, out2 = _run_ssh(f"sacct -j {reservation.slurm_job_id} --noheader --format=State 2>/dev/null || true")
        if ok2 and out2.strip():
            state = out2.strip().split()[0].lower()
            if state in ("pending", "running", "configuring", "completing"):
                return True
    return False


# ---------- Terminal endpoints ----------
@app.get("/terminal/{reservation_id}", response_class=HTMLResponse)
async def terminal_page(request: Request, reservation_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not reservation or reservation.user_id != user.id:
        raise HTTPException(404)
    if reservation.mode != "terminal":
        raise HTTPException(400, "This reservation is not a terminal session")
    now = datetime.utcnow()
    started = now < reservation.session_expires_at if reservation.session_expires_at else True
    remaining = max(0, int((reservation.session_expires_at - now).total_seconds())) if reservation.session_expires_at else 0
    active = _terminal_is_active(reservation)
    print(f"[TRACE] terminal_page reservation_id={reservation_id} session_expires_at={reservation.session_expires_at} now={now} remaining={remaining} active={active}")
    return templates.TemplateResponse(request, "terminal.html", {
        "request": request,
        "user": user,
        "reservation": reservation,
        "started": started,
        "remaining_seconds": remaining,
        "active": active,
    })


@app.post("/terminal/{reservation_id}/start")
async def terminal_start(reservation_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user), csrf: None = Depends(csrf_protect)):
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not reservation or reservation.user_id != user.id:
        raise HTTPException(404)
    if reservation.mode != "terminal":
        raise HTTPException(400, MSG_NOT_A_TERMINAL_SESSION)
    if reservation.status not in ("approved", "running"):
        raise HTTPException(400, "Terminal session not approved yet")

    now = datetime.utcnow()
    if reservation.session_expires_at and now > reservation.session_expires_at:
        raise HTTPException(403, "Session expired")

    if reservation_id in _active_terminal_sessions:
        _cleanup_terminal_session(reservation_id, db)

    session = TerminalSession(
        reservation_id=reservation_id,
        cpu=reservation.cpu,
        ram_gb=reservation.ram,
        duration_hours=reservation.duration,
        terminal_type=reservation.language,
    )
    result = session.start_slurm_allocation()
    print(f"[TRACE] terminal_start reservation_id={reservation_id} result={result}")
    if not result.get("success"):
        reservation.output = f"TERMINAL_START_ERROR: {result.get('error', 'Unknown error')}"
        if session.job_id:
            reservation.slurm_job_id = session.job_id
        _release_resources(db, reservation.id)
        db.commit()
        db.refresh(reservation)
        return RedirectResponse(url=f"/terminal/{reservation_id}?error=1", status_code=302)

    _active_terminal_sessions[reservation_id] = session
    reservation.status = "running"
    reservation.slurm_job_id = session.job_id
    reservation.session_expires_at = datetime.utcnow() + timedelta(minutes=Config.TERMINAL_SESSION_MINUTES)
    db.commit()
    db.refresh(reservation)
    return RedirectResponse(url=f"/terminal/{reservation_id}", status_code=302)


def _validate_terminal_stop(reservation: Reservation, user: User) -> None:
    if not reservation or reservation.user_id != user.id:
        raise HTTPException(404)
    if reservation.mode != "terminal":
        raise HTTPException(400, MSG_NOT_A_TERMINAL_SESSION)


def _finalize_terminal_stop(reservation_id: int, db: Session):
    _cleanup_terminal_session(reservation_id, db)
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    reservation.status = "completed"
    reservation.session_expires_at = datetime.utcnow()
    try:
        reservation.terminal_pid = None
    except Exception as e:
        logger.warning(f"Could not clear terminal_pid for reservation {reservation_id}: {e}")
    db.commit()
    db.refresh(reservation)


@app.post("/terminal/{reservation_id}/stop")
async def terminal_stop(reservation_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user), csrf: None = Depends(csrf_protect)):
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    _validate_terminal_stop(reservation, user)
    _finalize_terminal_stop(reservation_id, db)
    return RedirectResponse(url=PATH_DASHBOARD, status_code=302)


@app.get("/terminal/{reservation_id}/stop")
async def terminal_stop_get(reservation_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    print(f"[TRACE] terminal_stop GET reservation_id={reservation_id}")
    return await terminal_stop(reservation_id, db, user)


def _authenticate_websocket_user(websocket: WebSocket) -> User | None:
    token = websocket.cookies.get("access_token")
    if not token:
        return None
    payload = decode_jwt(token)
    if not payload:
        return None
    user_id = payload.get("user_id")
    if not user_id:
        return None
    db = SessionLocal()
    try:
        return db.query(User).filter(User.id == user_id).first()
    finally:
        db.close()


def _get_reservation_for_websocket(reservation_id: int, user: User) -> Reservation | None:
    db = SessionLocal()
    try:
        reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
        if not reservation or reservation.user_id != user.id:
            return None
        return reservation
    finally:
        db.close()


def _connect_terminal_session(reservation_id: int, reservation: Reservation) -> TerminalSession | None:
    if reservation.status not in ("approved", "running"):
        return None
    new_session = TerminalSession(
        reservation_id=reservation_id,
        cpu=reservation.cpu,
        ram_gb=reservation.ram,
        duration_hours=reservation.duration,
        terminal_type=reservation.language,
        job_id=reservation.slurm_job_id,
    )
    result = new_session.connect_to_existing()
    if result.get("success"):
        _active_terminal_sessions[reservation_id] = new_session
        return new_session
    return None


async def _read_terminal_channel(websocket: WebSocket, session: TerminalSession, reservation_id: int):
    while session.active:
        try:
            data = session.read_output()
            if data:
                await websocket.send_json({"output": data})
        except Exception as e:
            logger.error(f"Error reading from terminal channel for reservation {reservation_id}: {e}")
            break
        await asyncio.sleep(0.05)


async def _read_terminal_websocket(websocket: WebSocket, session: TerminalSession, reservation_id: int):
    while True:
        try:
            data = await websocket.receive_text()
            if session and session.active:
                session.send_input(data)
        except WebSocketDisconnect:
            break
        except Exception as e:
            logger.error(f"Error reading from websocket for reservation {reservation_id}: {e}")
            break


async def _run_terminal_io(websocket: WebSocket, session: TerminalSession, reservation_id: int):
    reader_task = asyncio.create_task(_read_terminal_channel(websocket, session, reservation_id))
    writer_task = asyncio.create_task(_read_terminal_websocket(websocket, session, reservation_id))

    done, pending = await asyncio.wait(
        [reader_task, writer_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()


@app.websocket("/terminal/{reservation_id}/ws")
async def terminal_ws(websocket: WebSocket, reservation_id: int):
    user = _authenticate_websocket_user(websocket)
    if not user:
        await websocket.close(code=4001)
        return

    reservation = _get_reservation_for_websocket(reservation_id, user)
    if not reservation:
        await websocket.close(code=4003)
        return

    await websocket.accept()
    session = _get_terminal_session(reservation_id)

    if not session or not session.active:
        db_local = SessionLocal()
        try:
            reservation = db_local.query(Reservation).filter(Reservation.id == reservation_id).first()
            session = _connect_terminal_session(reservation_id, reservation)
            if not session:
                status = reservation.status if reservation else "None"
                print(f"[TRACE] ws_no_active_session reservation_id={reservation_id} status={status}")
                await websocket.send_json({"error": "No active terminal session"})
                await websocket.close()
                return
        except Exception as e:
            print(f"[TRACE] ws_exception reservation_id={reservation_id} error={e}")
            logger.error(f"WebSocket exception for reservation {reservation_id}: {e}", exc_info=True)
            await websocket.close()
            return
        finally:
            db_local.close()

    try:
        await websocket.send_json({"status": "connected"})
    except Exception:
        await websocket.close()
        return

    await _run_terminal_io(websocket, session, reservation_id)

    if session and session.active:
        _cleanup_terminal_session(reservation_id)
    try:
        await websocket.send_json({"status": "closed", "reason": "Session ended"})
    except Exception as e:
        logger.warning(f"Could not send close message for reservation {reservation_id}: {e}")

    await websocket.close()


def _compute_terminal_status(reservation: Reservation, session) -> tuple[str, bool]:
    active = _terminal_is_active(reservation)
    remaining = max(0, int((reservation.session_expires_at - datetime.utcnow()).total_seconds())) if reservation.session_expires_at else 0
    from sanitize import sanitize_code_output
    output = sanitize_code_output(session.get_output() if session else reservation.output or "")

    if session and session.is_expired():
        _cleanup_terminal_session(reservation.id)
        reservation.status = "completed"
        reservation.session_expires_at = datetime.utcnow()
        try:
            reservation.terminal_pid = None
        except Exception as e:
            logger.warning(f"Could not clear terminal_pid for reservation {reservation.id}: {e}")
        db.commit()
        db.refresh(reservation)
        active = False

    status = reservation.status
    if active and status != "running":
        status = "running"
    elif not active and status == "running":
        status = "completed"

    return status, active, remaining, output


@app.get("/terminal/{reservation_id}/status")
async def terminal_status(reservation_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not reservation or reservation.user_id != user.id:
        raise HTTPException(404)
    if reservation.mode != "terminal":
        raise HTTPException(400, MSG_NOT_A_TERMINAL_SESSION)

    session = _get_terminal_session(reservation_id)
    status, active, remaining, output = _compute_terminal_status(reservation, session)

    return JSONResponse({
        "status": status,
        "active": active,
        "remaining_seconds": remaining,
        "output": output,
    })


def _validate_and_create_reservation(
    user: User,
    job_name: str,
    cpu: int,
    ram: int,
    duration: int,
    language: str,
    mode: str,
    code: str,
    db: Session,
):
    from sanitize import validate_job_name
    enforce_resources(cpu, ram, duration)
    try:
        job_name = validate_job_name(job_name)
    except ValueError as e:
        return None, e
    reservation = Reservation(
        user_id=user.id,
        job_name=job_name,
        cpu=cpu,
        ram=ram,
        duration=duration,
        script=code if mode == "batch" else "",
        status="pending",
        language=language,
        mode=mode,
        code=code,
    )
    db.add(reservation)
    db.commit()
    db.refresh(reservation)
    return reservation, None


@app.post("/terminal/request", response_class=HTMLResponse)
async def create_terminal_request(
    request: Request,
    job_name: str = Form(...),
    cpu: int = Form(...),
    ram: int = Form(...),
    duration: int = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    csrf: None = Depends(csrf_protect),
):
    reservation, error = _validate_and_create_reservation(
        user=user,
        job_name=job_name,
        cpu=cpu,
        ram=ram,
        duration=duration,
        language="docker",
        mode="terminal",
        code="",
        db=db,
    )
    if error:
        return RedirectResponse(url=PATH_DASHBOARD + "?error=" + str(error), status_code=302)
    return RedirectResponse(url=PATH_DASHBOARD, status_code=302)


# ---------- Admin endpoints ----------
@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    _release_all_orphaned_resources(db)
    pending = db.query(Reservation).filter(Reservation.status == "pending").all()
    queued = db.query(Reservation).filter(Reservation.status == "queued").order_by(Reservation.created_at.asc()).all()
    running = db.query(Reservation).filter(Reservation.status == "running").all()
    health = get_health()
    resources = _get_or_create_cluster_resources(db)
    _process_queue(db)
    return templates.TemplateResponse(request, "admin_panel.html", {
        "request": request,
        "pending": pending,
        "queued": queued,
        "running": running,
        "user": user,
        "services": health["services"],
        "overall_up": health["overall_up"],
        "downtime_message": format_downtime(get_downtime()),
        "resources": resources,
    })


@app.post("/admin/reset-resources")
async def reset_resources(db: Session = Depends(get_db), user: User = Depends(require_admin), csrf: None = Depends(csrf_protect)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    db.query(ResourceAllocation).delete()
    resources = _get_or_create_cluster_resources(db)
    resources.free_cpu = Config.MAX_CPU
    resources.free_ram_gb = Config.MAX_RAM_GB
    db.commit()
    db.refresh(resources)
    return RedirectResponse(url=PATH_ADMIN, status_code=302)


def _approve_editor_reservation(db: Session, reservation: Reservation, reservation_id: int):
    if not _allocate_resources(db, reservation.id, reservation.cpu, reservation.ram):
        reservation.status = "queued"
        reservation.output = "Insufficient free resources at approval time. Editor queued and will run when resources become available."
        db.commit()
        db.refresh(reservation)
        return RedirectResponse(url=f"{PATH_ADMIN}?warning=insufficient_resources&id={reservation_id}", status_code=302)
    from datetime import datetime, timedelta
    reservation.session_expires_at = datetime.utcnow() + timedelta(minutes=Config.EDITOR_SESSION_MINUTES)
    reservation.status = "approved"
    db.commit()
    db.refresh(reservation)
    return RedirectResponse(url=PATH_ADMIN, status_code=302)


def _approve_terminal_reservation(db: Session, reservation: Reservation, reservation_id: int):
    if not _allocate_resources(db, reservation.id, reservation.cpu, reservation.ram):
        reservation.status = "queued"
        reservation.output = "Insufficient free resources at approval time. Terminal queued and will run when resources become available."
        db.commit()
        db.refresh(reservation)
        return RedirectResponse(url=f"{PATH_ADMIN}?warning=insufficient_resources&id={reservation_id}", status_code=302)
    from datetime import datetime, timedelta
    reservation.session_expires_at = datetime.utcnow() + timedelta(minutes=Config.EDITOR_SESSION_MINUTES)
    reservation.status = "approved"
    db.commit()
    db.refresh(reservation)
    return RedirectResponse(url=PATH_ADMIN, status_code=302)


def _approve_batch_reservation(db: Session, reservation: Reservation):
    from code_runner import run_code
    result = run_code(reservation.language, reservation.code or reservation.script, reservation.cpu, reservation.ram, reservation.duration)
    reservation.slurm_job_id = result.get("job_id")
    if result.get("success"):
        reservation.status = "running"
    else:
        reservation.status = "failed"
        _release_resources(db, reservation.id)
    raw_output = result.get("output") or result.get("error") or "Job submitted"
    if reservation.slurm_job_id and reservation.status == "running":
        actual = read_slurm_output(reservation.slurm_job_id)
        reservation.output = actual or raw_output
    else:
        reservation.output = raw_output
    db.commit()
    db.refresh(reservation)
    return RedirectResponse(url=PATH_ADMIN, status_code=302)


@app.post("/admin/approve/{reservation_id}")
async def approve_reservation(
    reservation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
    csrf: None = Depends(csrf_protect),
):
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not reservation:
        raise HTTPException(status_code=404, detail=MSG_RESERVATION_NOT_FOUND)

    if reservation.mode == "editor":
        return _approve_editor_reservation(db, reservation, reservation_id)
    if reservation.mode == "terminal":
        return _approve_terminal_reservation(db, reservation, reservation_id)
    return _approve_batch_reservation(db, reservation)


@app.get("/debug/resources")
async def debug_resources(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    resources = _get_or_create_cluster_resources(db)
    return JSONResponse({
        "total_cpu": resources.total_cpu,
        "free_cpu": resources.free_cpu,
        "total_ram_gb": resources.total_ram_gb,
        "free_ram_gb": resources.free_ram_gb,
    })


@app.post("/admin/reject/{reservation_id}")
async def reject_reservation(
    reservation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
    csrf: None = Depends(csrf_protect),
):
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not reservation:
        raise HTTPException(status_code=404, detail=MSG_RESERVATION_NOT_FOUND)
    reservation.status = "rejected"
    db.commit()
    return RedirectResponse(url=PATH_ADMIN, status_code=302)
