"""Configuration management for openOcto.

Handles reading/writing ~/.octo/config.json.
"""

import base64
import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".octo"
CONFIG_FILE = CONFIG_DIR / "config.json"

# No default relay — user must configure their own Redis
DEFAULT_REDIS_URL = ""
DEFAULT_REDIS_TOKEN = ""
DEFAULT_WORKSPACE = "default"


def ensure_config_dir() -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def save_config(config: dict) -> None:
    ensure_config_dir()
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_relay_config() -> dict:
    """Return relay config, raising if not initialized."""
    config = load_config()
    has_direct = config.get("redis_url") and config.get("redis_token")
    has_proxy = config.get("proxy_url")
    if not has_direct and not has_proxy:
        raise SystemExit(
            "openOcto not configured. Run 'octo init' first."
        )
    return {
        "redis_url": config.get("redis_url", ""),
        "redis_token": config.get("redis_token", ""),
        "workspace": config.get("workspace", DEFAULT_WORKSPACE),
        "proxy_url": config.get("proxy_url", ""),
    }


def encode_token(redis_url: str, redis_token: str, workspace: str,
                  proxy_url: str = "") -> str:
    """Encode relay config into a join token."""
    data = {"u": redis_url, "t": redis_token, "w": workspace}
    if proxy_url:
        data["p"] = proxy_url
    payload = json.dumps(data, separators=(",", ":"))
    b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    return f"octo://{b64}"


def decode_token(token: str) -> dict:
    """Decode a join token into relay config."""
    if not token.startswith("octo://"):
        raise ValueError("Invalid token format. Expected 'octo://...'")
    b64 = token[7:]
    # Add padding if needed
    b64 += "=" * (4 - len(b64) % 4) if len(b64) % 4 else ""
    payload = json.loads(base64.urlsafe_b64decode(b64))
    result = {
        "redis_url": payload.get("u", ""),
        "redis_token": payload.get("t", ""),
        "workspace": payload.get("w", DEFAULT_WORKSPACE),
    }
    if payload.get("p"):
        result["proxy_url"] = payload["p"]
    return result


def get_identity() -> str:
    """Get the local device identity name, or empty string."""
    config = load_config()
    return config.get("identity", "")


def set_identity(name: str) -> None:
    """Save the local device identity name."""
    config = load_config()
    config["identity"] = name
    save_config(config)


def clear_identity() -> None:
    """Remove the local device identity."""
    config = load_config()
    config.pop("identity", None)
    config.pop("private_key", None)
    config.pop("public_key", None)
    save_config(config)


def get_keypair() -> tuple:
    """Get (private_key_pem, public_key_pem) from local config. Returns ('', '') if not set."""
    config = load_config()
    return config.get("private_key", ""), config.get("public_key", "")


def set_keypair(private_key: str, public_key: str) -> None:
    """Save keypair to local config."""
    config = load_config()
    config["private_key"] = private_key
    config["public_key"] = public_key
    save_config(config)


def get_storage_config() -> dict:
    """Return storage config section, or empty dict if not configured."""
    config = load_config()
    return config.get("storage", {})


def set_storage_config(storage: dict) -> None:
    """Save storage configuration."""
    config = load_config()
    config["storage"] = storage
    save_config(config)
