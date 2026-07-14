from fastapi import FastAPI, Depends, HTTPException, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy.sql import func
from datetime import datetime, timedelta
import json

from database import engine, SessionLocal, get_db, get_raw_connection
from models import Base, User, Reservation, ClusterResources, ResourceAllocation
from auth import authenticate_ldap, get_or_create_user, create_jwt, get_current_user, get_current_user_optional
from slurm_client import submit_slurm_job
from vm_monitor import get_health, get_downtime, format_downtime, read_slurm_output
from slurm_resources import get_node_resources
from code_runner import run_code
from config import Config

Base.metadata.create_all(bind=engine)


def migrate_db():
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    dialect = engine.dialect.name
    if dialect == "sqlite":
        with engine.connect() as conn:
            cols = [c["name"] for c in insp.get_columns("reservations")]
            for col in ["language", "mode", "code", "session_expires_at", "slurm_allocation", "output"]:
                if col not in cols:
                    conn.execute(text(f"ALTER TABLE reservations ADD COLUMN {col} TEXT"))
            conn.commit()
        with engine.connect() as conn:
            tables = insp.get_table_names()
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
            conn.commit()
    elif dialect == "mysql":
        with engine.connect() as conn:
            cols = [c["name"] for c in insp.get_columns("reservations")]
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


def _process_queue(db: Session) -> None:
    queued = db.query(Reservation).filter(Reservation.status == "queued").order_by(Reservation.created_at.asc()).all()
    for reservation in queued:
        if _allocate_resources(db, reservation.id, reservation.cpu, reservation.ram):
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


app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.on_event("startup")
def startup():
    migrate_db()


PUBLIC_PATHS = {"/", "/login", "/system-down"}


class HealthGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path in PUBLIC_PATHS or path.startswith("/static"):
            return await call_next(request)

        health = get_health()
        if not health["overall_up"]:
            return RedirectResponse(url="/system-down", status_code=302)

        return await call_next(request)


app.add_middleware(HealthGateMiddleware)


def get_user_reservations(db: Session, user_id: int, search: str = None):
    if search:
        raw_conn = get_raw_connection()
        cursor = raw_conn.cursor()
        query = f"SELECT * FROM reservations WHERE user_id = {user_id} AND job_name LIKE '%{search}%'"
        cursor.execute(query)
        rows = cursor.fetchall()
        columns = ['id', 'user_id', 'job_name', 'cpu', 'ram', 'duration', 'script', 'status', 'slurm_job_id', 'created_at']
        result = [dict(zip(columns, row)) for row in rows]
        cursor.close()
        raw_conn.close()
        return result
    else:
        return db.query(Reservation).filter(Reservation.user_id == user_id).all()


def enforce_resources(cpu: int, ram: int, duration: int, db: Session = None):
    if cpu > Config.MAX_CPU or ram > Config.MAX_RAM_GB or duration > Config.MAX_WALLTIME_HOURS:
        raise HTTPException(400, f"Max allowed: {Config.MAX_CPU} CPU / {Config.MAX_RAM_GB} GB RAM / {Config.MAX_WALLTIME_HOURS}h")
    if db is None:
        db = SessionLocal()
    try:
        res = _get_or_create_cluster_resources(db)
        if cpu > res.free_cpu or ram > res.free_ram_gb:
            raise HTTPException(400, f"Insufficient free resources. Free: {res.free_cpu} CPU, {res.free_ram_gb} GB RAM")
    finally:
        db.close()


# ---------- Routes ----------
@app.get("/", response_class=HTMLResponse)
async def root(request: Request, user: User = Depends(get_current_user_optional)):
    if user:
        health = get_health()
        if not health["overall_up"]:
            return RedirectResponse(url="/system-down", status_code=302)
        return RedirectResponse(url="/dashboard", status_code=302)

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
async def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    if authenticate_ldap(username, password):
        user = get_or_create_user(db, username)
        token = create_jwt(user.id, user.role)
        health = get_health()
        target = "/system-down" if not health["overall_up"] else "/dashboard"
        response = RedirectResponse(url=target, status_code=302)
        response.set_cookie(key="access_token", value=token, httponly=False)
        return response
    else:
        return templates.TemplateResponse(request, "login.html", {"request": request, "error": "Invalid credentials"})


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, search: str = Query(None), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    reservations = get_user_reservations(db, user.id, search)
    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request,
        "reservations": reservations,
        "search": search,
        "user": user
    })


@app.get("/request", response_class=HTMLResponse)
async def request_form(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
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
):
    from code_runner import build_script
    import uuid
    enforce_resources(cpu, ram, duration, db)
    job_id = uuid.uuid4().hex[:8]
    script = build_script(language, code, cpu, ram, duration, job_id) if mode == "batch" else code
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

    if mode == "editor":
        from datetime import datetime, timedelta
        reservation.session_expires_at = datetime.utcnow() + timedelta(minutes=Config.EDITOR_SESSION_MINUTES)
        db.commit()
        db.refresh(reservation)

    return RedirectResponse(url="/dashboard", status_code=302)


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
        raise HTTPException(status_code=404, detail="Reservation not found")
    return templates.TemplateResponse(request, "reservation_detail.html", {
        "request": request,
        "reservation": reservation,
        "user": user
    })


@app.get("/editor/{reservation_id}", response_class=HTMLResponse)
async def editor_page(request: Request, reservation_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not reservation or reservation.user_id != user.id:
        raise HTTPException(404)
    if reservation.mode != "editor":
        raise HTTPException(400, "This reservation is not an editor session")
    now = datetime.utcnow()
    started = now < reservation.session_expires_at if reservation.session_expires_at else True
    remaining = max(0, int((reservation.session_expires_at - now).total_seconds())) if reservation.session_expires_at else 0
    return templates.TemplateResponse(request, "editor.html", {
        "request": request,
        "user": user,
        "reservation": reservation,
        "started": started,
        "remaining_seconds": remaining,
    })


@app.post("/editor/{reservation_id}/run", response_class=HTMLResponse)
async def editor_run(
    request: Request,
    reservation_id: int,
    code: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not reservation or reservation.user_id != user.id:
        raise HTTPException(404, "Not found")
    if reservation.mode != "editor":
        raise HTTPException(400, "Not an editor session")

    now = datetime.utcnow()
    if reservation.session_expires_at and now > reservation.session_expires_at:
        raise HTTPException(403, "Session expired")

    reservation.code = code or reservation.code
    db.commit()
    db.refresh(reservation)

    from code_runner import run_code
    result = run_code(reservation.language, reservation.code, reservation.cpu, reservation.ram, reservation.duration)
    reservation.slurm_job_id = result.get("job_id")
    if result.get("success"):
        reservation.status = "running"
    else:
        reservation.status = "failed"
    raw_output = result.get("output") or result.get("error") or "Job submitted"
    if reservation.slurm_job_id and reservation.status == "running":
        actual = read_slurm_output(reservation.slurm_job_id)
        reservation.output = actual or raw_output
    else:
        reservation.output = raw_output
    db.commit()
    db.refresh(reservation)

    return RedirectResponse(url=f"/editor/{reservation_id}", status_code=303)


@app.get("/editor/{reservation_id}/status")
async def editor_status(reservation_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not reservation or reservation.user_id != user.id:
        raise HTTPException(404)
    return _poll_job_status(reservation, db)


@app.get("/reservation/{reservation_id}/status")
async def reservation_status(reservation_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not reservation or reservation.user_id != user.id:
        raise HTTPException(404)
    return _poll_job_status(reservation, db)


def _poll_job_status(reservation: Reservation, db: Session) -> dict:
    data = {
        "status": reservation.status,
        "job_id": reservation.slurm_job_id,
        "output": reservation.output or "",
    }
    if not reservation.slurm_job_id:
        return data

    actual_status = _get_slurm_job_status(reservation.slurm_job_id)
    if actual_status in ("completed", "failed", "cancelled"):
        actual_output = read_slurm_output(reservation.slurm_job_id) or reservation.output or ""
        reservation.output = actual_output
        reservation.status = actual_status
        _release_resources(db, reservation.id)
        db.commit()
        db.refresh(reservation)
        _process_queue(db)
        data["status"] = reservation.status
        data["output"] = reservation.output or ""
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


# ---------- Admin endpoints ----------
@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    pending = db.query(Reservation).filter(Reservation.status == "pending").all()
    queued = db.query(Reservation).filter(Reservation.status == "queued").order_by(Reservation.created_at.asc()).all()
    health = get_health()
    resources = _get_or_create_cluster_resources(db)
    _process_queue(db)
    return templates.TemplateResponse(request, "admin_panel.html", {
        "request": request,
        "pending": pending,
        "queued": queued,
        "user": user,
        "services": health["services"],
        "overall_up": health["overall_up"],
        "downtime_message": format_downtime(get_downtime()),
        "resources": resources,
    })


@app.post("/admin/approve/{reservation_id}")
async def approve_reservation(
    reservation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not reservation:
        raise HTTPException(status_code=404, detail="Reservation not found")

    print(f"[APPROVE] id={reservation_id} mode={reservation.mode} before status={reservation.status}")
    if reservation.mode == "editor":
        from datetime import datetime, timedelta
        reservation.session_expires_at = datetime.utcnow() + timedelta(minutes=Config.EDITOR_SESSION_MINUTES)
        reservation.status = "approved"
        db.commit()
        db.refresh(reservation)
        print(f"[APPROVE] editor branch set status approved")
        return RedirectResponse(url="/admin", status_code=302)

    # batch mode: check static resources first, then run via code_runner
    if not _allocate_resources(db, reservation.id, reservation.cpu, reservation.ram):
        reservation.status = "queued"
        db.commit()
        db.refresh(reservation)
        print(f"[APPROVE] not enough resources, queued reservation_id={reservation_id}")
        return RedirectResponse(url="/admin", status_code=302)

    from code_runner import run_code
    print(f"[APPROVE] reservation_id={reservation_id} cpu={reservation.cpu} ram={reservation.ram} duration={reservation.duration} language={reservation.language}")
    result = run_code(reservation.language, reservation.code or reservation.script, reservation.cpu, reservation.ram, reservation.duration)
    print(f"[APPROVE] batch result={result}")
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
    print(f"[APPROVE] after commit status={reservation.status} slurm_job_id={reservation.slurm_job_id} output={reservation.output!r}")
    return RedirectResponse(url="/admin", status_code=302)


@app.get("/debug/resources")
async def debug_resources(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
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
    user: User = Depends(get_current_user)
):
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not reservation:
        raise HTTPException(status_code=404, detail="Reservation not found")
    reservation.status = "rejected"
    db.commit()
    return RedirectResponse(url="/admin", status_code=302)
