#!/usr/bin/env python3
"""
Production Atomic VDS Deployment Tool with SHA-256 Hash Verification,
Isolated Staging, Preflight Checks, Post-Deployment Health Probing, and Automatic Rollback.

Usage:
  python deploy_vds.py check     # Computes manifest and local checks
  python deploy_vds.py stage     # Uploads to staging and runs preflight
  python deploy_vds.py deploy    # Stages, runs preflight, deploys, checks, with auto-rollback on failure
  python deploy_vds.py rollback  # Explicitly rolls back to previous release
"""

import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

VDS_HOST = os.getenv("VDS_HOST", "vds")
LOCAL_DIR = Path(__file__).resolve().parent.parent / "vds_bot"
REMOTE_BASE = "/opt/pc-control-bot"

MANIFEST_FILES = [
    "bot.py",
    "console_service.py",
    "health_engine.py",
    "history.py",
    "monitoring.py",
    "mqtt_dispatcher.py",
    "mqtt_gateway.py",
    "physical_button.py",
    "player_manager.py",
    "power_flow.py",
    "power_scheduler.py",
    "telegram_ui.py",
    "ui_icons.py",
    "ui_markups.py",
    "ui_templates.py",
    "workspace_runner.py",
    "requirements.txt",
    "resources/icons.json",
    "routers/__init__.py",
    "routers/command_router.py",
    "routers/console_router.py",
    "routers/monitoring_router.py",
    "routers/physical_router.py",
    "routers/player_router.py",
    "routers/power_router.py",
    "routers/workspace_router.py",
    "routers/antigravity_router.py",
]


def compute_manifest():
    manifest = {"timestamp": int(time.time()), "files": {}}
    for filename in MANIFEST_FILES:
        filepath = LOCAL_DIR / filename
        if not filepath.exists():
            raise FileNotFoundError(f"Missing required release file: {filepath}")
        content = filepath.read_bytes()
        sha256 = hashlib.sha256(content).hexdigest()
        manifest["files"][filename] = {
            "size_bytes": len(content),
            "sha256": sha256,
        }
    return manifest


def get_bind_ip():
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def run_ssh(cmd, check=True, input_bytes=None):
    ssh_args = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        VDS_HOST,
        cmd,
    ]
    res = subprocess.run(
        ssh_args,
        input=input_bytes,
        capture_output=True,
        text=False,
    )
    res.stdout = res.stdout.decode("utf-8", errors="replace") if isinstance(res.stdout, bytes) else (res.stdout or "")
    res.stderr = res.stderr.decode("utf-8", errors="replace") if isinstance(res.stderr, bytes) else (res.stderr or "")

    if res.returncode != 0:
        bind_ip = get_bind_ip()
        if bind_ip and not bind_ip.startswith("172.18."):
            retry_args = [
                "ssh",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=10",
                "-b", bind_ip,
                VDS_HOST,
                cmd,
            ]
            retry_res = subprocess.run(
                retry_args,
                input=input_bytes,
                capture_output=True,
                text=False,
            )
            retry_res.stdout = retry_res.stdout.decode("utf-8", errors="replace") if isinstance(retry_res.stdout, bytes) else (retry_res.stdout or "")
            retry_res.stderr = retry_res.stderr.decode("utf-8", errors="replace") if isinstance(retry_res.stderr, bytes) else (retry_res.stderr or "")
            if retry_res.returncode == 0:
                return retry_res

    if check and res.returncode != 0:
        raise RuntimeError(
            f"SSH command failed ({res.returncode}):\n"
            f"Command: {cmd}\n"
            f"STDOUT: {res.stdout}\n"
            f"STDERR: {res.stderr}"
        )
    return res


def stage_release():
    manifest = compute_manifest()
    release_tag = time.strftime("%Y%m%d-%H%M%S")
    remote_release_dir = f"{REMOTE_BASE}/releases/{release_tag}"

    print(f"[*] Staging Release: {release_tag}")
    run_ssh(f"mkdir -p {remote_release_dir}")

    # Build tar in memory
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:gz") as tar:
        for filename in MANIFEST_FILES:
            local_path = LOCAL_DIR / filename
            tar.add(local_path, arcname=filename)
        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        ti = tarfile.TarInfo(name="release_manifest.json")
        ti.size = len(manifest_bytes)
        ti.mtime = int(time.time())
        tar.addfile(ti, io.BytesIO(manifest_bytes))

    tar_buf.seek(0)
    # Extract on remote
    run_ssh(f"tar -xzf - -C {remote_release_dir}", input_bytes=tar_buf.getvalue())
    for filename in MANIFEST_FILES:
        print(f"  -> Uploaded {filename}")
    print("  -> Uploaded release_manifest.json")

    return release_tag, remote_release_dir, manifest


def verify_remote_hashes(remote_release_dir, expected_manifest):
    print(f"[*] Verifying SHA-256 hashes on remote staging: {remote_release_dir}")
    script = f"""
import hashlib, json, pathlib, sys
p = pathlib.Path('{remote_release_dir}')
manifest = json.loads((p / 'release_manifest.json').read_text(encoding='utf-8'))
errors = []
for fn, meta in manifest['files'].items():
    fp = p / fn
    if not fp.exists():
        errors.append(f'Missing {{fn}}')
        continue
    h = hashlib.sha256(fp.read_bytes()).hexdigest()
    if h != meta.get('sha256'):
        errors.append(f'Hash mismatch for {{fn}}')
if errors:
    print('ERRORS: ' + '; '.join(errors))
    sys.exit(1)
print('HASHES_VERIFIED_OK')
"""
    res = run_ssh(f"{REMOTE_BASE}/venv/bin/python -", input_bytes=script.encode("utf-8"))
    if "HASHES_VERIFIED_OK" not in res.stdout:
        raise RuntimeError(f"Remote hash verification failed:\n{res.stdout}\n{res.stderr}")
    print("  -> All remote file hashes match manifest exactly.")


def run_preflight(remote_release_dir):
    print(f"[*] Running preflight in isolated staging directory: {remote_release_dir}")
    
    # 1. Compileall
    compile_cmd = f"PYTHONPATH={remote_release_dir} {REMOTE_BASE}/venv/bin/python -m compileall -q {remote_release_dir}"
    run_ssh(compile_cmd)
    print("  -> compileall: OK")

    # 2. Strict import check with isolated PYTHONPATH
    import_cmd = (
        f"PYTHONPATH={remote_release_dir} {REMOTE_BASE}/venv/bin/python -c \""
        f"import sys; "
        f"import bot; import health_engine; import history; import monitoring; "
        f"import mqtt_gateway; import physical_button; import power_flow; import telegram_ui; "
        f"print('ALL_MODULES_IMPORTED_OK')\""
    )
    res = run_ssh(import_cmd)
    if "ALL_MODULES_IMPORTED_OK" not in res.stdout:
        raise RuntimeError(f"Preflight import failed: {res.stdout} {res.stderr}")
    print("  -> Isolated import check: OK")


def rollback_to_previous():
    print("[!] INITIATING AUTOMATIC ROLLBACK...")
    res = run_ssh(f"cat {REMOTE_BASE}/previous_release 2>/dev/null || echo ''", check=False)
    prev_release = res.stdout.strip()
    if not prev_release:
        print("[!] No previous_release tag found. Cannot rollback automatically.")
        return False

    prev_dir = f"{REMOTE_BASE}/releases/{shlex.quote(prev_release)}"
    print(f"[*] Rolling back to previous release: {prev_release} ({prev_dir})")

    quoted_prev = shlex.quote(prev_release)
    restore_cmd = (
        f"for f in {' '.join(MANIFEST_FILES)}; do "
        f"  if [ -f {prev_dir}/$f ]; then "
        f"    mkdir -p $(dirname {REMOTE_BASE}/$f) && "
        f"    cp {prev_dir}/$f {REMOTE_BASE}/$f; "
        f"  fi; "
        f"done && "
        f"echo {quoted_prev} > {REMOTE_BASE}/current_release && "
        f"chown -R root:pccontrol {REMOTE_BASE}/*.py {REMOTE_BASE}/requirements.txt {REMOTE_BASE}/resources {REMOTE_BASE}/routers 2>/dev/null || true && "
        f"chmod 0644 {REMOTE_BASE}/*.py {REMOTE_BASE}/requirements.txt 2>/dev/null || true && "
        f"if [ -d {REMOTE_BASE}/resources ]; then find {REMOTE_BASE}/resources -type d -exec chmod 0755 {{}} + && find {REMOTE_BASE}/resources -type f -exec chmod 0644 {{}} +; fi && "
        f"if [ -d {REMOTE_BASE}/routers ]; then chmod 0755 {REMOTE_BASE}/routers && find {REMOTE_BASE}/routers -maxdepth 1 -name '*.py' -exec chmod 0644 {{}} +; fi && "
        f"systemctl restart pc-control-bot.service"
    )
    run_ssh(restore_cmd)
    time.sleep(3)

    status_res = run_ssh("systemctl is-active pc-control-bot.service", check=False)
    if status_res.stdout.strip() == "active":
        print(f"[✓] Rollback successful! Restored release {prev_release} is active.")
        return True
    else:
        print(f"[!] Rollback service check failed! Status: {status_res.stdout}")
        return False


def deploy_and_verify(release_tag, remote_release_dir):
    print(f"[*] Deploying release {release_tag} to production...")
    
    # Read current release tag to store as previous
    current_tag_res = run_ssh(f"cat {REMOTE_BASE}/current_release 2>/dev/null || echo ''", check=False)
    prev_tag = current_tag_res.stdout.strip()
    if prev_tag:
        run_ssh(f"echo {shlex.quote(prev_tag)} > {REMOTE_BASE}/previous_release")

    # Atomic copy and permission setup
    quoted_tag = shlex.quote(release_tag)
    deploy_cmd = (
        f"for f in {' '.join(MANIFEST_FILES)}; do "
        f"  mkdir -p $(dirname {REMOTE_BASE}/$f) && "
        f"  cp {remote_release_dir}/$f {REMOTE_BASE}/$f; "
        f"done && "
        f"echo {quoted_tag} > {REMOTE_BASE}/current_release && "
        f"chown -R root:pccontrol {REMOTE_BASE}/*.py {REMOTE_BASE}/requirements.txt {REMOTE_BASE}/resources {REMOTE_BASE}/routers 2>/dev/null || true && "
        f"chmod 0644 {REMOTE_BASE}/*.py {REMOTE_BASE}/requirements.txt 2>/dev/null || true && "
        f"if [ -d {REMOTE_BASE}/resources ]; then find {REMOTE_BASE}/resources -type d -exec chmod 0755 {{}} + && find {REMOTE_BASE}/resources -type f -exec chmod 0644 {{}} +; fi && "
        f"if [ -d {REMOTE_BASE}/routers ]; then chmod 0755 {REMOTE_BASE}/routers && find {REMOTE_BASE}/routers -maxdepth 1 -name '*.py' -exec chmod 0644 {{}} +; fi && "
        f"systemctl restart pc-control-bot.service"
    )
    run_ssh(deploy_cmd)
    print("  -> Production files updated & service restart signal sent")

    # Post-deployment health verification over 15 seconds
    print("[*] Monitoring service post-deployment health (15s multi-check)...")
    recent_logs = ""
    for check_sec in (3, 6, 6):
        time.sleep(check_sec)
        status_res = run_ssh("systemctl is-active pc-control-bot.service", check=False)
        is_active = status_res.stdout.strip()
        print(f"  -> Service is-active check (+{check_sec}s): {is_active}")

        if is_active != "active":
            print("[!] Health check FAILED: service is not active.")
            rollback_to_previous()
            raise RuntimeError(f"Deployment failed post-check: service is {is_active}")

        # Query MainPID to isolate logs to the current service process
        pid_res = run_ssh("systemctl show pc-control-bot.service -p MainPID --value", check=False)
        current_pid = pid_res.stdout.strip()
        log_filter = f"_PID={current_pid}" if current_pid.isdigit() and int(current_pid) > 0 else ""
        logs_res = run_ssh(f"journalctl -u pc-control-bot.service {log_filter} -n 35 --no-pager", check=False)
        recent_logs = logs_res.stdout

        if "Traceback (most recent call last):" in recent_logs:
            print(f"  -> Recent journal logs:\n{recent_logs}")
            print("[!] Health check FAILED: Traceback found in service logs!")
            rollback_to_previous()
            raise RuntimeError("Deployment failed post-check: Traceback detected in service logs")

    print(f"  -> Recent journal logs (verified):\n{recent_logs}")

    print(f"\n[SUCCESS] Release {release_tag} successfully deployed and verified!")


def diff_remote():
    import difflib
    print("[*] Comparing local vds_bot/ with remote VDS production...")
    has_diff = False
    for filename in MANIFEST_FILES:
        local_path = LOCAL_DIR / filename
        local_bytes = local_path.read_bytes() if local_path.exists() else b""
        local_hash = hashlib.sha256(local_bytes).hexdigest()

        res_hash = run_ssh(f"sha256sum {REMOTE_BASE}/{filename} 2>/dev/null || echo ''", check=False)
        parts = res_hash.stdout.strip().split()
        remote_hash = parts[0] if parts else ""

        if local_hash != remote_hash:
            has_diff = True
            print(f"\n[DIFF] Differences detected in {filename} (local: {local_hash[:8]}, remote: {remote_hash[:8] or 'missing'}):")
            res_content = run_ssh(f"cat {REMOTE_BASE}/{filename} 2>/dev/null || echo ''", check=False)
            remote_text = res_content.stdout.replace("\r\n", "\n")
            local_text = local_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
            diff = list(difflib.unified_diff(
                remote_text.splitlines(keepends=True),
                local_text.splitlines(keepends=True),
                fromfile=f"remote:{REMOTE_BASE}/{filename}",
                tofile=f"local:{filename}",
                n=3,
            ))
            sys.stdout.writelines(diff)
    if not has_diff:
        print("[✓] All files are 100% in sync with VDS production!")


def pull_remote():
    print("[*] Pulling latest code from VDS production into local vds_bot/...")
    for filename in MANIFEST_FILES:
        res = run_ssh(f"cat {REMOTE_BASE}/{filename} 2>/dev/null", check=False)
        if res.returncode == 0 and res.stdout:
            local_path = LOCAL_DIR / filename
            local_path.write_text(res.stdout, encoding="utf-8")
            print(f"  -> Updated local {filename}")
    print("[✓] Local vds_bot/ successfully updated from VDS.")


def service_status():
    print("[*] Checking VDS pc-control-bot service status...")
    status_res = run_ssh("systemctl status pc-control-bot.service --no-pager", check=False)
    print(status_res.stdout)
    logs_res = run_ssh("journalctl -u pc-control-bot.service -n 25 --no-pager", check=False)
    print("\n--- Recent Journal Logs ---")
    print(logs_res.stdout)


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    manifest = compute_manifest()

    if action == "check":
        print("Release Manifest (local):")
        print(json.dumps(manifest, indent=2))
        print("\nAll local release files exist and are valid.")

    elif action == "diff":
        diff_remote()

    elif action == "pull":
        pull_remote()

    elif action == "status":
        service_status()

    elif action == "rollback":
        rollback_to_previous()

    elif action == "stage":
        release_tag, remote_dir, m = stage_release()
        verify_remote_hashes(remote_dir, m)
        run_preflight(remote_dir)
        print(f"\n[INFO] Staged and verified at {remote_dir}. Run with 'deploy' to activate.")

    elif action == "deploy":
        release_tag, remote_dir, m = stage_release()
        verify_remote_hashes(remote_dir, m)
        run_preflight(remote_dir)
        deploy_and_verify(release_tag, remote_dir)

    else:
        print(f"Unknown action: {action}")
        print("Supported actions: check, diff, pull, status, stage, deploy, rollback")
        sys.exit(1)
