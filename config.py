import os
import tempfile
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

class Config:
    # Database - defaults for lab environment, override via env vars in production
    DB_HOST = os.getenv("DB_HOST", "192.168.74.171")
    DB_PORT = int(os.getenv("DB_PORT", 3306))
    DB_USER = os.getenv("DB_USER", "appuser")
    DB_PASS = os.getenv("DB_PASS", "apppass")
    DB_NAME = os.getenv("DB_NAME", "reservations")
    DATABASE_URL = f"mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

    # Redis - defaults for lab environment, override via env vars in production
    REDIS_HOST = os.getenv("REDIS_HOST", "192.168.74.171")
    REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
    REDIS_DB = int(os.getenv("REDIS_DB", 0))

    # LDAP - defaults for lab environment, override via env vars in production
    LDAP_HOST = os.getenv("LDAP_HOST", "192.168.74.171")
    LDAP_PORT = int(os.getenv("LDAP_PORT", 389))
    LDAP_BASE_DN = os.getenv("LDAP_BASE_DN", "dc=mylab,dc=local")
    LDAP_BIND_DN = os.getenv("LDAP_BIND_DN", "cn=admin,dc=mylab,dc=local")
    LDAP_BIND_PASSWORD = os.getenv("LDAP_BIND_PASSWORD", "admin123")
    LDAP_USER_FILTER = os.getenv("LDAP_USER_FILTER", "(uid={username})")

    # JWT
    JWT_SECRET = os.getenv("JWT_SECRET")
    if not JWT_SECRET:
        _jwt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".jwt_secret")
        if os.path.exists(_jwt_path):
            try:
                with open(_jwt_path, "r") as f:
                    JWT_SECRET = f.read().strip()
            except Exception:
                JWT_SECRET = None
        if not JWT_SECRET:
            import secrets
            JWT_SECRET = secrets.token_hex(32)
            try:
                with open(_jwt_path, "w") as f:
                    f.write(JWT_SECRET)
            except Exception as e:
                logger.warning(f"Could not write JWT secret file: {e}")
    JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

    # Slurm - defaults for lab environment, override via env vars in production
    SLURM_REST_URL = os.getenv("SLURM_REST_URL", "http://192.168.74.171:6820")
    SLURM_SERVICE_USER = os.getenv("SLURM_SERVICE_USER", "webapp")
    SLURM_JWT_TOKEN = os.getenv("SLURM_JWT_TOKEN", "")

    # VM / SSH monitoring - defaults for lab environment, override via env vars in production
    VM_HOST = os.getenv("VM_HOST", "192.168.74.171")
    VM_SSH_PORT = int(os.getenv("VM_SSH_PORT", 22))
    VM_SSH_USER = os.getenv("VM_SSH_USER", "ubuntu")
    VM_SSH_KEY_PATH = os.getenv("VM_SSH_KEY_PATH", os.path.expanduser("~/.ssh/id_rsa"))
    VM_SSH_PASSPHRASE = os.getenv("VM_SSH_PASSPHRASE", "")
    VM_HEALTH_CACHE_SECONDS = int(os.getenv("VM_HEALTH_CACHE_SECONDS", 30))

    # Terminal restricted user
    TERMINAL_SSH_USER = os.getenv("TERMINAL_SSH_USER", "terminal_user")
    TERMINAL_SSH_KEY_PATH = os.getenv("TERMINAL_SSH_KEY_PATH", os.path.expanduser("~/.ssh/id_rsa_hpc"))
    TERMINAL_SSH_PASSPHRASE = os.getenv("TERMINAL_SSH_PASSPHRASE", "")
    TERMINAL_HOME = os.getenv("TERMINAL_HOME", "/home/terminal_user")
    _TERMINAL_DOCKER_TEMP = os.getenv("TERMINAL_DOCKER_TEMP")
    if _TERMINAL_DOCKER_TEMP:
        TERMINAL_DOCKER_TEMP = _TERMINAL_DOCKER_TEMP
    else:
        _tmp = tempfile.mkdtemp(prefix="terminal_workspace_")
        TERMINAL_DOCKER_TEMP = _tmp

    # Catalogue / Editor / Terminal limits
    MAX_CPU = int(os.getenv("MAX_CPU", 4))
    MAX_RAM_GB = int(os.getenv("MAX_RAM_GB", 4))
    MAX_WALLTIME_HOURS = int(os.getenv("MAX_WALLTIME_HOURS", 2))
    EDITOR_SESSION_MINUTES = int(os.getenv("EDITOR_SESSION_MINUTES", 60))
    TERMINAL_SESSION_MINUTES = int(os.getenv("TERMINAL_SESSION_MINUTES", 60))
    TERMINAL_DIND_IMAGE = os.getenv("TERMINAL_DIND_IMAGE", "dind-terminal")
