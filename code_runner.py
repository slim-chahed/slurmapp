from config import Config
from slurm_client import submit_slurm_job
from vm_monitor import _run_ssh
import base64
import uuid

LANGUAGE_RUNNERS = {
    "python": "python3 /shared/input_{job_id}.py",
    "java": "cd /shared && javac Main_{job_id}.java && java Main_{job_id}",
    "c": "cd /shared && gcc main_{job_id}.c -o main_{job_id} && ./main_{job_id}",
}


def _write_code_to_vm(language: str, code: str, job_id: str) -> bool:
    filename_map = {
        "python": f"input_{job_id}.py",
        "java": f"Main_{job_id}.java",
        "c": f"main_{job_id}.c",
    }
    filename = filename_map[language]
    if language == "java":
        code = _patch_java_class_name(code, filename.replace(".java", ""))
    encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
    ok, out = _run_ssh(f"echo '{encoded}' | base64 -d > /shared/{filename}")
    if not ok:
        return False
    ok2, out2 = _run_ssh(f"chmod 644 /shared/{filename}")
    return ok2


def _patch_java_class_name(code: str, class_name: str) -> str:
    import re
    patched = re.sub(r"public\s+class\s+\w+", f"public class {class_name}", code)
    return patched


def build_script(language: str, cpu: int, ram: int, duration_hours: int, job_id: str) -> str:
    runner = LANGUAGE_RUNNERS[language].format(job_id=job_id)
    script = f"""#!/bin/bash
#SBATCH --job-name={language}_job
#SBATCH --cpus-per-task={cpu}
#SBATCH --mem={ram}G
#SBATCH --time={duration_hours}:00:00
#SBATCH --output=/shared/output_%j.log
#SBATCH --cpu-bind=cores

{runner}
"""
    print(f"[BUILD_SCRIPT] job_id={job_id} cpu={cpu} ram={ram} duration={duration_hours} language={language}")
    print(f"[BUILD_SCRIPT] script=\n{script}")
    return script


def run_code(language: str, code: str, cpu: int, ram: int, duration_hours: int) -> dict:
    job_id = uuid.uuid4().hex[:8]
    print(f"[RUN_CODE] language={language} cpu={cpu} ram={ram} duration={duration_hours} job_id={job_id}")
    ok = _write_code_to_vm(language, code, job_id)
    if not ok:
        print(f"[RUN_CODE] write_code FAILED for job_id={job_id}")
        return {"success": False, "error": "Failed to write code to VM /shared"}
    script = build_script(language, cpu, ram, duration_hours, job_id)
    print(f"[RUN_CODE] submitting script for job_id={job_id}")
    result = submit_slurm_job(script)
    print(f"[RUN_CODE] submit result={result} for job_id={job_id}")
    return result
