import fcntl
import glob
import os
import shutil
import signal
import select
import subprocess
import sys
import threading
import time
import traceback


_shutdown = False
_preview_proc = None
_cursor_hider_proc = None
_transition_cover_proc = None
_current_device = None
_current_format = None
_current_size = None
_current_framerate = None
_last_start_ts = 0.0
_warned_force_device_missing = False
_last_capture_ts = 0.0
_capture_in_progress = False
_capture_state_lock = threading.Lock()
_preview_lock = threading.Lock()
_keyboard_listener_threads = []
_vintage_mode_lock = threading.Lock()
_last_filter_switch_ts = 0.0
_capture_save_lock = threading.Lock()
_instance_lock_file = None
_selected_vintage_mode = None
_capture_confirm_pending = False
_capture_confirm_lock = threading.Lock()
_capture_confirm_started_at = 0.0
_status_overlay_lock = threading.Lock()
_status_overlay_token = 0

RETRY_INTERVAL_SEC = 2.0
MAX_RETRY_INTERVAL_SEC = 10.0


def env_float(name, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except Exception:
        return default


PERIODIC_RESTART_SEC = max(0.0, env_float("PERIODIC_RESTART_SEC", 0.0))
FILTER_SWITCH_COOLDOWN_SEC = max(0.0, env_float("FILTER_SWITCH_COOLDOWN_SEC", 0.22))
TRANSITION_COVER_ENABLED = os.getenv("TRANSITION_COVER_ENABLED", "0").strip().lower() not in {"0", "false", "off", "no"}
FILTER_SWITCH_LIVE_APPLY = os.getenv("FILTER_SWITCH_LIVE_APPLY", "0").strip().lower() not in {"0", "false", "off", "no"}
CAPTURE_FALLBACK_STOP_PREVIEW = os.getenv("CAPTURE_FALLBACK_STOP_PREVIEW", "0").strip().lower() not in {"0", "false", "off", "no"}
FILTER_SWITCH_MANUAL_APPLY_ENABLED = os.getenv("FILTER_SWITCH_MANUAL_APPLY_ENABLED", "1").strip().lower() not in {"0", "false", "off", "no"}
CAPTURE_CONFIRM_ENABLED = os.getenv("CAPTURE_CONFIRM_ENABLED", "1").strip().lower() not in {"0", "false", "off", "no"}
CAPTURE_CONFIRM_TIMEOUT_SEC = max(0.2, env_float("CAPTURE_CONFIRM_TIMEOUT_SEC", 1.6))

# Filter controls
VINTAGE_MODE = os.getenv("VINTAGE_MODE", "vhs").strip().lower()  # base | vhs | cyber_glitch | pixel_lofi
VINTAGE_MODE_SEQUENCE = ["base", "vhs", "cyber_glitch", "pixel_lofi"]
CRT_OUTPUT_SIZE = os.getenv("CRT_OUTPUT_SIZE", "1024x768").strip()
FORCE_VIDEO_DEVICE = os.getenv("FORCE_VIDEO_DEVICE", "").strip()
HIDE_MOUSE_CURSOR = os.getenv("HIDE_MOUSE_CURSOR", "1").strip().lower() not in {"0", "false", "off", "no"}
KEYBOARD_GRAB_EXCLUSIVE = os.getenv("KEYBOARD_GRAB_EXCLUSIVE", "1").strip().lower() not in {"0", "false", "off", "no"}
KEYBOARD_GRAB_ALL_DEVICES = os.getenv("KEYBOARD_GRAB_ALL_DEVICES", "1").strip().lower() not in {"0", "false", "off", "no"}

# Overlay controls
OVERLAY_ENABLED = os.getenv("OVERLAY_ENABLED", "1").strip().lower() not in {"0", "false", "off", "no"}
OVERLAY_TEXT = os.getenv("OVERLAY_TEXT", "").strip()
OVERLAY_TEXT_FILE = os.getenv("OVERLAY_TEXT_FILE", "/home/bran/photobooth/overlay_text.txt").strip()
OVERLAY_CORNER = os.getenv("OVERLAY_CORNER", "bottom-left").strip().lower()  # bottom-left | bottom-right | middle-top | middle-bottom
SHOW_REC_HUD = os.getenv("SHOW_REC_HUD", "0").strip().lower() in {"1", "true", "on", "yes"}
OVERLAY_FONT_NAME = os.getenv("OVERLAY_FONT_NAME", "VCR OSD Mono").strip()
OVERLAY_FONT_FILE = os.getenv("OVERLAY_FONT_FILE", "").strip()
OVERLAY_FONT_SIZE = max(1, int(env_float("OVERLAY_FONT_SIZE", 28)))
OVERLAY_FONT_COLOR = os.getenv("OVERLAY_FONT_COLOR", "white").strip() or "white"
OVERLAY_BLOCKY_MODE = os.getenv("OVERLAY_BLOCKY_MODE", "1").strip().lower() not in {"0", "false", "off", "no"}
OVERLAY_GLOW_ENABLED = os.getenv("OVERLAY_GLOW_ENABLED", "0").strip().lower() in {"1", "true", "on", "yes"}
OVERLAY_REQUIRE_PIXEL_FONT = os.getenv("OVERLAY_REQUIRE_PIXEL_FONT", "0").strip().lower() in {"1", "true", "on", "yes"}
STATUS_OVERLAY_ENABLED = os.getenv("STATUS_OVERLAY_ENABLED", "1").strip().lower() not in {"0", "false", "off", "no"}
STATUS_OVERLAY_FILE = os.getenv("STATUS_OVERLAY_FILE", "/tmp/photobooth_status.txt").strip() or "/tmp/photobooth_status.txt"

# Capture and print controls
CAPTURE_KEY_ENABLED = os.getenv("CAPTURE_KEY_ENABLED", "1").strip().lower() not in {"0", "false", "off", "no"}
CAPTURE_COOLDOWN_SEC = max(0.0, env_float("CAPTURE_COOLDOWN_SEC", 1.2))
CAPTURE_PAUSE_PREVIEW = os.getenv("CAPTURE_PAUSE_PREVIEW", "0").strip().lower() not in {"0", "false", "off", "no"}
CAPTURE_SAVE_ENABLED = os.getenv("CAPTURE_SAVE_ENABLED", "1").strip().lower() not in {"0", "false", "off", "no"}
CAPTURE_SAVE_DIR = os.getenv("CAPTURE_SAVE_DIR", "/home/bran/photobooth/captures").strip() or "/home/bran/photobooth/captures"
CAPTURE_SAVE_FORMAT = os.getenv("CAPTURE_SAVE_FORMAT", "png").strip().lower() or "png"
CAPTURE_SAVE_JPEG_QUALITY = max(10, min(100, int(env_float("CAPTURE_SAVE_JPEG_QUALITY", 92))))

AUTO_PRINT_ENABLED = os.getenv("AUTO_PRINT_ENABLED", "1").strip().lower() not in {"0", "false", "off", "no"}
PRINT_BACKEND = os.getenv("PRINT_BACKEND", "none").strip().lower()  # none | escpos
PRINT_PAPER_WIDTH_MM = max(1, int(env_float("PRINT_PAPER_WIDTH_MM", 58)))  # 58 or 80
PRINT_HEAD_DOTS = max(1, int(env_float("PRINT_HEAD_DOTS", 384)))
PRINT_DITHER = os.getenv("PRINT_DITHER", "1").strip().lower() not in {"0", "false", "off", "no"}
PRINT_ROTATE = os.getenv("PRINT_ROTATE", "0").strip().lower() in {"1", "true", "on", "yes"}
PRINT_USB_VENDOR_ID = os.getenv("PRINT_USB_VENDOR_ID", "").strip()
PRINT_USB_PRODUCT_ID = os.getenv("PRINT_USB_PRODUCT_ID", "").strip()
PRINT_DEVICE_FILE = os.getenv("PRINT_DEVICE_FILE", "").strip()  # ex: /dev/usb/lp0
VINTAGE_SCALE_FLAGS = os.getenv("VINTAGE_SCALE_FLAGS", "neighbor").strip() or "neighbor"
INSTANCE_LOCK_PATH = os.getenv("INSTANCE_LOCK_PATH", "/tmp/photobooth-preview.lock").strip() or "/tmp/photobooth-preview.lock"


def acquire_instance_lock():
    global _instance_lock_file

    try:
        os.makedirs(os.path.dirname(INSTANCE_LOCK_PATH) or "/tmp", exist_ok=True)
        lock_file = open(INSTANCE_LOCK_PATH, "w", encoding="utf-8")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        _instance_lock_file = lock_file
        return True
    except Exception:
        return False


def normalize_filter_mode(mode):
    mode = (mode or "").strip().lower()
    aliases = {
        "retro": "vhs",
        "crt": "vhs",
        "cyber": "cyber_glitch",
        "glitch": "cyber_glitch",
        "pixel": "pixel_lofi",
        "lofi": "pixel_lofi",
    }
    mode = aliases.get(mode, mode)
    if mode in VINTAGE_MODE_SEQUENCE:
        return mode
    return "base"


def get_vintage_mode():
    with _vintage_mode_lock:
        return normalize_filter_mode(VINTAGE_MODE)


def set_vintage_mode(mode):
    global VINTAGE_MODE
    with _vintage_mode_lock:
        VINTAGE_MODE = normalize_filter_mode(mode)


def get_selected_vintage_mode():
    global _selected_vintage_mode
    if _selected_vintage_mode is None:
        _selected_vintage_mode = get_vintage_mode()
    return normalize_filter_mode(_selected_vintage_mode)


def set_selected_vintage_mode(mode):
    global _selected_vintage_mode
    _selected_vintage_mode = normalize_filter_mode(mode)


def apply_selected_filter_mode():
    current_mode = get_vintage_mode()
    selected_mode = get_selected_vintage_mode()
    if selected_mode == current_mode:
        print(f"Filter apply: already active ({selected_mode}).")
        show_status_message(f"Filter active: {selected_mode}", 1.2)
        return

    with _preview_lock:
        was_running = _preview_proc is not None and _preview_proc.poll() is None
        video_device = _current_device
        profile = {
            "format": _current_format,
            "size": _current_size,
            "framerate": _current_framerate,
        }

        set_vintage_mode(selected_mode)
        print(f"Filter mode applied: {current_mode} -> {selected_mode}")
        show_status_message(f"Filter applied: {selected_mode}", 1.4)

        if not was_running or not video_device or not profile["size"] or not profile["framerate"]:
            return

        try:
            restart_preview_after_switch(video_device, profile, selected_mode, current_mode)
        except Exception as exc:
            print(f"Filter apply failed: {exc}")
            raise


def cycle_vintage_mode(direction):
    global _last_filter_switch_ts

    now = time.time()
    if (now - _last_filter_switch_ts) < FILTER_SWITCH_COOLDOWN_SEC:
        return
    _last_filter_switch_ts = now

    current_mode = get_selected_vintage_mode() if FILTER_SWITCH_MANUAL_APPLY_ENABLED else get_vintage_mode()
    if current_mode not in VINTAGE_MODE_SEQUENCE:
        current_index = VINTAGE_MODE_SEQUENCE.index("vhs")
    else:
        current_index = VINTAGE_MODE_SEQUENCE.index(current_mode)

    next_index = (current_index + direction) % len(VINTAGE_MODE_SEQUENCE)
    next_mode = VINTAGE_MODE_SEQUENCE[next_index]
    if next_mode == current_mode:
        return

    if FILTER_SWITCH_MANUAL_APPLY_ENABLED:
        set_selected_vintage_mode(next_mode)
        print(f"Filter selected (pending): {current_mode} -> {next_mode}. Press Enter to apply.")
        show_status_message(f"Filter selected: {next_mode} | Enter to apply", 2.0)
        return

    with _preview_lock:
        was_running = _preview_proc is not None and _preview_proc.poll() is None
        video_device = _current_device
        profile = {
            "format": _current_format,
            "size": _current_size,
            "framerate": _current_framerate,
        }
        set_vintage_mode(next_mode)
        set_selected_vintage_mode(next_mode)
        print(f"Filter mode changed: {current_mode} -> {next_mode}")

        if not FILTER_SWITCH_LIVE_APPLY:
            print("Filter switch live apply disabled; new mode will apply on next preview restart.")
            return

        if was_running and video_device and profile["size"] and profile["framerate"]:
            try:
                restart_preview_after_switch(video_device, profile, next_mode, current_mode)
            except Exception as exc:
                print(f"Filter switch failed: {exc}")
                raise


def _capture_confirm_mark_pending():
    global _capture_confirm_pending, _capture_confirm_started_at
    with _capture_confirm_lock:
        _capture_confirm_pending = True
        _capture_confirm_started_at = time.time()
    show_status_message("Capture pending: Enter/Space confirm | Esc cancel", CAPTURE_CONFIRM_TIMEOUT_SEC)


def _capture_confirm_clear_pending():
    global _capture_confirm_pending, _capture_confirm_started_at
    with _capture_confirm_lock:
        _capture_confirm_pending = False
        _capture_confirm_started_at = 0.0


def _capture_confirm_is_pending():
    with _capture_confirm_lock:
        return _capture_confirm_pending


def request_capture_confirmation(source="space"):
    del source
    if not CAPTURE_CONFIRM_ENABLED:
        trigger_capture_now("confirm-disabled")
        return

    if _capture_in_progress:
        return

    if _capture_confirm_is_pending():
        confirm_pending_capture("repeat-space")
        return

    _capture_confirm_mark_pending()
    print(f"Capture pending. Press Enter/Space to confirm or Esc to cancel (auto-confirm in {CAPTURE_CONFIRM_TIMEOUT_SEC:.1f}s).")

    started_at = time.time()

    def _auto_confirm():
        time.sleep(CAPTURE_CONFIRM_TIMEOUT_SEC)
        with _capture_confirm_lock:
            if not _capture_confirm_pending:
                return
            if _capture_confirm_started_at != started_at:
                return
        confirm_pending_capture("auto-confirm")

    threading.Thread(target=_auto_confirm, daemon=True).start()


def confirm_pending_capture(source="enter"):
    del source
    if not _capture_confirm_is_pending():
        return
    _capture_confirm_clear_pending()
    show_status_message("Capture confirmed", 1.0)
    trigger_capture_now("confirmed")


def cancel_pending_capture(source="esc"):
    del source
    if not _capture_confirm_is_pending():
        return
    _capture_confirm_clear_pending()
    print("Capture canceled.")
    show_status_message("Capture canceled", 1.0)


def _write_status_overlay(text):
    if not STATUS_OVERLAY_ENABLED:
        return
    try:
        os.makedirs(os.path.dirname(STATUS_OVERLAY_FILE) or "/tmp", exist_ok=True)
        with open(STATUS_OVERLAY_FILE, "w", encoding="utf-8") as handle:
            handle.write((text or "").strip())
    except Exception:
        pass


def show_status_message(text, duration_sec=0.0):
    global _status_overlay_token
    if not STATUS_OVERLAY_ENABLED:
        return

    with _status_overlay_lock:
        _status_overlay_token += 1
        token = _status_overlay_token
    _write_status_overlay(text)

    if duration_sec <= 0:
        return

    def _clear_later(local_token):
        time.sleep(duration_sec)
        with _status_overlay_lock:
            if local_token != _status_overlay_token:
                return
        _write_status_overlay("")

    threading.Thread(target=_clear_later, args=(token,), daemon=True).start()

# Analog capture cards are usually most stable with SD resolutions.
SOURCE_PROFILES = [
    {"format": "mjpeg", "size": "720x480", "framerate": "30"},  # NTSC default
    {"format": "mjpeg", "size": "720x576", "framerate": "25"},  # PAL default
    {"format": "mjpeg", "size": "640x480", "framerate": "30"},
    {"format": "yuyv422", "size": "720x480", "framerate": "30"},
    {"format": "yuyv422", "size": "720x576", "framerate": "25"},
    {"format": None, "size": "640x480", "framerate": "25"},
]


def preferred_print_width():
    if PRINT_PAPER_WIDTH_MM >= 80:
        return 576
    return 384


def parse_int(value, base=10):
    try:
        return int(value, base)
    except Exception:
        return None


def capture_frame_bytes(video_device, profile, filter_chain=None, timeout_sec=8):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found. Install ffmpeg package.")

    input_format = profile.get("format")
    video_size = profile.get("size")
    framerate = profile.get("framerate")

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "video4linux2",
    ]
    if input_format:
        command.extend(["-input_format", input_format])
    command.extend([
        "-framerate",
        framerate,
        "-video_size",
        video_size,
        "-i",
        video_device,
    ])

    if filter_chain:
        command.extend(["-vf", filter_chain])

    command.extend([
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "-",
    ])

    result = subprocess.run(command, capture_output=True, timeout=timeout_sec)
    if result.returncode != 0 or not result.stdout:
        err = (result.stderr or b"").decode("utf-8", errors="ignore").strip()
        raise RuntimeError(f"frame capture failed: {err or 'unknown error'}")
    return result.stdout


def capture_frame_bytes_with_retries(video_device, profile, filter_chain=None, attempts=4):
    last_exc = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return capture_frame_bytes(video_device, profile, filter_chain=filter_chain)
        except Exception as exc:
            last_exc = exc
            message = str(exc).lower()
            # The capture card can stay busy briefly after preview exit.
            if "device or resource busy" in message:
                time.sleep(0.18 * attempt)
                continue
            # Some USB cards return a transient bad first frame after reopen.
            if "no jpeg data found" in message or "invalid data" in message:
                time.sleep(0.12 * attempt)
                continue
            raise

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("frame capture failed after retries")


def should_retry_capture_with_paused_preview(exc):
    message = str(exc).lower()
    return (
        "device or resource busy" in message
        or "no jpeg data found" in message
        or "invalid data" in message
        or "resource busy" in message
    )


def start_transition_cover():
    global _transition_cover_proc

    if not TRANSITION_COVER_ENABLED or not compositor_active():
        return None

    ffplay = shutil.which("ffplay")
    if not ffplay:
        return None

    command = [
        ffplay,
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        "-alwaysontop",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={CRT_OUTPUT_SIZE}:r=30",
        "-fs",
        "-noborder",
    ]
    try:
        proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.06)
        if proc.poll() is not None:
            return None
        _transition_cover_proc = proc
        return proc
    except Exception:
        return None


def stop_transition_cover(proc):
    global _transition_cover_proc

    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=1.2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    if _transition_cover_proc is proc:
        _transition_cover_proc = None


def restart_preview_after_switch(video_device, profile, target_mode, fallback_mode):
    cover_proc = start_transition_cover()
    try:
        stop_preview()
        kill_stale_preview_processes()

        last_exc = None
        for attempt in range(1, 5):
            if _shutdown:
                return
            try:
                time.sleep(0.06 * attempt)
                start_preview(video_device, profile)
                return
            except Exception as exc:
                last_exc = exc
                print(f"Preview restart attempt {attempt} failed for mode {target_mode}: {exc}")
                kill_stale_preview_processes()

        # Roll back to previous mode if all attempts failed, so user is not left stuck.
        set_vintage_mode(fallback_mode)
        for attempt in range(1, 4):
            if _shutdown:
                return
            try:
                time.sleep(0.08 * attempt)
                start_preview(video_device, profile)
                print(f"Reverted to previous mode after failed switch: {fallback_mode}")
                return
            except Exception as exc:
                last_exc = exc
                print(f"Rollback restart attempt {attempt} failed: {exc}")
                kill_stale_preview_processes()

        if last_exc:
            raise last_exc
        raise RuntimeError("Preview restart failed after mode switch")
    finally:
        stop_transition_cover(cover_proc)


def save_capture_bytes(image_bytes):
    if not CAPTURE_SAVE_ENABLED:
        return None

    ext = "png" if CAPTURE_SAVE_FORMAT not in {"jpg", "jpeg"} else "jpg"
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    millis = int((time.time() % 1) * 1000)
    mode = get_vintage_mode()
    filename = f"capture_{timestamp}_{millis:03d}_{mode}.{ext}"

    try:
        with _capture_save_lock:
            os.makedirs(CAPTURE_SAVE_DIR, exist_ok=True)
            output_path = os.path.join(CAPTURE_SAVE_DIR, filename)

            if ext == "png":
                with open(output_path, "wb") as handle:
                    handle.write(image_bytes)
                return output_path

            try:
                from io import BytesIO
                from PIL import Image
            except Exception as exc:
                raise RuntimeError(f"Pillow import failed for JPEG save: {exc}") from exc

            image = Image.open(BytesIO(image_bytes)).convert("RGB")
            image.save(output_path, format="JPEG", quality=CAPTURE_SAVE_JPEG_QUALITY, optimize=True)
            return output_path
    except Exception as exc:
        raise RuntimeError(f"capture save failed: {exc}") from exc


def print_image_bytes(image_bytes):
    if not AUTO_PRINT_ENABLED:
        print("Capture complete (auto print disabled).")
        return

    if PRINT_BACKEND == "none":
        print("Capture complete. Printer backend is 'none', skipping print.")
        return

    try:
        from io import BytesIO
        from PIL import Image
    except Exception as exc:
        raise RuntimeError(f"Pillow import failed: {exc}") from exc

    try:
        from escpos.printer import File, Usb
    except Exception as exc:
        raise RuntimeError(f"python-escpos import failed: {exc}") from exc

    try:
        image = Image.open(BytesIO(image_bytes)).convert("L")
        if PRINT_ROTATE:
            image = image.rotate(90, expand=True)

        target_width = PRINT_HEAD_DOTS if PRINT_HEAD_DOTS else preferred_print_width()
        if target_width <= 0:
            target_width = preferred_print_width()

        if image.width != target_width:
            ratio = target_width / float(max(1, image.width))
            target_height = max(1, int(image.height * ratio))
            image = image.resize((target_width, target_height), Image.Resampling.NEAREST)

        if PRINT_DITHER:
            image = image.convert("1")

        printer = None
        vendor_id = parse_int(PRINT_USB_VENDOR_ID, 0)
        product_id = parse_int(PRINT_USB_PRODUCT_ID, 0)

        if PRINT_DEVICE_FILE:
            printer = File(PRINT_DEVICE_FILE)
        elif vendor_id is not None and product_id is not None:
            printer = Usb(vendor_id, product_id)
        else:
            raise RuntimeError("Printer target not configured. Set PRINT_DEVICE_FILE or PRINT_USB_VENDOR_ID/PRINT_USB_PRODUCT_ID.")

        printer.image(image)
        printer.cut()
        printer.close()
        print(f"Printed capture ({PRINT_PAPER_WIDTH_MM}mm, one copy).")
    except Exception:
        # Bubble up to caller for detailed controller logging.
        raise


def trigger_capture_now(source="space"):
    del source
    global _capture_in_progress, _last_capture_ts

    if not CAPTURE_KEY_ENABLED:
        return

    with _capture_state_lock:
        if _capture_in_progress:
            return

        now = time.time()
        if (now - _last_capture_ts) < CAPTURE_COOLDOWN_SEC:
            return

        _last_capture_ts = now
        _capture_in_progress = True

    def _capture_worker():
        global _capture_in_progress
        try:
            with _preview_lock:
                video_device = _current_device
                profile = {
                    "format": _current_format,
                    "size": _current_size,
                    "framerate": _current_framerate,
                }
                capture_filter_chain = build_video_filter_chain()
                was_running = _preview_proc is not None and _preview_proc.poll() is None

                if not video_device or not profile["size"] or not profile["framerate"]:
                    print("Capture ignored: no active camera source.")
                    return

                print("Capturing frame...")
                captured_bytes = None

                # First try to capture without interrupting preview to avoid desktop flash.
                if was_running:
                    try:
                        captured_bytes = capture_frame_bytes_with_retries(
                            video_device,
                            profile,
                            filter_chain=capture_filter_chain,
                            attempts=4,
                        )
                    except Exception as exc:
                        if not CAPTURE_FALLBACK_STOP_PREVIEW or not should_retry_capture_with_paused_preview(exc):
                            raise

                if captured_bytes is None:
                    cover_proc = start_transition_cover() if (CAPTURE_PAUSE_PREVIEW and was_running and CAPTURE_FALLBACK_STOP_PREVIEW) else None
                    try:
                        if CAPTURE_PAUSE_PREVIEW and was_running and CAPTURE_FALLBACK_STOP_PREVIEW:
                            stop_preview()
                            # Ensure ffplay/cvlc fully releases /dev/video* before one-shot capture.
                            kill_stale_preview_processes()
                            time.sleep(0.2)

                        captured_bytes = capture_frame_bytes_with_retries(
                            video_device,
                            profile,
                            filter_chain=capture_filter_chain,
                            attempts=4,
                        )

                        if CAPTURE_PAUSE_PREVIEW and was_running and CAPTURE_FALLBACK_STOP_PREVIEW and not _shutdown:
                            start_preview(video_device, profile)
                    finally:
                        stop_transition_cover(cover_proc)

            saved_path = save_capture_bytes(captured_bytes)
            if saved_path:
                print(f"Saved capture: {saved_path}")
            print_image_bytes(captured_bytes)
        except Exception as exc:
            # Attempt to recover preview if capture failed after stopping it.
            try:
                with _preview_lock:
                    if _preview_proc is None and _current_device and _current_size and _current_framerate and not _shutdown:
                        start_preview(
                            _current_device,
                            {
                                "format": _current_format,
                                "size": _current_size,
                                "framerate": _current_framerate,
                            },
                        )
            except Exception as restart_exc:
                print(f"Preview recovery failed after capture error: {restart_exc}")
            print(f"Capture/print failed: {exc}")
        finally:
            with _capture_state_lock:
                _capture_in_progress = False

    threading.Thread(target=_capture_worker, daemon=True).start()


def trigger_filter_cycle(direction):
    if direction not in {-1, 1}:
        return

    try:
        cycle_vintage_mode(direction)
    except Exception as exc:
        print(f"Filter cycle failed: {exc}")


def start_tty_space_listener():
    if not CAPTURE_KEY_ENABLED:
        return

    if not sys.stdin or not sys.stdin.isatty():
        print("Space capture listener: no TTY stdin; skipping TTY listener.")
        return

    def _listen():
        print("Capture/filter listener: TTY mode enabled (space + up/down arrows).")
        while not _shutdown:
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 0.2)
                if not ready:
                    continue
                ch = sys.stdin.read(1)
                if ch == " ":
                    request_capture_confirmation("space")
                elif ch == "\x1b":
                    seq = ""
                    for _ in range(2):
                        next_ready, _, _ = select.select([sys.stdin], [], [], 0.02)
                        if not next_ready:
                            break
                        seq += sys.stdin.read(1)

                    if seq == "[A":
                        trigger_filter_cycle(1)
                    elif seq == "[B":
                        trigger_filter_cycle(-1)
                    else:
                        cancel_pending_capture("esc")
                elif ch in {"\n", "\r"}:
                    if _capture_confirm_is_pending():
                        confirm_pending_capture("enter")
                    elif FILTER_SWITCH_MANUAL_APPLY_ENABLED:
                        apply_selected_filter_mode()
            except Exception:
                time.sleep(0.2)

    t = threading.Thread(target=_listen, daemon=True)
    _keyboard_listener_threads.append(t)
    t.start()


def start_evdev_space_listener():
    if not CAPTURE_KEY_ENABLED:
        return

    device_path = os.getenv("KEYBOARD_EVENT_DEVICE", "").strip()
    if not device_path:
        print("Space capture listener: KEYBOARD_EVENT_DEVICE not set; skipping evdev listener.")
        return

    try:
        from evdev import InputDevice, ecodes
    except Exception as exc:
        print(f"Space capture listener: evdev unavailable ({exc}); skipping evdev listener.")
        return

    if not os.path.exists(device_path):
        print(f"Space capture listener: device not found: {device_path}")
        return

    def _listen():
        dev = None
        grabbed = False
        grabbed_extra_devices = []
        try:
            dev = InputDevice(device_path)
            if KEYBOARD_GRAB_EXCLUSIVE:
                try:
                    dev.grab()
                    grabbed = True
                    print(f"Capture/filter listener: grabbed {device_path} exclusively")
                except Exception as exc:
                    print(f"Capture/filter listener: failed to grab {device_path} exclusively ({exc}); keys may leak to desktop")

                if KEYBOARD_GRAB_ALL_DEVICES:
                    target_codes = {
                        ecodes.KEY_SPACE,
                        ecodes.KEY_UP,
                        ecodes.KEY_DOWN,
                        ecodes.KEY_KP8,
                        ecodes.KEY_KP2,
                        ecodes.KEY_ESC,
                    }
                    for extra_path in sorted(glob.glob("/dev/input/event*")):
                        if extra_path == device_path:
                            continue
                        try:
                            extra_dev = InputDevice(extra_path)
                            key_caps = set(extra_dev.capabilities().get(ecodes.EV_KEY, []))
                            if not (key_caps & target_codes):
                                extra_dev.close()
                                continue
                            extra_dev.grab()
                            grabbed_extra_devices.append(extra_dev)
                            print(f"Capture/filter listener: additionally grabbed {extra_path}")
                        except Exception:
                            # Best-effort only; do not fail the listener.
                            try:
                                extra_dev.close()
                            except Exception:
                                pass
            print(f"Capture/filter listener: evdev mode on {device_path} (space + up/down arrows)")
            for event in dev.read_loop():
                if _shutdown:
                    break
                if event.type != ecodes.EV_KEY or event.value != 1:
                    continue
                if event.code == ecodes.KEY_SPACE:
                    request_capture_confirmation("space")
                elif event.code in {ecodes.KEY_UP, ecodes.KEY_KP8}:
                    trigger_filter_cycle(1)
                elif event.code in {ecodes.KEY_DOWN, ecodes.KEY_KP2}:
                    trigger_filter_cycle(-1)
                elif event.code in {ecodes.KEY_ENTER, ecodes.KEY_KPENTER}:
                    if _capture_confirm_is_pending():
                        confirm_pending_capture("enter")
                    elif FILTER_SWITCH_MANUAL_APPLY_ENABLED:
                        apply_selected_filter_mode()
                elif event.code == ecodes.KEY_ESC:
                    cancel_pending_capture("esc")
        except Exception as exc:
            print(f"Space capture listener: evdev error: {exc}")
        finally:
            for extra_dev in grabbed_extra_devices:
                try:
                    extra_dev.ungrab()
                except Exception:
                    pass
                try:
                    extra_dev.close()
                except Exception:
                    pass
            if dev is not None and grabbed:
                try:
                    dev.ungrab()
                except Exception:
                    pass

    t = threading.Thread(target=_listen, daemon=True)
    _keyboard_listener_threads.append(t)
    t.start()


def start_capture_listeners():
    if not CAPTURE_KEY_ENABLED:
        print("Capture key listener disabled.")
        return

    start_evdev_space_listener()
    start_tty_space_listener()


def escape_drawtext_text(text):
    # Escape characters with special meaning in FFmpeg drawtext.
    return text.replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'")


def find_fontfile(preferred_name=None, preferred_file=None):
    if preferred_file and os.path.exists(preferred_file):
        return f"fontfile={preferred_file}"

    preferred_candidates = [
        "/home/bran/photobooth/fonts/VCR_OSD_MONO_1.001.ttf",
        "/home/bran/photobooth/fonts/VCR OSD Mono.ttf",
        "/usr/local/share/fonts/VCR_OSD_MONO_1.001.ttf",
        "/usr/local/share/fonts/VCR OSD Mono.ttf",
        "/usr/share/fonts/truetype/vcr/VCR_OSD_MONO_1.001.ttf",
        "/usr/share/fonts/truetype/vcr/VCR OSD Mono.ttf",
    ]
    for path in preferred_candidates:
        if os.path.exists(path):
            return f"fontfile={path}"

    if preferred_name:
        return f"font='{escape_drawtext_text(preferred_name)}'"

    fallback_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for path in fallback_candidates:
        if os.path.exists(path):
            return f"fontfile={path}"
    return None


def drawtext_filter(text, x, y, size, color, border_color="black@0.8", borderw=2, font_name=None, font_file=None):
    safe_text = escape_drawtext_text(text)
    font_spec = find_fontfile(font_name, font_file)
    parts = [
        f"text='{safe_text}'",
        f"x={x}",
        f"y={y}",
        f"fontsize={size}",
        f"fontcolor={color}",
        f"borderw={borderw}",
        f"bordercolor={border_color}",
    ]
    if font_spec:
        parts.insert(0, font_spec)
    return "drawtext=" + ":".join(parts)


def drawtext_textfile_filter(textfile_path, x, y, size, color, border_color="black@0.8", borderw=2, font_name=None, font_file=None):
    font_spec = find_fontfile(font_name, font_file)
    safe_path = textfile_path.replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'")
    parts = [
        f"textfile='{safe_path}'",
        "reload=1",
        f"x={x}",
        f"y={y}",
        f"fontsize={size}",
        f"fontcolor={color}",
        f"borderw={borderw}",
        f"bordercolor={border_color}",
    ]
    if font_spec:
        parts.insert(0, font_spec)
    return "drawtext=" + ":".join(parts)


def normalize_overlay_position(position):
    aliases = {
        "top-center": "middle-top",
        "center-top": "middle-top",
        "bottom-center": "middle-bottom",
        "center-bottom": "middle-bottom",
    }
    position = aliases.get(position, position)
    if position in {"bottom-left", "bottom-right", "middle-top", "middle-bottom"}:
        return position
    return "bottom-left"


def overlay_layout(position):
    position = normalize_overlay_position(position)

    if position == "bottom-right":
        return "w-text_w-28", "h-text_h-20", "drawbox=x=18:y=h-58:w=iw-36:h=42:color=black@0.18:t=fill"
    if position == "middle-top":
        return "(w-text_w)/2", "20", "drawbox=x=18:y=10:w=iw-36:h=42:color=black@0.18:t=fill"
    if position == "middle-bottom":
        return "(w-text_w)/2", "h-text_h-20", "drawbox=x=18:y=h-58:w=iw-36:h=42:color=black@0.18:t=fill"
    return "28", "h-text_h-20", "drawbox=x=18:y=h-58:w=iw-36:h=42:color=black@0.18:t=fill"


def build_overlay_filter():
    if get_vintage_mode() == "base":
        return None

    overlay_text = OVERLAY_TEXT
    if OVERLAY_ENABLED and not overlay_text and OVERLAY_TEXT_FILE and os.path.exists(OVERLAY_TEXT_FILE):
        try:
            with open(OVERLAY_TEXT_FILE, "r", encoding="utf-8") as f:
                overlay_text = f.read().strip()
        except Exception:
            overlay_text = OVERLAY_TEXT

    chain = []
    font_spec = find_fontfile(OVERLAY_FONT_NAME, OVERLAY_FONT_FILE)

    if OVERLAY_ENABLED:
        if OVERLAY_REQUIRE_PIXEL_FONT and (not font_spec or not font_spec.startswith("fontfile=")):
            print("Overlay text disabled: OVERLAY_REQUIRE_PIXEL_FONT is enabled but no usable font file was found.")
            overlay_text = ""

        if SHOW_REC_HUD:
            chain.extend([
                # Camcorder-style REC marker.
                "drawbox=x=30:y=30:w=14:h=14:color=red@0.95:t=fill:enable='lt(mod(t,1),0.5)'",
                drawtext_filter("REC", "52", "23", 24, "white", "black@0.8", 2, OVERLAY_FONT_NAME, OVERLAY_FONT_FILE),
            ])

        if overlay_text:
            text_x, text_y, box_filter = overlay_layout(OVERLAY_CORNER)
            chain.append(box_filter)
            if OVERLAY_BLOCKY_MODE:
                chain.append(drawtext_filter(overlay_text, text_x, text_y, OVERLAY_FONT_SIZE, OVERLAY_FONT_COLOR, "black@0.92", 2, OVERLAY_FONT_NAME, OVERLAY_FONT_FILE))
            else:
                if OVERLAY_GLOW_ENABLED:
                    chain.append(drawtext_filter(overlay_text, text_x, text_y, OVERLAY_FONT_SIZE, "#3fd7ff@0.14", "#3fd7ff@0.10", 8, OVERLAY_FONT_NAME, OVERLAY_FONT_FILE))
                    chain.append(drawtext_filter(overlay_text, text_x, text_y, OVERLAY_FONT_SIZE, "#61e3ff@0.22", "#3fd7ff@0.16", 4, OVERLAY_FONT_NAME, OVERLAY_FONT_FILE))
                chain.append(drawtext_filter(overlay_text, text_x, text_y, OVERLAY_FONT_SIZE, OVERLAY_FONT_COLOR, "black@0.75", 1, OVERLAY_FONT_NAME, OVERLAY_FONT_FILE))

    if STATUS_OVERLAY_ENABLED:
        chain.append("drawbox=x=18:y=12:w=iw-36:h=46:color=black@0.22:t=fill")
        chain.append(drawtext_textfile_filter(STATUS_OVERLAY_FILE, "30", "20", max(18, int(OVERLAY_FONT_SIZE * 0.6)), "white", "black@0.95", 2, OVERLAY_FONT_NAME, OVERLAY_FONT_FILE))

    if not chain:
        return None
    return ",".join(chain)


def build_vintage_filter():
    vintage_mode = get_vintage_mode()

    if vintage_mode == "base":
        return None

    if vintage_mode == "vhs":
        # VHS look: heavier tape wobble vibe, stronger grain and scanline texture.
        return (
            "format=yuv420p,"
            "setsar=1,setdar=4/3,"
            f"scale={CRT_OUTPUT_SIZE}:flags={VINTAGE_SCALE_FLAGS},"
            "eq=contrast=1.18:brightness=-0.01:saturation=1.04:gamma=1.00,"
            "hue=h=-6:s=1.02,"
            "noise=alls=14:allf=t+u,"
            "gblur=sigma=0.58,"
            "drawgrid=width=iw:height=4:thickness=1:color=black@0.24,"
            "drawgrid=width=iw:height=4:thickness=1:color=black@0.12:y=2"
        )

    if vintage_mode == "cyber_glitch":
        # Strong cyber/glitch look with hard contrast and aggressive color shift.
        return (
            "format=yuv420p,"
            "setsar=1,setdar=4/3,"
            f"scale={CRT_OUTPUT_SIZE}:flags={VINTAGE_SCALE_FLAGS},"
            "rgbashift=rh=6:rv=2:gh=-4:gv=-2:bh=-9:bv=4,"
            "eq=contrast=1.48:brightness=-0.02:saturation=1.62:gamma=1.11,"
            "hue=h=18:s=1.34,"
            "colorbalance=rs=0.14:gs=-0.20:bs=0.10,"
            "noise=alls=10:allf=t+u,"
            "unsharp=7:7:2.1:7:7:0.0,"
            "drawgrid=width=iw:height=3:thickness=1:color=black@0.26,"
            "drawgrid=width=iw:height=3:thickness=1:color=#00ffff@0.10:y=1"
        )

    if vintage_mode == "pixel_lofi":
        # Strong low-fi pixelation with chunky nearest-neighbor scaling.
        return (
            "format=yuv420p,"
            "setsar=1,setdar=4/3,"
            "scale=240:180:flags=neighbor,"
            f"scale={CRT_OUTPUT_SIZE}:flags=neighbor,"
            "eq=contrast=1.28:brightness=-0.04:saturation=1.42:gamma=1.08,"
            "noise=alls=9:allf=t+u,"
            "drawgrid=width=3:height=3:thickness=1:color=black@0.14"
        )

    # Any unknown value falls back to base camera output.
    return None


def build_video_filter_chain():
    parts = []
    vintage = build_vintage_filter()
    overlay = build_overlay_filter()

    if vintage:
        parts.append(vintage)
    if overlay:
        parts.append(overlay)

    if not parts:
        return None
    return ",".join(parts)


def signal_handler(sig, frame):
    del sig, frame
    global _shutdown
    _shutdown = True


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def find_video_devices():
    global _warned_force_device_missing

    if FORCE_VIDEO_DEVICE:
        if os.path.exists(FORCE_VIDEO_DEVICE):
            return [FORCE_VIDEO_DEVICE]

        if not _warned_force_device_missing:
            print(f"Configured FORCE_VIDEO_DEVICE not found: {FORCE_VIDEO_DEVICE}. Falling back to auto-detect.")
            _warned_force_device_missing = True

    preferred = ["/dev/video0", "/dev/video1", "/dev/video2", "/dev/video3"]
    candidates = [dev for dev in preferred if os.path.exists(dev)]
    if not candidates:
        candidates = sorted(glob.glob("/dev/video*"))

    return candidates


def probe_device(device, profile):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return True

    input_format = profile["format"]
    video_size = profile["size"]
    framerate = profile["framerate"]

    probe_cmd = [
        ffmpeg,
        "-y",
        "-f",
        "video4linux2",
    ]
    if input_format:
        probe_cmd.extend(["-input_format", input_format])
    probe_cmd.extend([
        "-framerate",
        framerate,
        "-video_size",
        video_size,
        "-i",
        device,
        "-frames:v",
        "1",
        "-f",
        "null",
        "-",
    ])

    try:
        result = subprocess.run(
            probe_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def pick_source():
    candidates = find_video_devices()

    if not candidates:
        return None, None

    for dev in candidates:
        for profile in SOURCE_PROFILES:
            if probe_device(dev, profile):
                return dev, profile

    return candidates[0], SOURCE_PROFILES[-1]


def compositor_active():
    try:
        result = subprocess.run(
            ["pgrep", "-f", "labwc|wayfire|weston|sway|kwin_wayland|mutter|Xorg|Xwayland"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False


def start_cursor_hider():
    global _cursor_hider_proc

    if not HIDE_MOUSE_CURSOR:
        return
    if _cursor_hider_proc is not None and _cursor_hider_proc.poll() is None:
        return

    unclutter = shutil.which("unclutter")
    if not unclutter:
        print("Cursor hide requested but 'unclutter' is not installed; mouse pointer may remain visible.")
        return

    try:
        # Keep running while preview is active so cursor stays hidden.
        _cursor_hider_proc = subprocess.Popen(
            [unclutter, "-idle", "0", "-root"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        print(f"Failed to start cursor hider: {exc}")


def stop_cursor_hider():
    global _cursor_hider_proc

    if _cursor_hider_proc is None:
        return

    try:
        if _cursor_hider_proc.poll() is None:
            _cursor_hider_proc.terminate()
            _cursor_hider_proc.wait(timeout=2)
    except Exception:
        try:
            _cursor_hider_proc.kill()
        except Exception:
            pass
    _cursor_hider_proc = None


def kill_stale_preview_processes():
    # Prevent old ffplay/cvlc instances from holding /dev/video* and causing
    # "Invalid argument" / "No such device" loops.
    for pattern in ["ffplay.*video4linux2", "cvlc.*v4l2://"]:
        try:
            subprocess.run(["pkill", "-f", pattern], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def start_preview(video_device, profile):
    global _preview_proc, _current_device, _current_format, _current_size, _current_framerate, _last_start_ts

    input_format = profile["format"]
    video_size = profile["size"]
    framerate = profile["framerate"]
    filter_chain = build_video_filter_chain()

    if compositor_active():
        start_cursor_hider()
        ffplay = shutil.which("ffplay")
        if not ffplay:
            raise RuntimeError("ffplay not found. Install ffmpeg package.")

        # Compositor mode (desktop session): fullscreen preview window
        command = [
            ffplay,
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "error",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-framedrop",
            "-f",
            "video4linux2",
        ]
        if input_format:
            command.extend(["-input_format", input_format])
        command.extend([
            "-video_size",
            video_size,
            "-framerate",
            framerate,
            "-i",
            video_device,
        ])

        if filter_chain:
            command.extend(["-vf", filter_chain])

        command.extend([
            "-fs",
            "-noborder",
        ])
        print("Preview mode: compositor fullscreen (ffplay)")
        print(f"Filter mode: {get_vintage_mode()}, overlay: {'off' if get_vintage_mode() == 'base' else ('on' if OVERLAY_ENABLED else 'off')}")
        if OVERLAY_TEXT:
            print(f"Overlay text (env): {OVERLAY_TEXT}")
        elif OVERLAY_TEXT_FILE:
            print(f"Overlay text file: {OVERLAY_TEXT_FILE}")
        _preview_proc = subprocess.Popen(command)
    else:
        cvlc = shutil.which("cvlc")
        if not cvlc:
            raise RuntimeError("cvlc not found. Install vlc package.")

        # DRM mode (TTY): direct output to HDMI without desktop
        command = [
            cvlc,
            "-I",
            "dummy",
            f"v4l2://{video_device}",
            "--vout",
            "drm_vout",
            "--fullscreen",
            "--no-audio",
            "--quiet",
        ]
        print("Preview mode: direct DRM (cvlc)")
        _preview_proc = subprocess.Popen(command)

    # Give process a moment and verify it started.
    time.sleep(0.8)
    if _preview_proc.poll() is not None:
        raise RuntimeError("Preview process exited immediately")

    _current_device = video_device
    _current_format = input_format
    _current_size = video_size
    _current_framerate = framerate
    _last_start_ts = time.time()


def stop_preview():
    global _preview_proc

    if _preview_proc is None:
        return

    try:
        if _preview_proc.poll() is None:
            _preview_proc.terminate()
            _preview_proc.wait(timeout=2)
    except Exception:
        try:
            _preview_proc.kill()
            _preview_proc.wait(timeout=1)
        except Exception:
            pass

    _preview_proc = None
    stop_cursor_hider()


def start_with_retries(reason):
    global _current_device, _current_format, _current_size, _current_framerate

    retry_interval = RETRY_INTERVAL_SEC
    restart_count = 0

    while not _shutdown:
        # Ensure stale ffplay/cvlc instances are not holding /dev/video* between retries.
        kill_stale_preview_processes()
        time.sleep(0.08)

        video_device = None
        profile = None

        # Prefer last known-good source first to avoid expensive probing churn.
        if _current_device and os.path.exists(_current_device) and _current_size and _current_framerate:
            video_device = _current_device
            profile = {
                "format": _current_format,
                "size": _current_size,
                "framerate": _current_framerate,
            }
        else:
            video_device, profile = pick_source()

        if not video_device:
            print(f"[{reason}] No /dev/video* camera device found, retrying in {retry_interval:.1f}s...")
            time.sleep(retry_interval)
            retry_interval = min(retry_interval * 1.5, MAX_RETRY_INTERVAL_SEC)
            continue

        print(f"[{reason}] Using camera device: {video_device}")
        print(f"[{reason}] Using profile: {profile['size']} @ {profile['framerate']}fps, format={profile['format'] or 'auto'}")

        try:
            start_preview(video_device, profile)
            restart_count += 1
            if restart_count == 1 and PERIODIC_RESTART_SEC > 0:
                print(f"Periodic refresh enabled every {PERIODIC_RESTART_SEC:.1f}s")
            print(f"[{reason}] Preview started (restart #{restart_count})")
            return True
        except Exception as exc:
            print(f"[{reason}] Failed to start preview: {exc}")
            # If cached profile fails repeatedly, force a fresh probe on next loop.
            _current_device = None
            _current_format = None
            _current_size = None
            _current_framerate = None
            time.sleep(retry_interval)
            retry_interval = min(retry_interval * 1.5, MAX_RETRY_INTERVAL_SEC)

    return False


def main():
    kill_stale_preview_processes()
    _write_status_overlay("")

    # Keep trying until a camera source appears and preview starts.
    if not start_with_retries("startup"):
        return 0

    if _shutdown:
        return 0

    show_status_message(f"Filter active: {get_vintage_mode()} | Up/Down select, Enter apply", 2.2)
    start_capture_listeners()
    print("Live preview running. Press Ctrl+C to stop.")

    try:
        while not _shutdown:
            if _capture_in_progress:
                time.sleep(0.05)
                continue

            if _current_device and not os.path.exists(_current_device):
                print(f"Active device disappeared: {_current_device}. Re-probing...")
                stop_preview()
                if not start_with_retries("device-lost"):
                    break

            # Prevent long-running ffplay/cvlc drift/freezes on flaky USB capture.
            if PERIODIC_RESTART_SEC > 0 and _preview_proc is not None and (time.time() - _last_start_ts) > PERIODIC_RESTART_SEC:
                print("Refreshing preview stream...")
                stop_preview()
                if not start_with_retries("periodic-refresh"):
                    break

            if _preview_proc is not None and _preview_proc.poll() is not None:
                print("Preview process stopped unexpectedly. Re-probing...")
                if not start_with_retries("unexpected-exit"):
                    break
            time.sleep(0.2)
    finally:
        stop_preview()

    print("Preview stopped.")
    return 0


if __name__ == "__main__":
    if not acquire_instance_lock():
        print(f"Another controller instance is already running (lock: {INSTANCE_LOCK_PATH}). Exiting.")
        sys.exit(0)

    exit_code = 0
    while not _shutdown:
        try:
            exit_code = main()
        except Exception:
            # Keep the controller alive on unexpected Python exceptions.
            traceback.print_exc()
            stop_preview()
            if _shutdown:
                break
            print(f"Controller exception; retrying in {RETRY_INTERVAL_SEC:.1f}s...")
            time.sleep(RETRY_INTERVAL_SEC)
            continue
        break

    sys.exit(exit_code)
