import paramiko
import queue
import time
import logging
import os
from datetime import datetime, timedelta
from config import Config
from vm_monitor import _run_ssh

logger = logging.getLogger(__name__)


class TerminalSession:
    def __init__(self, reservation_id: int, cpu: int, ram_gb: int, duration_hours: int, terminal_type: str = "docker", job_id: str = None):
        self.reservation_id = reservation_id
        self.cpu = cpu
        self.ram_gb = ram_gb
        self.duration_hours = duration_hours
        self.terminal_type = terminal_type
        self.job_id = job_id
        self.container_name = f"terminal_{reservation_id}"
        self.client = None
        self.channel = None
        self.output_buffer = []
        self.input_queue = queue.Queue()
        self.active = False
        self.start_time = None
        self.expires_at = None

    def _ssh_client(self):
        client = paramiko.SSHClient()
        known_hosts = os.path.expanduser("~/.ssh/known_hosts")
        if os.path.exists(known_hosts):
            client.load_host_keys(known_hosts)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(
            hostname=Config.VM_HOST,
            port=Config.VM_SSH_PORT,
            username=Config.VM_SSH_USER,
            key_filename=Config.VM_SSH_KEY_PATH,
            passphrase=Config.VM_SSH_PASSPHRASE or None,
            timeout=10,
        )
        return client

    def _build_slurm_script(self) -> str:
        duration = f"{self.duration_hours}:00:00"
        image = Config.TERMINAL_DIND_IMAGE
        return f"""#!/bin/bash
#SBATCH --job-name=terminal_{self.reservation_id}
#SBATCH --cpus-per-task={self.cpu}
#SBATCH --mem={self.ram_gb}G
#SBATCH --time={duration}
#SBATCH --output=/tmp/terminal_{self.reservation_id}.log
#SBATCH --cpu-bind=cores

CONTAINER_NAME="{self.container_name}"

echo "Removing any existing container with same name..." > /tmp/terminal_{self.reservation_id}.log
docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

echo "Starting container..." >> /tmp/terminal_{self.reservation_id}.log

docker run -d \\
    --name "$CONTAINER_NAME" \\
    --cpus={self.cpu} \\
    --memory={self.ram_gb}g \\
    --privileged \\
    --label terminal_session={self.reservation_id} \\
    {image} >> /tmp/terminal_{self.reservation_id}.log 2>&1 || exit 1

echo "Container started, waiting 5s for init..." >> /tmp/terminal_{self.reservation_id}.log
sleep 5

echo "Final container state:" >> /tmp/terminal_{self.reservation_id}.log
docker ps -a --filter name="$CONTAINER_NAME" --format '{{{{.Names}}}} {{{{.Status}}}}' >> /tmp/terminal_{self.reservation_id}.log 2>&1 || true
docker logs "$CONTAINER_NAME" 2>&1 | tail -10 >> /tmp/terminal_{self.reservation_id}.log 2>&1 || true

exit 0
"""

    def _wait_for_container(self) -> None:
        expected_id = None
        while True:
            if expected_id is None:
                ok, out = _run_ssh(f"sed -n '3p' /tmp/terminal_{self.reservation_id}.log 2>/dev/null || true")
                if ok and out.strip():
                    candidate = out.strip()
                    if len(candidate) == 64 and all(c in "0123456789abcdef" for c in candidate):
                        expected_id = candidate

            if expected_id:
                short_id = expected_id[:12]
                ok, out = _run_ssh(f"docker ps --filter id={expected_id} --format '{{{{.ID}}}}' 2>/dev/null || true")
                if ok and short_id in out:
                    return
                ok2, out2 = _run_ssh(f"docker ps -a --filter id={expected_id} --format '{{{{.ID}}}}' 2>/dev/null || true")
                if ok2 and short_id in out2:
                    return

            time.sleep(2)

    def start_slurm_allocation(self) -> dict:
        try:
            self._cleanup_existing_container()

            from slurm_client import submit_slurm_job
            script = self._build_slurm_script()
            result = submit_slurm_job(script)
            if not result.get("success"):
                return {"success": False, "error": result.get("error", "Slurm submission failed")}

            self.job_id = result.get("job_id")

            self._wait_for_slurm_job()
            self._wait_for_container()

            return self._connect_to_container()
        except Exception as e:
            logger.error(f"Failed to start slurm allocation for terminal {self.reservation_id}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    def _cleanup_existing_container(self) -> None:
        try:
            _run_ssh(f"docker rm -f {self.container_name} 2>/dev/null || true")
            time.sleep(1)
        except Exception as e:
            logger.warning(f"Could not cleanup existing container {self.container_name}: {e}")

    def _wait_for_slurm_job(self) -> None:
        while True:
            ok, out = _run_ssh(f"squeue -j {self.job_id} --noheader --format='%s' 2>/dev/null || true")
            if ok and out.strip():
                state = out.strip().split()[0].lower()
                if state in ("failed", "cancelled", "timeout", "node_fail", "preempted"):
                    return
                if state in ("running", "completed"):
                    return
            else:
                ok2, out2 = _run_ssh(f"sacct -j {self.job_id} --noheader --format=State 2>/dev/null || true")
                if ok2 and out2.strip():
                    state = out2.strip().split()[0].lower()
                    if state in ("failed", "cancelled", "timeout", "node_fail"):
                        return
                    if state in ("running", "completed"):
                        return
            time.sleep(2)

    def connect_to_existing(self) -> dict:
        try:
            self._wait_for_container()
            return self._connect_to_container()
        except Exception as e:
            logger.error(f"Failed to connect to existing container for terminal {self.reservation_id}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    def _connect_to_container(self) -> dict:
        self.client = self._ssh_client()
        self.client.get_transport().set_keepalive(30)
        self.channel = self.client.invoke_shell(term="xterm-256color", width=120, height=40)
        self.channel.send(f"docker exec -it {self.container_name} sh -c 'while true; do /bin/sh || /bin/bash || true; done'\n")

        self.active = True
        self.start_time = datetime.utcnow()
        self.expires_at = self.start_time + timedelta(hours=self.duration_hours)

        time.sleep(2)
        initial = self.read_output()
        self.output_buffer.append(initial)
        if "error" in initial.lower() or "failed" in initial.lower():
            self.active = False
            return {"success": False, "error": initial or "DinD container failed to start"}

        return {"success": True}

    def send_input(self, data: str):
        if self.channel and self.active:
            try:
                self.channel.send(data)
            except Exception as e:
                logger.warning(f"Could not send input to terminal {self.reservation_id}: {e}")
                self.active = False

    def read_output(self) -> str:
        if not self.channel or not self.active:
            return ""
        try:
            if self.channel.recv_ready():
                data = self.channel.recv(4096).decode("utf-8", errors="replace")
                self.output_buffer.append(data)
                return data
        except Exception as e:
            logger.warning(f"Could not read output from terminal {self.reservation_id}: {e}")
            self.active = False
        return ""

    def get_remaining_seconds(self) -> int:
        if not self.expires_at:
            return 0
        remaining = (self.expires_at - datetime.utcnow()).total_seconds()
        return max(0, int(remaining))

    def is_expired(self) -> bool:
        return self.get_remaining_seconds() <= 0

    def cleanup(self):
        self.active = False
        if self.job_id:
            try:
                from slurm_client import cancel_slurm_job
                cancel_slurm_job(self.job_id)
            except Exception as e:
                logger.warning(f"Could not cancel slurm job {self.job_id} for terminal {self.reservation_id}: {e}")
        try:
            _run_ssh(f"docker rm -f {self.container_name} 2>/dev/null || true")
        except Exception as e:
            logger.warning(f"Could not remove container {self.container_name}: {e}")
        if self.channel:
            try:
                self.channel.close()
            except Exception as e:
                logger.warning(f"Could not close channel for terminal {self.reservation_id}: {e}")
        if self.client:
            try:
                self.client.close()
            except Exception as e:
                logger.warning(f"Could not close SSH client for terminal {self.reservation_id}: {e}")

    def get_output(self) -> str:
        return "".join(self.output_buffer)
