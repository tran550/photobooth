#!/usr/bin/env python3
"""Detect likely keyboard/printer devices on Raspberry Pi and print service env hints.

Run this on the Raspberry Pi after connecting keyboard and receipt printer:
  python3 pi_device_helper.py

Optional JSON output:
  python3 pi_device_helper.py --json
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
from dataclasses import dataclass, asdict
from typing import List, Optional, Tuple

KEY_SPACE_CODE = 57


@dataclass
class EventDevice:
    path: str
    name: str
    has_space: bool
    readable: bool


@dataclass
class UsbDevice:
    bus: str
    device: str
    vendor_id: str
    product_id: str
    description: str
    is_likely_printer: bool


def run_command(args: List[str]) -> Tuple[int, str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=4)
        return result.returncode, (result.stdout or "").strip()
    except Exception:
        return 1, ""


def read_first_line(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.readline().strip()
    except Exception:
        return ""


def parse_linux_hex_bitmap(hex_words: str, bit_index: int) -> bool:
    """Parse Linux bitmap strings like '120013 0 0 0' and test one bit.

    The rightmost hex word is bits 0..31 (or 0..63 depending on kernel output width).
    """
    words = [w for w in hex_words.strip().split() if w]
    if not words:
        return False

    # Linux presents lower bits at the rightmost word.
    words = list(reversed(words))
    word_size_guess = 32
    for word in words:
        if len(word) > 8:
            word_size_guess = 64
            break

    word_index = bit_index // word_size_guess
    bit_in_word = bit_index % word_size_guess
    if word_index >= len(words):
        return False

    try:
        value = int(words[word_index], 16)
    except Exception:
        return False
    return ((value >> bit_in_word) & 1) == 1


def detect_event_devices() -> List[EventDevice]:
    devices: List[EventDevice] = []
    for path in sorted(glob.glob("/dev/input/event*")):
        base = os.path.basename(path)
        sys_name_path = f"/sys/class/input/{base}/device/name"
        sys_keys_path = f"/sys/class/input/{base}/device/capabilities/key"

        name = read_first_line(sys_name_path) or "(unknown)"
        key_caps = read_first_line(sys_keys_path)
        has_space = parse_linux_hex_bitmap(key_caps, KEY_SPACE_CODE)
        readable = os.access(path, os.R_OK)

        devices.append(EventDevice(path=path, name=name, has_space=has_space, readable=readable))

    return devices


def parse_lsusb_line(line: str) -> Optional[UsbDevice]:
    # Example:
    # Bus 001 Device 004: ID 04b8:0e15 EPSON TM-T20II
    m = re.match(r"^Bus\s+(\d+)\s+Device\s+(\d+):\s+ID\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s*(.*)$", line)
    if not m:
        return None

    bus, device, vendor, product, desc = m.groups()
    description = (desc or "").strip()
    lower = description.lower()
    printer_hint = any(token in lower for token in ["printer", "thermal", "receipt", "pos", "epson", "star", "bixolon", "esc/pos", "escpos"])

    return UsbDevice(
        bus=bus,
        device=device,
        vendor_id=vendor.lower(),
        product_id=product.lower(),
        description=description,
        is_likely_printer=printer_hint,
    )


def detect_usb_devices() -> List[UsbDevice]:
    code, output = run_command(["lsusb"])
    if code != 0 or not output:
        return []

    devices: List[UsbDevice] = []
    for line in output.splitlines():
        parsed = parse_lsusb_line(line.strip())
        if parsed:
            devices.append(parsed)
    return devices


def detect_lp_devices() -> List[str]:
    paths = sorted(glob.glob("/dev/usb/lp*")) + sorted(glob.glob("/dev/lp*"))
    return sorted(set(paths))


def pick_best_keyboard(devices: List[EventDevice]) -> Optional[EventDevice]:
    candidates = [d for d in devices if d.has_space]
    if not candidates:
        return None

    def score(dev: EventDevice) -> int:
        name = dev.name.lower()
        s = 0
        if "keyboard" in name:
            s += 20
        if "kbd" in name:
            s += 10
        if "mouse" in name:
            s -= 20
        if dev.readable:
            s += 5
        return s

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def pick_best_printer(usb_devices: List[UsbDevice]) -> Optional[UsbDevice]:
    likely = [d for d in usb_devices if d.is_likely_printer]
    if not likely:
        return None
    return likely[0]


def as_json(event_devices: List[EventDevice], usb_devices: List[UsbDevice], lp_devices: List[str]) -> str:
    payload = {
        "event_devices": [asdict(d) for d in event_devices],
        "usb_devices": [asdict(d) for d in usb_devices],
        "lp_devices": lp_devices,
        "recommended": {},
    }

    kb = pick_best_keyboard(event_devices)
    pr = pick_best_printer(usb_devices)

    payload["recommended"]["KEYBOARD_EVENT_DEVICE"] = kb.path if kb else ""
    payload["recommended"]["PRINT_DEVICE_FILE"] = lp_devices[0] if lp_devices else ""
    payload["recommended"]["PRINT_USB_VENDOR_ID"] = ("0x" + pr.vendor_id) if pr else ""
    payload["recommended"]["PRINT_USB_PRODUCT_ID"] = ("0x" + pr.product_id) if pr else ""

    return json.dumps(payload, indent=2)


def print_human(event_devices: List[EventDevice], usb_devices: List[UsbDevice], lp_devices: List[str]) -> None:
    print("== Input Event Devices ==")
    if not event_devices:
        print("No /dev/input/event* devices found.")
    for dev in event_devices:
        flags = []
        if dev.has_space:
            flags.append("SPACE")
        if dev.readable:
            flags.append("READABLE")
        flag_text = ", ".join(flags) if flags else "-"
        print(f"{dev.path:18} | {flag_text:14} | {dev.name}")

    print("\n== USB Devices (lsusb) ==")
    if not usb_devices:
        print("No lsusb output (lsusb missing or no USB devices).")
    for dev in usb_devices:
        hint = "PRINTER?" if dev.is_likely_printer else ""
        print(f"Bus {dev.bus} Dev {dev.device} | {dev.vendor_id}:{dev.product_id} | {dev.description} {hint}")

    print("\n== Printer Character Device Nodes ==")
    if not lp_devices:
        print("No /dev/usb/lp* or /dev/lp* found.")
    else:
        for path in lp_devices:
            print(path)

    kb = pick_best_keyboard(event_devices)
    pr = pick_best_printer(usb_devices)

    print("\n== Suggested Environment Lines ==")
    if kb:
        print(f"Environment=KEYBOARD_EVENT_DEVICE={kb.path}")
    else:
        print("Environment=KEYBOARD_EVENT_DEVICE=")

    print("Environment=AUTO_PRINT_ENABLED=1")
    print("Environment=PRINT_BACKEND=escpos")
    print("Environment=PRINT_PAPER_WIDTH_MM=58")
    print("Environment=PRINT_HEAD_DOTS=384")

    if lp_devices:
        print(f"Environment=PRINT_DEVICE_FILE={lp_devices[0]}")
    else:
        print("Environment=PRINT_DEVICE_FILE=")

    if pr:
        print(f"Environment=PRINT_USB_VENDOR_ID=0x{pr.vendor_id}")
        print(f"Environment=PRINT_USB_PRODUCT_ID=0x{pr.product_id}")
    else:
        print("Environment=PRINT_USB_VENDOR_ID=")
        print("Environment=PRINT_USB_PRODUCT_ID=")

    print("\nTip: Use either PRINT_DEVICE_FILE or USB vendor/product IDs. You do not need both.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect keyboard/printer settings for photobooth service.")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    args = parser.parse_args()

    event_devices = detect_event_devices()
    usb_devices = detect_usb_devices()
    lp_devices = detect_lp_devices()

    if args.json:
        print(as_json(event_devices, usb_devices, lp_devices))
    else:
        print_human(event_devices, usb_devices, lp_devices)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
