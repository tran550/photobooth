#!/usr/bin/env python3
"""Auto-configure photobooth service printer/keyboard Environment lines.

This script detects likely keyboard and receipt printer targets, then updates
photobooth-preview-root.service in-place.

Examples:
  python3 apply_pi_device_settings.py
  python3 apply_pi_device_settings.py --dry-run
  python3 apply_pi_device_settings.py --printer-mode usb
  python3 apply_pi_device_settings.py --service /home/bran/photobooth/photobooth-preview-root.service
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
from typing import Dict, List, Optional, Tuple

from pi_device_helper import (
    detect_event_devices,
    detect_lp_devices,
    detect_usb_devices,
    pick_best_keyboard,
    pick_best_printer,
)


def format_environment_line(key: str, value: str) -> str:
    assignment = f"{key}={value}"
    if " " in assignment or '"' in assignment:
        assignment = assignment.replace('"', '\\"')
        return f'Environment="{assignment}"'
    return f"Environment={assignment}"


def find_service_bounds(lines: List[str]) -> Tuple[int, int]:
    start = -1
    end = len(lines)

    for i, line in enumerate(lines):
        if line.strip() == "[Service]":
            start = i
            break

    if start == -1:
        return -1, -1

    for i in range(start + 1, len(lines)):
        s = lines[i].strip()
        if s.startswith("[") and s.endswith("]"):
            end = i
            break

    return start, end


def upsert_env(lines: List[str], key: str, value: str) -> List[str]:
    pattern = re.compile(rf'^\s*Environment=(?:"?){re.escape(key)}=')
    new_line = format_environment_line(key, value)

    found_indexes = [idx for idx, line in enumerate(lines) if pattern.search(line)]
    if found_indexes:
        first = found_indexes[0]
        lines[first] = new_line
        # Remove duplicates, if any.
        for idx in reversed(found_indexes[1:]):
            del lines[idx]
        return lines

    service_start, service_end = find_service_bounds(lines)
    if service_start == -1:
        lines.append(new_line)
        return lines

    insert_at = service_end
    for i in range(service_start + 1, service_end):
        if lines[i].strip().startswith("ExecStart="):
            insert_at = i
            break

    lines.insert(insert_at, new_line)
    return lines


def parse_hex_id(raw: Optional[str]) -> str:
    if not raw:
        return ""
    raw = raw.strip().lower()
    if not raw:
        return ""
    if raw.startswith("0x"):
        return raw
    # Keep exact hex form for service values.
    return "0x" + raw


def head_dots_for_width(width_mm: int) -> int:
    if width_mm >= 80:
        return 576
    return 384


def choose_targets(args) -> Dict[str, str]:
    event_devices = detect_event_devices()
    usb_devices = detect_usb_devices()
    lp_devices = detect_lp_devices()

    best_keyboard = pick_best_keyboard(event_devices)
    best_printer = pick_best_printer(usb_devices)

    keyboard_path = args.keyboard_device or (best_keyboard.path if best_keyboard else "")

    usb_vendor = parse_hex_id(args.usb_vendor_id)
    usb_product = parse_hex_id(args.usb_product_id)
    lp_device = args.lp_device or (lp_devices[0] if lp_devices else "")

    if not usb_vendor and best_printer:
        usb_vendor = parse_hex_id(best_printer.vendor_id)
    if not usb_product and best_printer:
        usb_product = parse_hex_id(best_printer.product_id)

    selected_mode = args.printer_mode
    if selected_mode == "auto":
        if lp_device:
            selected_mode = "device"
        elif usb_vendor and usb_product:
            selected_mode = "usb"
        else:
            selected_mode = "none"

    backend = args.backend
    if selected_mode == "none":
        backend = "none"

    width_mm = args.paper_width
    head_dots = args.head_dots if args.head_dots else head_dots_for_width(width_mm)

    result = {
        "KEYBOARD_EVENT_DEVICE": keyboard_path,
        "AUTO_PRINT_ENABLED": "1",
        "PRINT_BACKEND": backend,
        "PRINT_PAPER_WIDTH_MM": str(width_mm),
        "PRINT_HEAD_DOTS": str(head_dots),
        "PRINT_DITHER": "1",
        "PRINT_ROTATE": "0",
        "PRINT_DEVICE_FILE": "",
        "PRINT_USB_VENDOR_ID": "",
        "PRINT_USB_PRODUCT_ID": "",
    }

    if selected_mode == "device":
        result["PRINT_DEVICE_FILE"] = lp_device
    elif selected_mode == "usb":
        result["PRINT_USB_VENDOR_ID"] = usb_vendor
        result["PRINT_USB_PRODUCT_ID"] = usb_product

    return result


def load_lines(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as handle:
        return [line.rstrip("\n") for line in handle.readlines()]


def save_lines(path: str, lines: List[str]) -> None:
    content = "\n".join(lines).rstrip("\n") + "\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def apply_env_updates(lines: List[str], env_values: Dict[str, str]) -> List[str]:
    for key, value in env_values.items():
        lines = upsert_env(lines, key, value)
    return lines


def print_preview(service_path: str, env_values: Dict[str, str]) -> None:
    print(f"Service file: {service_path}")
    print("Planned Environment values:")
    for key, value in env_values.items():
        print(f"  {key}={value}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply detected keyboard/printer settings to photobooth service.")
    parser.add_argument("--service", default="photobooth-preview-root.service", help="Path to service file")
    parser.add_argument("--dry-run", action="store_true", help="Show planned changes without writing file")
    parser.add_argument("--no-backup", action="store_true", help="Do not write .bak backup")

    parser.add_argument("--printer-mode", choices=["auto", "device", "usb", "none"], default="auto")
    parser.add_argument("--backend", choices=["escpos", "none"], default="escpos")
    parser.add_argument("--paper-width", type=int, default=58, help="Paper width in mm, e.g. 58 or 80")
    parser.add_argument("--head-dots", type=int, default=0, help="Print head width in dots (0 = auto from paper width)")

    parser.add_argument("--keyboard-device", default="", help="Override KEYBOARD_EVENT_DEVICE")
    parser.add_argument("--lp-device", default="", help="Override PRINT_DEVICE_FILE")
    parser.add_argument("--usb-vendor-id", default="", help="Override PRINT_USB_VENDOR_ID, e.g. 0x04b8")
    parser.add_argument("--usb-product-id", default="", help="Override PRINT_USB_PRODUCT_ID, e.g. 0x0e15")

    args = parser.parse_args()

    service_path = args.service
    if not os.path.exists(service_path):
        print(f"Service file not found: {service_path}")
        return 2

    env_values = choose_targets(args)
    print_preview(service_path, env_values)

    if args.dry_run:
        print("Dry-run only. No changes written.")
        return 0

    lines = load_lines(service_path)
    updated_lines = apply_env_updates(lines, env_values)

    if not args.no_backup:
        backup_path = service_path + ".bak"
        shutil.copyfile(service_path, backup_path)
        print(f"Backup written: {backup_path}")

    save_lines(service_path, updated_lines)
    print("Service file updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
