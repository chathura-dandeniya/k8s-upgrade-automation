#!/usr/bin/env python3
"""
=============================================================================
  Kubernetes Cluster Upgrade Script  (hardened v5)
  From: v1.34.3  →  To: v1.35.3 (latest stable)

  Cluster layout:
    cp1      control-plane   192.168.8.121   Ubuntu 24.04.4 LTS  arm64
    worker1  worker          192.168.8.102   Ubuntu 24.04.4 LTS  arm64

  CNI:               Calico
  Container runtime: containerd
  Architecture:      arm64 (Raspberry Pi 5)
=============================================================================

  USAGE:
    Dry run (always do this first):
      sudo KUBECONFIG=/home/ubuntu/.kube/config python3 k8s_upgrade.py --dry-run

    Real upgrade — SSH key is generated and cleaned up automatically:
      sudo KUBECONFIG=/home/ubuntu/.kube/config python3 k8s_upgrade.py

    With your own existing SSH key (skips auto key setup):
      sudo KUBECONFIG=/home/ubuntu/.kube/config python3 k8s_upgrade.py --ssh-key ~/.ssh/id_ed25519

    Control plane only:
      sudo KUBECONFIG=/home/ubuntu/.kube/config python3 k8s_upgrade.py --control-plane-only

    Workers only (if control plane already upgraded):
      sudo KUBECONFIG=/home/ubuntu/.kube/config python3 k8s_upgrade.py --workers-only

  FIXES vs v3 (from live run errors):
    [FIX L1] containerd version parser rewritten — old parser only matched
             lines containing "Version:" label, which doesn't exist in
             Raspberry Pi output formats. New parser scans every token in
             the output for a version-shaped string, handling all formats:
             "containerd containerd.io 2.2.2 <hash>"
             "containerd github.com/containerd/containerd v1.7.28 <hash>"
    [FIX L2] Added wait_for_apiserver() called after kubelet restart on
             the control plane. Previously the script immediately ran
             "kubectl uncordon" causing a TLS handshake timeout because
             the API server hadn't finished restarting yet on the Pi.

=============================================================================
"""

import subprocess
import sys
import os
import argparse
import logging
import datetime
import json
import time
import shutil
import socket
import base64

# ─────────────────────────────────────────────────────────────
#  CLUSTER CONFIGURATION
# ─────────────────────────────────────────────────────────────
CURRENT_VERSION    = "1.34.3"
TARGET_VERSION     = "1.35.3"
TARGET_MINOR       = "v1.35"

CONTROL_PLANE_NODE = "cp1"
CONTROL_PLANE_IP   = "192.168.8.121"

WORKER_NODES = [
    {"name": "worker1", "ip": "192.168.8.102"},
]

DEFAULT_SSH_USER   = "ubuntu"

# Timeouts
DRAIN_TIMEOUT         = 300   # seconds
NODE_READY_TIMEOUT    = 300   # [FIX 7] raised from 120s → 300s for Raspberry Pi

# Drain behaviour  [FIX 4]
DRAIN_DELETE_EMPTYDIR = False  # set True only if you accept ephemeral data loss
DRAIN_FORCE           = False  # --force evicts pods not managed by a controller

# Paths
BACKUP_DIR           = "/var/backup/k8s-upgrade"
LOG_FILE             = f"/var/log/k8s-upgrade-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
K8S_APT_KEYRING      = "/etc/apt/keyrings/kubernetes-apt-keyring.gpg"
K8S_APT_SOURCES_FILE = "/etc/apt/sources.list.d/kubernetes.list"

# ─────────────────────────────────────────────────────────────
#  TERMINAL COLOURS
# ─────────────────────────────────────────────────────────────
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    CYAN   = "\033[96m"
    BLUE   = "\033[94m"
    DIM    = "\033[2m"

def banner(msg, colour=C.CYAN):
    w = 66
    print(f"\n{colour}{C.BOLD}{'─'*w}{C.RESET}")
    print(f"{colour}{C.BOLD}  {msg}{C.RESET}")
    print(f"{colour}{C.BOLD}{'─'*w}{C.RESET}\n")

def ok(msg):   print(f"  {C.GREEN}✔{C.RESET}  {msg}")
def warn(msg): print(f"  {C.YELLOW}⚠{C.RESET}  {C.YELLOW}{msg}{C.RESET}")
def err(msg):  print(f"  {C.RED}✘{C.RESET}  {C.RED}{msg}{C.RESET}")
def info(msg): print(f"  {C.BLUE}→{C.RESET}  {msg}")
def step(msg): print(f"\n  {C.CYAN}{C.BOLD}[STEP]{C.RESET} {C.BOLD}{msg}{C.RESET}")
def dry(msg):  print(f"  {C.DIM}[DRY-RUN] Would run: {msg}{C.RESET}")

# ─────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────
def setup_logging():
    os.makedirs("/var/log", exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ]
    )
    logging.getLogger().handlers[1].setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
#  RECOVERY GUIDANCE  [FIX 12]
# ─────────────────────────────────────────────────────────────
def print_recovery_guidance(phase: str):
    """Print context-aware recovery steps based on where the failure occurred."""
    print(f"\n{C.RED}{C.BOLD}{'─'*66}{C.RESET}")
    print(f"{C.RED}{C.BOLD}  UPGRADE FAILED — RECOVERY GUIDANCE{C.RESET}")
    print(f"{C.RED}{C.BOLD}{'─'*66}{C.RESET}\n")

    info("1. Check cluster state:")
    print("       kubectl get nodes")
    print("       kubectl get pods -n kube-system")

    if phase in ("control-plane", "backup"):
        info("2. If control plane is broken, restore from etcd backup:")
        print(f"      ETCDCTL_API=3 etcdctl snapshot restore <backup>.db \\")
        print(f"        --data-dir /var/lib/etcd-restore")
        info("3. If kubelet won't start:")
        print("       sudo systemctl status kubelet")
        print("       sudo journalctl -xeu kubelet --no-pager | tail -50")
        info("4. To re-run the control plane upgrade step only:")
        print("       sudo KUBECONFIG=/home/ubuntu/.kube/config python3 k8s_upgrade.py --control-plane-only")

    if phase == "worker":
        info("2. If a worker node is stuck cordoned/drained:")
        print("       kubectl uncordon worker1")
        info("3. If kubelet on worker1 won't start:")
        print("       ssh ubuntu@192.168.8.102 'sudo journalctl -xeu kubelet --no-pager | tail -50'")
        info("4. To re-run the worker upgrade step only:")
        print("       sudo KUBECONFIG=/home/ubuntu/.kube/config python3 k8s_upgrade.py --workers-only")

    info("5. Full Kubernetes upgrade troubleshooting docs:")
    print("       https://kubernetes.io/docs/tasks/administer-cluster/kubeadm/kubeadm-upgrade/")
    print()

# ─────────────────────────────────────────────────────────────
#  COMMAND RUNNER  [FIX 1]
# ─────────────────────────────────────────────────────────────
def run(cmd, dry_run=False, check=True, capture=True, timeout=300, label=None):
    """
    Run a local shell command safely.
    FIX 1: stdout/stderr are only accessed when capture=True, preventing
           AttributeError from calling .strip() on None.
    Returns (stdout_str, returncode).
    """
    display = label or (cmd if isinstance(cmd, str) else " ".join(cmd))
    logger.debug(f"CMD: {display}")

    if dry_run:
        dry(display)
        return ("", 0)

    try:
        result = subprocess.run(
            cmd,
            shell=isinstance(cmd, str),
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
        # [FIX 1] Only access stdout/stderr when capture=True
        stdout = (result.stdout or "").strip() if capture else ""
        stderr = (result.stderr or "").strip() if capture else ""

        if stdout:
            logger.debug(f"STDOUT: {stdout}")
        if result.returncode != 0:
            if stderr:
                logger.warning(f"STDERR: {stderr}")
            if check:
                err(f"Command failed (exit {result.returncode}): {display}")
                if stderr:
                    err(stderr)
                raise SystemExit(1)
        return (stdout, result.returncode)

    except subprocess.TimeoutExpired:
        err(f"Command timed out after {timeout}s: {display}")
        raise SystemExit(1)
    except FileNotFoundError as e:
        err(f"Binary not found: {e}")
        raise SystemExit(1)


def run_ssh(host, user, cmd, ssh_key=None, dry_run=False, check=True, timeout=300):
    """
    Run a command on a remote node via SSH.
    [FIX 8]  Uses subprocess list form (no shell=True on the local side).
    [FIX 1b] Base64-encodes the remote command before sending, so any characters
             in cmd — single quotes, brackets, special chars — are never
             interpreted by the remote shell before execution.
             This fixes update_apt_repo_remote() where the apt sources line
             contained single quotes that corrupted the sudo bash -c call.
    """
    if dry_run:
        dry(f"[{host}] {cmd}")
        return ("", 0)

    # Encode the command — eliminates ALL remote quoting issues
    encoded = base64.b64encode(cmd.encode()).decode()

    ssh_args = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
    ]
    if ssh_key:
        ssh_args += ["-i", ssh_key]
    # Remote side: decode the base64 string and pipe into sudo bash
    ssh_args += [f"{user}@{host}", f"echo {encoded} | base64 -d | sudo bash"]

    logger.debug(f"SSH CMD [{host}]: {cmd}")
    try:
        result = subprocess.run(
            ssh_args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if stdout:
            logger.debug(f"[{host}] STDOUT: {stdout}")
        if result.returncode != 0:
            if stderr:
                logger.warning(f"[{host}] STDERR: {stderr}")
            if check:
                err(f"SSH command failed on {host} (exit {result.returncode}): {cmd}")
                if stderr:
                    err(stderr)
                raise SystemExit(1)
        return (stdout, result.returncode)
    except subprocess.TimeoutExpired:
        err(f"SSH command timed out after {timeout}s on {host}: {cmd}")
        raise SystemExit(1)

# ─────────────────────────────────────────────────────────────
#  PACKAGE VERSION SUFFIX DETECTION  [FIX 2]
# ─────────────────────────────────────────────────────────────
def detect_package_suffix(package_name: str, version: str, dry_run=False) -> str:
    """
    FIX 2: Auto-detect the full apt package version string (e.g. 1.35.3-1.1)
    instead of blindly appending -1.1, which may not exist in the repo.
    """
    if dry_run:
        dry(f"apt-cache madison {package_name}  # to detect suffix for v{version}")
        return f"{version}-1.1"   # placeholder for dry-run display

    stdout, rc = run(f"apt-cache madison {package_name}", check=False)
    if rc != 0 or not stdout:
        warn(f"Could not query apt-cache for {package_name} — falling back to {version}-1.1")
        return f"{version}-1.1"

    # Lines look like:  kubeadm | 1.35.3-1.1 | https://pkgs.k8s.io ...
    for line in stdout.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2 and parts[1].startswith(version):
            detected = parts[1]
            ok(f"Detected apt package version for {package_name}: {detected}")
            return detected

    warn(f"Version {version} not found in apt-cache for {package_name} — falling back to {version}-1.1")
    return f"{version}-1.1"

# ─────────────────────────────────────────────────────────────
#  PRE-FLIGHT CHECKS
# ─────────────────────────────────────────────────────────────
def check_root():
    if os.geteuid() != 0:
        err("Must be run as root:  sudo KUBECONFIG=... python3 k8s_upgrade.py")
        raise SystemExit(1)
    ok("Running as root")


def check_hostname():
    actual = socket.gethostname()
    if actual != CONTROL_PLANE_NODE:
        err(f"Expected hostname '{CONTROL_PLANE_NODE}' but got '{actual}'.")
        err("Run this script on the control plane node only.")
        raise SystemExit(1)
    ok(f"Hostname confirmed: {actual}")


def check_cgroup_v2():
    stdout, _ = run("stat -fc %T /sys/fs/cgroup/", check=False)
    if "cgroup2fs" in stdout:
        ok("cgroup v2 active — compatible with Kubernetes 1.35 ✔")
    else:
        err("cgroup v1 detected — Kubernetes 1.35 requires cgroup v2.")
        raise SystemExit(1)


def check_binaries():
    for binary in ["kubectl", "kubeadm", "kubelet"]:
        path = shutil.which(binary)
        if path:
            ok(f"{binary} found: {path}")
        else:
            err(f"{binary} not found in PATH")
            raise SystemExit(1)


def check_cluster_health():
    """
    FIX 5: Abort (not just warn) if any expected node is missing or not Ready.
    """
    info("Checking cluster node health…")
    stdout, rc = run("kubectl get nodes -o json", check=False)
    if rc != 0:
        err("Cannot reach cluster — check KUBECONFIG and kubectl access.")
        raise SystemExit(1)

    nodes     = json.loads(stdout)["items"]
    expected  = {CONTROL_PLANE_NODE} | {w["name"] for w in WORKER_NODES}
    found     = set()
    not_ready = []

    for node in nodes:
        name = node["metadata"]["name"]
        found.add(name)
        conditions = node["status"].get("conditions", [])
        ready = next((c for c in conditions if c["type"] == "Ready"), None)
        if not ready or ready["status"] != "True":
            not_ready.append(name)

    missing = expected - found
    if missing:
        # [FIX 5] Hard abort — don't upgrade with missing nodes
        err(f"Expected nodes missing from cluster: {', '.join(missing)}")
        err("Resolve this before upgrading.")
        raise SystemExit(1)

    if not_ready:
        err(f"Nodes not Ready: {', '.join(not_ready)}")
        err("Fix cluster health before upgrading.")
        raise SystemExit(1)

    ok(f"All {len(nodes)} node(s) Ready: {', '.join(sorted(found))}")


def check_version():
    stdout, _ = run("kubeadm version -o json")
    detected = json.loads(stdout)["clientVersion"]["gitVersion"].lstrip("v")
    ok(f"Detected kubeadm version: v{detected}")

    if detected != CURRENT_VERSION:
        warn(f"Expected v{CURRENT_VERSION} but found v{detected} — update CURRENT_VERSION if intentional.")

    current_minor = int(detected.split(".")[1])
    target_minor  = int(TARGET_VERSION.split(".")[1])
    diff = target_minor - current_minor

    if diff == 0:
        warn("Already at target minor version — patch-only upgrade.")
    elif diff == 1:
        ok(f"Version skew: v1.{current_minor} → v1.{target_minor} — valid ✔")
    elif diff > 1:
        err(f"Cannot skip {diff} minor versions. Upgrade one at a time.")
        raise SystemExit(1)
    else:
        err(f"Target v{TARGET_VERSION} is older than current v{detected}.")
        raise SystemExit(1)


def check_containerd_compatibility(ssh_user, ssh_key, dry_run=False):
    """
    FIX 10: Check containerd version on all nodes before upgrading.
    FIX 2b: Now correctly uses caller-supplied ssh_user and ssh_key.
    FIX L1: Rewritten parser handles all Raspberry Pi output formats:
            'containerd containerd.io 2.2.2 <hash>'
            'containerd github.com/containerd/containerd v1.7.28 <hash>'
            Old parser only matched lines with a 'Version:' label —
            none of the Pi formats have that, causing parse to always fail.
    Kubernetes 1.35 requires containerd >= 1.6.
    """
    step("Checking containerd runtime compatibility")
    MIN_CONTAINERD = (1, 6, 0)

    def parse_containerd_version(raw: str):
        """
        Scan every whitespace-separated token for a version number,
        regardless of line format or label. Returns (major, minor, patch).
        """
        import re
        pattern = re.compile(r'^v?(\d+)\.(\d+)(?:\.(\d+))?$')
        for token in raw.split():
            m = pattern.match(token)
            if m:
                major = int(m.group(1))
                minor = int(m.group(2))
                patch = int(m.group(3)) if m.group(3) else 0
                # Sanity: skip years (2026) and IPs (192)
                if major < 10 and minor < 100:
                    return (major, minor, patch)
        return None

    # Check local (control plane)
    if not dry_run:
        stdout, rc = run("containerd --version", check=False)
        ver = parse_containerd_version(stdout)
        if ver and ver >= MIN_CONTAINERD:
            ok(f"cp1: containerd v{'.'.join(str(x) for x in ver)} — compatible ✔")
        elif ver:
            err(f"cp1: containerd v{'.'.join(str(x) for x in ver)} is below minimum {'.'.join(str(x) for x in MIN_CONTAINERD)}")
            err("Upgrade containerd before proceeding.")
            raise SystemExit(1)
        else:
            warn("cp1: Could not parse containerd version — skipping check")

    # Check each worker using the correct ssh_user and ssh_key
    for worker in WORKER_NODES:
        name, ip = worker["name"], worker["ip"]
        if dry_run:
            dry(f"containerd --version on {name} ({ip})")
            continue
        stdout, rc = run_ssh(ip, ssh_user, "containerd --version",
                             ssh_key=ssh_key, dry_run=False, check=False)
        ver = parse_containerd_version(stdout)
        if ver and ver >= MIN_CONTAINERD:
            ok(f"{name}: containerd v{'.'.join(str(x) for x in ver)} — compatible ✔")
        elif ver:
            err(f"{name}: containerd v{'.'.join(str(x) for x in ver)} is below minimum")
            err(f"Update containerd on {name}:  apt-get install -y containerd.io")
            raise SystemExit(1)
        else:
            warn(f"{name}: Could not parse containerd version — manual check recommended")


# ─────────────────────────────────────────────────────────────
#  SSH KEY LIFECYCLE  [NEW]
# ─────────────────────────────────────────────────────────────

# Path used for the temporary upgrade key — kept separate from any
# existing user keys so cleanup is surgical and safe.
UPGRADE_KEY_PATH = f"/home/{DEFAULT_SSH_USER}/.ssh/k8s_upgrade_tmp"

def setup_ssh_keys(ssh_user, dry_run=False):
    """
    Generate a temporary ed25519 key pair and copy the public key to every
    worker node using ssh-copy-id (requires the worker's password once).
    Returns the private key path to pass to all subsequent SSH calls.
    """
    step("Setting up temporary SSH key for upgrade")

    pub_key = f"{UPGRADE_KEY_PATH}.pub"

    if dry_run:
        dry(f"ssh-keygen -t ed25519 -f {UPGRADE_KEY_PATH} -N ''")
        for w in WORKER_NODES:
            dry(f"ssh-copy-id -i {pub_key} {ssh_user}@{w['ip']}")
        return UPGRADE_KEY_PATH

    # Remove any leftover key from a previous interrupted run
    for path in [UPGRADE_KEY_PATH, pub_key]:
        if os.path.exists(path):
            os.remove(path)
            logger.debug(f"Removed stale key: {path}")

    # Generate the key pair (no passphrase)
    run(
        f"ssh-keygen -t ed25519 -f {UPGRADE_KEY_PATH} -N '' -C 'k8s-upgrade-tmp'",
        dry_run=False
    )
    ok(f"Temporary key generated → {UPGRADE_KEY_PATH}")

    # Copy public key to each worker (ssh-copy-id needs interactive password)
    for worker in WORKER_NODES:
        name, ip = worker["name"], worker["ip"]
        info(f"Copying public key to {name} ({ip}) — enter {name}'s password when prompted:")
        # ssh-copy-id must run interactively (capture=False) so the password
        # prompt is visible to the user
        run(
            f"ssh-copy-id -i {pub_key} "
            f"-o StrictHostKeyChecking=no "
            f"{ssh_user}@{ip}",
            capture=False,
            dry_run=False,
            timeout=60
        )
        ok(f"Public key installed on {name} ✔")

    return UPGRADE_KEY_PATH


def cleanup_ssh_keys(ssh_user, dry_run=False):
    """
    Remove the temporary public key from every worker's authorized_keys,
    then delete the local key pair. Called after upgrade completes OR on
    any failure so keys are never left behind.
    """
    step("Cleaning up temporary SSH keys")

    pub_key_path = f"{UPGRADE_KEY_PATH}.pub"

    # Read the public key content so we can remove it from remote hosts
    pub_key_content = None
    if os.path.exists(pub_key_path) and not dry_run:
        with open(pub_key_path) as f:
            pub_key_content = f.read().strip()

    # Remove from each worker's authorized_keys
    for worker in WORKER_NODES:
        name, ip = worker["name"], worker["ip"]
        if dry_run:
            dry(f"Remove upgrade key from {name}:{ssh_user}@authorized_keys")
            continue
        if pub_key_content:
            # Use grep -v to strip the exact key line from authorized_keys
            remove_cmd = (
                f"grep -v '{pub_key_content}' ~/.ssh/authorized_keys "
                f"> ~/.ssh/authorized_keys.tmp && "
                f"mv ~/.ssh/authorized_keys.tmp ~/.ssh/authorized_keys && "
                f"chmod 600 ~/.ssh/authorized_keys"
            )
            _, rc = run_ssh(ip, ssh_user, remove_cmd,
                            ssh_key=UPGRADE_KEY_PATH, check=False)
            if rc == 0:
                ok(f"Upgrade key removed from {name} authorized_keys ✔")
            else:
                warn(f"Could not remove key from {name} — remove manually:")
                warn(f"  ssh {ssh_user}@{ip} 'grep -v k8s-upgrade-tmp ~/.ssh/authorized_keys | sponge ~/.ssh/authorized_keys'")

    # Delete local key pair
    for path in [UPGRADE_KEY_PATH, pub_key_path]:
        if os.path.exists(path):
            if not dry_run:
                os.remove(path)
                ok(f"Deleted local key: {path}")
            else:
                dry(f"Delete {path}")


def check_ssh_connectivity(ssh_user, ssh_key, dry_run=False):
    step("Testing SSH connectivity to worker nodes")
    for worker in WORKER_NODES:
        name, ip = worker["name"], worker["ip"]
        out, rc = run_ssh(ip, ssh_user, "echo SSH_OK",
                          ssh_key=ssh_key, dry_run=dry_run, check=False)
        if dry_run or "SSH_OK" in out:
            ok(f"SSH → {name} ({ip}) ✔")
        else:
            err(f"Cannot SSH to {name} ({ip}) as '{ssh_user}'.")
            err(f"Test manually:  ssh {ssh_user}@{ip} echo OK")
            raise SystemExit(1)

# ─────────────────────────────────────────────────────────────
#  CALICO / CNI CHECK  [FIX 3]
# ─────────────────────────────────────────────────────────────
def check_calico_health(label="pre-upgrade", dry_run=False):
    """
    FIX 3:  Verify Calico pods are healthy before and after upgrade.
    FIX 3b: Now checks BOTH calico-node AND calico-kube-controllers pods.
    FIX 3c: If the kubectl query fails during pre-upgrade, abort instead of warn.
    """
    step(f"Checking Calico CNI health ({label})")
    is_pre = (label == "pre-upgrade")

    if dry_run:
        dry("kubectl get pods -n kube-system -l k8s-app=calico-node -o json")
        dry("kubectl get pods -n kube-system -l k8s-app=calico-kube-controllers -o json")
        return

    def check_pod_group(selector: str, group_label: str):
        """Check all pods matching a label selector. Returns list of unhealthy pod names."""
        stdout, rc = run(
            f"kubectl get pods -n kube-system -l {selector} -o json",
            check=False
        )
        if rc != 0 or not stdout:
            msg = f"Could not query {group_label} pods (selector: {selector})"
            if is_pre:
                err(msg + " — aborting to protect cluster integrity.")
                raise SystemExit(1)
            else:
                warn(msg + " — check CNI manually.")
                return []

        pods = json.loads(stdout).get("items", [])
        if not pods:
            msg = f"No {group_label} pods found — is Calico installed correctly?"
            if is_pre:
                err(msg)
                raise SystemExit(1)
            else:
                warn(msg)
                return []

        unhealthy = []
        for pod in pods:
            name  = pod["metadata"]["name"]
            phase = pod["status"].get("phase", "Unknown")
            container_statuses = pod["status"].get("containerStatuses", [])
            all_ready = all(cs.get("ready", False) for cs in container_statuses)
            crash_loop = any(
                cs.get("state", {}).get("waiting", {}).get("reason") == "CrashLoopBackOff"
                for cs in container_statuses
            )
            if phase != "Running" or not all_ready or crash_loop:
                unhealthy.append(name)

        ok(f"{group_label}: {len(pods)} pod(s) checked")
        return unhealthy

    # Check both Calico components
    unhealthy_nodes       = check_pod_group("k8s-app=calico-node",              "calico-node")
    unhealthy_controllers = check_pod_group("k8s-app=calico-kube-controllers",  "calico-kube-controllers")
    all_unhealthy = unhealthy_nodes + unhealthy_controllers

    if all_unhealthy:
        if is_pre:
            err(f"Unhealthy Calico pods — fix before upgrading: {', '.join(all_unhealthy)}")
            raise SystemExit(1)
        else:
            warn(f"Calico pods not fully healthy post-upgrade: {', '.join(all_unhealthy)}")
            warn("Monitor:  kubectl get pods -n kube-system | grep calico")
    else:
        ok(f"All Calico pods (calico-node + calico-kube-controllers) healthy ✔  ({label})")

# ─────────────────────────────────────────────────────────────
#  BACKUPS  [FIX 11]
# ─────────────────────────────────────────────────────────────
def backup_etcd(dry_run=False, skip_backup=False):
    step("Backing up etcd snapshot")

    if not shutil.which("etcdctl"):
        if skip_backup:
            warn("etcdctl not found — skipping (--skip-backup passed).")
            return
        else:
            # [FIX 11] Hard abort if etcdctl missing and backup not explicitly skipped
            err("etcdctl not found — cannot take etcd snapshot.")
            err("Install it:  sudo apt-get install -y etcd-client")
            err("Or pass --skip-backup to bypass (NOT recommended).")
            raise SystemExit(1)

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{BACKUP_DIR}/etcd-snapshot-{ts}.db"

    if not dry_run:
        os.makedirs(BACKUP_DIR, exist_ok=True)

    cmd = (
        f"ETCDCTL_API=3 etcdctl snapshot save {backup_path} "
        f"--endpoints=https://127.0.0.1:2379 "
        f"--cacert=/etc/kubernetes/pki/etcd/ca.crt "
        f"--cert=/etc/kubernetes/pki/etcd/server.crt "
        f"--key=/etc/kubernetes/pki/etcd/server.key"
    )
    run(cmd, dry_run=dry_run, timeout=120)
    if not dry_run:
        ok(f"etcd snapshot saved → {backup_path}")


def backup_config(dry_run=False):
    step("Backing up kubeadm config and PKI")
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    if not dry_run:
        os.makedirs(BACKUP_DIR, exist_ok=True)

    config_path = f"{BACKUP_DIR}/kubeadm-config-{ts}.yaml"
    run(f"kubectl -n kube-system get configmap kubeadm-config -o yaml > {config_path}",
        dry_run=dry_run)

    pki_dst = f"{BACKUP_DIR}/pki-{ts}"
    if not dry_run:
        shutil.copytree("/etc/kubernetes/pki", pki_dst, dirs_exist_ok=False)
        ok(f"PKI backed up → {pki_dst}")
    else:
        dry(f"shutil.copytree /etc/kubernetes/pki → {pki_dst}")

    ok(f"kubeadm config backed up → {config_path}")

# ─────────────────────────────────────────────────────────────
#  APT REPOSITORY
# ─────────────────────────────────────────────────────────────
def update_apt_repo_local(dry_run=False):
    step(f"Updating local apt repo → {TARGET_MINOR}")
    new_line = (
        f"deb [signed-by={K8S_APT_KEYRING}] "
        f"https://pkgs.k8s.io/core:/stable:/{TARGET_MINOR}/deb/ /"
    )
    info(f"New repo: {new_line}")
    if not dry_run:
        with open(K8S_APT_SOURCES_FILE, "w") as f:
            f.write(new_line + "\n")
        ok(f"Written → {K8S_APT_SOURCES_FILE}")
    else:
        dry(f"Write to {K8S_APT_SOURCES_FILE}")
    run("apt-get update -q", dry_run=dry_run, timeout=120)
    ok("apt-get update complete")


def update_apt_repo_remote(name, ip, ssh_user, ssh_key, dry_run=False):
    step(f"Updating apt repo on {name} ({ip})")
    new_line = (
        f"deb [signed-by={K8S_APT_KEYRING}] "
        f"https://pkgs.k8s.io/core:/stable:/{TARGET_MINOR}/deb/ /"
    )
    run_ssh(ip, ssh_user, f"echo '{new_line}' > {K8S_APT_SOURCES_FILE}",
            ssh_key=ssh_key, dry_run=dry_run)
    run_ssh(ip, ssh_user, "apt-get update -q",
            ssh_key=ssh_key, dry_run=dry_run, timeout=120)
    ok(f"apt repo updated on {name}")

# ─────────────────────────────────────────────────────────────
#  NODE DRAIN / UNCORDON / WAIT
# ─────────────────────────────────────────────────────────────
def drain_node(node_name, dry_run=False):
    """
    FIX 4: --force and --delete-emptydir-data are now opt-in config flags,
    not always-on. This is safer for production workloads.
    """
    info(f"Draining node: {node_name}")
    cmd = (
        f"kubectl drain {node_name} "
        f"--ignore-daemonsets "
        f"--timeout={DRAIN_TIMEOUT}s"
    )
    if DRAIN_DELETE_EMPTYDIR:
        cmd += " --delete-emptydir-data"
    if DRAIN_FORCE:
        cmd += " --force"
    run(cmd, dry_run=dry_run, timeout=DRAIN_TIMEOUT + 30)
    ok(f"Node {node_name} drained")


def wait_for_apiserver(dry_run=False, timeout=180):
    """
    FIX L2: After kubelet restart on the control plane, the API server takes
    time to come back up. Calling 'kubectl uncordon' before it is ready causes
    a TLS handshake timeout (exactly what happened in the live run).
    This function polls until the API server responds before continuing.
    """
    if dry_run:
        dry(f"Wait for API server to respond (timeout={timeout}s)")
        return

    info(f"Waiting for API server to become reachable (timeout={timeout}s)…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, rc = run("kubectl get nodes --request-timeout=5s",
                    check=False, capture=True)
        if rc == 0:
            ok("API server is reachable ✔")
            return
        remaining = int(deadline - time.time())
        info(f"  API server not ready yet — retrying ({remaining}s remaining)…")
        time.sleep(8)

    err(f"API server did not become reachable within {timeout}s after kubelet restart.")
    err("Check kubelet status:  sudo systemctl status kubelet")
    raise SystemExit(1)


def uncordon_node(node_name, dry_run=False):
    info(f"Uncordoning node: {node_name}")
    run(f"kubectl uncordon {node_name}", dry_run=dry_run)
    ok(f"Node {node_name} uncordoned")


def wait_for_node_ready(node_name, dry_run=False):
    """FIX 7: Timeout raised to 300s for Raspberry Pi hardware."""
    if dry_run:
        dry(f"Wait for {node_name} to become Ready (timeout={NODE_READY_TIMEOUT}s)")
        return

    info(f"Waiting for {node_name} to become Ready (timeout={NODE_READY_TIMEOUT}s)…")
    deadline = time.time() + NODE_READY_TIMEOUT
    while time.time() < deadline:
        stdout, rc = run(
            f"kubectl get node {node_name} "
            f"-o jsonpath='{{.status.conditions[?(@.type==\"Ready\")].status}}'",
            check=False
        )
        if stdout.strip() == "True":
            ok(f"Node {node_name} is Ready ✔")
            return
        remaining = int(deadline - time.time())
        info(f"  Not Ready yet — retrying ({remaining}s remaining)…")
        time.sleep(8)

    err(f"Timed out waiting for {node_name} to become Ready after {NODE_READY_TIMEOUT}s")
    raise SystemExit(1)

# ─────────────────────────────────────────────────────────────
#  CONTROL PLANE UPGRADE
# ─────────────────────────────────────────────────────────────
def upgrade_control_plane(dry_run=False):
    banner(f"UPGRADING CONTROL PLANE: {CONTROL_PLANE_NODE}", C.CYAN)

    # [FIX 2] Detect package suffix from apt-cache
    kubeadm_pkg = detect_package_suffix("kubeadm", TARGET_VERSION, dry_run=dry_run)

    step(f"Upgrading kubeadm → {kubeadm_pkg}")
    run("apt-mark unhold kubeadm", dry_run=dry_run)
    run(f"apt-get install -y kubeadm={kubeadm_pkg}", dry_run=dry_run, timeout=180)
    run("apt-mark hold kubeadm", dry_run=dry_run)

    step("Running kubeadm upgrade plan")
    run(f"kubeadm upgrade plan v{TARGET_VERSION}",
        dry_run=dry_run, capture=False, timeout=120)

    step(f"Applying kubeadm upgrade v{TARGET_VERSION}")
    run(f"kubeadm upgrade apply v{TARGET_VERSION} --yes",
        dry_run=dry_run, capture=False, timeout=600)
    ok("kubeadm upgrade apply complete")

    step(f"Draining {CONTROL_PLANE_NODE}")
    drain_node(CONTROL_PLANE_NODE, dry_run=dry_run)

    # [FIX 2] Detect suffix for kubelet/kubectl too
    kubelet_pkg = detect_package_suffix("kubelet", TARGET_VERSION, dry_run=dry_run)
    kubectl_pkg = detect_package_suffix("kubectl", TARGET_VERSION, dry_run=dry_run)

    step(f"Upgrading kubelet ({kubelet_pkg}) and kubectl ({kubectl_pkg})")
    run("apt-mark unhold kubelet kubectl", dry_run=dry_run)
    run(f"apt-get install -y kubelet={kubelet_pkg} kubectl={kubectl_pkg}",
        dry_run=dry_run, timeout=180)
    run("apt-mark hold kubelet kubectl", dry_run=dry_run)

    step("Reloading systemd and restarting kubelet")
    run("systemctl daemon-reload", dry_run=dry_run)
    run("systemctl restart kubelet", dry_run=dry_run)
    if not dry_run:
        time.sleep(8)

    # FIX L2: Wait for API server to come back before running any kubectl commands
    wait_for_apiserver(dry_run=dry_run)

    step(f"Uncordoning {CONTROL_PLANE_NODE}")
    uncordon_node(CONTROL_PLANE_NODE, dry_run=dry_run)
    wait_for_node_ready(CONTROL_PLANE_NODE, dry_run=dry_run)

    ok(f"Control plane {CONTROL_PLANE_NODE} upgrade complete ✔")

# ─────────────────────────────────────────────────────────────
#  WORKER NODE UPGRADE
# ─────────────────────────────────────────────────────────────
def upgrade_worker_node(worker, ssh_user, ssh_key, dry_run=False):
    name, ip = worker["name"], worker["ip"]
    banner(f"UPGRADING WORKER NODE: {name}  ({ip})", C.BLUE)

    ssh = lambda cmd, **kw: run_ssh(ip, ssh_user, cmd,
                                     ssh_key=ssh_key, dry_run=dry_run, **kw)

    update_apt_repo_remote(name, ip, ssh_user, ssh_key, dry_run=dry_run)

    # [FIX 2] Detect package suffix on worker via SSH
    if dry_run:
        kubeadm_pkg = kubelet_pkg = kubectl_pkg = f"{TARGET_VERSION}-1.1"
    else:
        stdout, _ = run_ssh(ip, ssh_user,
                            f"apt-cache madison kubeadm | grep {TARGET_VERSION} | head -1",
                            ssh_key=ssh_key, check=False)
        parts = [p.strip() for p in stdout.split("|")]
        kubeadm_pkg = parts[1] if len(parts) >= 2 and parts[1].startswith(TARGET_VERSION) \
                      else f"{TARGET_VERSION}-1.1"

        stdout, _ = run_ssh(ip, ssh_user,
                            f"apt-cache madison kubelet | grep {TARGET_VERSION} | head -1",
                            ssh_key=ssh_key, check=False)
        parts = [p.strip() for p in stdout.split("|")]
        kubelet_pkg = parts[1] if len(parts) >= 2 and parts[1].startswith(TARGET_VERSION) \
                      else f"{TARGET_VERSION}-1.1"

        kubectl_pkg = kubelet_pkg  # always same suffix

        ok(f"Worker package versions: kubeadm={kubeadm_pkg} kubelet={kubelet_pkg}")

    step(f"Upgrading kubeadm on {name} → {kubeadm_pkg}")
    ssh("apt-mark unhold kubeadm")
    ssh(f"apt-get install -y kubeadm={kubeadm_pkg}", timeout=180)
    ssh("apt-mark hold kubeadm")

    step(f"Running kubeadm upgrade node on {name}")
    ssh("kubeadm upgrade node", timeout=300)
    ok(f"kubeadm upgrade node complete on {name}")

    step(f"Draining worker: {name}")
    drain_node(name, dry_run=dry_run)

    step(f"Upgrading kubelet and kubectl on {name}")
    ssh("apt-mark unhold kubelet kubectl")
    ssh(f"apt-get install -y kubelet={kubelet_pkg} kubectl={kubectl_pkg}", timeout=180)
    ssh("apt-mark hold kubelet kubectl")

    step(f"Reloading systemd and restarting kubelet on {name}")
    ssh("systemctl daemon-reload")
    ssh("systemctl restart kubelet")
    if not dry_run:
        time.sleep(8)

    step(f"Uncordoning {name}")
    uncordon_node(name, dry_run=dry_run)
    wait_for_node_ready(name, dry_run=dry_run)

    ok(f"Worker {name} upgrade complete ✔")

# ─────────────────────────────────────────────────────────────
#  POST-UPGRADE VERIFICATION  [FIX 6]
# ─────────────────────────────────────────────────────────────
def verify_cluster(dry_run=False):
    banner("POST-UPGRADE VERIFICATION", C.GREEN)

    if dry_run:
        dry("kubectl get nodes -o wide")
        dry("kubectl get pods -n kube-system (check phase + containerStatuses)")
        dry("Calico pod health check")
        return

    # ── Node versions ──────────────────────────────────────────
    step("Checking node versions and readiness")
    stdout, _ = run("kubectl get nodes -o json")
    nodes = json.loads(stdout)["items"]
    all_good = True

    for node in nodes:
        name       = node["metadata"]["name"]
        version    = node["status"]["nodeInfo"]["kubeletVersion"]
        conditions = node["status"].get("conditions", [])
        ready_cond = next((c for c in conditions if c["type"] == "Ready"), {})
        is_ready   = ready_cond.get("status") == "True"

        if is_ready and f"v{TARGET_VERSION}" in version:
            ok(f"{name:<12}  version={version}  Ready=True")
        else:
            warn(f"{name:<12}  version={version}  Ready={is_ready}  ← attention needed")
            all_good = False

    # ── kube-system pod health (FIX 6) ───────────────────────
    step("Checking kube-system pod health (phase + container readiness)")
    stdout, _ = run("kubectl get pods -n kube-system -o json", check=False)
    try:
        pods = json.loads(stdout)["items"]
        problem_pods = []

        for pod in pods:
            pod_name = pod["metadata"]["name"]
            phase    = pod["status"].get("phase", "Unknown")

            # [FIX 6] Check container-level readiness and CrashLoopBackOff
            container_statuses = pod["status"].get("containerStatuses", [])
            crash_loop = any(
                cs.get("state", {}).get("waiting", {}).get("reason") == "CrashLoopBackOff"
                for cs in container_statuses
            )
            not_ready_containers = [
                cs["name"] for cs in container_statuses
                if not cs.get("ready", False)
            ]

            if phase not in ("Running", "Succeeded") or crash_loop or not_ready_containers:
                reason = []
                if phase not in ("Running", "Succeeded"):
                    reason.append(f"phase={phase}")
                if crash_loop:
                    reason.append("CrashLoopBackOff")
                if not_ready_containers:
                    reason.append(f"containers not ready: {not_ready_containers}")
                problem_pods.append(f"{pod_name} ({', '.join(reason)})")

        if problem_pods:
            warn("Unhealthy kube-system pods:")
            for p in problem_pods:
                warn(f"  • {p}")
            all_good = False
        else:
            ok(f"All {len(pods)} kube-system pods healthy ✔")

    except (json.JSONDecodeError, KeyError) as e:
        warn(f"Could not parse pod list ({e}) — check manually: kubectl get pods -n kube-system")

    # ── Calico post-upgrade check ──────────────────────────────
    check_calico_health(label="post-upgrade", dry_run=dry_run)

    # ── containerd reminder ────────────────────────────────────
    print()
    warn("REMINDER: Update containerd on worker1 to match cp1:")
    info("  ssh ubuntu@192.168.8.102 'sudo apt-get install -y containerd.io'")
    info("  ssh ubuntu@192.168.8.102 'sudo systemctl restart containerd'")

    # ── Final result ───────────────────────────────────────────
    print()
    if all_good:
        banner(f"🎉  UPGRADE SUCCESSFUL — All nodes on v{TARGET_VERSION}", C.GREEN)
    else:
        warn("Upgrade finished but some items need attention — review output above.")

# ─────────────────────────────────────────────────────────────
#  ARGUMENT PARSING
# ─────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(
        description=f"Upgrade Kubernetes cluster v{CURRENT_VERSION} → v{TARGET_VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Preview all steps without making changes")
    p.add_argument("--ssh-user", default=DEFAULT_SSH_USER,
                   help=f"SSH user for worker nodes (default: {DEFAULT_SSH_USER})")
    p.add_argument("--ssh-key", default=None,
                   help="Path to SSH private key (default: ssh-agent)")
    p.add_argument("--skip-backup", action="store_true",
                   help="Skip etcd backup — NOT recommended (aborts by default if etcdctl missing)")
    p.add_argument("--control-plane-only", action="store_true",
                   help="Upgrade control plane only")
    p.add_argument("--workers-only", action="store_true",
                   help="Upgrade workers only (control plane already done)")
    return p.parse_args()

# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
def main():
    args = parse_args()
    setup_logging()

    banner("Kubernetes Cluster Upgrade  (hardened v5)", C.CYAN)
    info(f"Upgrade path   :  v{CURRENT_VERSION}  →  v{TARGET_VERSION}")
    info(f"Control plane  :  {CONTROL_PLANE_NODE}  ({CONTROL_PLANE_IP})")
    info(f"Worker nodes   :  {', '.join(w['name'] + ' (' + w['ip'] + ')' for w in WORKER_NODES)}")
    info(f"SSH user       :  {args.ssh_user}")
    info(f"SSH key        :  {args.ssh_key or '(system default / ssh-agent)'}")
    info(f"Log file       :  {LOG_FILE}")
    info(f"Mode           :  {'DRY RUN — zero changes' if args.dry_run else 'LIVE — changes WILL be applied'}")
    info(f"Drain flags    :  --delete-emptydir-data={'yes' if DRAIN_DELETE_EMPTYDIR else 'no'}  --force={'yes' if DRAIN_FORCE else 'no'}")

    if not args.dry_run:
        print()
        print(f"  {C.YELLOW}{C.BOLD}⚠  This will upgrade your Kubernetes cluster to v{TARGET_VERSION}.{C.RESET}")
        print(f"  {C.YELLOW}   Workloads will be briefly disrupted during each node drain.{C.RESET}")
        confirm = input(f"\n  {C.BOLD}Type 'yes' to continue or anything else to abort: {C.RESET}").strip()
        if confirm.lower() != "yes":
            info("Aborted.")
            raise SystemExit(0)

    # ── Auto SSH key setup ────────────────────────────────────
    # If no --ssh-key provided, generate a temporary key and install
    # it on workers automatically. It will be cleaned up after upgrade.
    auto_key = args.ssh_key is None and not args.control_plane_only
    if auto_key:
        args.ssh_key = setup_ssh_keys(args.ssh_user, dry_run=args.dry_run)

    # ── Pre-flight ────────────────────────────────────────────
    _phase = "pre-flight"
    try:
        banner("PRE-FLIGHT CHECKS", C.YELLOW)
        check_root()
        check_hostname()
        check_cgroup_v2()
        check_binaries()
        check_cluster_health()
        check_version()
        check_containerd_compatibility(args.ssh_user, args.ssh_key, dry_run=args.dry_run)
        check_calico_health(label="pre-upgrade", dry_run=args.dry_run)
        check_ssh_connectivity(args.ssh_user, args.ssh_key, dry_run=args.dry_run)

        # ── Backups ───────────────────────────────────────────
        _phase = "backup"
        if not args.workers_only:
            backup_etcd(dry_run=args.dry_run, skip_backup=args.skip_backup)
            backup_config(dry_run=args.dry_run)

        # ── Control plane ─────────────────────────────────────
        _phase = "control-plane"
        if not args.workers_only:
            update_apt_repo_local(dry_run=args.dry_run)
            upgrade_control_plane(dry_run=args.dry_run)

        # ── Workers ───────────────────────────────────────────
        _phase = "worker"
        if not args.control_plane_only:
            banner(f"UPGRADING {len(WORKER_NODES)} WORKER NODE(S)", C.BLUE)
            for i, worker in enumerate(WORKER_NODES, 1):
                info(f"Worker {i}/{len(WORKER_NODES)}: {worker['name']} ({worker['ip']})")
                upgrade_worker_node(worker, args.ssh_user, args.ssh_key,
                                    dry_run=args.dry_run)

    except SystemExit as e:
        if e.code != 0:
            print_recovery_guidance(_phase)
        # Always clean up keys, even on failure
        if auto_key:
            cleanup_ssh_keys(args.ssh_user, dry_run=args.dry_run)
        raise

    # ── Verification ──────────────────────────────────────────
    verify_cluster(dry_run=args.dry_run)

    # ── SSH key cleanup ───────────────────────────────────────
    if auto_key:
        cleanup_ssh_keys(args.ssh_user, dry_run=args.dry_run)

    logger.info("Upgrade script completed.")
    info(f"\nFull log saved to: {LOG_FILE}")


if __name__ == "__main__":
    main()
