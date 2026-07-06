from fastapi import FastAPI, Depends, HTTPException, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from datetime import datetime
import json

from database import engine, SessionLocal, get_db, get_raw_connection
from models import Base, User, Reservation
from auth import authenticate_ldap, get_or_create_user, create_jwt, get_current_user, get_current_user_optional
from slurm_client import submit_slurm_job
from vm_monitor import get_health, get_downtime, format_downtime
from config import Config

Base.metadata.create_all(bind=engine)

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------- Middleware: block app use when services are down ----------
PUBLIC_PATHS = {"/", "/server-down"}


class HealthGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path.startswith("/static"):
            return await call_next(request)

        if path in PUBLIC_PATHS:
            return await call_next(request)

        health = get_health()
        if not health["overall_up"]:
            return RedirectResponse(url="/server-down", status_code=302)

        return await call_next(request)


app.add_middleware(HealthGateMiddleware)


# ---------- Helper Functions ----------
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


# ---------- Routes ----------
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    health = get_health()
    downtime = get_downtime()
    return templates.TemplateResponse(request, "landing.html", {
        "request": request,
        "overall_up": health["overall_up"],
        "downtime_message": format_downtime(downtime),
    })


@app.get("/server-down", response_class=HTMLResponse)
async def server_down(request: Request):
    health = get_health()
    downtime = get_downtime()
    return templates.TemplateResponse(request, "server_down.html", {
        "request": request,
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
        response = RedirectResponse(url="/dashboard", status_code=302)
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
async def request_form(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse(request, "request_form.html", {"request": request})


@app.post("/request")
async def create_reservation(
    request: Request,
    job_name: str = Form(...),
    cpu: int = Form(...),
    ram: int = Form(...),
    duration: int = Form(...),
    script: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    reservation = Reservation(
        user_id=user.id,
        job_name=job_name,
        cpu=cpu,
        ram=ram,
        duration=duration,
        script=script,
        status="pending"
    )
    db.add(reservation)
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
        "reservation": reservation
    })


# ---------- Admin endpoints ----------
@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    pending = db.query(Reservation).filter(Reservation.status == "pending").all()
    health = get_health()
    return templates.TemplateResponse(request, "admin_panel.html", {
        "request": request,
        "pending": pending,
        "services": health["services"],
        "overall_up": health["overall_up"],
        "downtime_message": format_downtime(get_downtime()),
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
    
    script_content = f"""#!/bin/bash
#SBATCH --job-name={reservation.job_name}
#SBATCH --cpus-per-task={reservation.cpu}
#SBATCH --mem={reservation.ram}G
#SBATCH --time={reservation.duration}:00:00

{reservation.script}
"""
    result = submit_slurm_job(script_content)
    if result["success"]:
        reservation.status = "approved"
        reservation.slurm_job_id = result["job_id"]
    else:
        reservation.status = "failed"
    db.commit()
    return RedirectResponse(url="/admin", status_code=302)


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
