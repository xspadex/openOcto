"""openOcto Companion — lightweight desktop agent that listens for wake_input tasks.

When triggered (e.g. from AI Cube on phone), pops up an input dialog on the desktop.
User types a message → sent to AI Cube for processing → AI executes on target devices.

Usage:
    python -m openocto.companion              # auto-detect config from ~/.octo/config.json
    python -m openocto.companion --name mypc  # override terminal name
"""

import sys
import time
import json
import platform
import subprocess
import threading
from .config import get_relay_config
from .relay import Relay


def popup_input(title: str = "openOcto", prompt: str = "Enter command:") -> str:
    """Show a native input dialog and return user input. Cross-platform."""
    system = platform.system()

    if system == "Darwin":
        # macOS: use osascript
        script = (
            f'display dialog "{prompt}" default answer "" '
            f'with title "{title}" buttons {{"Cancel", "Send"}} default button "Send"'
        )
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=300
            )
            if result.returncode != 0:
                return ""
            # Parse "text returned:xxx, button returned:Send"
            output = result.stdout.strip()
            if "text returned:" in output:
                text = output.split("text returned:")[1]
                if ", button returned:" in text:
                    text = text.split(", button returned:")[0]
                return text.strip()
            return ""
        except (subprocess.TimeoutExpired, Exception):
            return ""

    elif system == "Windows":
        # Windows: use PowerShell
        ps_script = (
            'Add-Type -AssemblyName Microsoft.VisualBasic; '
            f'[Microsoft.VisualBasic.Interaction]::InputBox("{prompt}", "{title}", "")'
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True, text=True, timeout=300
            )
            return result.stdout.strip()
        except (subprocess.TimeoutExpired, Exception):
            return ""

    else:
        # Linux: try zenity, kdialog, or fallback to terminal input
        for cmd in [
            ["zenity", "--entry", f"--title={title}", f"--text={prompt}"],
            ["kdialog", "--inputbox", prompt, "--title", title],
        ]:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if result.returncode == 0:
                    return result.stdout.strip()
            except FileNotFoundError:
                continue

        # Terminal fallback
        try:
            return input(f"[{title}] {prompt} ").strip()
        except EOFError:
            return ""


def focus_terminal():
    """Try to bring a terminal window to the foreground."""
    system = platform.system()
    if system == "Darwin":
        subprocess.run(
            ["osascript", "-e", 'tell application "Terminal" to activate'],
            capture_output=True
        )
    # Windows and Linux: the popup itself will grab focus


def run_companion(name: str = None):
    """Main loop: poll for wake_input tasks, show popup, relay input back."""
    rc = get_relay_config()
    relay = Relay(rc["redis_url"], rc["redis_token"], rc["workspace"],
                  proxy_url=rc.get("proxy_url", ""))

    if not name:
        name = platform.node().split(".")[0].lower().replace(" ", "_")

    print(f"[companion] Listening as '{name}' for wake signals...")
    print(f"[companion] Press Ctrl+C to stop.")

    while True:
        try:
            task = relay.poll_task(name)
            if task and task.get("status") == "PENDING" and task.get("type") == "wake_input":
                relay.update_task(name, {"status": "RUNNING"})
                print(f"[companion] Wake signal received!")

                # Show popup
                focus_terminal()
                user_input = popup_input(
                    title="openOcto Cube",
                    prompt="AI Cube is asking for input:"
                )

                if user_input:
                    relay.complete_task(name, user_input, 0)
                    print(f"[companion] Sent: {user_input[:50]}...")
                else:
                    relay.complete_task(name, "", 1)
                    print(f"[companion] Cancelled.")
            else:
                time.sleep(3)

        except KeyboardInterrupt:
            print("\n[companion] Stopped.")
            break
        except Exception as e:
            print(f"[companion] Error: {e}")
            time.sleep(5)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="openOcto Companion - desktop wake agent")
    parser.add_argument("--name", "-n", default=None, help="Terminal name (default: hostname)")
    args = parser.parse_args()
    run_companion(name=args.name)


if __name__ == "__main__":
    main()
