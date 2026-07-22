#!/bin/bash
set -e

echo "=== Setting up restricted terminal user ==="

# Create user if not exists
if ! id "terminal_user" &>/dev/null; then
    sudo useradd -m -s /bin/rbash terminal_user
    echo "Created user: terminal_user"
else
    echo "User terminal_user already exists"
fi

# Add to docker group
sudo usermod -aG docker terminal_user
echo "Added terminal_user to docker group"

# Create restricted bin directory
sudo mkdir -p /home/terminal_user/bin
sudo chown terminal_user:terminal_user /home/terminal_user/bin
sudo chmod 755 /home/terminal_user/bin

# Create docker wrapper script
sudo tee /home/terminal_user/bin/docker > /dev/null <<'DOCKER_EOF'
#!/bin/bash
# Restricted docker wrapper for terminal_user
# Only allows safe docker commands

# Remove any --privileged, --pid, --network=host, etc.
SAFE_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --privileged|--pid=host|--network=host|--cgroupns=host)
            echo "Error: $arg is not allowed" >&2
            exit 1
            ;;
        *)
            SAFE_ARGS+=("$arg")
            ;;
    esac
done

# Auto-add --rm to run/create commands if not present
if [[ " ${SAFE_ARGS[*]} " =~ " run " ]] || [[ " ${SAFE_ARGS[*]} " =~ " create " ]]; then
    if [[ ! " ${SAFE_ARGS[*]} " =~ " --rm " ]]; then
        SAFE_ARGS=(--rm "${SAFE_ARGS[@]}")
    fi
fi

exec /usr/bin/docker "${SAFE_ARGS[@]}"
DOCKER_EOF
sudo chmod +x /home/terminal_user/bin/docker
echo "Created restricted docker wrapper"

# Set up restricted environment
sudo tee /home/terminal_user/.bash_profile > /dev/null <<'PROFILE_EOF'
# Restricted environment for terminal_user
export PATH="$HOME/bin"
export HOME="/home/terminal_user"
export TERM=xterm-256color

# Limit history
export HISTSIZE=100
export HISTFILESIZE=100
export HISTFILE="$HOME/.bash_history"

# Block sensitive commands
blocked() {
    echo "This command is restricted in the terminal environment." >&2
    return 1
}

alias sudo='blocked'
alias su='blocked'
alias ssh='blocked'
alias scp='blocked'
alias sftp='blocked'
alias passwd='blocked'
alias chpasswd='blocked'
alias useradd='blocked'
alias userdel='blocked'
alias usermod='blocked'
alias visudo='blocked'
alias systemctl='blocked'
alias service='blocked'
alias journalctl='blocked'
alias dmesg='blocked'
alias cat='blocked'
alias less='blocked'
alias more='blocked'
alias tail='blocked'
alias head='blocked'
alias vi='blocked'
alias vim='blocked'
alias nano='blocked'
alias mount='blocked'
alias umount='blocked'
alias fdisk='blocked'
alias dd='blocked'
alias kill='blocked'
alias pkill='blocked'
alias top='blocked'
alias htop='blocked'
alias ps='blocked'
alias netstat='blocked'
alias ss='blocked'
alias ip='blocked'
alias ifconfig='blocked'
alias iptables='blocked'
alias firewall-cmd='blocked'
alias ufw='blocked'
alias apt='blocked'
alias apt-get='blocked'
alias yum='blocked'
alias dnf='blocked'
alias snap='blocked'
PROFILE_EOF
sudo chown terminal_user:terminal_user /home/terminal_user/.bash_profile
sudo chmod 644 /home/terminal_user/.bash_profile
echo "Created restricted .bash_profile"

# Create .bashrc
sudo touch /home/terminal_user/.bashrc
sudo chown terminal_user:terminal_user /home/terminal_user/.bashrc
sudo chmod 644 /home/terminal_user/.bashrc

# Create workspace directory
sudo mkdir -p /tmp/terminal_workspace
sudo chown terminal_user:terminal_user /tmp/terminal_workspace
sudo chmod 755 /tmp/terminal_workspace

# Set password
echo "terminal_user:TerminalPass123" | sudo chpasswd
echo "Set password for terminal_user"

echo "=== Setup complete ==="
echo "User: terminal_user"
echo "Password: TerminalPass123"
echo "Home: /home/terminal_user"
echo ""
echo "Add to .env:"
echo "TERMINAL_SSH_USER=terminal_user"
echo "TERMINAL_SSH_KEY_PATH=C:\\Users\\ASUS\\.ssh\\id_rsa_hpc"
echo "TERMINAL_SSH_PASSPHRASE=Password*123"
