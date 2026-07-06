import socket
import paramiko
from datetime import datetime, timedelta
from typing import Dict, Optional

from config import Config


_health_cache: Dict = {}
_cache_expires_at: Optional[datetime] = None
CACHE_TTL = timedelta(seconds=Config.VM_HEALTH_CACHE_SECONDS)

_first_failure: Optional[datetime] = None


def _is_cache_valid() -> bool:
    return _cache_expires_at is not None and datetime.utcnow() < _cache_expires_at


def _ssh_client() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=Config.VM_HOST,
        port=Config.VM_SSH_PORT,
        username=Config.VM_SSH_USER,
        key_filename=Config.VM_SSH_KEY_PATH,
        passphrase=Config.VM_SSH_PASSPHRASE or None,
        timeout=5,
    )
    return client


def _run_ssh(command: str) -> tuple[bool, str]:
    try:
        client = _ssh_client()
        stdin, stdout, stderr = client.exec_command(command, timeout=10)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        client.close()
        return True, out.strip()
    except Exception as e:
        return False, str(e)


def _tcp_check(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _check_service_active(systemctl_name: str) -> Dict:
    success, output = _run_ssh(f"systemctl is-active {systemctl_name} || true")
    is_active = success and output == "active"
    return {
        "name": systemctl_name,
        "type": "systemd",
        "up": is_active,
        "raw": output,
    }


def _check_container(container_name: str) -> Dict:
    success, output = _run_ssh(
        f"docker ps --filter name={container_name} --format '{{{{.Names}}}}' || true"
    )
    found = success and container_name in output
    return {
        "name": container_name,
        "type": "docker",
        "up": found,
        "raw": output,
    }


def _check_tcp(name: str, host: str, port: int) -> Dict:
    up = _tcp_check(host, port)
    return {
        "name": name,
        "type": "tcp",
        "up": up,
        "raw": f"{host}:{port} {'open' if up else 'closed'}",
    }


def run_health_check() -> Dict:
    global _cache_expires_at, _first_failure, _health_cache

    if _is_cache_valid():
        return _health_cache

    services = []

    ssh_ok = True
    try:
        client = _ssh_client()
        client.close()
    except Exception:
        ssh_ok = False

    if not ssh_ok:
        services = [
            {"name": "vm_ssh", "type": "ssh", "up": False, "raw": "SSH connection failed"},
            {"name": "slurmrestd", "type": "systemd", "up": False, "raw": "SSH failed"},
            {"name": "slurmctld", "type": "systemd", "up": False, "raw": "SSH failed"},
            {"name": "slurmd", "type": "systemd", "up": False, "raw": "SSH failed"},
            {"name": "munge", "type": "systemd", "up": False, "raw": "SSH failed"},
            {"name": "app-mysql", "type": "docker", "up": False, "raw": "SSH failed"},
            {"name": "ldap-server", "type": "docker", "up": False, "raw": "SSH failed"},
            _check_tcp("ldap", Config.LDAP_HOST, Config.LDAP_PORT),
            _check_tcp("mysql", Config.DB_HOST, Config.DB_PORT),
            _check_tcp("slurm_rest", Config.VM_HOST, 6820),
        ]
        _first_failure = _first_failure or datetime.utcnow()
        _health_cache = {"services": services, "overall_up": False}
        _cache_expires_at = datetime.utcnow() + CACHE_TTL
        return _health_cache

    services.extend([
        _check_service_active("slurmrestd.service"),
        _check_service_active("slurmctld.service"),
        _check_service_active("slurmd.service"),
        _check_service_active("munge.service"),
    ])
    services.append(_check_container("app-mysql"))
    services.append(_check_container("ldap-server"))
    services.append(_check_tcp("ldap", Config.LDAP_HOST, Config.LDAP_PORT))
    services.append(_check_tcp("mysql", Config.DB_HOST, Config.DB_PORT))
    services.append(_check_tcp("slurm_rest", Config.VM_HOST, 6820))

    overall_up = all(s["up"] for s in services)

    if overall_up:
        _first_failure = None
    else:
        _first_failure = _first_failure or datetime.utcnow()

    _health_cache = {"services": services, "overall_up": overall_up}
    _cache_expires_at = datetime.utcnow() + CACHE_TTL
    return _health_cache


def get_health() -> Dict:
    return run_health_check()


def get_downtime() -> Optional[timedelta]:
    if _first_failure is None:
        return None
    return datetime.utcnow() - _first_failure


def format_downtime(delta: Optional[timedelta]) -> str:
    if delta is None:
        return "Server is up and running."
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return (
        f"Server has been down for "
        f"{hours}h {minutes}m {seconds}s "
        f"(since {_first_failure.strftime('%Y-%m-%d %H:%M:%S UTC')})."
    )
