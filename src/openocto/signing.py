"""Ed25519 signing for openOcto public networks.

Lazy-loaded: only imported when a public network actually needs signing.
Priority: ssh-keygen CLI → cryptography library.

This module is never imported at startup. It is only called from
permission-checking code paths in public network mode.
"""

import base64
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

_OCTO_KEY_DIR = Path.home() / ".octo" / "keys"


def _ensure_key_dir():
    _OCTO_KEY_DIR.mkdir(parents=True, exist_ok=True)


def _key_path(identity: str) -> Path:
    return _OCTO_KEY_DIR / f"{identity}_ed25519"


# ---- Key Generation ----

def generate_keypair(identity: str) -> tuple:
    """Generate Ed25519 keypair. Returns (private_key_b64, public_key_b64).

    Tries ssh-keygen first, falls back to cryptography library.
    """
    # Try ssh-keygen
    try:
        return _generate_ssh_keygen(identity)
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        pass

    # Fallback: cryptography library
    try:
        return _generate_cryptography(identity)
    except ImportError:
        raise SystemExit(
            "[octo] Public network requires Ed25519 signing.\n"
            "  Option 1: Install OpenSSH (ssh-keygen)\n"
            "  Option 2: pip install cryptography"
        )


def _generate_ssh_keygen(identity: str) -> tuple:
    _ensure_key_dir()
    key_file = _key_path(identity)
    # Remove existing key files to avoid ssh-keygen prompt
    for f in [key_file, key_file.with_suffix(".pub")]:
        if f.exists():
            f.unlink()

    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_file),
         "-N", "", "-C", f"octo:{identity}"],
        capture_output=True, check=True, timeout=10,
    )
    private_pem = key_file.read_text().strip()
    public_pem = key_file.with_suffix(".pub").read_text().strip()
    private_b64 = base64.b64encode(private_pem.encode()).decode()
    public_b64 = base64.b64encode(public_pem.encode()).decode()
    return private_b64, public_b64


def _generate_cryptography(identity: str) -> tuple:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    )
    # Also save to disk for ssh-keygen compat
    _ensure_key_dir()
    key_file = _key_path(identity)
    key_file.write_bytes(private_bytes)
    key_file.chmod(0o600)
    key_file.with_suffix(".pub").write_bytes(public_bytes)

    private_b64 = base64.b64encode(private_bytes).decode()
    public_b64 = base64.b64encode(public_bytes).decode()
    return private_b64, public_b64


# ---- Signing ----

def sign_message(message: str, identity: str) -> str:
    """Sign a message string. Returns base64-encoded signature.

    Tries ssh-keygen first, falls back to cryptography library.
    """
    # Try ssh-keygen
    try:
        return _sign_ssh_keygen(message, identity)
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        pass

    # Fallback: cryptography
    try:
        return _sign_cryptography(message, identity)
    except ImportError:
        raise SystemExit(
            "[octo] Signing requires ssh-keygen or: pip install cryptography"
        )


def _sign_ssh_keygen(message: str, identity: str) -> str:
    key_file = _key_path(identity)
    if not key_file.exists():
        raise FileNotFoundError(f"Key not found: {key_file}")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(message)
        msg_file = f.name

    try:
        result = subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-f", str(key_file),
             "-n", "octo", msg_file],
            capture_output=True, text=True, check=True, timeout=10,
        )
        sig_file = msg_file + ".sig"
        if os.path.exists(sig_file):
            with open(sig_file) as f:
                sig = f.read().strip()
            os.unlink(sig_file)
            return base64.b64encode(sig.encode()).decode()
        # Some ssh-keygen versions output to stdout
        return base64.b64encode(result.stdout.strip().encode()).decode()
    finally:
        os.unlink(msg_file)


def _sign_cryptography(message: str, identity: str) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    key_file = _key_path(identity)
    if not key_file.exists():
        raise FileNotFoundError(f"Key not found: {key_file}")

    private_key = serialization.load_ssh_private_key(
        key_file.read_bytes(), password=None,
    )
    signature = private_key.sign(message.encode())
    return base64.b64encode(signature).decode()


# ---- Verification ----

def verify_signature(message: str, signature_b64: str, public_key_b64: str) -> bool:
    """Verify a signature against a public key. Returns True if valid."""
    # Try cryptography first (more reliable for verification)
    try:
        return _verify_cryptography(message, signature_b64, public_key_b64)
    except ImportError:
        pass

    # Try ssh-keygen
    try:
        return _verify_ssh_keygen(message, signature_b64, public_key_b64)
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        pass

    # Can't verify — reject
    return False


def _verify_cryptography(message: str, signature_b64: str, public_key_b64: str) -> bool:
    from cryptography.hazmat.primitives.serialization import load_ssh_public_key

    public_key_bytes = base64.b64decode(public_key_b64)
    public_key = load_ssh_public_key(public_key_bytes)
    signature = base64.b64decode(signature_b64)
    try:
        public_key.verify(signature, message.encode())
        return True
    except Exception:
        return False


def _verify_ssh_keygen(message: str, signature_b64: str, public_key_b64: str) -> bool:
    public_key_str = base64.b64decode(public_key_b64).decode()
    sig_str = base64.b64decode(signature_b64).decode()

    # ssh-keygen -Y verify needs an allowed_signers file and a signature file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as mf:
        mf.write(message)
        msg_file = mf.name

    with tempfile.NamedTemporaryFile(mode="w", suffix=".sig", delete=False) as sf:
        sf.write(sig_str)
        sig_file = sf.name

    # Extract key type and key data from public key line
    # Format: "ssh-ed25519 AAAA... comment"
    parts = public_key_str.strip().split()
    if len(parts) >= 2:
        principal = parts[2] if len(parts) > 2 else "octo"
    else:
        principal = "octo"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".allowed", delete=False) as af:
        af.write(f"{principal} {public_key_str.strip()}\n")
        allowed_file = af.name

    try:
        result = subprocess.run(
            ["ssh-keygen", "-Y", "verify",
             "-f", allowed_file,
             "-I", principal,
             "-n", "octo",
             "-s", sig_file],
            input=message,
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    finally:
        for f in [msg_file, sig_file, allowed_file]:
            try:
                os.unlink(f)
            except OSError:
                pass


def build_sign_payload(task_id: str, task_type: str, requester: str) -> str:
    """Build the canonical string to sign for a task."""
    return f"{task_id}:{task_type}:{requester}"
