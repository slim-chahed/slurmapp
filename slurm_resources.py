import paramiko
from config import Config


def _ssh(cmd: str) -> tuple[bool, str]:
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            Config.VM_HOST,
            username=Config.VM_SSH_USER,
            key_filename=Config.VM_SSH_KEY_PATH,
            passphrase=Config.VM_SSH_PASSPHRASE or None,
            timeout=5,
        )
        _, stdout, _ = client.exec_command(cmd, timeout=10)
        out = stdout.read().decode("utf-8", errors="replace").strip()
        client.close()
        return True, out
    except Exception as e:
        return False, str(e)


def get_node_resources() -> dict:
    ok, out = _ssh("scontrol show nodes 2>/dev/null || sinfo -N -l 2>/dev/null")
    print(f"[slurm_resources] ssh_ok={ok} raw={out!r}")
    if not ok:
        return {
            "total_cpu": Config.MAX_CPU,
            "free_cpu": Config.MAX_CPU,
            "total_ram_gb": Config.MAX_RAM_GB,
            "free_ram_gb": Config.MAX_RAM_GB,
            "ok": False,
            "raw": out,
        }

    total_cpu = Config.MAX_CPU
    total_ram_gb = Config.MAX_RAM_GB
    alloc_cpu = 0

    for part in out.replace("\n", " ").split(" "):
        if part.startswith("CPUs=") or part.startswith("CPUTot="):
            try:
                total_cpu = int(part.split("=")[1].split(" ")[0])
            except Exception:
                pass
        if part.startswith("CPUAlloc="):
            try:
                alloc_cpu = int(part.split("=")[1].split(" ")[0])
            except Exception:
                pass
        if part.startswith("RealMemory="):
            try:
                total_ram_gb = int(part.split("=")[1].split(" ")[0]) / 1024
            except Exception:
                pass

    free_cpu = max(0, total_cpu - alloc_cpu)

    ok_mem, out_mem = _ssh("free -m 2>/dev/null || free 2>/dev/null || true")
    free_ram_gb = round(total_ram_gb, 1)
    if ok_mem and out_mem:
        lines = out_mem.splitlines()
        for line in lines:
            if line.startswith("Mem:"):
                parts = line.split()
                if len(parts) >= 7:
                    try:
                        total_mb = int(parts[1])
                        used_mb = int(parts[2])
                        free_mb = int(parts[3])
                        available_mb = int(parts[6])
                        total_ram_gb = round(total_mb / 1024, 1)
                        free_ram_gb = round(available_mb / 1024, 1)
                    except Exception:
                        pass
                break

    result = {
        "total_cpu": total_cpu,
        "free_cpu": free_cpu,
        "total_ram_gb": round(total_ram_gb, 1),
        "free_ram_gb": free_ram_gb,
        "ok": True,
        "raw": out,
    }
    print(f"[slurm_resources] result={result}")
    return result
