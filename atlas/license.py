"""
Atlas license key verification.
Public key is baked in — no external files needed for verification.
"""
import base64
import json
import sys
from datetime import date
from pathlib import Path

_LICENSE_PATH = Path.home() / ".atlas" / "license.json"
_UPGRADE_URL = "https://horn-dev.co.za/atlas"

# Ed25519 public key — baked in at build time, never changes per release
_PUBLIC_KEY_PEM = b"""\
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAesIY+mRcjKY6l1AmP1C9X7YhBITYb9E9W9Z9RHsqoaQ=
-----END PUBLIC KEY-----
"""


def _load_public_key():
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    return load_pem_public_key(_PUBLIC_KEY_PEM)


def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)


def generate_key(tier: str, email: str, expiry_days: int) -> str:
    """
    Generate a signed Atlas license key (server-side).
    Requires the private key in ~/.atlas/keys/private.pem.
    Use keygen.py for production key generation.
    """
    from datetime import timedelta
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    private_pem = Path.home() / ".atlas" / "keys" / "private.pem"
    if not private_pem.exists():
        raise FileNotFoundError(f"Private key not found: {private_pem}")
    private_key = load_pem_private_key(private_pem.read_bytes(), password=None)

    payload = {
        "v": 1,
        "tier": tier,
        "email": email,
        "iss": date.today().isoformat(),
        "exp": (date.today() + timedelta(days=expiry_days)).isoformat(),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
    signature = private_key.sign(payload_bytes)
    sig_b64 = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"ATLAS-{payload_b64}.{sig_b64}"


def verify_key(key: str) -> dict:
    """
    Verify an Atlas license key.

    Returns:
        {valid, tier, email, expiry, days_remaining, error}
    """
    result = {"valid": False, "tier": "free", "email": "", "expiry": "", "days_remaining": 0, "error": None}

    if not key or not key.startswith("ATLAS-"):
        result["error"] = "Invalid key format"
        return result

    body = key[len("ATLAS-"):]
    parts = body.rsplit(".", 1)
    if len(parts) != 2:
        result["error"] = "Invalid key format"
        return result

    payload_b64, sig_b64 = parts
    try:
        payload_bytes = _b64url_decode(payload_b64)
        signature = _b64url_decode(sig_b64)
    except Exception:
        result["error"] = "Key decode failed"
        return result

    try:
        pub = _load_public_key()
        pub.verify(signature, payload_bytes)
    except Exception:
        result["error"] = "Signature verification failed — key may be tampered or forged"
        return result

    try:
        payload = json.loads(payload_bytes)
    except Exception:
        result["error"] = "Key payload is not valid JSON"
        return result

    expiry_str = payload.get("exp", "")
    try:
        expiry_date = date.fromisoformat(expiry_str)
    except ValueError:
        result["error"] = "Key has invalid expiry date"
        return result

    days_remaining = (expiry_date - date.today()).days
    if days_remaining < 0:
        result["error"] = f"Key expired on {expiry_str}"
        return result

    result.update({
        "valid": True,
        "tier": payload.get("tier", "free"),
        "email": payload.get("email", ""),
        "expiry": expiry_str,
        "days_remaining": days_remaining,
        "error": None,
    })
    return result


def load_license() -> dict:
    """
    Load and verify the stored license.
    Returns verify_key() result, or a free-tier stub if no key is stored.
    """
    if not _LICENSE_PATH.exists():
        return {"valid": False, "tier": "free", "email": "", "expiry": "", "days_remaining": 0, "error": "No license activated"}

    try:
        stored = json.loads(_LICENSE_PATH.read_text(encoding="utf-8"))
        key = stored.get("key", "")
    except Exception:
        return {"valid": False, "tier": "free", "email": "", "expiry": "", "days_remaining": 0, "error": "License file corrupted"}

    return verify_key(key)


def activate(key: str) -> dict:
    """
    Verify a key and save it to ~/.atlas/license.json.
    Returns the verify_key() result.
    """
    result = verify_key(key)
    if not result["valid"]:
        return result

    _LICENSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LICENSE_PATH.write_text(
        json.dumps({"key": key}, indent=2),
        encoding="utf-8",
    )
    return result


def requires_pro():
    """
    Gate a feature to Pro tier and above.
    Prints an upgrade prompt and exits if the active license is Free.
    """
    info = load_license()
    tier = info.get("tier", "free")
    if tier in ("pro", "team", "enterprise") and info.get("valid"):
        return

    print("This feature requires Atlas Pro ($19/month).")
    print(f"Get your key at: {_UPGRADE_URL}")
    sys.exit(0)
