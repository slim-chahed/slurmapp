import paramiko
import os
import logging
from config import Config

logger = logging.getLogger(__name__)


def _ssh_client():
    client = paramiko.SSHClient()
    known_hosts = os.path.expanduser("~/.ssh/known_hosts")
    if os.path.exists(known_hosts):
        client.load_host_keys(known_hosts)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(
        Config.VM_HOST,
        username=Config.VM_SSH_USER,
        key_filename=Config.VM_SSH_KEY_PATH,
        passphrase=Config.VM_SSH_PASSPHRASE or None,
        timeout=5,
    )
    return client


def _ssh(cmd: str) -> tuple[bool, str]:
    try:
        client = _ssh_client()
        stdin, stdout, stderr = client.exec_command(cmd, timeout=10)  # nosec B601 - cmd is internal/hardcoded
        out = stdout.read().decode("utf-8", errors="replace").strip()
        client.close()
        return True, out
    except Exception as e:
        logger.error(f"SSH command failed: {cmd!r}: {e}")
        return False, str(e)


def _parse_cpu_value(part: str) -> int | None:
    try:
        return int(part.split("=")[1].split(" ")[0])
    except (ValueError, IndexError) as e:
        logger.warning(f"Could not parse CPU value from {part!r}: {e}")
        return None


def _parse_node_part(part: str) -> tuple[int | None, int | None, float | None]:
    total_cpu = None
    alloc_cpu = None
    total_ram_gb = None

    if part.startswith("CPUs=") or part.startswith("CPUTot="):
        total_cpu = _parse_cpu_value(part)
    if part.startswith("CPUAlloc="):
        alloc_cpu = _parse_cpu_value(part)
    if part.startswith("RealMemory="):
        try:
            total_ram_gb = int(part.split("=")[1].split(" ")[0]) / 1024
        except (ValueError, IndexError) as e:
            logger.warning(f"Could not parse RealMemory value from {part!r}: {e}")

    return total_cpu, alloc_cpu, total_ram_gb


def _parse_free_memory_line(line: str) -> tuple[float | None, float | None]:
    parts = line.split()
    if len(parts) < 7:
        return None, None
    try:
        total_mb = int(parts[1])
        available_mb = int(parts[6])
        return round(total_mb / 1024, 1), round(available_mb / 1024, 1)
    except (ValueError, IndexError) as e:
        logger.warning(f"Could not parse memory info: {e}")
        return None, None


def _parse_free_memory_output(out_mem: str) -> tuple[float, float]:
    total_ram_gb = 0.0
    free_ram_gb = 0.0
    lines = out_mem.splitlines()
    for line in lines:
        if line.startswith("Mem:"):
            total_ram_gb, free_ram_gb = _parse_free_memory_line(line)
            break
    return total_ram_gb, free_ram_gb


def _build_resource_result(total_cpu: int, free_cpu: int, total_ram_gb: float, free_ram_gb: float, raw: str, ok: bool = True) -> dict:
    return {
        "total_cpu": total_cpu,
        "free_cpu": free_cpu,
        "total_ram_gb": round(total_ram_gb, 1),
        "free_ram_gb": round(free_ram_gb, 1),
        "ok": ok,
        "raw": raw,
    }


def get_node_resources() -> dict:
    ok, out = _ssh("scontrol show nodes 2>/dev/null || sinfo -N -l 2>/dev/null")
    if not ok:
        return _build_resource_result(
            total_cpu=Config.MAX_CPU,
            free_cpu=Config.MAX_CPU,
            total_ram_gb=Config.MAX_RAM_GB,
            free_ram_gb=Config.MAX_RAM_GB,
            raw=out,
            ok=False,
        )

    total_cpu = Config.MAX_CPU
    total_ram_gb = Config.MAX_RAM_GB
    alloc_cpu = 0

    for part in out.replace("\n", " ").split(" "):
        parsed_cpu, parsed_alloc, parsed_ram = _parse_node_part(part)
        if parsed_cpu is not None:
            total_cpu = parsed_cpu
        if parsed_alloc is not None:
            alloc_cpu = parsed_alloc
        if parsed_ram is not None:
            total_ram_gb = parsed_ram

    free_cpu = max(0, total_cpu - alloc_cpu)

    ok_mem, out_mem = _ssh("free -m 2>/dev/null || free 2>/dev/null || true")
    free_ram_gb = round(total_ram_gb, 1)
    if ok_mem and out_mem:
        parsed_total_ram, parsed_free_ram = _parse_free_memory_output(out_mem)
        if parsed_total_ram:
            total_ram_gb = parsed_total_ram
        if parsed_free_ram:
            free_ram_gb = parsed_free_ram

    result = _build_resource_result(total_cpu, free_cpu, total_ram_gb, free_ram_gb, out)
    return result
