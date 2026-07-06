from vm_monitor import _run_ssh

cmds = [
    'docker ps -a --format "{{{{.Names}}}}"',
    'docker ps --format "{{{{.Names}}}}"',
    'docker info 2>&1 | tail -n 20',
    'docker version 2>&1',
    'groups',
    'sudo -n docker ps --format "{{{{.Names}}}}" 2>&1',
    'sudo -n docker info 2>&1 | tail -n 10',
    'ls -la /var/run/docker.sock 2>&1',
]
for cmd in cmds:
    ok, out = _run_ssh(cmd)
    print(f'CMD: {cmd}')
    print(f'  ok={ok}, out={repr(out[:800])}')
    print()
