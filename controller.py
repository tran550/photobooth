import glob
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback


_shutdown = False
_preview_proc = None
_current_device = None
_current_format = None
_current_size = None
_current_framerate = None
_last_start_ts = 0.0
_warned_force_device_missing = False

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

# Vintage look controls (override with env vars if needed)
VINTAGE_MODE = os.getenv("VINTAGE_MODE", "crt").strip().lower()  # camcorder | crt | film | light | off
CRT_OUTPUT_SIZE = os.getenv("CRT_OUTPUT_SIZE", "1024x768").strip()
FORCE_VIDEO_DEVICE = os.getenv("FORCE_VIDEO_DEVICE", "").strip()

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

# Analog capture cards are usually most stable with SD resolutions.
SOURCE_PROFILES = [
    {"format": "mjpeg", "size": "720x480", "framerate": "30"},  # NTSC default
    {"format": "mjpeg", "size": "720x576", "framerate": "25"},  # PAL default
    {"format": "mjpeg", "size": "640x480", "framerate": "30"},
    {"format": "yuyv422", "size": "720x480", "framerate": "30"},
    {"format": "yuyv422", "size": "720x576", "framerate": "25"},
    {"format": None, "size": "640x480", "framerate": "25"},
]


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
    if not OVERLAY_ENABLED:
        return None

    overlay_text = OVERLAY_TEXT
    if not overlay_text and OVERLAY_TEXT_FILE and os.path.exists(OVERLAY_TEXT_FILE):
        try:
            with open(OVERLAY_TEXT_FILE, "r", encoding="utf-8") as f:
                overlay_text = f.read().strip()
        except Exception:
            overlay_text = OVERLAY_TEXT

    chain = []
    if SHOW_REC_HUD:
        chain.extend([
            # Camcorder-style REC marker.
            "drawbox=x=30:y=30:w=14:h=14:color=red@0.95:t=fill:enable='lt(mod(t,1),0.5)'",
            drawtext_filter("REC", "52", "23", 24, "white", "black@0.8", 2, OVERLAY_FONT_NAME, OVERLAY_FONT_FILE),
        ])

    if overlay_text:
        text_x, text_y, box_filter = overlay_layout(OVERLAY_CORNER)
        chain.append(box_filter)
        chain.append(drawtext_filter(overlay_text, text_x, text_y, OVERLAY_FONT_SIZE, "#3fd7ff@0.14", "#3fd7ff@0.10", 8, OVERLAY_FONT_NAME, OVERLAY_FONT_FILE))
        chain.append(drawtext_filter(overlay_text, text_x, text_y, OVERLAY_FONT_SIZE, "#61e3ff@0.22", "#3fd7ff@0.16", 4, OVERLAY_FONT_NAME, OVERLAY_FONT_FILE))
        chain.append(drawtext_filter(overlay_text, text_x, text_y, OVERLAY_FONT_SIZE, OVERLAY_FONT_COLOR, "black@0.75", 1, OVERLAY_FONT_NAME, OVERLAY_FONT_FILE))

    if not chain:
        return None
    return ",".join(chain)


def build_vintage_filter():
    if VINTAGE_MODE in {"off", "none", "0", "false"}:
        return None

    if VINTAGE_MODE == "film":
        # Softer contrast curve, warm bias, and fine grain for a filmic look.
        return (
            "format=yuv420p,"
            "setsar=1,setdar=4/3,"
            f"scale={CRT_OUTPUT_SIZE}:flags=bilinear,"
            "eq=contrast=1.08:brightness=-0.01:saturation=0.96:gamma=0.99,"
            "colorbalance=rs=0.05:gs=0.02:bs=-0.03,"
            "noise=alls=8:allf=t+u,"
            "gblur=sigma=0.35,"
            "vignette=PI/9"
        )

    if VINTAGE_MODE == "camcorder":
        return (
            "format=yuv420p,"
            "setsar=1,setdar=4/3,"
            f"scale={CRT_OUTPUT_SIZE}:flags=bilinear,"
            "eq=contrast=1.20:brightness=-0.02:saturation=0.88:gamma=1.05,"
            "hue=h=5:s=1.08,"
            "noise=alls=12:allf=t+u,"
            "gblur=sigma=0.65,"
            "vignette=PI/6,"
            "drawgrid=width=iw:height=3:thickness=1:color=black@0.16"
        )

    # Keep a stable 4:3 image for CRT displays and add analog character.
    base_chain = (
        "format=yuv420p,"
        "setsar=1,setdar=4/3,"
        f"scale={CRT_OUTPUT_SIZE}:flags=bilinear,"
        "eq=contrast=1.12:brightness=-0.01:saturation=1.02:gamma=1.02,"
        "hue=h=3:s=1.10,"
        "noise=alls=5:allf=t+u,"
        "drawgrid=width=iw:height=4:thickness=1:color=black@0.10"
    )

    if VINTAGE_MODE == "light":
        return (
            "format=yuv420p,"
            "setsar=1,setdar=4/3,"
            f"scale={CRT_OUTPUT_SIZE}:flags=bilinear,"
            "eq=contrast=1.07:brightness=0.00:saturation=1.06:gamma=1.01,"
            "hue=h=2:s=1.04"
        )

    return base_chain


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
        print(f"Vintage mode: {VINTAGE_MODE}, overlay: {'on' if OVERLAY_ENABLED else 'off'}")
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
        except Exception:
            pass

    _preview_proc = None


def start_with_retries(reason):
    retry_interval = RETRY_INTERVAL_SEC
    restart_count = 0

    while not _shutdown:
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
            time.sleep(retry_interval)
            retry_interval = min(retry_interval * 1.5, MAX_RETRY_INTERVAL_SEC)

    return False


def main():
    kill_stale_preview_processes()

    # Keep trying until a camera source appears and preview starts.
    if not start_with_retries("startup"):
        return 0

    if _shutdown:
        return 0

    print("Live preview running. Press Ctrl+C to stop.")

    try:
        while not _shutdown:
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
