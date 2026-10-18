# WallpaperSync (Linux / live wallpaper fork)

Sets your RGB lighting through [OpenRGB](https://openrgb.org/) to match your desktop wallpaper, including live wallpapers.

Based on [Zephyr73/WallpaperSync](https://github.com/Zephyr73/WallpaperSync). This fork runs on Linux (Wayland) instead of Windows.

## Wallpaper sources

It checks every `poll_seconds` and uses the first source it finds:

1. **linux-wallpaperengine**: reads the running process's `--bg` (workshop id or path).
   - *video* wallpapers: ffmpeg samples `sample_frames` frames spread across the video.
   - *scene* / *web* wallpapers: the item's preview image is used.
2. **awww / swww**: the image from `awww query`. An animated GIF is sampled across its frames.
3. `~/.cache/current_wallpaper`, as written by `set-wallpaper.sh`.

## Config (`config.json`)

| key | default | meaning |
|---|---|---|
| `ip`, `port` | `127.0.0.1`, `6742` | OpenRGB SDK server |
| `mode` | `static` | `static`: one colour per wallpaper. `animate`: slowly cycles through the colours of the sampled frames |
| `sample_frames` | `12` | frames sampled from a video or GIF |
| `fade_seconds` | `1.0` | length of the fade to a new colour |
| `cycle_seconds` | `6.0` | animate mode: how long each colour is held |
| `poll_seconds` | `2.0` | how often to check for a wallpaper change |
| `brightness` | `1.0` | 0 to 1, scales the LED colour |

## Running

```sh
./run.sh                 # foreground; creates .venv on first run
./run.sh --once          # set the colour once and exit
./run.sh --mode animate
```

To start it at login with systemd user services:

```sh
cp systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now openrgb-server.service wallpapersync.service
journalctl --user -u wallpapersync -f
```

`wallpapersync.service` expects the repo at `~/src/WallpaperSync`. Change `ExecStart` if you keep it somewhere else.
Requires `ffmpeg` (for video wallpapers) and `openrgb`.
