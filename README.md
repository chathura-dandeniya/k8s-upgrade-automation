# k8s-upgrade-automation

A hardened Python automation script for upgrading a **kubeadm-based Kubernetes cluster** safely and reliably — built and tested on real hardware (Raspberry Pi 5, Ubuntu 24.04 LTS, arm64).

Upgraded a 2-node cluster from **v1.34.3 → v1.35.3** (latest stable as of March 2026).

---

## Cluster Topology

```
cp1      control-plane   192.168.8.121   Ubuntu 24.04.4 LTS   arm64   containerd 2.2.2
worker1  worker          192.168.8.102   Ubuntu 24.04.4 LTS   arm64   containerd 1.7.28
```

- **CNI:** Calico
- **Container runtime:** containerd
- **Architecture:** arm64 (Raspberry Pi 5)
- **Installed via:** kubeadm

---

## Features

| Feature | Detail |
|---|---|
| **Dry-run mode** | Preview every step with zero changes made |
| **Auto SSH key management** | Generates a temporary ed25519 key, installs it on workers, removes it after upgrade |
| **etcd snapshot backup** | Full etcd + PKI backup before any changes |
| **Package version auto-detection** | Uses `apt-cache madison` — no hardcoded `-1.1` suffix |
| **Version skew enforcement** | Aborts if upgrade path skips more than one minor version |
| **cgroup v2 verification** | Required for Kubernetes 1.35 — checked on startup |
| **Calico CNI health check** | Validates both `calico-node` and `calico-kube-controllers` before and after |
| **containerd compatibility check** | Ensures containerd ≥ 1.6 on all nodes |
| **API server readiness wait** | Polls until API server is back up after kubelet restart — prevents TLS timeout on uncordon |
| **Safe node drain** | `--force` and `--delete-emptydir-data` are opt-in, not default |
| **Phase-aware recovery guidance** | On any failure, prints context-specific recovery commands |
| **Full audit log** | Every command logged to `/var/log/k8s-upgrade-<timestamp>.log` |

---

## Upgrade Flow

```
PRE-FLIGHT
  ├── Root check
  ├── Hostname verification (must run on control plane)
  ├── cgroup v2 check
  ├── Binary check (kubectl / kubeadm / kubelet)
  ├── Cluster node health (all nodes must be Ready)
  ├── Version skew validation
  ├── containerd compatibility check (all nodes)
  ├── Calico CNI health check
  └── SSH connectivity test to all workers

BACKUPS
  ├── etcd snapshot → /var/backup/k8s-upgrade/
  └── PKI + kubeadm ConfigMap backup

CONTROL PLANE (cp1)
  ├── Update apt repo → v1.35
  ├── Upgrade kubeadm (auto-detected version suffix)
  ├── kubeadm upgrade plan
  ├── kubeadm upgrade apply
  ├── Drain cp1
  ├── Upgrade kubelet + kubectl
  ├── systemctl daemon-reload + restart kubelet
  ├── Wait for API server to respond         ← prevents TLS timeout
  ├── Uncordon cp1
  └── Wait for cp1 Ready

WORKER NODES (worker1)
  ├── Update apt repo on worker
  ├── Upgrade kubeadm on worker
  ├── kubeadm upgrade node
  ├── Drain worker1
  ├── Upgrade kubelet + kubectl on worker
  ├── Restart kubelet on worker
  ├── Uncordon worker1
  └── Wait for worker1 Ready

POST-UPGRADE VERIFICATION
  ├── All node versions = v1.35.3
  ├── All kube-system pods Running + container-level ready
  └── Calico CNI health (post-upgrade)

SSH KEY CLEANUP
  ├── Remove upgrade key from worker authorized_keys
  └── Delete local key pair
```

---

## Usage

### Prerequisites
- Script must be run as `root` on the **control plane node** (`cp1`)
- `etcdctl` installed: `sudo apt-get install -y etcd-client`
- Worker node's SSH password available (for one-time key installation)

### Commands

```bash
# Always dry-run first — zero changes made
sudo KUBECONFIG=/home/ubuntu/.kube/config python3 k8s_upgrade.py --dry-run

# Live upgrade — SSH key generated and cleaned up automatically
sudo KUBECONFIG=/home/ubuntu/.kube/config python3 k8s_upgrade.py

# Bring your own SSH key
sudo KUBECONFIG=/home/ubuntu/.kube/config python3 k8s_upgrade.py \
  --ssh-key ~/.ssh/id_ed25519

# Upgrade control plane only
sudo KUBECONFIG=/home/ubuntu/.kube/config python3 k8s_upgrade.py \
  --control-plane-only

# Upgrade workers only (if control plane already done)
sudo KUBECONFIG=/home/ubuntu/.kube/config python3 k8s_upgrade.py \
  --workers-only
```

### All flags

| Flag | Description |
|---|---|
| `--dry-run` | Preview all steps, zero changes |
| `--ssh-user` | SSH user for workers (default: `ubuntu`) |
| `--ssh-key` | Path to existing SSH key (skips auto key setup) |
| `--skip-backup` | Skip etcd backup — not recommended |
| `--control-plane-only` | Upgrade control plane only |
| `--workers-only` | Upgrade workers only |

---

## Hardening Iterations

This script was developed and hardened through **5 versions** based on code review feedback and real live-run failures.

| Version | Key changes |
|---|---|
| **v1** | Initial script — generic, dynamic node discovery |
| **v2** | Fixed `run()` None crash, auto package suffix detection, Calico check, safer drain flags, CrashLoopBackOff detection, 300s node ready timeout, subprocess list SSH |
| **v3** | base64-encoded SSH commands (no quoting issues), `ssh_user`/`ssh_key` passed correctly to containerd check, recovery guidance on all failure phases, Calico checks both components |
| **v4** | Fixed containerd parser for Raspberry Pi output formats, added `wait_for_apiserver()` to prevent TLS timeout after kubelet restart |
| **v5** | Automatic SSH key generation, installation, and cleanup — no manual key setup needed |

---

## Real Issues Solved

### TLS Handshake Timeout on Uncordon
After `systemctl restart kubelet` on the control plane, the script immediately ran `kubectl uncordon` — but the API server needed more time to restart on Raspberry Pi hardware, causing a TLS handshake timeout. Fixed by adding `wait_for_apiserver()` which polls until `kubectl get nodes` succeeds before proceeding.

### containerd Version Parse Failure
The containerd version parser only matched lines containing a `"Version:"` label. Raspberry Pi's containerd outputs version differently:
```
containerd containerd.io 2.2.2 7c865d0...
containerd github.com/containerd/containerd v1.7.28 ...
```
Fixed by rewriting the parser to scan every whitespace-separated token for a version-shaped string.

### SSH Key Scope Under sudo
Running the script with `sudo` switches to `root`, whose SSH key (`/root/.ssh/`) differs from the ubuntu user's keys. Fixed by auto-generating a dedicated upgrade key and passing it explicitly to all SSH calls.

---

## Log Output

Every run saves a full audit log:
```
/var/log/k8s-upgrade-20260329-231334.log
```

---

## Tech Stack

- **Language:** Python 3 (stdlib only — no pip dependencies)
- **Platform:** Raspberry Pi 5 · Ubuntu 24.04.4 LTS · arm64
- **Kubernetes:** kubeadm · v1.34.3 → v1.35.3
- **CNI:** Calico
- **Runtime:** containerd

---

## Author

**Chathura Dandeniya**
Deakin University Graduate | DevOps & Cloud Infrastructure
[LinkedIn](https://www.linkedin.com/in/chathura-dandeniya) · [GitHub](https://github.com/chathura-dandeniya)