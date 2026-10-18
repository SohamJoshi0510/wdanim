# wdanim

Play videos and images as colored ASCII/ANSI art directly in your terminal, with synced audio, true per-pixel color matching, and a handful of stylized rendering modes.

## Features

- **Two render modes**: a brightness-gradient mode (` .:-=+*#%@`) or a Canny edge-detection mode that draws outlines instead of filled shading.
- **True color matching**: samples the RGB of every source pixel and matches it to the nearest of the fixed xterm 256-color palette (colors 16-255, which don't change with terminal themes) using a precomputed lookup table.
- **Mosaic mode**: solid `▀` half-blocks with separate foreground and background colors, giving two pixels per character cell vertically for a pixel-art look.
- **Negative mode**: inverts brightness and color.
- **Synced audio**: plays the source video's audio through `ffplay`. Frames are chosen from real elapsed time, so a slow terminal drops frames instead of drifting out of sync.
- **Frame caching**: `-save` stores the fully rendered frames (gzip-compressed) so re-running the same command loads instantly.
- **Trim to a range**: process only part of a video with `-start` / `-end` and/or a `-frames` cap.
- **Custom-message rendering**: `-wish` spells out your own text across the image in place of the normal shading characters (requires `-color`).
- **Fits your terminal**: output width is chosen from your terminal size (and height, for video) unless you set `-width`.
- **Live control**: press `Esc` or `q` to skip to the end of a video, or to finish typing out an image instantly.
- **Images render differently from video**: a still image is typed out once into your normal scrollback (not the alternate screen), so it stays on screen after the script exits.

## Requirements

- Python 3.9+
- Windows, macOS or Linux, in a terminal with 256-color support (Windows Terminal is recommended on Windows)
- [`ffmpeg`](https://ffmpeg.org/) on your `PATH`: optional, only needed for audio (`ffplay` specifically). Without it the script plays silently.

Python dependencies (see `requirements.txt`):

```
opencv-python
numpy
```

## Installation

```
git clone https://github.com/SohamJoshi0510/wdanim.git
cd wdanim
pip install -r requirements.txt
```

## Usage

```
python wdanim.py <path_to_video_or_image> [flags]
```

Run `python wdanim.py --help` for a summary.

### Flags

| Flag | Argument | Description |
| --- | --- | --- |
| `-color` | none | Match each cell's foreground color to the nearest xterm 256-color of the source pixel. |
| `-edge` | none | Use Canny edge detection instead of brightness shading. |
| `-negative` | none | Invert brightness and color before rendering. |
| `-moz` | none | Mosaic mode: solid half-block pixels. Overrides `-edge` and `-wish`. Without `-color` it renders in grayscale. |
| `-wish` | `"TEXT"` | Spell out a custom message across the art. Requires `-color`; ignored with a note otherwise or when combined with `-edge`. |
| `-frames` | `N` | Process at most `N` frames. |
| `-start` | `SS`, `MM:SS` or `HH:MM:SS` | Start at this point in the video (the audio is seeked too). |
| `-end` | `SS`, `MM:SS` or `HH:MM:SS` | Stop at this point. Must be greater than `-start`. |
| `-save` | none | Cache rendered frames in a `.ascii_cache/` folder next to the video. |
| `-width` | `N` | Width in characters. Default: fit the terminal. |
| `-typedelay` | `SECONDS` | Per-character delay when typing out an image. Default `0.005`; `0` prints instantly. |
| `-hold` | none | Keep the last frame on screen after a video ends (or is skipped with `Esc`/`q`). |

### Examples

Play a video with true-color rendering:

```
python wdanim.py clip.mp4 -color
```

Edge-detected, negative, only the first 45 seconds:

```
python wdanim.py clip.mp4 -edge -negative -end 45
```

Trim to a range, cap at 200 frames, and cache the result for instant replay:

```
python wdanim.py clip.mp4 -color -start 0:30 -end 1:15 -frames 200 -save
```

Mosaic block-art with full color:

```
python wdanim.py clip.mp4 -moz -color
```

Render a still image with a custom message typed across it:

```
python wdanim.py photo.jpg -wish "HAPPYBIRTHDAY" -color
```

## Notes

- **Caching**: cache files are keyed by the source file's path, size and modified time, the render flags (`-color`, `-edge`, `-negative`, `-moz`, `-wish`, `-frames`, `-start`, `-end`) and the output width. Changing any of these creates a new cache entry. Delete the `.ascii_cache` folder next to a video to clear it.
- **Audio sync**: audio is launched with `ffplay -nodisp -autoexit` right as playback begins, limited to the length of the rendered range. If the terminal can't keep up, frames are skipped to stay in sync with the audio.
- **Images vs. video**: images skip the alternate-screen logic used for video, since there is nothing to animate. The art is typed into normal scrollback and left there.
- **Terminal size**: resize your terminal before running. Video is sized to fit both the width and height of the window so frames never wrap.
- Terminal color fidelity depends on your terminal emulator.
