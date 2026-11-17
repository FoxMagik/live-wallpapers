# live-wallpapers

Animated HTML scenes that work three ways:

- **Live desktop background** on Omarchy/Hyprland, via a small WebKitGTK host (`livewall`).
- **OBS browser source** for streaming. Point it at a scene URL and size it 1920×1080.
- **Any browser.** Open the file, or the GitHub Pages URL.

Each scene is one self-contained HTML file. Scenes make no network requests and load no external assets, and their random timing runs on a seeded clock that the host can pause.

| Scene | Theme |
|---|---|
| `aegis` | Futuristic holographic command HUD with live system telemetry, bar readouts and a 3D geodesic core |
| `helldivers` | A Super Destroyer calls down stratagem beams and hellpods. Battles flash on the horizon. |
| `carbon` | Sleek carbon fiber with electric-blue light traces |
| `eldergrove` | An enchanted forest at dusk, with gold wisps and light shafts |
| `synthwave` | 80s neon sunset with a scrolling grid |
| `neon-rain` | A blue cyberpunk city in the rain |

## Desktop (Omarchy)

```bash
./install.sh                 # links `livewall`, adds a systemd user service, a theme-set hook and the themes/
livewall list
livewall set helldivers      # or any path to an .html file
livewall off
```

- **Themes:** `themes/` holds a matching Omarchy theme for each scene (colors, icons and stills). `install.sh` copies any you don't already have. Then run `omarchy theme set <name>`.
- **Theme matching:** when you switch Omarchy themes, the hook starts the scene with the same name as the theme. If no scene matches, it turns the live background off.
- **Pausing:** the host pauses scenes when windows cover the desktop or an app is fullscreen. It follows the `[pause]` settings in `~/.config/owe/config.toml`. Set `LIVEWALL_ALWAYS_ANIMATE=1` to keep animating regardless.
- **Frame rate:** scenes render at 30fps by default. Set `LIVEWALL_FPS` to change it.

## OBS / browser

Add a **Browser Source** with the scene URL (GitHub Pages or a local file) at 1920×1080. URL parameters:

| Param | Meaning |
|---|---|
| `fps=60` | Frame cap (the default is 60 in a browser) |
| `seed=42` | Same random sequence every time |
| `still=1&t=8` | Simulate 8 seconds, draw one frame and stop |
| `telemetry=http://127.0.0.1:19845/stats` | Custom telemetry endpoint for `aegis` (polls `127.0.0.1:19845/stats` by default; `telemetry=0` forces simulation) |

### Live PC telemetry in a browser / OBS (`aegis`)

On an Omarchy desktop, `livewall` automatically pushes live `/proc` and `/sys` hardware metrics into `aegis` AND serves them on `http://127.0.0.1:19845/stats`. On any other machine (or when running without the `livewall` desktop host), start the standalone agent so OBS Browser Sources or the GitHub Pages URL display real hardware stats:

```bash
python3 tools/telemetry-agent.py
```

## Making a scene

Copy `scenes/_template.html`. Keep the runtime block unchanged and write `init/update/draw` below it. The contract is described in the comments of that block.

Render a preview or background still with:

```bash
tools/render-still.sh scenes/<name>.html out.jpg <sim-seconds> <seed>
```
