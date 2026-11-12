# Cursor Jiggler

A lightweight Windows utility that smoothly glides your mouse cursor **3 to 10 cm** back and forth every **10 seconds** (both configurable), using the Win32 `SendInput` API so Windows recognizes real input activity.

## Quick Start

- **Desktop App (GUI):** Double-click `Start_Cursor_Jiggler.bat`, or run:
  ```powershell
  python cursor_jiggler.py
  ```
- **Command / Terminal Mode (CLI):**
  ```powershell
  .\jiggle.bat
  ```
  Or with custom settings (supports `10s`, `9m`, etc.):
  ```powershell
  python cursor_jiggler.py --cli --interval 10s --min-cm 3 --max-cm 10
  python cursor_jiggler.py --cli --interval 9m --min-cm 3 --max-cm 10
  ```
- **Single Immediate Jiggle Command:**
  ```powershell
  python cursor_jiggler.py --once --min-cm 3 --max-cm 10
  ```

## Interactive Commands (Inside `--cli` Mode)

While running in terminal mode, you can type commands directly:
- `jiggle` — jiggle the cursor right now
- `interval 15s` or `interval 9m` — change the interval on the fly
- `distance 3 10` — change the min/max distance in cm
- `pause` / `start` — pause or resume
- `status` — show current state and countdown
- `quit` — stop and exit

## Options

| Flag | Default | Description |
|---|---|---|
| `--interval` | `10` | Interval between jiggles (supports `10s`, `9m`, or a number) |
| `--unit` | `seconds` | `seconds` or `minutes` (when `--interval` has no suffix) |
| `--min-cm` | `3` | Minimum jiggle distance in centimeters |
| `--max-cm` | `10` | Maximum jiggle distance in centimeters |
| `--idle-only` | `false` | Only jiggle if system idle time >= interval |
| `--cli` | `false` | Run in terminal with live interactive commands |
| `--once` | `false` | Jiggle once immediately and exit |

## Run Tests

```powershell
python -m unittest test_cursor_jiggler.py -v
```

## Docker

> Note: Moving the Windows mouse cursor (`SendInput`) requires running directly on Windows. The Docker image packages the CLI/test suite in a portable Python 3.12 container.

```bash
docker build -t therahulpatil/cursor-jiggler:latest .
docker run --rm therahulpatil/cursor-jiggler:latest --help
docker run --rm --entrypoint python therahulpatil/cursor-jiggler:latest -m unittest test_cursor_jiggler.py -v
```
