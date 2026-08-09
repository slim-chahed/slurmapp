import re
from html import escape as html_escape


def sanitize_text(value: str, max_length: int = 255, allow_html: bool = False) -> str:
    value = str(value or "")
    value = value.strip()
    if not allow_html:
        value = html_escape(value, quote=True)
    return value[:max_length]


def validate_job_name(name: str) -> str:
    name = sanitize_text(name, max_length=100)

    suspicious_patterns = [
        "../", "..\\", "/etc/passwd", "WEB-INF", "/request", "system.ini",
        "SELECT ", "INSERT ", "UPDATE ", "DELETE ", "UNION ALL", "AND 1=1", "OR 1=1",
        "<script", "alert(", "onerror=", "onmouseover=", "prompt()",
        "ShellShock", "owasp.org", "sleep(", "exec ", "cmd=", "dir ", "ls /",
    ]

    lowered = name.lower()
    for pattern in suspicious_patterns:
        if pattern.lower() in lowered:
            raise ValueError("Invalid job name")

    if not re.fullmatch(r"[A-Za-z0-9 _\-]{1,100}", name):
        raise ValueError("Invalid job name")

    return name


def validate_code_input(code: str, max_length: int = 50000) -> str:
    code = str(code or "")
    if len(code) > max_length:
        raise ValueError("Code input is too long")
    return code


def sanitize_code_output(output: str) -> str:
    output = str(output or "")
    return output[:100000]
