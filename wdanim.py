import re

import cv2
import numpy as np
import os
import sys
import time
import msvcrt
import threading
import ctypes
import subprocess
import pickle
import hashlib

# 1. Configuration & CLI Argument Handling
if len(sys.argv) < 2:
    print("Usage: python script.py <path_to_video> [-color] [-edge] [-negative] [-moz] [-wish \"TEXT\"] [-frames N] [-start SS] [-end SS] [-save]")
    sys.exit(1)

video_path = sys.argv[1]

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.bmp', '.webp', '.tiff', '.tif'}
is_image = os.path.splitext(video_path)[1].lower() in IMAGE_EXTENSIONS

# Parsing flags
use_wish = '-wish' in sys.argv

if use_wish:
    idx = sys.argv.index('-wish')
    if idx + 1 >= len(sys.argv):
        print("Error: -wish flag requires a string(wish), e.g. -wish happybirthdaysoham")
        sys.exit(1)
    try:
        WISH = str(sys.argv[idx + 1])
    except TypeError:
        print("Error: -wish must be a string")
        sys.exit(1)

use_mozaic = '-moz' in sys.argv
use_color = '-color' in sys.argv
use_edge = '-edge' in sys.argv
use_negative = '-negative' in sys.argv
use_save = '-save' in sys.argv

if use_wish and not use_color:
    print("Note: -wish looks best with -color -- ignoring it without -color.")
    use_wish = False

max_frames = None
if '-frames' in sys.argv:
    idx = sys.argv.index('-frames')
    if idx + 1 >= len(sys.argv):
        print("Error: -frames flag requires a number, e.g. -frames 150")
        sys.exit(1)
    try:
        max_frames = int(sys.argv[idx + 1])
        if max_frames <= 0:
            raise ValueError
    except ValueError:
        print("Error: -frames must be a positive integer")
        sys.exit(1)


def parse_timestamp(value):
    """Accepts plain seconds ('90', '12.5') or 'MM:SS' / 'HH:MM:SS' and returns seconds as a float."""
    if ':' in value:
        parts = value.split(':')
        try:
            parts = [float(p) for p in parts]
        except ValueError:
            return None
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds
    try:
        return float(value)
    except ValueError:
        return None


start_time = None
if '-start' in sys.argv:
    idx = sys.argv.index('-start')
    if idx + 1 >= len(sys.argv):
        print("Error: -start flag requires a timestamp, e.g. -start 30 or -start 1:30")
        sys.exit(1)
    start_time = parse_timestamp(sys.argv[idx + 1])
    if start_time is None or start_time < 0:
        print("Error: -start must be seconds (e.g. 30) or MM:SS / HH:MM:SS")
        sys.exit(1)

end_time = None
if '-end' in sys.argv:
    idx = sys.argv.index('-end')
    if idx + 1 >= len(sys.argv):
        print("Error: -end flag requires a timestamp, e.g. -end 90 or -end 2:00")
        sys.exit(1)
    end_time = parse_timestamp(sys.argv[idx + 1])
    if end_time is None or end_time < 0:
        print("Error: -end must be seconds (e.g. 90) or MM:SS / HH:MM:SS")
        sys.exit(1)

if start_time is not None and end_time is not None and end_time <= start_time:
    print("Error: -end must be greater than -start")
    sys.exit(1)

# State variables for playback timing
if is_image:
    playback_speed = 0.1
else:
    playback_speed = 1.0  # Drops to 0.0 if ESC is pressed

# Characters for structural lines (edge mode) and brightness gradient (standard mode)
EDGE_CHARS = ["#", "*", "=", "+", ":", "."]
ASCII_GRADIENT = ' .:-=+*#%@'

if use_wish:
    wish_ptr = -1
    ASCII_GRADIENT =  WISH[wish_ptr]  # dark -> light == sparse -> dense
if use_mozaic:
    ASCII_GRADIENT = "▄"

def enable_windows_ansi():
    """Turn on native VT100 processing so raw ANSI escape codes (cursor movement,
    256-color foreground codes) work correctly on Windows consoles."""
    if os.name == 'nt':
        kernel32 = ctypes.windll.kernel32
        h = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        kernel32.GetConsoleMode(h, ctypes.byref(mode))
        kernel32.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING


enable_windows_ansi()


# --- ANSI-256 nearest-color matching -----------------------------------------------
def _build_ansi256_palette():
    """Builds the actual RGB values of all 256 standard xterm ANSI colors, so we can
    find which one is truly closest to a given pixel's real color."""
    basic = [
        (0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0),
        (0, 0, 128), (128, 0, 128), (0, 128, 128), (192, 192, 192),
        (128, 128, 128), (255, 0, 0), (0, 255, 0), (255, 255, 0),
        (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
    ]
    cube_steps = [0, 95, 135, 175, 215, 255]
    cube = [(cube_steps[r], cube_steps[g], cube_steps[b])
            for r in range(6) for g in range(6) for b in range(6)]
    gray = [(8 + 10 * i,) * 3 for i in range(24)]
    return np.array(basic + cube + gray, dtype=np.int32)  # shape (256, 3)


ANSI256_PALETTE = _build_ansi256_palette()


def rgb_array_to_ansi256(rgb):
    """rgb: HxWx3 array (R,G,B). Returns an HxW array of the nearest ANSI-256 color
    code for each pixel, found by true Euclidean distance against the real palette."""
    h, w, _ = rgb.shape
    pixels = rgb.reshape(-1, 1, 3).astype(np.int32)              # (N,1,3)
    diffs = pixels - ANSI256_PALETTE[None, :, :]                  # (N,256,3)
    dist_sq = np.einsum('ijk,ijk->ij', diffs, diffs)              # (N,256)
    nearest = np.argmin(dist_sq, axis=-1)                          # (N,)
    return nearest.reshape(h, w)


def build_frame_string(char_rows, color_codes):
    """Combines the character grid with per-cell ANSI-256 color codes. Non-space
    cells get a foreground color match; space cells are left uncolored.
    Consecutive cells with identical styling share one escape code to stay compact."""
    lines = []
    for y, row_chars in enumerate(char_rows):
        parts = []
        last_style = None
        for x, ch in enumerate(row_chars):
            style = None
            if color_codes is not None and ch != ' ':
                code = int(color_codes[y, x])
                style = f'\x1b[38;5;{code}m'
            if style != last_style:
                if last_style is not None:
                    parts.append('\x1b[0m')
                if style is not None:
                    parts.append(style)
                last_style = style
            parts.append(ch)
        if last_style is not None:
            parts.append('\x1b[0m')
        lines.append(''.join(parts))
    return '\n'.join(lines)

def print_with_delay(text):
    global playback_speed
    delay = playback_speed
    # Matches ANSI escape codes, standard escape sequences, or single characters
    pattern = re.compile(r"(\x1b\[[0-9;]*[a-zA-Z]|.)", re.DOTALL)

    for match in pattern.finditer(text):
        token = match.group()

        sys.stdout.write(token)
        sys.stdout.flush()

        # Only delay if it is a visible character, not an escape code
        if not token.startswith("\x1b") and not token.startswith("\\"):
            delay = playback_speed
            time.sleep(delay)

# 2. Background Escape Key Listener
def check_esc():
    """Listens for the Escape key to instantly skip frame delays."""
    global playback_speed
    while True:
        if msvcrt.kbhit():
            key = msvcrt.getwch()
            if key == "\x1b":  # ESC key hex code
                playback_speed = 0.0
                break
        time.sleep(0.1)


# 3. Video Processing Phase (or load from cache)
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(video_path)) or '.', '.ascii_cache')


def compute_cache_key(w):
    """Fingerprints the video file plus every setting that changes the rendered
    output, so a cache is only reused when it's guaranteed to match exactly --
    if the video file or any relevant flag changes, this key changes too."""
    stat = os.stat(video_path)
    fingerprint = (
        os.path.abspath(video_path), stat.st_mtime, stat.st_size,
        use_edge, use_color, use_negative, max_frames, start_time, end_time, w,
    )
    return hashlib.sha256(repr(fingerprint).encode()).hexdigest()[:16]


width = 120 if use_edge else 100

cache_path = None
if use_save and not is_image:
    cache_path = os.path.join(
        CACHE_DIR, f"{os.path.basename(video_path)}.{compute_cache_key(width)}.cache"
    )

ascii_frames = []  # list of (char_rows, color_codes_or_None)
fps = None
delay = None
loaded_from_cache = False

if cache_path is not None and os.path.exists(cache_path):
    print("Matching cache found -- loading pre-rendered frames instead of reprocessing...")
    with open(cache_path, 'rb') as f:
        cached = pickle.load(f)
    ascii_frames = cached['ascii_frames']
    fps = cached['fps']
    delay = cached['delay']
    loaded_from_cache = True
    print(f"Loaded {len(ascii_frames)} cached frames.")

if not loaded_from_cache:
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    delay = 1.0 / fps if fps > 0 else 0.04

    video_duration = cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps if fps > 0 else None
    if start_time is not None and video_duration is not None and start_time >= video_duration:
        print(f"Error: -start ({start_time}s) is beyond the video length ({video_duration:.1f}s)")
        sys.exit(1)

    if start_time is not None:
        cap.set(cv2.CAP_PROP_POS_MSEC, start_time * 1000)

    end_time_ms = end_time * 1000 if end_time is not None else None

    frame_limit_note = f" (capped at {max_frames} frames)" if max_frames is not None else ""
    if start_time is not None or end_time is not None:
        range_start = f"{start_time}s" if start_time is not None else "start"
        range_end = f"{end_time}s" if end_time is not None else "end"
        frame_limit_note += f" [range {range_start} -> {range_end}]"
    if use_color:
        frame_limit_note += " [true per-pixel color matching]"
    if use_negative:
        frame_limit_note += " [negative]"

    mode_label = "Edge Detection" if use_edge else "Brightness Gradient"
    
    if not is_image:
        print(f"Processing video via {mode_label} mode...{frame_limit_note}")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if end_time_ms is not None and cap.get(cv2.CAP_PROP_POS_MSEC) > end_time_ms:
            break

        if use_negative:
            # Invert every channel (255 - value) so both the brightness-driven character
            # choice and the color match reflect the negative image, not just the color.
            frame = 255 - frame

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h_orig, w_orig = gray.shape
        height = max(1, int((width * h_orig / w_orig) * 0.5))  # Squashed for terminal aspect ratio
        gray_small = cv2.resize(gray, (width, height))

        char_rows = []
        if use_edge:
            blurred = cv2.GaussianBlur(gray_small, (3, 3), 0)
            edges = cv2.Canny(blurred, 50, 150)
            for y in range(height):
                row_chars = []
                for x in range(width):
                    edge_val = edges[y, x]
                    pixel_val = gray_small[y, x]
                    if edge_val > 0:
                        idx = int(pixel_val / 256 * len(EDGE_CHARS))
                        row_chars.append(EDGE_CHARS[idx])
                    else:
                        row_chars.append(" " if pixel_val < 128 else ".")
                char_rows.append(row_chars)
        else:
            for y in range(height):
                row_chars = []
                for x in range(width):
                    pixel_val = gray_small[y, x]
                    idx = min(int(pixel_val / 256 * len(ASCII_GRADIENT)), len(ASCII_GRADIENT) - 1)
                    
                    if use_wish:
                        wish_ptr = (wish_ptr + 1) % len(WISH)
                        ASCII_GRADIENT = WISH[wish_ptr]

                    row_chars.append(ASCII_GRADIENT[idx])
                char_rows.append(row_chars)

        color_codes = None
        if use_color:
            color_small_bgr = cv2.resize(frame, (width, height))
            color_small_rgb = cv2.cvtColor(color_small_bgr, cv2.COLOR_BGR2RGB)
            color_codes = rgb_array_to_ansi256(color_small_rgb)

        ascii_frames.append((char_rows, color_codes))

        if max_frames is not None and len(ascii_frames) >= max_frames:
            break

    cap.release()

    if cache_path is not None:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache_path, 'wb') as f:
            pickle.dump({'ascii_frames': ascii_frames, 'fps': fps, 'delay': delay},
                        f, protocol=pickle.HIGHEST_PROTOCOL)
        print(f"Saved {len(ascii_frames)} frames to cache -- future runs with the same "
              f"video and flags will load instantly instead of reprocessing.")

if is_image:
    print("Conversion finished. Rendering image...")
else:
    print(f"Conversion finished. Playing {len(ascii_frames)} frames...")
time.sleep(1)

# 4. Playback
bgr_chk = threading.Thread(target=check_esc, daemon=True)
bgr_chk.start()

if is_image:
    # For a single image there's nothing to animate, and clearing the terminal would
    # just wipe the art away immediately -- print it once, straight to normal
    # scrollback, and leave it on screen.
    char_rows, color_codes = ascii_frames[0]
    frame_string = build_frame_string(char_rows, color_codes)
    print_with_delay(frame_string)
else:

    # Launch audio as close as possible to the moment playback actually starts --
    # not during frame conversion above, which can take a while and would leave
    # audio running far ahead by the time rendering catches up. ffplay is used
    # instead of a GUI player because it's headless (-nodisp), exits itself when
    # done (-autoexit), and -ss gives it real seek support matching -start.
    audio_process = None
    audio_start = start_time if start_time is not None else 0
    try:
        audio_process = subprocess.Popen(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
             "-ss", str(audio_start), video_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        print("Note: ffplay not found -- skipping audio. Install ffmpeg (winget install ffmpeg) to enable it.")

    try:
        # Enter the alternate screen buffer and hide the cursor. The alt buffer is a
        # fixed-size canvas with no scrollback, so "\033[H" always lands at the true
        # top-left of the visible viewport -- no more scrolling out from under us.
        sys.stdout.write("\033[?1049h\033[?25l")
        sys.stdout.flush()

        # Anchor every frame to real elapsed wall-clock time instead of chaining
        # sleeps -- this is what actually fixes the drift, since sleep(delay) per
        # frame ignores the time spent building/printing each frame and the error
        # compounds over the whole clip.
        playback_start = time.perf_counter()
        for i, (char_rows, color_codes) in enumerate(ascii_frames):
            frame_string = build_frame_string(char_rows, color_codes)

            # Move to home and overwrite in place -- no full clear needed each frame,
            # which also cuts down on flicker.
            sys.stdout.write("\033[H" + frame_string)
            sys.stdout.flush()

            if playback_speed <= 0:
                continue  # ESC was pressed: blast through remaining frames instantly

            target_time = i * delay
            elapsed = time.perf_counter() - playback_start
            sleep_time = target_time - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
            # If we're already behind target_time, skip the sleep entirely rather
            # than adding it on top -- this is what keeps video from permanently
            # lagging further behind the audio as the clip goes on.

    finally:
        # Always restore the normal screen buffer and cursor, even on Ctrl+C or error,
        # so the user's shell isn't left in a broken state.
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
        if audio_process is not None:
            audio_process.terminate()

    print("Playback finished.")