# Ambient DMX Bridge

A real-time screen-to-light colour bridge. Captures a video output screen (e.g. ProPresenter), samples colour zones across the frame, and drives DMX fixtures via Lightkey OSC — making your PAR cans look like an extension of the screen.

Built for live worship environments but works with any video source and any Lightkey-controlled DMX rig.

![Ambient DMX Bridge UI](docs/screenshot.png)

---

## How It Works

```
ProPresenter (or any video output)
  └── Screen capture (monitor 4, or whichever screen)
        ↓
  Colour sampling (16×10 grid per fixture zone, saturation-weighted)
        ↓
  RGBW / RGBA / RGBWW / RGBAW / RGB conversion
        ↓
  OSC → Lightkey (Mac only)
        ↓
  DMX → Fixtures
```

Each fixture is assigned a **zone** — a rectangular region of the video frame. The bridge samples that zone 15 times per second, extracts the most vivid colour, converts it to the fixture's channel format, and sends it to Lightkey via OSC.

---

## Requirements

- **Windows** (tested on Windows 11) — runs on the same machine as ProPresenter
- **Python 3.10+** — download from [python.org](https://python.org) (tick "Add to PATH")
- **Lightkey** — running on Mac on the same network
- **OSC enabled** in Lightkey (Settings > External Control > OSC)

---

## Installation

### 1. Download

Click **Code > Download ZIP** on this page, or clone:

```bash
git clone https://github.com/yourusername/ambient-dmx-bridge.git
cd ambient-dmx-bridge
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Copy the example config

```bash
cp config.example.json config.json
```

### 4. Run

```bash
python app.py
```

A browser window opens automatically at `http://localhost:5000`.

---

## Setup in the Browser UI

### Step 1 — Lightkey Connection (sidebar)

| Setting | Description |
|---------|-------------|
| IP Address | IP of the machine running Lightkey |
| OSC Port | Found in Lightkey > Settings > External Control > OSC. Default varies — check yours |
| Monitor # | Which screen ProPresenter outputs to (use Detect Monitors to find it) |

### Step 2 — Add Fixtures

Go to the **Fixtures** tab:

1. Click **+ Add Fixture**
2. Enter the fixture **name** — must match the **short name** in Lightkey exactly (case sensitive)
3. Select the **fixture type** matching your DMX mode
4. Click **Add**

Repeat for each fixture.

### Step 3 — Set Zones

Go to the **Zone Editor** tab:

1. Select a fixture from the dropdown
2. Drag on the video frame canvas to draw the zone that fixture should sample
3. Position zones to match where fixtures are physically — top-left fixture samples top-left of frame, etc.
4. Zones are inset slightly from the edge by default to avoid letterbox borders

### Step 4 — Test

Go to the **Test** tab and send colours to all fixtures or individual ones to confirm OSC is working and fixture names match.

### Step 5 — Save and Start

Click **Save Changes**, then **Start**.

---

## Fixture Types

| Type | Channels | Description | OSC properties sent |
|------|----------|-------------|---------------------|
| RGB | 3ch | Red, Green, Blue | `color` |
| RGBW | 4ch | RGB + White (neutral) | `color` + `warmWhite` |
| RGBA | 4ch | RGB + Amber | `color` + `amber` |
| RGBWW | 5ch | RGB + Warm White + Cool White | `color` + `warmWhite` + `coolWhite` |
| RGBAW | 5ch | RGB + Amber + White | `color` + `amber` + `warmWhite` |

**RGBWW colour temperature split:** warm video tones (reds, oranges) drive the warm white channel; cool tones (blues, cyans) drive the cool white channel. This happens automatically.

---

## Configuration Reference

All settings are saved in `config.json`. You can edit this directly or use the browser UI.

```json
{
  "lightkey_host": "192.168.1.x",
  "lightkey_port": 21600,
  "monitor": 2,
  "sample_rate_fps": 15,
  "smoothing": 0.12,
  "min_brightness": 30,
  "colour_boost": 2.2,
  "master_brightness": 255,
  "white_mode": "min",
  "fixtures": [
    {
      "name": "P1",
      "type": "RGBW",
      "zone": { "x1": 0.03, "y1": 0.08, "x2": 0.18, "y2": 0.30 }
    }
  ]
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `lightkey_host` | `192.168.0.14` | IP address of Lightkey machine |
| `lightkey_port` | `21600` | OSC port in Lightkey settings |
| `monitor` | `2` | Monitor index (1 = primary, 2+ = secondary screens) |
| `sample_rate_fps` | `15` | Samples per second. 10–15 is plenty |
| `smoothing` | `0.12` | Colour transition speed. Lower = slower/smoother (0.05–0.3) |
| `min_brightness` | `30` | Zones below this brightness borrow from the brightest zone instead of going dark |
| `colour_boost` | `2.2` | Multiplier applied to extracted colours. Higher = more vivid |
| `master_brightness` | `255` | Scales all output. Use Lightkey master fader instead if preferred |
| `white_mode` | `"min"` | White extraction method: `"min"` = neutral white, `"none"` = no white channel |
| `min_output` | `40` | Floor — no light drops below this value (0 to disable) |
| `stage_floor_pct` | `50` | % of lights kept lit on dark scenes (raises floor automatically when much of the stage is dark) |


### Zone coordinates

Zones use fractions of the frame (0.0 = top/left, 1.0 = bottom/right):

```json
"zone": { "x1": 0.0, "y1": 0.0, "x2": 0.5, "y2": 0.5 }
```

This samples the top-left quarter of the frame. Use the Zone Editor to draw zones visually rather than editing by hand.

---

## Lightkey OSC Setup

1. Open Lightkey on Mac
2. Go to **Settings (⌘,)** > **External Control** tab
3. Enable **Receive OSC messages**
4. Note the port number — enter this in the app's sidebar

The bridge sends these OSC addresses per fixture:

```
/fixture/{name}/overrides/dimmer    1.0
/fixture/{name}/overrides/color     r g b      (floats 0.0–1.0)
/fixture/{name}/overrides/warmWhite w          (float 0.0–1.0)
/fixture/{name}/overrides/amber     a          (float 0.0–1.0)
/fixture/{name}/overrides/coolWhite c          (float 0.0–1.0)
```

The `{name}` must exactly match the **short name** of the fixture in Lightkey's patch view.

---

## Runtime Controls

| Control | Action |
|---------|--------|
| **Start** | Begins capture and OSC output |
| **Stop** | Stops capture and clears all Lightkey overrides (returns control to LD) |
| **Pause** | Stops updating but leaves last colours on fixtures |
| **Clear Overrides** | Manually wipes all Lightkey fixture overrides at any time |

When **stopped**, all fixture overrides are cleared in Lightkey — cues, presets and manual control work as normal.

---

## Troubleshooting

### Lights not responding to OSC test
- Confirm OSC is enabled in Lightkey Settings > External Control
- Check the port number matches exactly
- Check Windows Firewall isn't blocking outbound UDP on that port
- Confirm fixture short names in Lightkey match the names in the app exactly (case sensitive)

### BitBlt / screen capture errors on Windows
- This happens when the target monitor has negative screen coordinates (positioned left of the primary monitor)
- The app uses PIL ImageGrab with `all_screens=True` to handle this — make sure Pillow is installed: `pip install pillow`
- Use **Detect Monitors** in the sidebar to confirm the correct monitor index

### Colours look washed out
- Increase **Colour Boost** (try 2.5–3.0)
- Lower **Min Brightness Threshold** to 20
- Check the zone is actually over a colourful part of the video frame in the Zone Editor

### Lights go dark during dark video sections
- Lower **Min Brightness Threshold** — zones below this value borrow colour from the brightest zone instead of going dark
- Setting it to 0 disables the threshold entirely (lights will always show something)

### Wrong monitor being captured
- Click **Detect Monitors** in the sidebar — it shows all screens with their resolution
- Set the monitor number to match your ProPresenter output screen

---

## Adding Fixture Types

To add a new fixture type, edit `app.py`:

**1. Add to `FIXTURE_TYPES` dict:**
```python
FIXTURE_TYPES = {
    ...
    "RGBUV": {
        "label": "RGBUV (4ch)",
        "channels": 4,
        "description": "Red, Green, Blue, UV"
    },
}
```

**2. Add conversion in `rgb_to_channels()`:**
```python
elif fixture_type == "RGBUV":
    # UV from blue/violet content
    uv = max(0.0, bf * 0.8 - rf * 0.3 - gf * 0.2)
    return {
        "color": [rf, gf, max(0.0, bf - uv)],
        "uV": [uv]
    }
```

The OSC property name (e.g. `uV`) must match what Lightkey uses for that channel in the fixture profile.

---

## Light Bars

Light bars are split into individually addressable segments, each becoming a
separate Lightkey fixture. Go to the **Light Bars** tab:

1. Click **+ Add Bar**
2. Set a name prefix (e.g. `B1-`), segment count, type per segment, and position
3. The app generates segment zones automatically across the chosen edge
4. In Lightkey, patch each segment as its own fixture using the same naming
   (e.g. `B1-1`, `B1-2` ... `B1-N`)

**Note:** Lightkey OSC cannot address individual beams within one fixture, so
each segment must be a separate Lightkey fixture.

Positions: **Top**, **Bottom**, **Left**, **Right** auto-divide the matching
edge of the frame. **Custom** uses a manually defined zone.

---

## Webhooks (Stream Deck / Remote Control)

The bridge exposes simple GET endpoints so a Stream Deck or any networked device
can pause/resume without touching the machine. See the **Webhooks** panel in the
Test tab for the URLs with your machine's IP filled in.

| URL | Action |
|-----|--------|
| `http://<ip>:5000/hook/toggle` | Flip pause/resume (single button) |
| `http://<ip>:5000/hook/pause` | Pause — clears overrides, LD takes control |
| `http://<ip>:5000/hook/resume` | Resume ambient control |
| `http://<ip>:5000/hook/start` | Start the bridge |
| `http://<ip>:5000/hook/stop` | Stop and clear overrides |
| `http://<ip>:5000/hook/status` | Return current state as JSON |

**Stream Deck setup:** add a **Website** action, paste the URL, and enable
"Access in background" so it fires without opening a browser.

---

## Colour Handling

The bridge is tuned to keep colours vivid and avoid washing out to white:

- **Ratio-preserving boost** — brightens colours while keeping their hue, so
  bright yellows/oranges stay coloured instead of clipping to white
- **Saturation enhancement** — pushes colours toward their true hue on stage
- **Smart white channel** — the white channel only fires on genuinely neutral
  frames; any colour cast pulls the white back so the colour shows through
- **Brightness floor + stage protection** — lights never black out on dark or
  pale backgrounds

---

## Project Structure

```
ambient-dmx-bridge/
├── app.py                  # Flask server + bridge engine
├── config.json             # Your saved configuration (gitignored)
├── config.example.json     # Example configuration to copy (fixtures + bars)
├── requirements.txt        # Python dependencies
├── README.md               # This file
└── templates/
    └── index.html          # Browser UI (single page)
```

---

## Contributing

PRs welcome. Key areas that would be useful:

- NDI input source (alternative to screen capture)
- Additional fixture type presets
- MIDI start/stop control
- Multi-venue config profiles
- artnet/sACN direct output (bypassing Lightkey)

---

## License

MIT — free to use, modify, and distribute. Credit appreciated but not required.
