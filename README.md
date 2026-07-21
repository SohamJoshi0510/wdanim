# wdanim

Play videos and images as colored ASCII/ANSI art directly in your terminal, with synced audio, true per-pixel color matching, and a handful of stylized rendering modes.

## Features

- **Two render modes** — a brightness-gradient mode (`.:-=+*#%@`) or a Canny edge-detection mode that draws outlines instead of filled shading.
- **True color matching** — samples the actual RGB of every source pixel and matches it to the *closest* of the real 256 xterm ANSI colors (not an approximation formula — an actual nearest-neighbor search), so the ASCII art's colors reflect the real footage.
- **Mosaic mode** — swaps character-based shading for solid `▄` blocks, so color alone carries all the detail (closer to a low-res pixel-art look).
- **Negative mode** — inverts brightness and color for a photographic-negative effect.
- **Synced background audio** — plays the source video's audio via `ffplay` in a separate process, using a wall-clock-anchored frame scheduler so playback doesn't drift out of sync over longer clips.
- **Frame caching** — `-save` caches the fully rendered ASCII+color frames to disk, fingerprinted against the source file and every render flag, so re-running the same command later loads instantly instead of reprocessing the video.
- **Trim to a range** — process only a portion of a video with `-start` / `-end` timestamps and/or a hard `-frames` cap.
- **Custom-message rendering** — `-wish` spells out your own text across the image using its characters in place of the normal shading gradient (requires `-color` to take effect).
- **Live playback control** — press `Esc` at any time to instantly skip to the end of a video instead of waiting it out.
- **Images render differently from video** — a still image is typed out once, straight into your normal scrollback (not the alternate screen buffer), so it stays on screen after the script exits instead of vanishing when playback "ends."

## Requirements

- **Windows** (uses `msvcrt` for the Esc listener and the Win32 console API to enable ANSI escape processing — this will not run as-is on macOS/Linux)
- Python 3.9+
- [`ffmpeg`](https://ffmpeg.org/) on your `PATH` — optional, only needed for audio playback (`ffplay` specifically). Without it the script still runs, just silently.

### Python dependencies

```
opencv-python
numpy
```

## Installation

```bash
git clone https://github.com/<your-username>/wdanim.git
cd wdanim
pip install -r requirements.txt
```

(or just `pip install opencv-python numpy` if you don't want a requirements file)

## Usage

```bash
python wdanim.py <path_to_video_or_image> [flags]
```

### Flags

| Flag | Argument | Description |
|---|---|---|
| `-color` | — | Match each cell's foreground color to the closest real pixel color, using the actual xterm 256-color palette. |
| `-edge` | — | Use Canny edge detection instead of brightness shading — draws outlines rather than filled tone. |
| `-negative` | — | Invert brightness and color before rendering, for a photo-negative look. |
| `-moz` | — | Mosaic mode — render every cell as a solid `▄` block instead of shaded ASCII characters (best paired with `-color`). |
| `-wish` | `"TEXT"` | Spell out a custom message across the art, cycling through its characters in place of the normal gradient. Requires `-color` — ignored with a note otherwise. |
| `-frames` | `N` | Stop after processing at most `N` frames. |
| `-start` | `SS` or `MM:SS` or `HH:MM:SS` | Seek to this point in the video before processing/playback (also seeks the audio track). |
| `-end` | `SS` or `MM:SS` or `HH:MM:SS` | Stop processing once the video passes this timestamp. |
| `-save` | — | Cache the fully rendered frames to a `.ascii_cache/` folder next to the video. A matching run later (same file, same flags) loads instantly instead of reprocessing. |

### Examples

Play a video with true-color rendering:
```bash
python wdanim.py clip.mp4 -color
```

Edge-detected, negative, only the first 45 seconds:
```bash
python wdanim.py clip.mp4 -edge -negative -end 45
```

Trim to a range, cap at 200 frames, and cache the result for instant replay next time:
```bash
python wdanim.py clip.mp4 -color -start 0:30 -end 1:15 -frames 200 -save
```

Mosaic block-art with full color:
```bash
python wdanim.py clip.mp4 -moz -color
```

Render a still image with a custom message typed across it:
```bash
python wdanim.py photo.jpg -wish "HAPPYBIRTHDAY" -color
```

## Notes

- **Caching**: cache files are keyed by the source file's path, size, and modified time, plus every render flag (`-color`, `-edge`, `-negative`, `-frames`, `-start`, `-end`). Changing any of these produces a new cache entry rather than reusing a stale one. Delete the `.ascii_cache` folder next to a video to clear its caches.
- **Audio sync**: audio is launched via `ffplay -nodisp -autoexit` right before playback begins, and each frame is scheduled against real elapsed wall-clock time (not a naive per-frame `sleep`), so timing doesn't drift as the clip runs longer.
- **Images vs. video**: images skip the alternate-screen-buffer / clear-and-overwrite logic used for video, since there's nothing to animate — the art is typed out once into normal scrollback and left there.
- Terminal color fidelity depends on your terminal emulator; Windows Terminal is recommended for full 256-color and VT100 escape support.
