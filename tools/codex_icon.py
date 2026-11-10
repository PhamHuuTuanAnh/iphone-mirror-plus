"""Ask Codex (built-in image_gen, the user's ChatGPT sign-in) for app icon concepts.

    python tools/codex_icon.py [direction ...]      # default: all directions, in parallel

Each attempt is copied to tools/icon_attempts/<direction>-NN.png. Pick one, then
    python tools/codex_icon.py --use tools/icon_attempts/<file>.png
which turns it into iphone_mirror/coredevice/app.ico (+ app.png).
"""
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ATTEMPTS = ROOT / "tools" / "icon_attempts"
CODEX_IMAGES = Path.home() / ".codex" / "generated_images"
MODEL = os.environ.get("CODEX_MODEL", "gpt-6.1-sol")
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def codex_exe():
    if os.environ.get("CODEX_EXE"):
        return os.environ["CODEX_EXE"]
    cfg = (Path.home() / ".codex" / "config.toml").read_text(encoding="utf-8")
    for line in cfg.splitlines():
        if line.strip().startswith("CODEX_CLI_PATH"):
            return line.split("=", 1)[1].strip().strip('"').replace("\\\\", "\\")
    raise FileNotFoundError("CODEX_CLI_PATH not found in ~/.codex/config.toml")


COMMON = (
    "Use case: production desktop application icon for Windows 11 (also shown in the taskbar, Start menu and system tray). "
    "The app is 'iPhone Mirror Plus': it shows a connected iPhone's screen in a window on a Windows PC and lets the user "
    "control the phone with the mouse and keyboard, wirelessly. "
    "Output: ONE square 1024x1024 PNG with a TRANSPARENT background outside the icon shape. The icon is a single rounded-square "
    "app tile (corner radius about 22% of the side) filling about 92% of the canvas, centered, no drop shadow outside the tile. "
    "Quality bar: polished, premium, like a first-party Microsoft Fluent or Apple system app icon; clean geometry, crisp edges, "
    "a clear focal shape and strong silhouette that stays recognizable when scaled down to 32x32 and 16x16 pixels (no thin "
    "lines, no tiny details, no more than 3 main shapes). "
    "Strict rules: NO text, NO letters, NO Apple logo or any brand logo, NO real product photo, do not copy any existing app icon."
)

DIRECTIONS = {
    "fluent_glass": (
        "Style: Windows 11 Fluent 3D. On a deep blue-to-violet gradient tile, a stylized modern smartphone (rounded slab, "
        "dynamic-island pill, glowing gradient screen) stands slightly in front of and overlapping a sleek desktop monitor "
        "silhouette; the phone screen's light reflects onto the monitor to suggest mirroring. Soft frosted-glass materials, "
        "gentle top-left light, subtle depth."
    ),
    "minimal_cursor": (
        "Style: bold, flat, minimal (one gradient, no 3D). A white smartphone silhouette with a vivid gradient screen sits on a "
        "saturated cobalt-to-indigo tile; a large, chunky white mouse pointer with a dark outline touches the phone's screen from "
        "the lower right, meaning 'control the phone with the mouse'. Generous spacing, perfectly balanced."
    ),
    "wireless_3d": (
        "Style: soft 3D clay / glossy render. A small rounded smartphone on the left and a rounded laptop or monitor on the right, "
        "joined by three bold concentric wireless arcs between them, on a teal-to-blue gradient tile. Friendly, modern, "
        "high-contrast so the two devices and the arcs read at small sizes."
    ),
    "symbol_sync": (
        "Style: geometric symbol, flat with a subtle gradient. A tall rounded phone frame overlaps a wide rounded window frame "
        "(like a desktop app window with a title bar), and a bold circular two-arrow sync/mirror mark sits where they overlap. "
        "Two-tone palette: electric blue and vivid purple on a near-white tile, or white shapes on a blue-purple tile, whichever "
        "gives stronger contrast."
    ),
}


def generate(direction):
    prompt = COMMON + "\n\n" + DIRECTIONS[direction]
    instruction = (
        "Call your built-in image generation tool exactly once, asking for a transparent background (PNG with alpha). "
        "Pass this prompt to the tool verbatim:\n\n" + prompt + "\n\n"
        "Do not run shell commands, do not write or edit files. When the image exists, reply DONE."
    )
    args = [codex_exe(), "exec", "--skip-git-repo-check", "-s", "read-only", "--json", "-m", MODEL,
            "-c", 'model_reasoning_effort="low"', "-"]
    with tempfile.TemporaryDirectory() as work:
        run = subprocess.run(args, input=instruction, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", cwd=work, timeout=1500)
    thread = None
    for line in run.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started":
            thread = event["thread_id"]
    images = sorted((CODEX_IMAGES / thread).glob("*.png"), key=lambda p: p.stat().st_mtime) if thread else []
    if not images:
        return direction, None, (run.stdout[-600:] + run.stderr[-300:])
    ATTEMPTS.mkdir(parents=True, exist_ok=True)
    n = len(list(ATTEMPTS.glob(f"{direction}-*.png"))) + 1
    out = ATTEMPTS / f"{direction}-{n:02d}.png"
    out.write_bytes(images[-1].read_bytes())
    return direction, out, ""


def use(path):
    """Chosen PNG -> app.ico with all sizes (+ app.png at 256)."""
    from PIL import Image

    img = Image.open(path).convert("RGBA")
    box = img.getbbox()  # trim fully transparent margins, then re-square with a small margin
    if box:
        img = img.crop(box)
    side = round(max(img.size) / 0.94)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    out_dir = ROOT / "iphone_mirror" / "coredevice"
    canvas.resize((256, 256), Image.LANCZOS).save(out_dir / "app.png")
    frames = [canvas.resize((s, s), Image.LANCZOS) for s in SIZES]
    frames[-1].save(out_dir / "app.ico", format="ICO", sizes=[(s, s) for s in SIZES], append_images=frames[:-1])
    print("wrote", out_dir / "app.ico")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--use":
        use(sys.argv[2])
        return
    wanted = sys.argv[1:] or list(DIRECTIONS)
    with ThreadPoolExecutor(len(wanted)) as pool:
        for direction, out, err in pool.map(generate, wanted):
            print(f"{direction}: {out or 'FAILED ' + err}", flush=True)


if __name__ == "__main__":
    main()
