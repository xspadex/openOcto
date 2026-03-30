"""
Nearby P2P file transfer — desktop side (macOS/Linux/Windows).

Implements the same OCTO protocol as Android's NearbyTransferManager:
  1. BLE scan/advertise to discover Octo devices
  2. Read hotspot info from sender's GATT (or create hotspot for sending)
  3. TCP transfer with OCTO binary framing + AES-256-GCM encryption

Usage as CLI:
    python -m openocto.nearby scan            # find nearby devices
    python -m openocto.nearby send FILE        # send a file (start TCP server, advertise via BLE)
    python -m openocto.nearby receive ADDRESS  # receive from a BLE-advertised sender
"""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import secrets
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ── Protocol constants (must match Android NearbyTransferManager) ──

SERVICE_UUID = "00000c70-0000-1000-8000-00805f9b34fb"
CHAR_DEVICE_NAME_UUID = "00000c71-0000-1000-8000-00805f9b34fb"
CHAR_HOTSPOT_INFO_UUID = "00000c72-0000-1000-8000-00805f9b34fb"

MAGIC = b"OCTO"
VERSION = 0x01
TYPE_FILE_META = 0x01
TYPE_CHUNK = 0x02
TYPE_ACK = 0x03
TYPE_DONE = 0x04
TYPE_ERROR = 0x05

HEADER_SIZE = 4 + 1 + 1 + 8 + 12  # magic(4) + ver(1) + type(1) + len(8) + nonce(12) = 26
GCM_NONCE_SIZE = 12
GCM_TAG_BITS = 128
CHUNK_SIZE = 1024 * 1024  # 1MB
TCP_PORT = 9528
BLE_SCAN_TIMEOUT = 10.0


# ══════════════════════════════════════════════════════════════════════
#  Wire Protocol — identical to Android side
# ══════════════════════════════════════════════════════════════════════

def write_frame(writer, frame_type: int, plaintext: bytes, key: bytes):
    """Write one OCTO encrypted frame."""
    nonce = secrets.token_bytes(GCM_NONCE_SIZE)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)

    header = struct.pack("<4sBBq",
                         MAGIC, VERSION, frame_type, len(ciphertext))
    header += nonce

    writer.write(header)
    writer.write(ciphertext)


async def async_write_frame(writer: asyncio.StreamWriter, frame_type: int,
                            plaintext: bytes, key: bytes):
    """Async version of write_frame."""
    nonce = secrets.token_bytes(GCM_NONCE_SIZE)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)

    header = struct.pack("<4sBBq",
                         MAGIC, VERSION, frame_type, len(ciphertext))
    header += nonce

    writer.write(header + ciphertext)
    await writer.drain()


async def async_read_frame(reader: asyncio.StreamReader,
                           key: bytes) -> tuple[int, bytes]:
    """Read and decrypt one OCTO frame. Returns (type, plaintext)."""
    header = await reader.readexactly(HEADER_SIZE)
    magic = header[:4]
    if magic != MAGIC:
        raise ValueError(f"Invalid magic: {magic}")

    ver = header[4]
    frame_type = header[5]
    payload_len = struct.unpack_from("<q", header, 6)[0]
    nonce = header[14:14 + GCM_NONCE_SIZE]

    if payload_len > 200 * 1024 * 1024:
        raise ValueError(f"Frame too large: {payload_len}")

    ciphertext = await reader.readexactly(payload_len)

    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return frame_type, plaintext


# ══════════════════════════════════════════════════════════════════════
#  1. BLE Discovery
# ══════════════════════════════════════════════════════════════════════

async def ble_scan(timeout: float = BLE_SCAN_TIMEOUT) -> list[dict]:
    """Scan for nearby Octo devices via BLE. Returns list of {name, address, rssi}."""
    from bleak import BleakScanner

    devices = []
    seen = set()

    def callback(device, advertising_data):
        if device.address in seen:
            return
        # Check if our service UUID is advertised
        uuids = advertising_data.service_uuids or []
        if SERVICE_UUID not in [u.lower() for u in uuids]:
            return
        seen.add(device.address)

        # Try to extract name from service data
        name = None
        sd = advertising_data.service_data or {}
        for uuid_key, data in sd.items():
            if "0c70" in uuid_key.lower():
                name = data.decode("utf-8", errors="replace")
                break
        if not name:
            name = device.name or device.address

        devices.append({
            "name": name,
            "address": device.address,
            "rssi": advertising_data.rssi or 0,
        })
        print(f"  Found: {name} ({device.address}) rssi={advertising_data.rssi}dBm")

    scanner = BleakScanner(detection_callback=callback)
    print(f"Scanning for Octo devices ({timeout}s)...")
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()
    return devices


async def ble_read_hotspot_info(address: str) -> dict | None:
    """Connect to a sender's BLE GATT and read hotspot info."""
    from bleak import BleakClient

    print(f"Reading hotspot info from {address}...")
    try:
        async with BleakClient(address, timeout=15.0) as client:
            # Read device name
            name_data = await client.read_gatt_char(CHAR_DEVICE_NAME_UUID)
            name = name_data.decode("utf-8", errors="replace")

            # Read hotspot info
            info_data = await client.read_gatt_char(CHAR_HOTSPOT_INFO_UUID)
            info_str = info_data.decode("utf-8", errors="replace")
            parts = info_str.split("\n")
            if len(parts) < 3:
                print(f"Invalid hotspot info: {info_str}")
                return None

            return {
                "name": name,
                "ssid": parts[0],
                "passphrase": parts[1],
                "port": int(parts[2]),
            }
    except Exception as e:
        print(f"BLE GATT read failed: {e}")
        return None


CHAR_TRIGGER_UUID = "00000c73-0000-1000-8000-00805f9b34fb"


async def ble_trigger_and_wait(address: str, timeout: float = 30.0) -> dict | None:
    """
    Full BLE flow:
    1. Connect to device's GATT
    2. Write "PREPARE_RECEIVE" to trigger characteristic
    3. Poll trigger characteristic until state is "READY"
    4. Read hotspot info
    5. Return hotspot info dict
    """
    from bleak import BleakClient

    print(f"Connecting to {address} via BLE...")
    try:
        async with BleakClient(address, timeout=15.0) as client:
            # Read device name
            name_data = await client.read_gatt_char(CHAR_DEVICE_NAME_UUID)
            name = name_data.decode("utf-8", errors="replace")
            print(f"Connected to '{name}', triggering receive mode...")

            # Read challenge token from trigger characteristic
            # Format: "STATE:token"
            trigger_data = await client.read_gatt_char(CHAR_TRIGGER_UUID)
            trigger_str = trigger_data.decode("utf-8", errors="replace").strip()
            parts = trigger_str.split(":", 1)
            if len(parts) < 2 or not parts[1]:
                print(f"Could not read challenge token: {trigger_str}")
                return None
            token = parts[1]

            # Write PREPARE_RECEIVE with token (prevents unauthorized triggers)
            await client.write_gatt_char(
                CHAR_TRIGGER_UUID,
                f"PREPARE_RECEIVE:{token}".encode("utf-8"),
                response=True,
            )
            print("Trigger sent. Waiting for hotspot to come up...")

            # Poll for READY state
            deadline = time.time() + timeout
            last_state = ""
            while time.time() < deadline:
                state_data = await client.read_gatt_char(CHAR_TRIGGER_UUID)
                state = state_data.decode("utf-8", errors="replace").strip()
                # State format is "STATE:token" — extract just the state
                state = state.split(":")[0]
                if state != last_state:
                    if state == "CONFIRMING":
                        print("Waiting for user to accept on device...")
                    elif state == "PREPARING":
                        print("User accepted. Creating hotspot...")
                    last_state = state
                if state == "READY":
                    break
                if state == "ERROR":
                    print("Device failed to create hotspot.")
                    return None
                if state == "IDLE":
                    print("User rejected the transfer.")
                    return None
                await asyncio.sleep(1)
            else:
                print("Timeout waiting for device to become ready.")
                return None

            # Read hotspot info
            info_data = await client.read_gatt_char(CHAR_HOTSPOT_INFO_UUID)
            info_str = info_data.decode("utf-8", errors="replace")
            parts = info_str.split("\n")
            if len(parts) < 3 or not parts[0]:
                print(f"Invalid hotspot info: {info_str}")
                return None

            result = {
                "name": name,
                "ssid": parts[0],
                "passphrase": parts[1],
                "port": int(parts[2]),
            }
            print(f"Hotspot ready: SSID={result['ssid']}")
            return result

    except Exception as e:
        print(f"BLE trigger failed: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════
#  2. Wi-Fi Hotspot Connection (receiver side)
# ══════════════════════════════════════════════════════════════════════

def connect_wifi(ssid: str, passphrase: str) -> bool:
    """Connect to a Wi-Fi network. Returns True on success."""
    system = platform.system()
    try:
        if system == "Darwin":
            # macOS: use networksetup
            iface = _get_macos_wifi_interface()
            cmd = ["networksetup", "-setairportnetwork", iface, ssid, passphrase]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                print(f"Connected to Wi-Fi '{ssid}'")
                return True
            print(f"Wi-Fi connect failed: {result.stderr.strip()}")
            return False

        elif system == "Linux":
            # Linux: use nmcli
            subprocess.run(
                ["nmcli", "device", "wifi", "connect", ssid,
                 "password", passphrase],
                capture_output=True, text=True, timeout=30, check=True,
            )
            print(f"Connected to Wi-Fi '{ssid}'")
            return True

        elif system == "Windows":
            # Windows: use netsh
            profile = f"""<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
    <name>{ssid}</name>
    <SSIDConfig><SSID><name>{ssid}</name></SSID></SSIDConfig>
    <connectionType>ESS</connectionType>
    <connectionMode>manual</connectionMode>
    <MSM><security>
        <authEncryption><authentication>WPA2PSK</authentication>
            <encryption>AES</encryption></authEncryption>
        <sharedKey><keyType>passPhrase</keyType>
            <protected>false</protected><keyMaterial>{passphrase}</keyMaterial>
        </sharedKey>
    </security></MSM>
</WLANProfile>"""
            profile_path = os.path.join(os.environ.get("TEMP", "/tmp"), "octo_wifi.xml")
            with open(profile_path, "w") as f:
                f.write(profile)
            subprocess.run(["netsh", "wlan", "add", "profile",
                            f"filename={profile_path}"],
                           capture_output=True, check=True)
            subprocess.run(["netsh", "wlan", "connect",
                            f"name={ssid}"],
                           capture_output=True, check=True, timeout=30)
            os.remove(profile_path)
            print(f"Connected to Wi-Fi '{ssid}'")
            return True
        else:
            print(f"Unsupported platform: {system}")
            return False
    except Exception as e:
        print(f"Wi-Fi connection error: {e}")
        return False


def disconnect_wifi():
    """Disconnect from current Wi-Fi (best effort)."""
    # Usually not needed — user's auto-connect will restore


def _get_macos_wifi_interface() -> str:
    """Get the Wi-Fi interface name on macOS (usually en0)."""
    try:
        result = subprocess.run(
            ["networksetup", "-listallhardwareports"],
            capture_output=True, text=True,
        )
        lines = result.stdout.splitlines()
        for i, line in enumerate(lines):
            if "Wi-Fi" in line or "AirPort" in line:
                for j in range(i + 1, min(i + 3, len(lines))):
                    if lines[j].strip().startswith("Device:"):
                        return lines[j].split(":", 1)[1].strip()
    except Exception:
        pass
    return "en0"


def get_gateway_ip() -> str:
    """Get the default gateway IP (= the hotspot creator's IP)."""
    system = platform.system()
    try:
        if system == "Darwin":
            result = subprocess.run(
                ["route", "-n", "get", "default"],
                capture_output=True, text=True,
            )
            for line in result.stdout.splitlines():
                if "gateway" in line.lower():
                    return line.split(":")[-1].strip()
        elif system == "Linux":
            result = subprocess.run(
                ["ip", "route", "show", "default"],
                capture_output=True, text=True,
            )
            parts = result.stdout.split()
            if "via" in parts:
                return parts[parts.index("via") + 1]
        elif system == "Windows":
            result = subprocess.run(
                ["ipconfig"], capture_output=True, text=True,
            )
            for line in result.stdout.splitlines():
                if "Default Gateway" in line and ":" in line:
                    gw = line.split(":")[-1].strip()
                    if gw:
                        return gw
    except Exception as e:
        print(f"Gateway detection error: {e}")
    return ""


def get_local_ip() -> str:
    """Get this machine's local IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ══════════════════════════════════════════════════════════════════════
#  3. TCP Transfer — SEND (this machine is the server/sender)
# ══════════════════════════════════════════════════════════════════════

async def send_file(file_path: str, port: int = TCP_PORT,
                    on_progress=None) -> bool:
    """
    Start TCP server, wait for receiver, then stream file.
    Returns True on success.
    """
    path = Path(file_path)
    if not path.exists():
        print(f"File not found: {file_path}")
        return False

    session_key = secrets.token_bytes(32)
    file_size = path.stat().st_size

    print(f"File: {path.name} ({file_size / 1024:.1f} KB)")
    print(f"Waiting for receiver on port {port}...")
    print(f"Local IP: {get_local_ip()}")

    server = await asyncio.start_server(
        lambda r, w: _handle_send(r, w, path, file_size, session_key, on_progress),
        "0.0.0.0", port,
    )

    async with server:
        # Wait for one transfer to complete
        await server.serve_forever()


async def _handle_send(reader: asyncio.StreamReader,
                       writer: asyncio.StreamWriter,
                       path: Path, file_size: int,
                       session_key: bytes, on_progress):
    """Handle one receiver connection — send the file."""
    peer = writer.get_extra_info("peername")
    print(f"Receiver connected: {peer}")

    try:
        # Send session key (plaintext over local hotspot — acceptable for LAN)
        writer.write(session_key)
        await writer.drain()

        # Send file metadata
        meta = json.dumps({
            "name": path.name,
            "size": file_size,
            "chunk_size": CHUNK_SIZE,
        }).encode("utf-8")
        await async_write_frame(writer, TYPE_FILE_META, meta, session_key)

        # Wait for ACK
        ack_type, _ = await async_read_frame(reader, session_key)
        if ack_type != TYPE_ACK:
            print(f"Expected ACK, got type {ack_type}")
            return

        # Stream file chunks
        sent = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                await async_write_frame(writer, TYPE_CHUNK, chunk, session_key)
                sent += len(chunk)
                pct = sent * 100 // file_size if file_size else 100
                _progress_bar(path.name, sent, file_size)
                if on_progress:
                    on_progress(path.name, sent, file_size)

        # Send DONE
        await async_write_frame(writer, TYPE_DONE, b"", session_key)

        # Wait final ACK
        await async_read_frame(reader, session_key)

        print(f"\nSent: {path.name} ({sent:,} bytes)")

    except Exception as e:
        print(f"\nSend error: {e}")
    finally:
        writer.close()
        await writer.wait_closed()
        # Stop the server after one transfer
        asyncio.get_event_loop().stop()


# ══════════════════════════════════════════════════════════════════════
#  4. TCP Transfer — RECEIVE (this machine is the client/receiver)
# ══════════════════════════════════════════════════════════════════════

async def receive_file(host: str, port: int = TCP_PORT,
                       save_dir: str = ".", on_progress=None) -> str | None:
    """
    Connect to sender's TCP server and receive file.
    Returns saved file path on success, None on failure.
    """
    print(f"Connecting to sender at {host}:{port}...")

    try:
        reader, writer = await asyncio.open_connection(host, port)
    except Exception as e:
        print(f"Connection failed: {e}")
        return None

    try:
        # Receive session key
        session_key = await reader.readexactly(32)

        # Read file metadata
        meta_type, meta_data = await async_read_frame(reader, session_key)
        if meta_type != TYPE_FILE_META:
            print(f"Expected file_meta, got type {meta_type}")
            return None

        meta = json.loads(meta_data.decode("utf-8"))
        file_name = meta["name"]
        file_size = meta["size"]
        print(f"Receiving: {file_name} ({file_size / 1024:.1f} KB)")

        # Send ACK
        await async_write_frame(writer, TYPE_ACK, b"OK", session_key)

        # Receive chunks
        save_path = Path(save_dir) / file_name
        save_path.parent.mkdir(parents=True, exist_ok=True)
        received = 0

        with open(save_path, "wb") as f:
            while True:
                frame_type, frame_data = await async_read_frame(reader, session_key)
                if frame_type == TYPE_CHUNK:
                    f.write(frame_data)
                    received += len(frame_data)
                    _progress_bar(file_name, received, file_size)
                    if on_progress:
                        on_progress(file_name, received, file_size)
                elif frame_type == TYPE_DONE:
                    break
                elif frame_type == TYPE_ERROR:
                    print(f"\nSender error: {frame_data.decode()}")
                    save_path.unlink(missing_ok=True)
                    return None

        # Send final ACK
        await async_write_frame(writer, TYPE_ACK, b"DONE", session_key)

        print(f"\nReceived: {save_path} ({received:,} bytes)")
        return str(save_path)

    except Exception as e:
        print(f"\nReceive error: {e}")
        return None
    finally:
        writer.close()
        await writer.wait_closed()


# ══════════════════════════════════════════════════════════════════════
#  5. High-level commands
# ══════════════════════════════════════════════════════════════════════

async def cmd_scan(timeout: float = BLE_SCAN_TIMEOUT):
    """Scan for nearby Octo devices."""
    devices = await ble_scan(timeout)
    if not devices:
        print("No nearby Octo devices found.")
    else:
        print(f"\n{len(devices)} device(s) found.")
    return devices


async def cmd_send(file_path: str, port: int = TCP_PORT,
                   use_ble: bool = True, target_name: str = ""):
    """
    Send a file to a nearby Octo device. Fully automatic flow:
    1. BLE scan to find target device
    2. Write PREPARE_RECEIVE to trigger device's hotspot + TCP server
    3. Connect to device's Wi-Fi hotspot
    4. TCP connect and send file
    5. Disconnect and restore Wi-Fi
    """
    path = Path(file_path)
    if not path.exists():
        print(f"Error: file not found: {file_path}")
        return False

    file_size = path.stat().st_size
    print(f"=" * 50)
    print(f"  OCTO NEARBY SEND")
    print(f"  File: {path.name} ({file_size / 1024:.1f} KB)")
    print(f"=" * 50)

    if use_ble:
        # Step 1: BLE scan
        print("\n[1/5] Scanning for nearby Octo devices...")
        devices = await ble_scan(timeout=8.0)
        if not devices:
            print("No Octo devices found nearby.")
            print("\nFallback: start a manual TCP server instead.")
            return await _fallback_send(file_path, port)

        # Pick target
        target = None
        if target_name:
            target = next((d for d in devices if target_name.lower() in d["name"].lower()), None)
        if not target:
            if len(devices) == 1:
                target = devices[0]
            else:
                print("\nMultiple devices found:")
                for i, d in enumerate(devices):
                    print(f"  [{i}] {d['name']} ({d['address']}) rssi={d['rssi']}dBm")
                try:
                    idx = int(input("Select device [0]: ") or "0")
                    target = devices[idx]
                except (ValueError, IndexError):
                    target = devices[0]

        print(f"Target: {target['name']} ({target['address']})")

        # Step 2: Trigger receive on target
        print("\n[2/5] Triggering receive mode on target device...")
        info = await ble_trigger_and_wait(target["address"])
        if not info:
            print("Failed to trigger receive. Falling back to manual mode.")
            return await _fallback_send(file_path, port)

        # Step 3: Connect to hotspot
        print(f"\n[3/5] Connecting to hotspot '{info['ssid']}'...")
        # Save current Wi-Fi to restore later
        original_wifi = _get_current_ssid()
        if not connect_wifi(info["ssid"], info["passphrase"]):
            print("Failed to connect to hotspot.")
            return False

        # Wait for DHCP
        await asyncio.sleep(3)

        # Step 4: Find gateway and send
        gateway = get_gateway_ip()
        if not gateway:
            print("Could not determine target device IP.")
            return False

        target_port = info["port"]
        print(f"\n[4/5] Sending file to {gateway}:{target_port}...")
        result = await send_to_receiver(file_path, gateway, target_port)

        # Step 5: Restore Wi-Fi
        print("\n[5/5] Restoring Wi-Fi...")
        if original_wifi:
            disconnect_wifi()
            # macOS will auto-reconnect to known networks
        print("Done!" if result else "Transfer failed.")
        return result

    else:
        return await _fallback_send(file_path, port)


async def _fallback_send(file_path: str, port: int = TCP_PORT):
    """Fallback: start TCP server and print connection info."""
    local_ip = get_local_ip()
    print(f"\nFallback mode: TCP server on {local_ip}:{port}")
    print(f"On the other device, run:")
    print(f"  octo nearby receive {local_ip}")
    print(f"Waiting for connection...")
    await send_file(file_path, port)
    return True


async def send_to_receiver(file_path: str, host: str, port: int = TCP_PORT) -> bool:
    """Connect to a receiver's TCP server and send file (this machine is the sender/client)."""
    path = Path(file_path)
    session_key = secrets.token_bytes(32)
    file_size = path.stat().st_size

    try:
        reader, writer = await asyncio.open_connection(host, port)
    except Exception as e:
        print(f"Connection failed: {e}")
        return False

    try:
        # Send session key
        writer.write(session_key)
        await writer.drain()

        # Send file metadata
        meta = json.dumps({
            "name": path.name,
            "size": file_size,
            "chunk_size": CHUNK_SIZE,
        }).encode("utf-8")
        await async_write_frame(writer, TYPE_FILE_META, meta, session_key)

        # Wait for ACK
        ack_type, _ = await async_read_frame(reader, session_key)
        if ack_type != TYPE_ACK:
            print(f"Expected ACK, got type {ack_type}")
            return False

        # Stream file
        sent = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                await async_write_frame(writer, TYPE_CHUNK, chunk, session_key)
                sent += len(chunk)
                _progress_bar(path.name, sent, file_size)

        await async_write_frame(writer, TYPE_DONE, b"", session_key)
        await async_read_frame(reader, session_key)  # final ACK

        print(f"\nSent: {path.name} ({sent:,} bytes)")
        return True

    except Exception as e:
        print(f"\nSend error: {e}")
        return False
    finally:
        writer.close()
        await writer.wait_closed()


def _get_current_ssid() -> str:
    """Get current Wi-Fi SSID (to restore after transfer)."""
    system = platform.system()
    try:
        if system == "Darwin":
            r = subprocess.run(
                ["networksetup", "-getairportnetwork",
                 _get_macos_wifi_interface()],
                capture_output=True, text=True,
            )
            # Output: "Current Wi-Fi Network: MyNetwork"
            if ":" in r.stdout:
                return r.stdout.split(":", 1)[1].strip()
    except Exception:
        pass
    return ""


async def cmd_receive(host: str, port: int = TCP_PORT,
                      save_dir: str = ".",
                      use_ble: bool = False, ble_address: str = ""):
    """
    Receive a file. Two modes:
    A) Direct IP: connect to host:port
    B) BLE: read hotspot info from sender, connect to hotspot, then TCP
    """
    if use_ble and ble_address:
        # Mode B: Full BLE flow
        info = await ble_read_hotspot_info(ble_address)
        if not info:
            print("Could not get hotspot info via BLE.")
            return

        print(f"Sender: {info['name']}")
        print(f"Hotspot: {info['ssid']}")

        if not connect_wifi(info["ssid"], info["passphrase"]):
            print("Failed to connect to sender's hotspot.")
            return

        # Wait for DHCP
        await asyncio.sleep(3)

        gateway = get_gateway_ip()
        if not gateway:
            print("Could not determine sender's IP.")
            return

        host = gateway
        port = info["port"]
        print(f"Sender IP: {host}:{port}")

    result = await receive_file(host, port, save_dir)
    if result:
        print(f"Saved to: {result}")
    else:
        print("Transfer failed.")


# ══════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════

def _progress_bar(name: str, current: int, total: int):
    """Print a progress bar."""
    if total <= 0:
        return
    pct = current * 100 // total
    bar_len = 30
    filled = bar_len * current // total
    bar = "=" * filled + "-" * (bar_len - filled)
    size_str = f"{current / 1024:.0f}/{total / 1024:.0f} KB"
    print(f"\r  [{bar}] {pct:3d}% {size_str}", end="", flush=True)


# ══════════════════════════════════════════════════════════════════════
#  CLI Entry Point
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        prog="octo-nearby",
        description="Octo Nearby P2P Transfer — send files without internet",
    )
    sub = parser.add_subparsers(dest="command")

    # scan
    p_scan = sub.add_parser("scan", help="Scan for nearby Octo devices via BLE")
    p_scan.add_argument("-t", "--timeout", type=float, default=10.0,
                        help="Scan timeout in seconds")

    # send
    p_send = sub.add_parser("send", help="Send a file to a nearby device")
    p_send.add_argument("file", help="File to send")
    p_send.add_argument("-p", "--port", type=int, default=TCP_PORT)
    p_send.add_argument("--no-ble", action="store_true",
                        help="Skip BLE advertising")

    # receive
    p_recv = sub.add_parser("receive", help="Receive a file from a sender")
    p_recv.add_argument("host", nargs="?", default="",
                        help="Sender IP (or omit to use BLE)")
    p_recv.add_argument("-p", "--port", type=int, default=TCP_PORT)
    p_recv.add_argument("-d", "--dir", default=".",
                        help="Save directory")
    p_recv.add_argument("--ble", default="",
                        help="BLE address of sender (triggers full BLE flow)")

    args = parser.parse_args()

    if args.command == "scan":
        asyncio.run(cmd_scan(args.timeout))

    elif args.command == "send":
        asyncio.run(cmd_send(args.file, args.port,
                             use_ble=not args.no_ble))

    elif args.command == "receive":
        if args.ble:
            asyncio.run(cmd_receive("", args.port, args.dir,
                                    use_ble=True, ble_address=args.ble))
        elif args.host:
            asyncio.run(cmd_receive(args.host, args.port, args.dir))
        else:
            print("Provide sender IP or --ble ADDRESS")
            print("  receive 192.168.x.x       Direct IP mode")
            print("  receive --ble AA:BB:CC:DD  Full BLE flow")
            sys.exit(1)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
