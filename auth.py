import jwt
import ldap3
from fastapi import HTTPException, Request, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from config import Config
from models import User
from database import SessionLocal, get_db

# ---------- LDAP Authentication ----------
def authenticate_ldap(username: str, password: str) -> bool:
    """Bind to LDAP with given credentials."""
    server = ldap3.Server(Config.LDAP_HOST, port=Config.LDAP_PORT, get_info=ldap3.ALL)
    try:
        bind_conn = ldap3.Connection(
            server,
            user=Config.LDAP_BIND_DN,
            password=Config.LDAP_BIND_PASSWORD,
            auto_bind=True,
        )
        search_filter = Config.LDAP_USER_FILTER.format(username=username)
        search_base = Config.LDAP_BASE_DN
        search_result = bind_conn.search(
            search_base,
            search_filter,
            attributes=["uid", "cn", "sn", "mail"],
        )
        if not search_result or not bind_conn.entries:
            return False

        user_dn = None
        if hasattr(bind_conn.entries[0], "entry_dn") and bind_conn.entries[0].entry_dn:
            user_dn = bind_conn.entries[0].entry_dn
        elif hasattr(bind_conn.entries[0], "dn") and bind_conn.entries[0].dn:
            user_dn = bind_conn.entries[0].dn
        else:
            user_dn = f"uid={username},{Config.LDAP_BASE_DN}"

        user_conn = ldap3.Connection(server, user=user_dn, password=password, auto_bind=True)
        return bool(user_conn.bound)
    except Exception:
        return False

def get_or_create_user(db: Session, username: str) -> User:
    user = db.query(User).filter(User.username == username).first()
    if not user:
        # If username is 'admin', assign admin role
        role = "admin" if username.lower() == "admin" else "user"
        user = User(username=username, role=role)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user

# ---------- JWT ----------
def create_jwt(user_id: int, role: str) -> str:
    payload = {
        "user_id": user_id,
        "role": role,
        "exp": datetime.utcnow() + timedelta(hours=24)
    }
    return jwt.encode(payload, Config.JWT_SECRET, algorithm=Config.JWT_ALGORITHM)

def decode_jwt(token: str) -> dict:
    """Decode JWT with strict algorithm whitelist."""
    try:
        payload = jwt.decode(token, Config.JWT_SECRET, algorithms=["HS256"])
        return payload
    except jwt.InvalidTokenError:
        return None

# def get_current_user(request: Request, db: Session = Depends(SessionLocal)):
#     token = request.cookies.get("access_token")
#     if not token:
#         raise HTTPException(status_code=401, detail="Not authenticated")
#     payload = decode_jwt(token)
#     if not payload:
#         raise HTTPException(status_code=401, detail="Invalid token")
#     user_id = payload.get("user_id")
#     if not user_id:
#         raise HTTPException(status_code=401, detail="Invalid token payload")
#     user = db.query(User).filter(User.id == user_id).first()
#     if not user:
#         raise HTTPException(status_code=401, detail="User not found")
#     return user
def get_current_user(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated please re login")

    payload = decode_jwt(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")

    user_id = payload.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return user

# Helper to get current user without raising (for optional checks)
# def get_current_user_optional(request: Request, db: Session = Depends(SessionLocal)):
#     token = request.cookies.get("access_token")
#     if not token:
#         return None
#     payload = decode_jwt(token)
#     if not payload:
#         return None
#     user_id = payload.get("user_id")
#     if not user_id:
#         return None
#     return db.query(User).filter(User.id == user_id).first()

def get_current_user_optional(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("access_token")
    if not token:
        return None

    payload = decode_jwt(token)
    if not payload:
        return None

    user_id = payload.get("user_id")
    if not user_id:
        return None

    return db.query(User).filter(User.id == user_id).first()


def require_admin(user: User = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user