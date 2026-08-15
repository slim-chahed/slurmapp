import requests
import json
import paramiko
import os
from config import Config


def submit_slurm_job(script: str) -> dict:
    url = f"{Config.SLURM_REST_URL}/slurm/v0.0.37/job/submit"
    headers = {
        "X-SLURM-USER-NAME": Config.SLURM_SERVICE_USER,
        "X-SLURM-USER-TOKEN": Config.SLURM_JWT_TOKEN,
        "Content-Type": "application/json"
    }
    payload = {
        "script": script,
        "job": {
            "partition": "debug",
            "current_working_directory": "/home/webapp",
            "environment": {
                "PATH": "/usr/local/bin:/usr/bin:/bin"
            }
        }
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        if response.status_code == 200:
            data = response.json()
            job_id = data.get("job_id")
            if job_id:
                return {"success": True, "job_id": str(job_id)}
            else:
                return {"success": False, "error": data.get("errors") or "No job_id in response"}
        else:
            return {"success": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}
    except Exception as e:
        return {"success": False, "error": "Submission failed"}


def cancel_slurm_job(job_id: str) -> dict:
    url = f"{Config.SLURM_REST_URL}/slurm/v0.0.37/job/{job_id}"
    headers = {
        "X-SLURM-USER-NAME": Config.SLURM_SERVICE_USER,
        "X-SLURM-USER-TOKEN": Config.SLURM_JWT_TOKEN,
    }
    try:
        response = requests.delete(url, headers=headers, timeout=20)
        if response.status_code in (200, 202, 204):
            return {"success": True}
        return {"success": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}
    except Exception:
        return {"success": False, "error": "Cancellation failed"}
