import os
import re
import io
import json
import time
import math
import colorsys
import argparse
import subprocess
from glob import glob
from typing import Tuple, List, Optional
from PIL import Image, ImageSequence
from colorthief import ColorThief
from openrgb import OpenRGBClient
from openrgb.utils import RGBColor

RGB = Tuple[int, int, int]

DEFAULT_CONFIG = {
    'ip': '127.0.0.1',
    'port': 6742,
    # 'static': one colour per wallpaper. 'animate': for live wallpapers, slowly
    # cycle through the colours of frames sampled across the video
    'mode': 'static',
    'sample_frames': 12,     # frames pulled from a video / animated image
    'fade_seconds': 1.0,     # length of the fade between two colours
    'cycle_seconds': 6.0,    # animate mode: how long each frame colour is held
    'poll_seconds': 2.0,     # how often to check whether the wallpaper changed
    'brightness': 1.0,       # 0..1 scale applied to the final LED colour
}

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
WORKSHOP_GLOBS = [
    '~/.local/share/Steam/steamapps/workshop/content/431960',
    '~/.steam/steam/steamapps/workshop/content/431960',
    '~/.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/workshop/content/431960',
]
IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif')
VIDEO_EXTS = ('.mp4', '.webm', '.mkv', '.mov', '.avi')


# --- colour maths (unchanged from the Windows version) ---

def euclidean_distance(color1: RGB, color2: RGB) -> float:
    return math.sqrt(sum((c1 - c2) ** 2 for c1, c2 in zip(color1, color2)))


def adjust_saturation_vibrance(color: RGB) -> RGB:
    r, g, b = [x / 255.0 for x in color]
    h, _, _ = colorsys.rgb_to_hsv(r, g, b)
    r, g, b = colorsys.hsv_to_rgb(h, 1.0, 1.0)
    return int(r * 255), int(g * 255), int(b * 255)


def get_brightness(rgb: RGB) -> float:
    return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255.0


def get_saturation(rgb: RGB) -> float:
    return colorsys.rgb_to_hsv(*[x / 255.0 for x in rgb])[1]


def is_near_white(rgb: RGB, threshold: float = 240.0) -> bool:
    return get_brightness(rgb) > threshold / 255.0


def check_palette(palette: List[RGB], tolerance: float = 10.0, threshold: float = 50.0) -> str:
    def is_greyscale() -> bool:
        return all(abs(r - g) <= tolerance and abs(r - b) <= tolerance and abs(g - b) <= tolerance
                   for r, g, b in palette)

    def is_low_contrast() -> bool:
        return any(euclidean_distance(color1, color2) < threshold
                   for i, color1 in enumerate(palette) for color2 in palette[i + 1:])

    if is_greyscale():
        return 'greyscale'
    if is_low_contrast():
        return 'low_contrast'
    return 'normal'


def accent_color(palette: List[RGB]) -> RGB:
    sorted_rgb = sorted(palette, key=lambda x: get_saturation(x) * (1 - get_brightness(x)), reverse=True)
    return adjust_saturation_vibrance(sorted_rgb[0])


def pick_color(image: Image.Image) -> Tuple[List[RGB], RGB]:
    """Return the filtered palette of an image and the LED colour chosen from it."""
    buf = io.BytesIO()
    image.convert('RGB').save(buf, 'PNG')
    buf.seek(0)
    palette = ColorThief(buf).get_palette(color_count=6)
    filtered_palette = [color for color in palette if not is_near_white(color)] or palette

    palette_type = check_palette(filtered_palette)
    if palette_type == 'greyscale':
        adjusted = (255, 255, 255)
    elif palette_type == 'low_contrast':
        adjusted = adjust_saturation_vibrance(max(filtered_palette, key=get_saturation))
    else:
        adjusted = accent_color(filtered_palette)
    return filtered_palette, adjusted


# --- frame extraction ---

def video_frames(path: str, count: int) -> List[Image.Image]:
    """Grab `count` evenly spaced, downscaled frames from a video with ffmpeg."""
    try:
        duration = float(subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
            capture_output=True, text=True, check=True).stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        duration = 0.0

    frames = []
    for i in range(count):
        t = duration * (i + 0.5) / count if duration else 0
        out = subprocess.run(
            ['ffmpeg', '-v', 'error', '-ss', f'{t:.2f}', '-i', path, '-frames:v', '1',
             '-vf', 'scale=320:-2', '-f', 'image2pipe', '-c:v', 'png', '-'],
            capture_output=True)
        if out.returncode == 0 and out.stdout:
            frames.append(Image.open(io.BytesIO(out.stdout)).convert('RGB'))
        if not duration:
            break
    return frames


def image_frames(path: str, count: int) -> List[Image.Image]:
    """One frame for a still image, up to `count` spread across an animated one."""
    img = Image.open(path)
    n = getattr(img, 'n_frames', 1)
    if n <= 1:
        img.thumbnail((640, 640))
        return [img.convert('RGB')]
    wanted = {int(n * (i + 0.5) / count) for i in range(min(count, n))}
    frames = []
    for i, frame in enumerate(ImageSequence.Iterator(img)):
        if i in wanted:
            f = frame.convert('RGB')
            f.thumbnail((320, 320))
            frames.append(f)
    return frames


def tile(frames: List[Image.Image]) -> Image.Image:
    """Lay frames side by side so the palette is taken across all of them."""
    h = min(f.height for f in frames)
    frames = [f.resize((max(1, f.width * h // f.height), h)) for f in frames]
    out = Image.new('RGB', (sum(f.width for f in frames), h))
    x = 0
    for f in frames:
        out.paste(f, (x, 0))
        x += f.width
    return out


# --- finding the current wallpaper ---

def workshop_dir(bg: str, assets_dir: Optional[str]) -> Optional[str]:
    """Resolve a linux-wallpaperengine background (id or path) to its folder."""
    bg = os.path.expanduser(bg)
    if os.path.isdir(bg):
        return bg
    if os.path.isfile(bg):
        return os.path.dirname(bg)
    if not bg.isdigit():
        return None
    roots = [os.path.expanduser(g) for g in WORKSHOP_GLOBS]
    if assets_dir:
        # .../steamapps/common/wallpaper_engine/assets -> .../steamapps/workshop/content/431960
        steamapps = os.path.dirname(os.path.dirname(os.path.dirname(assets_dir.rstrip('/'))))
        roots.insert(0, os.path.join(steamapps, 'workshop', 'content', '431960'))
    for root in roots:
        if os.path.isdir(os.path.join(root, bg)):
            return os.path.join(root, bg)
    return None


def wallpaperengine_source() -> Optional[Tuple[str, str]]:
    """(kind, path) of the wallpaper a running linux-wallpaperengine is showing."""
    for cmdline in glob('/proc/[0-9]*/cmdline'):
        try:
            with open(cmdline, 'rb') as f:
                args = [a.decode(errors='replace') for a in f.read().split(b'\0') if a]
        except OSError:
            continue
        if not args or not os.path.basename(args[0]).startswith('linux-wallpaperengine'):
            continue

        bg, assets = None, None
        for i, a in enumerate(args[1:], 1):
            nxt = args[i + 1] if i + 1 < len(args) else None
            if a in ('--bg', '-b') and nxt and not bg:
                bg = nxt
            elif a == '--assets-dir' and nxt:
                assets = nxt
        if not bg and len(args) > 1 and not args[-1].startswith('-'):
            bg = args[-1]  # `linux-wallpaperengine <id>` form
        if not bg:
            continue

        folder = workshop_dir(bg, assets)
        if not folder:
            continue
        try:
            with open(os.path.join(folder, 'project.json')) as f:
                project = json.load(f)
        except (OSError, ValueError):
            project = {}

        main_file = os.path.join(folder, project.get('file', ''))
        if project.get('type', '').lower() == 'video' and os.path.isfile(main_file):
            return 'video', main_file
        # scene / web wallpapers can't be rendered here, so use their preview
        for name in [project.get('preview', '')] + [f'preview{e}' for e in ('.jpg', '.png', '.gif')]:
            if name and os.path.isfile(os.path.join(folder, name)):
                return 'image', os.path.join(folder, name)
    return None


def awww_source() -> Optional[Tuple[str, str]]:
    """The image awww (or swww) is displaying, else the last one set-wallpaper.sh recorded."""
    for cli in ('awww', 'swww'):
        try:
            out = subprocess.run([cli, 'query'], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        m = re.search(r'image: (.+)$', out, re.M)
        if m and os.path.isfile(m.group(1).strip()):
            return 'image', m.group(1).strip()
    try:
        with open(os.path.expanduser('~/.cache/current_wallpaper')) as f:
            path = f.read().strip()
        if os.path.isfile(path):
            return 'image', path
    except OSError:
        pass
    return None


def current_wallpaper() -> Optional[Tuple[str, str]]:
    # a live wallpaper is drawn over awww, so it wins when both are running
    return wallpaperengine_source() or awww_source()


# --- LEDs ---

def print_palette(palette: List[RGB]):
    print('Palette: ' + ''.join(f'\033[48;2;{r};{g};{b}m    \033[0m' for r, g, b in palette))


def print_adjusted_color(color: RGB):
    r, g, b = color
    print(f'LED colour: \033[48;2;{r};{g};{b}m    \033[0m  #{r:02x}{g:02x}{b:02x}')


class Leds:
    """Keeps one OpenRGB connection alive and reconnects when the server goes away."""

    def __init__(self, ip: str, port: int, brightness: float):
        self.ip, self.port, self.brightness = ip, port, brightness
        self.client: Optional[OpenRGBClient] = None
        self.current: RGB = (0, 0, 0)

    def _connect(self) -> bool:
        if self.client:
            return True
        try:
            self.client = OpenRGBClient(self.ip, self.port, 'WallpaperSync')
        except Exception as e:
            print(f'Could not reach the OpenRGB SDK server at {self.ip}:{self.port}: {e}')
            return False
        for device in self.client.devices:
            # Direct takes per-frame colour updates without writing to flash;
            # Static is the fallback for controllers that have no Direct mode
            names = [m.name.lower() for m in device.modes]
            for wanted in ('direct', 'static'):
                if wanted in names:
                    device.set_mode(wanted)
                    break
        print(f'Connected to OpenRGB: {", ".join(d.name for d in self.client.devices)}')
        return True

    def _set(self, color: RGB):
        c = RGBColor(*(int(v * self.brightness) for v in color))
        for device in self.client.devices:
            if device.leds:
                device.set_color(c, fast=True)

    def fade_to(self, target: RGB, duration: float, steps: int = 30):
        if not self._connect():
            return
        start = self.current
        try:
            for step in range(1, steps + 1):
                t = step / steps
                self._set(tuple(int(a + (b - a) * t) for a, b in zip(start, target)))
                time.sleep(duration / steps)
            self.current = target
        except Exception as e:
            print(f'Lost connection to OpenRGB: {e}')
            self.client = None


# --- main loop ---

def load_config(path: str) -> dict:
    config = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path) as f:
            config.update(json.load(f))
    return config


def colors_for(kind: str, path: str, count: int) -> Tuple[RGB, List[RGB]]:
    """The overall LED colour for a wallpaper, plus one per sampled frame."""
    frames = video_frames(path, count) if kind == 'video' else image_frames(path, count)
    if not frames:
        raise RuntimeError(f'could not read any frames from {path}')
    palette, overall = pick_color(tile(frames))
    print_palette(palette)
    print_adjusted_color(overall)
    per_frame = [pick_color(f)[1] for f in frames] if len(frames) > 1 else [overall]
    return overall, per_frame


def main():
    parser = argparse.ArgumentParser(description='Sync OpenRGB lighting with your (live) wallpaper.')
    parser.add_argument('--ip', type=str, help='OpenRGB SDK server address')
    parser.add_argument('--port', type=int, help='OpenRGB SDK server port')
    parser.add_argument('--mode', choices=('static', 'animate'), help='one colour, or cycle through frame colours')
    parser.add_argument('--config', type=str, default=CONFIG_FILE, help='path to config.json')
    parser.add_argument('--once', action='store_true', help='set the colour for the current wallpaper and exit')
    args = parser.parse_args()

    config = load_config(args.config)
    for key in ('ip', 'port', 'mode'):
        if getattr(args, key):
            config[key] = getattr(args, key)

    leds = Leds(config['ip'], int(config['port']), float(config['brightness']))
    fade = float(config['fade_seconds'])

    source, colors, index, next_cycle = None, [], 0, 0.0
    print('Monitoring wallpaper changes...')
    while True:
        now = time.monotonic()
        found = current_wallpaper()
        if found and found != source:
            source = found
            print(f'\nWallpaper: {found[1]} ({found[0]})')
            try:
                overall, per_frame = colors_for(*found, int(config['sample_frames']))
            except Exception as e:
                print(f'Could not process wallpaper: {e}')
                overall, per_frame = None, []
            if overall:
                animate = config['mode'] == 'animate' and len(per_frame) > 1
                colors, index = (per_frame if animate else [overall]), 0
                leds.fade_to(colors[0], fade)
                next_cycle = time.monotonic() + float(config['cycle_seconds'])
        elif len(colors) > 1 and now >= next_cycle:
            index = (index + 1) % len(colors)
            leds.fade_to(colors[index], max(fade, float(config['cycle_seconds']) / 2))
            next_cycle = time.monotonic() + float(config['cycle_seconds'])
        elif colors and leds.client is None:
            leds.fade_to(colors[index], fade)  # OpenRGB came back after a restart

        if args.once and colors:
            break
        time.sleep(float(config['poll_seconds']))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Goodbye')
