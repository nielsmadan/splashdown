"""Overlay captions + arrows on docs/demo.gif.

Run via `just demo` (which renders the GIF with VHS first). Standalone:
    uv run --with pillow --with numpy python docs/annotate_demo.py

VHS can't annotate, so this post-processes the rendered GIF. It auto-detects the
long `Sleep` "hold" beats in docs/demo.tape (runs of near-identical frames) and
phases each one: a bit of bare output, then a caption pill, then an amber
highlight + arrow pointing at the PORT= line that beat is about. The BEATS
coordinates below are tied to the current tape layout (FontSize 20, Width 1100,
the exact command sequence); re-measure if you change the tape.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFont

GIF = "docs/user/assets/demo.gif"

NAVY = (2, 48, 71)
BABY = (142, 202, 230)
AMBER = (255, 183, 3)

MONO = "/System/Library/Fonts/Menlo.ttc"
SANS = "/System/Library/Fonts/Supplemental/Arial.ttf"

STILL_DIFF = 3000  # downsampled per-frame diff below which two frames count as "still"
MIN_HOLD_MS = 2000  # a still run at least this long is treated as a caption beat
BARE_MS = 500  # show bare command output this long before the caption appears
CAP_SPLIT = 0.4  # fraction of the post-bare span shown before the port highlight lands

# Per-beat overlay, in the order the beats occur. Each beat's hold is split into
# two phases: the caption pill appears first, then the port highlight lands.
# `port_box` is the pixel box around the PORT=NNNN token to highlight; `label`
# sits to its right after an arrow; `caption` is the narration pill, whose top
# is `cap_y` (placed just below that beat's on-screen content, not the window
# bottom, so there's no dead space).
BEATS = [
    {
        "port_box": (43, 268, 173, 296),
        "cap_y": 356,
        "label": "free port, reserved for this checkout",
        "caption": "splash init  —  scans your stack, wires mise + the git hook, reserves a port",
    },
    {
        "port_box": (43, 416, 173, 444),
        "cap_y": 500,
        "label": "a different port, picked automatically",
        "caption": "git worktree add  —  the post-checkout hook provisions the new checkout",
    },
]


def load_frames() -> tuple[list[Image.Image], list[int]]:
    im = Image.open(GIF)
    frames, durs = [], []
    for i in range(im.n_frames):
        im.seek(i)
        frames.append(im.convert("RGB").copy())
        durs.append(im.info.get("duration", 40))
    return frames, durs


def detect_beats(frames: list[Image.Image], durs: list[int]) -> list[tuple[int, int]]:
    def small(f: Image.Image) -> np.ndarray:
        return np.asarray(f.resize((275, 190))).astype(int)

    prev = small(frames[0])
    still = [True]
    for f in frames[1:]:
        cur = small(f)
        still.append(np.abs(cur - prev).sum() < STILL_DIFF)
        prev = cur

    runs, i, n = [], 0, len(frames)
    while i < n:
        if still[i]:
            j = i
            while j < n and still[j]:
                j += 1
            if sum(durs[i:j]) >= MIN_HOLD_MS:
                runs.append((i, j - 1))
            i = j
        else:
            i += 1
    return runs


def draw_overlay(size: tuple[int, int], beat: dict, *, show_port: bool) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    mono = ImageFont.truetype(MONO, 19)
    sans = ImageFont.truetype(SANS, 25)

    if show_port:
        x0, y0, x1, y1 = beat["port_box"]
        cy = (y0 + y1) // 2
        d.rounded_rectangle((x0, y0, x1, y1), radius=6, outline=AMBER, width=2)
        # arrow: horizontal line from the label back to the box, arrowhead at the box
        ax = x1 + 10
        tip = x1 + 6
        d.line((ax, cy, ax + 46, cy), fill=AMBER, width=3)
        d.polygon([(tip, cy), (ax + 8, cy - 6), (ax + 8, cy + 6)], fill=AMBER)
        d.text((ax + 60, cy), beat["label"], font=mono, fill=AMBER, anchor="lm")

    # caption pill, centered horizontally, top edge at the beat's cap_y
    cap = beat["caption"]
    W = size[0]
    tb = d.textbbox((0, 0), cap, font=sans)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    padx, pady = 26, 16
    pill_w, pill_h = tw + 2 * padx, th + 2 * pady
    px = (W - pill_w) // 2
    py = beat["cap_y"]
    d.rounded_rectangle(
        (px, py, px + pill_w, py + pill_h),
        radius=pill_h // 2,
        fill=(*NAVY, 235),
        outline=(*BABY, 255),
        width=2,
    )
    d.text((W // 2, py + pill_h // 2), cap, font=sans, fill=BABY, anchor="mm")
    return layer


def main() -> None:
    frames, durs = load_frames()
    beats = detect_beats(frames, durs)
    if len(beats) != len(BEATS):
        raise SystemExit(f"expected {len(BEATS)} hold beats, detected {len(beats)}: {beats}")

    for (start, end), beat in zip(beats, BEATS, strict=True):
        # phase the hold: ~500ms of bare output, then the caption, then (40% of
        # the remaining span later) the port highlight lands on top.
        cap_start = start
        acc = 0
        while cap_start < end and acc < BARE_MS:
            acc += durs[cap_start]
            cap_start += 1
        split = cap_start + max(1, round((end - cap_start) * CAP_SPLIT))
        cap_only = draw_overlay(frames[0].size, beat, show_port=False)
        full = draw_overlay(frames[0].size, beat, show_port=True)
        for i in range(start, end + 1):
            if i < cap_start:
                continue
            overlay = cap_only if i < split else full
            comp = Image.alpha_composite(frames[i].convert("RGBA"), overlay)
            frames[i] = comp.convert("RGB")

    # Recompositing dropped the GIF's shared palette; rebuild one (from an
    # annotated frame, so it carries the amber/baby overlay colors too) and map
    # every frame onto it — keeps the file small and avoids per-frame flicker.
    palette = frames[beats[-1][1]].quantize(colors=255, method=Image.Quantize.FASTOCTREE)
    pframes = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    for p in pframes:
        p.info.pop("transparency", None)  # a leftover tuple here breaks the GIF encoder

    pframes[0].save(
        GIF,
        save_all=True,
        append_images=pframes[1:],
        duration=durs,
        loop=0,
        optimize=True,
    )
    print(f"annotated {GIF}: {len(beats)} beats")


if __name__ == "__main__":
    main()
