import os
import time
import logging
import base64
import re
from datetime import datetime, timedelta
from config import Config
from vm_monitor import _run_ssh

logger = logging.getLogger(__name__)


class EditorSession:
    def __init__(self, reservation_id: int, cpu: int, ram_gb: int, duration_hours: int, language: str = "python"):
        self.reservation_id = reservation_id
        self.cpu = cpu
        self.ram_gb = ram_gb
        self.duration_hours = duration_hours
        self.language = language
        self.container_name = f"editor_{reservation_id}"
        self.active = False
        self.start_time = None
        self.expires_at = None
        self.job_id = None

    def _get_image(self) -> str:
        return {
            "python": "python:3.11-slim",
            "c": "gcc:latest",
            "java": "eclipse-temurin:17-jdk",
        }.get(self.language, "python:3.11-slim")

    def _build_slurm_script(self) -> str:
        duration = f"{self.duration_hours}:00:00"
        image = self._get_image()
        return f"""#!/bin/bash
#SBATCH --job-name=editor_{self.reservation_id}
#SBATCH --cpus-per-task={self.cpu}
#SBATCH --mem={self.ram_gb}G
#SBATCH --time={duration}
#SBATCH --output=/tmp/editor_{self.reservation_id}.log
#SBATCH --cpu-bind=cores

CONTAINER_NAME="{self.container_name}"

echo "Removing any existing container with same name..." > /tmp/editor_{self.reservation_id}.log
docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

echo "Starting container..." >> /tmp/editor_{self.reservation_id}.log

docker run -d \\
    --name "$CONTAINER_NAME" \\
    --cpus={self.cpu} \\
    --memory={self.ram_gb}g \\
    --network none \\
    --cap-drop ALL \\
    --security-opt no-new-privileges \\
    --read-only \\
    --tmpfs /tmp:rw,exec,size=256m \\
    --tmpfs /workspace:rw,exec,size=256m \\
    --label editor_session={self.reservation_id} \\
    {image} sleep infinity >> /tmp/editor_{self.reservation_id}.log 2>&1 || exit 1

echo "Container started, waiting 5s for init..." >> /tmp/editor_{self.reservation_id}.log
sleep 5

echo "Final container state:" >> /tmp/editor_{self.reservation_id}.log
docker ps -a --filter name="$CONTAINER_NAME" --format '{{{{.Names}}}} {{{{.Status}}}}' >> /tmp/editor_{self.reservation_id}.log 2>&1 || true
docker logs "$CONTAINER_NAME" 2>&1 | tail -10 >> /tmp/editor_{self.reservation_id}.log 2>&1 || true

exit 0
"""

    def _wait_for_container(self) -> None:
        expected_id = None
        while True:
            if expected_id is None:
                ok, out = _run_ssh(f"sed -n '3p' /tmp/editor_{self.reservation_id}.log 2>/dev/null || true")
                if ok and out:
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

    def _wait_for_slurm_job(self) -> None:
        while True:
            ok, out = _run_ssh(f"squeue -j {self.job_id} --noheader --format='%s' 2>/dev/null || true")
            if ok and out:
                state = out.strip().split()[0].lower()
                if state in ("failed", "cancelled", "timeout", "node_fail", "preempted"):
                    return
                if state in ("running", "completed"):
                    return
            else:
                ok2, out2 = _run_ssh(f"sacct -j {self.job_id} --noheader --format=State 2>/dev/null || true")
                if ok2 and out2:
                    state = out2.strip().split()[0].lower()
                    if state in ("failed", "cancelled", "timeout", "node_fail"):
                        return
                    if state in ("running", "completed"):
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

            self.active = True
            self.start_time = datetime.utcnow()
            self.expires_at = self.start_time + timedelta(hours=self.duration_hours)

            return {"success": True, "job_id": self.job_id}
        except Exception as e:
            logger.error(f"Failed to start slurm allocation for editor {self.reservation_id}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    def _cleanup_existing_container(self) -> None:
        try:
            _run_ssh(f"docker rm -f {self.container_name} 2>/dev/null || true")
            time.sleep(1)
        except Exception as e:
            logger.warning(f"Could not cleanup existing container {self.container_name}: {e}")

    def _patch_java_class_name(self, code: str, class_name: str) -> str:
        patched = re.sub(r"public\s+class\s+\w+", f"public class {class_name}", code)
        return patched

    def execute_code(self, code: str) -> dict:
        if not self.active:
            return {"success": False, "error": "Session not active", "output": ""}

        try:
            encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
            if self.language == "python":
                cmd = (
                    f"docker exec {self.container_name} sh -c \""
                    f"echo '{encoded}' | base64 -d > /tmp/editor_code.py && "
                    f"python3 /tmp/editor_code.py 2>&1"
                    f"\""
                )
            elif self.language == "c":
                cmd = (
                    f"docker exec {self.container_name} sh -c \""
                    f"echo '{encoded}' | base64 -d > /tmp/editor_code.c && "
                    f"gcc /tmp/editor_code.c -o /tmp/editor_code 2>&1 && "
                    f"chmod +x /tmp/editor_code && "
                    f"/tmp/editor_code 2>&1"
                    f"\""
                )
            elif self.language == "java":
                patched_code = self._patch_java_class_name(code, "EditorCode")
                encoded = base64.b64encode(patched_code.encode("utf-8")).decode("ascii")
                cmd = (
                    f"docker exec {self.container_name} sh -c \""
                    f"echo '{encoded}' | base64 -d > /tmp/EditorCode.java && "
                    f"cd /tmp && javac EditorCode.java 2>&1 && java -cp /tmp EditorCode 2>&1"
                    f"\""
                )
            else:
                return {"success": False, "error": f"Unsupported language: {self.language}", "output": ""}

            ok, out = _run_ssh(cmd)
            output = out or ""
            if ok:
                return {"success": True, "output": output, "error": ""}
            return {"success": False, "error": output or "Execution failed", "output": output}
        except Exception as e:
            logger.error(f"Failed to execute code for editor {self.reservation_id}: {e}", exc_info=True)
            return {"success": False, "error": str(e), "output": ""}

    def is_expired(self) -> bool:
        if not self.expires_at:
            return True
        return (self.expires_at - datetime.utcnow()).total_seconds() <= 0

    def cleanup(self):
        self.active = False
        if self.job_id:
            try:
                from slurm_client import cancel_slurm_job
                cancel_slurm_job(self.job_id)
            except Exception as e:
                logger.warning(f"Could not cancel slurm job {self.job_id} for editor {self.reservation_id}: {e}")
        try:
            _run_ssh(f"docker rm -f {self.container_name} 2>/dev/null || true")
        except Exception as e:
            logger.warning(f"Could not remove container {self.container_name}: {e}")
