#!/usr/bin/env python3
"""wdanim - play videos and images as colored ASCII/ANSI art in your terminal.

Usage: python wdanim.py <video_or_image> [-color] [-edge] [-negative] [-moz]
                        [-wish TEXT] [-frames N] [-start T] [-end T] [-save]
                        [-width N] [-typedelay SECONDS]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import itertools
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time

import cv2
import numpy as np

CACHE_VERSION = 2
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tiff", ".tif"}

GRADIENT = np.array(list(" .:-=+*#%@"))   # dark -> light == sparse -> dense
EDGE_CHARS = np.array(list("#*=+:."))
RESET = "\x1b[0m"
HALF_BLOCK = "\u2580"                      # upper half block: fg = top pixel, bg = bottom pixel
TERMINAL_CELL_ASPECT = 0.5                 # terminal cells are roughly twice as tall as wide


class UserError(Exception):
    """A problem the user can fix (bad path, bad flag combination, ...)."""


# --------------------------------------------------------------------------- CLI

def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parse_timestamp(value: str) -> float:
    """Accepts seconds ('90', '12.5') or 'MM:SS' / 'HH:MM:SS' and returns seconds."""
    try:
        seconds = 0.0
        for part in value.split(":"):
            seconds = seconds * 60 + float(part)
    except ValueError:
        seconds = -1.0
    if seconds < 0:
        raise argparse.ArgumentTypeError("must be seconds (e.g. 30) or MM:SS / HH:MM:SS")
    return seconds


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="wdanim", allow_abbrev=False,
        description="Play videos and images as colored ASCII/ANSI art in the terminal.",
    )
    p.add_argument("path", help="video or image file")
    p.add_argument("-color", action="store_true",
                   help="match each cell to the nearest xterm-256 color of the source pixel")
    p.add_argument("-edge", action="store_true", help="Canny edge outlines instead of brightness shading")
    p.add_argument("-negative", action="store_true", help="invert brightness and color")
    p.add_argument("-moz", action="store_true",
                   help="mosaic mode: solid half-block pixels (2 per cell vertically); overrides -edge/-wish")
    p.add_argument("-wish", metavar="TEXT", help="spell TEXT across the art (needs -color)")
    p.add_argument("-frames", type=positive_int, metavar="N", help="process at most N frames")
    p.add_argument("-start", type=parse_timestamp, metavar="T", help="start point: SS, MM:SS or HH:MM:SS")
    p.add_argument("-end", type=parse_timestamp, metavar="T", help="end point: SS, MM:SS or HH:MM:SS")
    p.add_argument("-save", action="store_true", help="cache rendered frames next to the video")
    p.add_argument("-width", type=positive_int, metavar="N",
                   help="width in characters (default: fit the terminal)")
    p.add_argument("-typedelay", type=float, default=0.005, metavar="SECONDS",
                   help="per-character delay when typing out an image (default 0.005, 0 = instant)")
    p.add_argument("-hold", action="store_true",
                    help="keep the last frame on screen after a video ends (or is skipped with Esc/q)")
    args = p.parse_args(argv)
    if args.start is not None and args.end is not None and args.end <= args.start:
        p.error("-end must be greater than -start")
    return args


def normalise_flags(opts: argparse.Namespace) -> list[str]:
    """Resolve flag combinations that don't make sense; returns notes to show the user."""
    notes = []
    if opts.moz:
        if opts.edge or opts.wish:
            notes.append("-moz overrides -edge and -wish.")
        opts.edge, opts.wish = False, None
    elif opts.edge and opts.wish:
        notes.append("-wish has no effect together with -edge; ignoring it.")
        opts.wish = None
    elif opts.wish and not opts.color:
        notes.append("-wish looks best with -color -- ignoring it without -color.")
        opts.wish = None
    return notes


# ------------------------------------------------------------------- terminal I/O

def enable_windows_ansi() -> None:
    """Turn on VT100 processing so ANSI escape codes work in Windows consoles."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


class KeyListener:
    """Non-blocking 'was Esc or q pressed?' check on Windows, Linux and macOS."""

    def __init__(self) -> None:
        self.enabled = sys.stdin is not None and sys.stdin.isatty()
        self._windows = os.name == "nt"
        self._fd = None
        self._saved = None

    def __enter__(self) -> "KeyListener":
        if self.enabled and not self._windows:
            import termios
            import tty
            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc) -> None:
        if self._saved is not None:
            import termios
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)

    def abort_requested(self) -> bool:
        if not self.enabled:
            return False
        pressed = False
        if self._windows:
            import msvcrt
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):      # arrow/function key prefix: swallow the 2nd code
                    msvcrt.getwch()
                elif ch in ("\x1b", "q", "Q"):
                    pressed = True
        else:
            import select
            while select.select([self._fd], [], [], 0)[0]:
                data = os.read(self._fd, 64)
                if not data:
                    break
                # a lone ESC byte is the Esc key; arrow keys arrive as ESC [ A etc.
                if data == b"\x1b" or b"q" in data or b"Q" in data:
                    pressed = True
        return pressed


# ----------------------------------------------------------------- colour matching

def build_ansi256_palette() -> np.ndarray:
    """RGB values of xterm colors 16..255 (6x6x6 cube + 24 grays).

    Colors 0..15 are left out on purpose: terminals let users re-theme them, so
    their real RGB is unpredictable, while 16..255 are fixed.
    """
    steps = [0, 95, 135, 175, 215, 255]
    cube = [(r, g, b) for r in steps for g in steps for b in steps]
    grays = [(8 + 10 * i,) * 3 for i in range(24)]
    return np.array(cube + grays, dtype=np.int32)  # (240, 3), index i -> color code i + 16


class ColorMatcher:
    """Nearest-palette-color lookup via a precomputed 64x64x64 table (6 bits per channel)."""

    BITS = 6

    def __init__(self) -> None:
        palette = build_ansi256_palette()
        n = 1 << self.BITS
        self.shift = 8 - self.BITS
        centers = (np.arange(n) << self.shift) + (1 << (self.shift - 1))
        g, b = np.meshgrid(centers, centers, indexing="ij")
        gb = np.stack([g.ravel(), b.ravel()], axis=1)                       # (n*n, 2)
        dist_gb = ((gb[:, None, :] - palette[None, :, 1:]) ** 2).sum(axis=2)  # (n*n, 240)
        self.lut = np.empty((n, n, n), dtype=np.uint8)
        for ri, r in enumerate(centers):
            dist = dist_gb + (r - palette[:, 0]) ** 2
            self.lut[ri] = (np.argmin(dist, axis=1) + 16).reshape(n, n)

    def codes(self, rgb: np.ndarray) -> np.ndarray:
        q = rgb >> self.shift
        return self.lut[q[..., 0], q[..., 1], q[..., 2]]


# ---------------------------------------------------------------------- rendering

def rows_to_text(chars: np.ndarray) -> str:
    return "\n".join("".join(row) for row in chars.tolist())


def styled_rows(chars: np.ndarray, codes: np.ndarray) -> str:
    """Characters + per-cell xterm-256 foreground codes. Blank cells inherit the current
    style (they have no visible foreground), so runs stay long and frames stay small."""
    lines = []
    for ch_row, code_row in zip(chars.tolist(), codes.tolist()):
        parts: list[str] = []
        last = None
        for ch, code in zip(ch_row, code_row):
            if ch == " ":
                code = last
            if code != last:
                parts.append(f"\x1b[38;5;{code}m")
                last = code
            parts.append(ch)
        if last is not None:
            parts.append(RESET)
        lines.append("".join(parts))
    return "\n".join(lines)


class Renderer:
    """Turns BGR frames into ready-to-print ANSI strings."""

    def __init__(self, opts: argparse.Namespace, width: int, rows: int) -> None:
        self.opts, self.width, self.rows = opts, width, rows
        self.matcher = ColorMatcher() if (opts.color or opts.moz) else None
        self.wish = np.array(list(opts.wish)) if opts.wish else None
        self.wish_offset = 0  # the message keeps scrolling across frames

    def render(self, frame: np.ndarray) -> str:
        if self.opts.negative:
            frame = cv2.bitwise_not(frame)
        if self.opts.moz:
            return self._half_blocks(frame)

        small = cv2.resize(frame, (self.width, self.rows), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        if self.opts.edge:
            chars = self._edge_chars(gray)
        elif self.wish is not None:
            chars = self._wish_chars()
        else:
            chars = GRADIENT[gray.astype(np.int32) * len(GRADIENT) // 256]

        if self.matcher is None:
            return rows_to_text(chars)
        codes = self.matcher.codes(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
        return styled_rows(chars, codes)

    @staticmethod
    def _edge_chars(gray: np.ndarray) -> np.ndarray:
        edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 50, 150)
        edge_chars = EDGE_CHARS[gray.astype(np.int32) * len(EDGE_CHARS) // 256]
        fill = np.where(gray < 128, " ", ".")
        return np.where(edges > 0, edge_chars, fill)

    def _wish_chars(self) -> np.ndarray:
        count = self.width * self.rows
        idx = (self.wish_offset + np.arange(count)) % len(self.wish)
        self.wish_offset = (self.wish_offset + count) % len(self.wish)
        return self.wish[idx].reshape(self.rows, self.width)

    def _half_blocks(self, frame: np.ndarray) -> str:
        small = cv2.resize(frame, (self.width, self.rows * 2), interpolation=cv2.INTER_AREA)
        if not self.opts.color:  # mosaic without -color: grayscale blocks
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            small = cv2.merge([gray, gray, gray])
        codes = self.matcher.codes(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
        lines = []
        for top_row, bottom_row in zip(codes[0::2].tolist(), codes[1::2].tolist()):
            parts = []
            for (top, bottom), run in itertools.groupby(zip(top_row, bottom_row)):
                parts.append(f"\x1b[38;5;{top};48;5;{bottom}m{HALF_BLOCK * len(list(run))}")
            parts.append(RESET)
            lines.append("".join(parts))
        return "\n".join(lines)


def pick_size(src_w: int, src_h: int, opts: argparse.Namespace, term, fit_height: bool):
    """Returns (width, rows) in terminal cells, fitted to the terminal unless -width is given."""
    cols, lines = term
    aspect = src_h / src_w * TERMINAL_CELL_ASPECT   # cell rows per cell column
    if opts.width:
        width = opts.width
    else:
        width = max(20, cols - 1)                    # leave the last column free to avoid wrapping
        if fit_height and width * aspect > lines - 1:
            width = max(20, int((lines - 1) / aspect))
    return width, max(1, round(width * aspect))


# ------------------------------------------------------------------------- cache

def cache_path_for(opts: argparse.Namespace, width: int) -> str:
    stat = os.stat(opts.path)
    fingerprint = repr((
        CACHE_VERSION, os.path.abspath(opts.path), stat.st_mtime_ns, stat.st_size,
        opts.color, opts.edge, opts.negative, opts.moz, opts.wish,
        opts.frames, opts.start, opts.end, width,
    ))
    digest = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
    folder = os.path.join(os.path.dirname(os.path.abspath(opts.path)), ".ascii_cache")
    return os.path.join(folder, f"{os.path.basename(opts.path)}.{digest}.cache")


def load_cache(path: str):
    """Returns (frames, fps) or None if the cache is missing, stale or damaged."""
    try:
        with gzip.open(path, "rt", encoding="utf-8", newline="\n") as f:
            header = json.loads(f.readline())
            frames = f.read().split("\x00")
        if header.get("version") != CACHE_VERSION or len(frames) != header.get("frames"):
            return None
        return frames, float(header["fps"])
    except (OSError, ValueError, KeyError, EOFError):
        return None


def save_cache(path: str, frames: list[str], fps: float) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8", newline="\n", compresslevel=3) as f:
            f.write(json.dumps({"version": CACHE_VERSION, "fps": fps, "frames": len(frames)}) + "\n")
            f.write("\x00".join(frames))
        os.replace(tmp, path)
        print(f"Saved {len(frames)} frames to cache; the same video and flags will load instantly next time.")
    except OSError as exc:
        print(f"Note: could not write cache ({exc}).")


# -------------------------------------------------------------------------- video

def load_video_frames(opts: argparse.Namespace, term) -> tuple[list[str], float]:
    cap = cv2.VideoCapture(opts.path)
    if not cap.isOpened():
        raise UserError(f"could not open video: {opts.path}")
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        if not fps or not math.isfinite(fps) or fps <= 0:
            fps = 25.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if src_w <= 0 or src_h <= 0:
            raise UserError("could not read the video dimensions")

        duration = total / fps if total > 0 else None
        if opts.start is not None and duration is not None and opts.start >= duration:
            raise UserError(f"-start ({opts.start}s) is beyond the video length ({duration:.1f}s)")

        width, rows = pick_size(src_w, src_h, opts, term, fit_height=True)

        cache_path = cache_path_for(opts, width) if opts.save else None
        if cache_path and os.path.exists(cache_path):
            print("Matching cache found -- loading pre-rendered frames...")
            cached = load_cache(cache_path)
            if cached:
                print(f"Loaded {len(cached[0])} cached frames.")
                return cached
            print("Cache was unreadable; re-rendering.")

        limit = opts.frames
        if opts.end is not None:
            end_frames = math.ceil((opts.end - (opts.start or 0.0)) * fps)
            limit = end_frames if limit is None else min(limit, end_frames)
        if opts.start:
            cap.set(cv2.CAP_PROP_POS_MSEC, opts.start * 1000)

        expected = None
        if total > 0:
            remaining = total - int((opts.start or 0.0) * fps)
            expected = max(1, min(limit, remaining) if limit else remaining)

        mode = "mosaic" if opts.moz else "edge detection" if opts.edge else "brightness gradient"
        print(f"Processing video ({mode}, {width}x{rows} cells"
              f"{', color' if opts.color else ''}{', negative' if opts.negative else ''})...")

        renderer = Renderer(opts, width, rows)
        frames: list[str] = []
        while limit is None or len(frames) < limit:
            ok, frame = cap.read()
            if not ok:
                break
            frames.append(renderer.render(frame))
            if len(frames) % 10 == 0:
                total_txt = f"/{expected}" if expected else ""
                print(f"\r  frame {len(frames)}{total_txt}", end="", flush=True)
        print()
    finally:
        cap.release()

    if not frames:
        raise UserError("no frames could be read from the video (check -start/-end)")
    if cache_path:
        save_cache(cache_path, frames, fps)
    return frames, fps


def play_video(frames: list[str], fps: float, opts: argparse.Namespace) -> None:
    delay = 1.0 / fps
    count = len(frames)

    ffplay = shutil.which("ffplay")
    if ffplay is None:
        print("Note: ffplay not found -- playing without audio. Install ffmpeg to enable it.")

    audio = None
    out = sys.stdout
    try:
        # Alternate screen buffer: fixed canvas, no scrollback, so "\x1b[H" is always top-left.
        out.write("\x1b[?1049h\x1b[?25l")
        out.flush()
        with KeyListener() as keys:
            if ffplay:
                cmd = [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet"]
                if opts.start:
                    cmd += ["-ss", str(opts.start)]
                cmd += ["-t", f"{count * delay:.3f}", opts.path]
                audio = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            # The frame shown is derived from real elapsed time. A slow terminal drops
            # frames instead of falling behind the audio.
            started = time.perf_counter()
            last = -1
            while True:
                if keys.abort_requested():
                    break
                elapsed = time.perf_counter() - started
                index = int(elapsed / delay)
                if index >= count:
                    break
                if index != last:
                    out.write("\x1b[H" + frames[index])
                    out.flush()
                    last = index
                wait = (index + 1) * delay - (time.perf_counter() - started)
                if wait > 0:
                    time.sleep(min(wait, 0.02))  # short naps keep key polling responsive
    finally:
        out.write("\x1b[?25h\x1b[?1049l")
        if opts.hold:
            out.write(frames[-1] + "\n")   # print into normal scrollback so it survives after exit
        out.flush()
        if audio is not None:
            audio.terminate()
    print("Playback finished.")


# -------------------------------------------------------------------------- image

TOKEN = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|.", re.DOTALL)


def type_out(text: str, char_delay: float) -> None:
    """Type the art out character by character into normal scrollback (Esc/q skips ahead)."""
    out = sys.stdout
    if char_delay <= 0:
        out.write(text + "\n")
        out.flush()
        return
    skipping = False
    shown = 0
    started = time.perf_counter()
    with KeyListener() as keys:
        for match in TOKEN.finditer(text):
            token = match.group()
            out.write(token)
            if skipping or token.startswith("\x1b"):
                continue
            shown += 1
            wait = started + shown * char_delay - time.perf_counter()
            if wait > 0:  # only sleep when ahead of schedule; catches up in batches otherwise
                out.flush()
                time.sleep(wait)
                if keys.abort_requested():
                    skipping = True
    out.write("\n")
    out.flush()


def show_image(opts: argparse.Namespace, term) -> None:
    data = np.fromfile(opts.path, dtype=np.uint8)  # imdecode handles non-ASCII paths on Windows
    image = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
    if image is None:
        raise UserError(f"could not read image: {opts.path}")
    height, width_px = image.shape[:2]
    width, rows = pick_size(width_px, height, opts, term, fit_height=False)
    print("Rendering image...")
    type_out(Renderer(opts, width, rows).render(image), opts.typedelay)

def launch_gui():
    """Launcher window shown when wdanim is run with no arguments.
    Returns an argv list for parse_args, or None if the window was closed."""
    try:
        import customtkinter as ctk
        from tkinter import filedialog
    except ImportError:
        print("No arguments given. For the launcher window run:  pip install customtkinter")
        print("Or see the options with:  wdanim --help")
        return None
    import contextlib
    import io

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    root.title("wdanim")
    root.geometry("520x680")

    result = {"argv": None}
    path_var = ctk.StringVar()
    flags = {name: ctk.BooleanVar() for name in ("color", "edge", "negative", "moz", "save", "hold")}
    fields = {name: ctk.StringVar() for name in ("wish", "frames", "start", "end", "width", "typedelay")}

    ctk.CTkLabel(root, text="wdanim", font=ctk.CTkFont(size=22, weight="bold")).pack(pady=(16, 8))

    # file picker
    row = ctk.CTkFrame(root, fg_color="transparent")
    row.pack(fill="x", padx=20)
    ctk.CTkEntry(row, textvariable=path_var, placeholder_text="Video or image file").pack(
        side="left", fill="x", expand=True)

    def browse():
        chosen = filedialog.askopenfilename(
            title="Choose a video or image",
            filetypes=[("Video / image", "*.mp4 *.mkv *.avi *.mov *.webm *.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff"),
                       ("All files", "*.*")])
        if chosen:
            path_var.set(chosen)

    ctk.CTkButton(row, text="Browse", width=80, command=browse).pack(side="left", padx=(8, 0))

    # on/off flags
    checks = ctk.CTkFrame(root)
    checks.pack(fill="x", padx=20, pady=12)
    labels = {"color": "Color", "edge": "Edge detect", "negative": "Negative",
              "moz": "Mosaic", "save": "Cache frames", "hold": "Hold last frame"}
    for i, (name, text) in enumerate(labels.items()):
        ctk.CTkCheckBox(checks, text=text, variable=flags[name]).grid(
            row=i // 2, column=i % 2, sticky="w", padx=14, pady=6)

    # flags that take a value
    form = ctk.CTkFrame(root)
    form.pack(fill="x", padx=20)
    form.grid_columnconfigure(1, weight=1)
    entries = [("wish", "Wish text", "e.g. HAPPYBIRTHDAY"),
               ("frames", "Max frames", "all"),
               ("start", "Start", "0:30"),
               ("end", "End", "1:15"),
               ("width", "Width (chars)", "fit terminal"),
               ("typedelay", "Type delay (s)", "0.005 (images)")]
    for i, (name, label, hint) in enumerate(entries):
        ctk.CTkLabel(form, text=label).grid(row=i, column=0, sticky="w", padx=14, pady=6)
        ctk.CTkEntry(form, textvariable=fields[name], placeholder_text=hint).grid(
            row=i, column=1, sticky="ew", padx=14, pady=6)

    status = ctk.CTkLabel(root, text="", text_color="#ff6b6b", wraplength=470)
    status.pack(pady=(10, 0))

    def build_argv():
        if fields["wish"].get().strip():
            flags["color"].set(True)          # -wish needs -color
        argv = [path_var.get().strip().strip('"')]
        argv += [f"-{name}" for name, var in flags.items() if var.get()]
        for name, var in fields.items():
            value = var.get().strip()
            if value:
                argv += [f"-{name}", value]
        return argv

    def run():
        argv = build_argv()
        if not argv[0] or not os.path.isfile(argv[0]):
            status.configure(text="Choose an existing video or image file.")
            return
        errors = io.StringIO()
        try:
            with contextlib.redirect_stderr(errors):
                parse_args(argv)              # reuse the real validation
        except SystemExit:
            lines = errors.getvalue().strip().splitlines()
            status.configure(text=lines[-1] if lines else "Invalid options.")
            return
        result["argv"] = argv
        root.destroy()

    ctk.CTkButton(root, text="Run", height=40, command=run).pack(pady=16, padx=20, fill="x")
    root.bind("<Return>", lambda event: run())
    root.mainloop()
    return result["argv"]

# --------------------------------------------------------------------------- main

def main(argv=None) -> int:
    if argv is None and len(sys.argv) == 1:      # no arguments -> show the launcher
        argv = launch_gui()
        if argv is None:
            return 0
    opts = parse_args(argv)
    try:
        if not os.path.isfile(opts.path):
            raise UserError(f"file not found: {opts.path}")
        for note in normalise_flags(opts):
            print("Note:", note)

        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        enable_windows_ansi()
        term = shutil.get_terminal_size(fallback=(120, 40))

        if os.path.splitext(opts.path)[1].lower() in IMAGE_EXTENSIONS:
            show_image(opts, term)
        else:
            frames, fps = load_video_frames(opts, term)
            print(f"Conversion finished. Playing {len(frames)} frames...")
            time.sleep(0.5)
            play_video(frames, fps, opts)
    except UserError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
