#!/bin/bash
# Run this ON THE VM as hpcadmin

echo "Setting up terminal_user SSH access..."

# Create .ssh directory
sudo mkdir -p /home/terminal_user/.ssh
sudo chown terminal_user:terminal_user /home/terminal_user/.ssh
sudo chmod 700 /home/terminal_user/.ssh

# Copy hpcadmin's public key to terminal_user
sudo cp ~/.ssh/id_rsa_hpc.pub /home/terminal_user/.ssh/authorized_keys
sudo chown terminal_user:terminal_user /home/terminal_user/.ssh/authorized_keys
sudo chmod 600 /home/terminal_user/.ssh/authorized_keys

echo "Done! terminal_user can now SSH with the same key as hpcadmin"
