"""
tls_cert.py

Generates/locates the TLS cert+key this app is served over. Deliberately a
standalone module with no other project imports: gunicorn.conf.py calls this
from the arbiter process, before any worker process imports app.py.
Importing app.py runs its top-level code - opening the ESP32 serial
connection, starting the camera - as import-time side effects, so this
staying import-clean means the arbiter never triggers those, only the one
worker process does.
"""

import json
import os
import subprocess


def ensure_self_signed_cert(cert_path="certs/robot.crt", key_path="certs/robot.key"):
    """
    Generates a persistent self-signed TLS cert on first run (reused after
    that) so the control page can be served over HTTPS.

    This isn't optional polish: browsers refuse navigator.mediaDevices
    .getUserMedia() on any origin except HTTPS/localhost, full stop, even
    for a private LAN/Tailscale IP you fully trust - so without this the
    voice record button fails instantly with no real permission prompt at
    all. A self-signed cert means every browser/device shows a one-time
    "connection not private" warning to click past on first visit.

    If you'd rather not see that warning, run this once:
        sudo tailscale set --operator=$USER
    then delete certs/ and restart the server - Tailscale's own
    `tailscale cert` issues a real, browser-trusted certificate for this
    machine's MagicDNS name, and that path is preferred automatically when
    it works.
    """
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path

    os.makedirs(os.path.dirname(cert_path), exist_ok=True)

    dns_name = None
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5, check=True
        )
        dns_name = json.loads(result.stdout).get("Self", {}).get("DNSName", "").rstrip(".")
    except Exception:
        pass

    if dns_name:
        try:
            subprocess.run(
                ["tailscale", "cert", "--cert-file", cert_path, "--key-file", key_path, dns_name],
                check=True, capture_output=True, text=True, timeout=30,
            )
            print(f"[tls_cert] issued a Tailscale-trusted cert for {dns_name}")
            return cert_path, key_path
        except Exception as e:
            print(f"[tls_cert] tailscale cert unavailable ({e}), falling back to a self-signed cert")

    sans = ["DNS:localhost", "IP:127.0.0.1"]
    if dns_name:
        sans.append(f"DNS:{dns_name}")
    try:
        for ip in subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5, check=True).stdout.split():
            sans.append(f"IP:{ip}")
    except Exception:
        pass

    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", key_path, "-out", cert_path, "-days", "365",
            "-subj", "/CN=robot-control",
            "-addext", f"subjectAltName={','.join(sans)}",
        ],
        check=True, capture_output=True, text=True,
    )
    print(f"[tls_cert] generated a self-signed cert at {cert_path} (SANs: {sans})")
    return cert_path, key_path
