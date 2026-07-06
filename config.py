import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Database
    DB_HOST = os.getenv("DB_HOST", "192.168.74.171")
    DB_PORT = int(os.getenv("DB_PORT", 3306))
    DB_USER = os.getenv("DB_USER", "appuser")
    DB_PASS = os.getenv("DB_PASS", "apppass")
    DB_NAME = os.getenv("DB_NAME", "reservations")
    DATABASE_URL = f"mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

    # Redis
    REDIS_HOST = os.getenv("REDIS_HOST", "192.168.74.171")
    REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
    REDIS_DB = int(os.getenv("REDIS_DB", 0))

    # LDAP
    LDAP_HOST = os.getenv("LDAP_HOST", "192.168.74.171")
    LDAP_PORT = int(os.getenv("LDAP_PORT", 389))
    LDAP_BASE_DN = os.getenv("LDAP_BASE_DN", "dc=mylab,dc=local")
    LDAP_BIND_DN = os.getenv("LDAP_BIND_DN", "cn=admin,dc=mylab,dc=local")
    LDAP_BIND_PASSWORD = os.getenv("LDAP_BIND_PASSWORD", "admin123")
    LDAP_USER_FILTER = os.getenv("LDAP_USER_FILTER", "(uid={username})")

    # JWT
    JWT_SECRET = os.getenv("JWT_SECRET", "supersecretkey")
    JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

    # Slurm
    SLURM_REST_URL = os.getenv("SLURM_REST_URL", "http://192.168.74.171:6820")
    SLURM_SERVICE_USER = os.getenv("SLURM_SERVICE_USER", "webapp")
    SLURM_JWT_TOKEN = os.getenv("SLURM_JWT_TOKEN", "")