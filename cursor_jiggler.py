"""Windows Cursor Jiggler.

Moves the mouse cursor back and forth by a configurable physical distance
(default: 3 to 10 cm) on a configurable schedule (default: every 10 seconds)
using the Win32 SendInput API. Supports both a Tkinter GUI and a CLI mode.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
import os
import random
import sys
import threading
import time

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:
    tk = None
    messagebox = None
    ttk = None

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

HORZSIZE_MM = 4
HORZRES_PX = 8

DEFAULT_INTERVAL_VALUE = 10.0
DEFAULT_INTERVAL_UNIT = "seconds"
DEFAULT_MIN_CM = 3.0
DEFAULT_MAX_CM = 10.0
DEFAULT_PX_PER_CM = 96.0 / 2.54  # ~37.8 px/cm fallback at 96 DPI
DEFAULT_STEPS = 12
DEFAULT_MOVE_DURATION = 0.18
DEFAULT_HOLD_DELAY = 0.08

ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32


class MOUSEINPUT(ctypes.Structure):
    """Win32 MOUSEINPUT structure."""

    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    """Win32 KEYBDINPUT structure (included so INPUT union matches Win32 size)."""

    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    """Win32 HARDWAREINPUT structure (included so INPUT union matches Win32 size)."""

    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    """Win32 INPUT structure for SendInput."""

    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _INPUT_UNION),
    ]


class LASTINPUTINFO(ctypes.Structure):
    """Win32 LASTINPUTINFO structure for GetLastInputInfo."""

    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


_DPI_AWARENESS_INITIALIZED = False


def _enable_dpi_awareness(user32) -> None:
    """Opt into DPI awareness so pixel metrics match physical screen pixels."""
    global _DPI_AWARENESS_INITIALIZED
    if _DPI_AWARENESS_INITIALIZED:
        return
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    _DPI_AWARENESS_INITIALIZED = True


def _load_win32_apis():
    """Bind required Win32 user32/kernel32/gdi32 functions on Windows."""
    if sys.platform != "win32":
        raise OSError("Cursor Jiggler requires Windows (win32).")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT

    user32.GetLastInputInfo.argtypes = (ctypes.POINTER(LASTINPUTINFO),)
    user32.GetLastInputInfo.restype = wintypes.BOOL

    user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
    user32.GetCursorPos.restype = wintypes.BOOL

    user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
    user32.GetSystemMetrics.restype = ctypes.c_int

    user32.GetDC.argtypes = (wintypes.HWND,)
    user32.GetDC.restype = wintypes.HDC

    user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
    user32.ReleaseDC.restype = ctypes.c_int

    user32.SetProcessDPIAware.argtypes = ()
    user32.SetProcessDPIAware.restype = wintypes.BOOL

    kernel32.GetTickCount.argtypes = ()
    kernel32.GetTickCount.restype = wintypes.DWORD

    gdi32.GetDeviceCaps.argtypes = (wintypes.HDC, ctypes.c_int)
    gdi32.GetDeviceCaps.restype = ctypes.c_int

    _enable_dpi_awareness(user32)
    return user32, kernel32, gdi32


def get_pixels_per_cm() -> float:
    """Estimate physical pixels per centimeter for the primary display."""
    if sys.platform != "win32":
        return DEFAULT_PX_PER_CM
    try:
        user32, _, gdi32 = _load_win32_apis()
        hdc = user32.GetDC(None)
        if not hdc:
            return DEFAULT_PX_PER_CM
        try:
            width_px = gdi32.GetDeviceCaps(hdc, HORZRES_PX)
            width_mm = gdi32.GetDeviceCaps(hdc, HORZSIZE_MM)
            if width_px > 0 and width_mm > 0:
                return float(width_px) / (float(width_mm) / 10.0)
        finally:
            user32.ReleaseDC(None, hdc)
    except Exception:
        pass
    return DEFAULT_PX_PER_CM


def cm_to_pixels(cm: float, px_per_cm: float | None = None) -> int:
    """Convert a distance in centimeters to at least 1 pixel."""
    if cm <= 0:
        raise ValueError("Distance in cm must be greater than 0.")
    scale = px_per_cm if px_per_cm and px_per_cm > 0 else get_pixels_per_cm()
    return max(1, int(round(cm * scale)))


def _send_mouse_input(user32, dx: int, dy: int, flags: int) -> None:
    """Send a single mouse event via Win32 SendInput."""
    inp = INPUT(
        type=INPUT_MOUSE,
        union=_INPUT_UNION(
            mi=MOUSEINPUT(
                dx=dx,
                dy=dy,
                mouseData=0,
                dwFlags=flags,
                time=0,
                dwExtraInfo=0,
            )
        ),
    )
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if sent != 1:
        if hasattr(ctypes, "WinError"):
            err = ctypes.get_last_error()
            raise ctypes.WinError(err)
        raise OSError("SendInput failed")


def _to_virtual_absolute(x: int, y: int, vx: int, vy: int, vw: int, vh: int) -> tuple[int, int]:
    """Map pixel coordinates onto Win32 0..65535 virtual desktop space."""
    norm_x = int(round((x - vx) * 65535 / max(1, vw - 1)))
    norm_y = int(round((y - vy) * 65535 / max(1, vh - 1)))
    return max(0, min(65535, norm_x)), max(0, min(65535, norm_y))


def _move_cursor_smooth_absolute(
    user32,
    start_x: int,
    start_y: int,
    target_x: int,
    target_y: int,
    vx: int,
    vy: int,
    vw: int,
    vh: int,
    steps: int,
    duration: float,
) -> None:
    """Smoothly move the cursor from start to target using absolute SendInput."""
    flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    step_delay = duration / max(1, steps)
    for i in range(1, steps + 1):
        t = i / steps
        cur_x = int(round(start_x + (target_x - start_x) * t))
        cur_y = int(round(start_y + (target_y - start_y) * t))
        ax, ay = _to_virtual_absolute(cur_x, cur_y, vx, vy, vw, vh)
        _send_mouse_input(user32, ax, ay, flags)
        if step_delay > 0:
            time.sleep(step_delay)


def _split_total_into_steps(total: int, steps: int) -> list[int]:
    """Split an integer pixel offset into `steps` chunks summing to `total`."""
    base, rem = divmod(abs(total), steps)
    sign = 1 if total >= 0 else -1
    return [sign * (base + (1 if i < rem else 0)) for i in range(steps)]


def jiggle_cursor(
    min_cm: float = DEFAULT_MIN_CM,
    max_cm: float = DEFAULT_MAX_CM,
    steps: int = DEFAULT_STEPS,
    move_duration: float = DEFAULT_MOVE_DURATION,
    hold_delay: float = DEFAULT_HOLD_DELAY,
    px_per_cm: float | None = None,
) -> float:
    """Move the cursor smoothly by 3..10 cm (configurable) and back.

    Uses Win32 SendInput so Windows resets its input idle timer.
    Returns the chosen distance in centimeters.
    """
    if min_cm <= 0 or max_cm <= 0:
        raise ValueError("min_cm and max_cm must be greater than 0.")
    if min_cm > max_cm:
        raise ValueError("min_cm cannot be greater than max_cm.")

    chosen_cm = min_cm if min_cm == max_cm else random.uniform(min_cm, max_cm)
    distance_px = cm_to_pixels(chosen_cm, px_per_cm=px_per_cm)

    user32, _, _ = _load_win32_apis()

    pt = wintypes.POINT()
    if user32.GetCursorPos(ctypes.byref(pt)):
        vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = max(1, user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
        vh = max(1, user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))

        # Prefer moving right unless near the right edge, then move left.
        margin = 10
        if pt.x + distance_px <= vx + vw - margin:
            target_x = pt.x + distance_px
        else:
            target_x = max(vx + margin, pt.x - distance_px)
        target_y = pt.y

        _move_cursor_smooth_absolute(
            user32,
            pt.x,
            pt.y,
            target_x,
            target_y,
            vx,
            vy,
            vw,
            vh,
            steps=steps,
            duration=move_duration,
        )
        if hold_delay > 0:
            time.sleep(hold_delay)
        _move_cursor_smooth_absolute(
            user32,
            target_x,
            target_y,
            pt.x,
            pt.y,
            vx,
            vy,
            vw,
            vh,
            steps=steps,
            duration=move_duration,
        )
    else:
        step_delay = move_duration / max(1, steps)
        out_chunks = _split_total_into_steps(distance_px, steps)
        for dx in out_chunks:
            _send_mouse_input(user32, dx, 0, MOUSEEVENTF_MOVE)
            if step_delay > 0:
                time.sleep(step_delay)
        if hold_delay > 0:
            time.sleep(hold_delay)
        for dx in reversed(out_chunks):
            _send_mouse_input(user32, -dx, 0, MOUSEEVENTF_MOVE)
            if step_delay > 0:
                time.sleep(step_delay)

    return chosen_cm


def get_idle_seconds() -> float:
    """Return seconds elapsed since the last OS keyboard or mouse input."""
    user32, kernel32, _ = _load_win32_apis()
    lii = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO), dwTime=0)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        raise ctypes.WinError(ctypes.get_last_error())
    elapsed_ms = (kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF
    return elapsed_ms / 1000.0


def interval_to_seconds(value: float, unit: str) -> float:
    """Convert an interval value and unit ('seconds' or 'minutes') to seconds."""
    if value <= 0:
        raise ValueError("Interval must be greater than 0.")
    normalized = unit.strip().lower()
    if normalized in ("s", "sec", "secs", "second", "seconds"):
        return float(value)
    if normalized in ("m", "min", "mins", "minute", "minutes"):
        return float(value) * 60.0
    raise ValueError(f"Unsupported interval unit: {unit!r}")


def parse_interval_spec(spec: str, default_unit: str = DEFAULT_INTERVAL_UNIT) -> tuple[float, str, float]:
    """Parse an interval string like '10', '10s', or '9m' into (value, unit, seconds)."""
    raw = spec.strip().lower()
    if not raw:
        raise ValueError("Interval cannot be empty.")
    unit = default_unit
    num_part = raw
    for suffix, mapped_unit in (
        ("seconds", "seconds"),
        ("second", "seconds"),
        ("secs", "seconds"),
        ("sec", "seconds"),
        ("s", "seconds"),
        ("minutes", "minutes"),
        ("minute", "minutes"),
        ("mins", "minutes"),
        ("min", "minutes"),
        ("m", "minutes"),
    ):
        if raw.endswith(suffix) and len(raw) > len(suffix):
            num_part = raw[: -len(suffix)].strip()
            unit = mapped_unit
            break
    try:
        value = float(num_part)
    except ValueError as exc:
        raise ValueError(f"Invalid interval: {spec!r}") from exc
    seconds = interval_to_seconds(value, unit)
    return value, unit, seconds


def format_duration(seconds: float) -> str:
    """Format a non-negative duration in seconds as HH:MM:SS or MM:SS."""
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


@dataclass
class JiggleStatus:
    """Snapshot of scheduler state for UI/CLI reporting."""

    running: bool
    interval_seconds: float
    seconds_remaining: float
    jiggle_count: int
    last_jiggle_at: datetime | None
    last_distance_cm: float | None
    last_error: str | None


class JigglerScheduler:
    """Background timer that triggers cursor jiggles on a fixed interval."""

    def __init__(
        self,
        interval_seconds: float = DEFAULT_INTERVAL_VALUE,
        min_cm: float = DEFAULT_MIN_CM,
        max_cm: float = DEFAULT_MAX_CM,
        only_when_idle: bool = False,
        jiggle_fn=jiggle_cursor,
        idle_fn=get_idle_seconds,
        clock_fn=time.monotonic,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than 0.")
        if min_cm <= 0 or max_cm <= 0 or min_cm > max_cm:
            raise ValueError("Invalid min_cm / max_cm range.")

        self._interval_seconds = float(interval_seconds)
        self._min_cm = float(min_cm)
        self._max_cm = float(max_cm)
        self._only_when_idle = bool(only_when_idle)
        self._jiggle_fn = jiggle_fn
        self._idle_fn = idle_fn
        self._clock_fn = clock_fn

        self._lock = threading.Lock()
        self._wakeup = threading.Event()
        self._stop_requested = False
        self._running = False
        self._next_run_at: float | None = None
        self._jiggle_count = 0
        self._last_jiggle_at: datetime | None = None
        self._last_distance_cm: float | None = None
        self._last_error: str | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start (or resume) scheduled jiggling."""
        with self._lock:
            self._stop_requested = False
            self._running = True
            self._next_run_at = self._clock_fn() + self._interval_seconds
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run_loop, name="CursorJigglerWorker", daemon=True
                )
                self._thread.start()
        self._wakeup.set()

    def pause(self) -> None:
        """Pause scheduled jiggling while keeping the worker available."""
        with self._lock:
            self._running = False
            self._next_run_at = None
        self._wakeup.set()

    def stop(self) -> None:
        """Stop the background worker thread."""
        with self._lock:
            self._running = False
            self._stop_requested = True
            self._next_run_at = None
        self._wakeup.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def configure(
        self,
        interval_seconds: float,
        min_cm: float | None = None,
        max_cm: float | None = None,
        only_when_idle: bool | None = None,
    ) -> None:
        """Update schedule settings and reset the countdown if running."""
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than 0.")
        with self._lock:
            new_min = self._min_cm if min_cm is None else float(min_cm)
            new_max = self._max_cm if max_cm is None else float(max_cm)
            if new_min <= 0 or new_max <= 0 or new_min > new_max:
                raise ValueError("Invalid min_cm / max_cm range.")
            self._interval_seconds = float(interval_seconds)
            self._min_cm = new_min
            self._max_cm = new_max
            if only_when_idle is not None:
                self._only_when_idle = bool(only_when_idle)
            if self._running:
                self._next_run_at = self._clock_fn() + self._interval_seconds
        self._wakeup.set()

    def trigger_now(self, reset_timer: bool = True) -> bool:
        """Perform an immediate jiggle. Returns True on success."""
        with self._lock:
            min_cm = self._min_cm
            max_cm = self._max_cm
        try:
            moved_cm = self._jiggle_fn(min_cm=min_cm, max_cm=max_cm)
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
            return False

        with self._lock:
            self._jiggle_count += 1
            self._last_jiggle_at = datetime.now()
            self._last_distance_cm = moved_cm
            self._last_error = None
            if reset_timer and self._running:
                self._next_run_at = self._clock_fn() + self._interval_seconds
        self._wakeup.set()
        return True

    def status(self) -> JiggleStatus:
        """Return a thread-safe snapshot of current state."""
        with self._lock:
            remaining = 0.0
            if self._running and self._next_run_at is not None:
                remaining = max(0.0, self._next_run_at - self._clock_fn())
            return JiggleStatus(
                running=self._running,
                interval_seconds=self._interval_seconds,
                seconds_remaining=remaining,
                jiggle_count=self._jiggle_count,
                last_jiggle_at=self._last_jiggle_at,
                last_distance_cm=self._last_distance_cm,
                last_error=self._last_error,
            )

    def _run_loop(self) -> None:
        while True:
            self._wakeup.clear()
            with self._lock:
                if self._stop_requested:
                    return
                running = self._running
                next_run_at = self._next_run_at
                interval = self._interval_seconds
                only_when_idle = self._only_when_idle
                min_cm = self._min_cm
                max_cm = self._max_cm

            if not running or next_run_at is None:
                self._wakeup.wait()
                continue

            now = self._clock_fn()
            wait_for = next_run_at - now
            if wait_for > 0:
                self._wakeup.wait(timeout=min(wait_for, 1.0))
                continue

            should_jiggle = True
            if only_when_idle:
                try:
                    idle_secs = self._idle_fn()
                    should_jiggle = idle_secs >= interval
                except Exception as exc:
                    with self._lock:
                        self._last_error = str(exc)
                    should_jiggle = False

            if should_jiggle:
                try:
                    moved_cm = self._jiggle_fn(min_cm=min_cm, max_cm=max_cm)
                    with self._lock:
                        self._jiggle_count += 1
                        self._last_jiggle_at = datetime.now()
                        self._last_distance_cm = moved_cm
                        self._last_error = None
                except Exception as exc:
                    with self._lock:
                        self._last_error = str(exc)

            with self._lock:
                if self._running:
                    self._next_run_at = self._clock_fn() + self._interval_seconds


class CursorJigglerApp:
    """Tkinter desktop UI for the Cursor Jiggler."""

    def __init__(
        self,
        root: tk.Tk,
        scheduler: JigglerScheduler,
        initial_interval: float = DEFAULT_INTERVAL_VALUE,
        initial_unit: str = DEFAULT_INTERVAL_UNIT,
        initial_min_cm: float = DEFAULT_MIN_CM,
        initial_max_cm: float = DEFAULT_MAX_CM,
        initial_idle_only: bool = False,
    ) -> None:
        self.root = root
        self.scheduler = scheduler
        self._px_per_cm = get_pixels_per_cm()

        self.root.title("Cursor Jiggler")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.interval_var = tk.StringVar(value=f"{initial_interval:g}")
        self.unit_var = tk.StringVar(value=initial_unit)
        self.min_cm_var = tk.StringVar(value=f"{initial_min_cm:g}")
        self.max_cm_var = tk.StringVar(value=f"{initial_max_cm:g}")
        self.idle_only_var = tk.BooleanVar(value=initial_idle_only)

        self.state_var = tk.StringVar(value="Running")
        self.countdown_var = tk.StringVar(
            value=format_duration(scheduler.status().interval_seconds)
        )
        self.idle_var = tk.StringVar(value="00:00")
        self.count_var = tk.StringVar(value="0")
        self.last_jiggle_var = tk.StringVar(value="Never")
        self.scale_info_var = tk.StringVar(value="")
        self.error_var = tk.StringVar(value="")

        self._build_ui()
        self._update_scale_info(initial_min_cm, initial_max_cm)
        self.scheduler.start()
        self._refresh_ui()

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=16)
        container.grid(row=0, column=0, sticky="nsew")

        header = ttk.Label(
            container,
            text="Cursor Jiggler",
            font=("Segoe UI", 13, "bold"),
        )
        header.grid(row=0, column=0, columnspan=3, sticky="w")

        subtitle = ttk.Label(
            container,
            text="Glides the cursor 3-10 cm back and forth on schedule.",
            foreground="#555555",
        )
        subtitle.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 12))

        ttk.Label(container, text="Interval:").grid(row=2, column=0, sticky="w", pady=4)
        interval_frame = ttk.Frame(container)
        interval_frame.grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)

        ttk.Entry(interval_frame, textvariable=self.interval_var, width=8).grid(
            row=0, column=0, padx=(0, 6)
        )
        ttk.Combobox(
            interval_frame,
            textvariable=self.unit_var,
            values=("seconds", "minutes"),
            state="readonly",
            width=9,
        ).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(interval_frame, text="Apply", command=self._apply_settings).grid(
            row=0, column=2
        )

        ttk.Label(container, text="Distance (cm):").grid(
            row=3, column=0, sticky="w", pady=4
        )
        dist_frame = ttk.Frame(container)
        dist_frame.grid(row=3, column=1, columnspan=2, sticky="w", pady=4)

        ttk.Label(dist_frame, text="Min").grid(row=0, column=0, padx=(0, 4))
        ttk.Entry(dist_frame, textvariable=self.min_cm_var, width=6).grid(
            row=0, column=1, padx=(0, 10)
        )
        ttk.Label(dist_frame, text="Max").grid(row=0, column=2, padx=(0, 4))
        ttk.Entry(dist_frame, textvariable=self.max_cm_var, width=6).grid(
            row=0, column=3
        )

        ttk.Label(
            container, textvariable=self.scale_info_var, foreground="#666666"
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(0, 4))

        ttk.Checkbutton(
            container,
            text="Only jiggle if already idle for full interval",
            variable=self.idle_only_var,
            command=self._apply_settings,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 10))

        ttk.Separator(container, orient="horizontal").grid(
            row=6, column=0, columnspan=3, sticky="ew", pady=6
        )

        status_grid = ttk.Frame(container)
        status_grid.grid(row=7, column=0, columnspan=3, sticky="ew", pady=4)

        rows = [
            ("Status:", self.state_var),
            ("Next jiggle in:", self.countdown_var),
            ("System idle time:", self.idle_var),
            ("Total jiggles:", self.count_var),
            ("Last jiggle:", self.last_jiggle_var),
        ]
        for idx, (label, var) in enumerate(rows):
            ttk.Label(status_grid, text=label).grid(
                row=idx, column=0, sticky="w", padx=(0, 12), pady=2
            )
            ttk.Label(status_grid, textvariable=var, font=("Segoe UI", 9, "bold")).grid(
                row=idx, column=1, sticky="w", pady=2
            )

        ttk.Label(
            container, textvariable=self.error_var, foreground="#b00020", wraplength=320
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(4, 8))

        btn_frame = ttk.Frame(container)
        btn_frame.grid(row=9, column=0, columnspan=3, sticky="ew")

        self.toggle_btn = ttk.Button(btn_frame, text="Pause", command=self._toggle_running)
        self.toggle_btn.grid(row=0, column=0, padx=(0, 8))

        ttk.Button(btn_frame, text="Jiggle Now", command=self._jiggle_now).grid(
            row=0, column=1
        )

    def _update_scale_info(self, min_cm: float, max_cm: float) -> None:
        min_px = cm_to_pixels(min_cm, self._px_per_cm)
        max_px = cm_to_pixels(max_cm, self._px_per_cm)
        self.scale_info_var.set(
            f"Screen scale: {self._px_per_cm:.1f} px/cm ({min_px}-{max_px} px)"
        )

    def _parse_settings(self) -> tuple[float, float, float, bool]:
        try:
            raw_interval = float(self.interval_var.get().strip())
        except ValueError as exc:
            raise ValueError("Interval must be a positive number.") from exc
        interval_seconds = interval_to_seconds(raw_interval, self.unit_var.get())

        try:
            min_cm = float(self.min_cm_var.get().strip())
            max_cm = float(self.max_cm_var.get().strip())
        except ValueError as exc:
            raise ValueError("Min and Max distance must be positive numbers.") from exc
        if min_cm <= 0 or max_cm <= 0:
            raise ValueError("Distances must be greater than 0 cm.")
        if min_cm > max_cm:
            raise ValueError("Min distance cannot be greater than Max distance.")
        if max_cm > 30:
            raise ValueError("Max distance must be 30 cm or less.")

        return interval_seconds, min_cm, max_cm, self.idle_only_var.get()

    def _apply_settings(self) -> None:
        try:
            interval_seconds, min_cm, max_cm, only_when_idle = self._parse_settings()
        except ValueError as exc:
            messagebox.showerror("Invalid Settings", str(exc), parent=self.root)
            return
        self.scheduler.configure(
            interval_seconds=interval_seconds,
            min_cm=min_cm,
            max_cm=max_cm,
            only_when_idle=only_when_idle,
        )
        self._update_scale_info(min_cm, max_cm)
        self.error_var.set("")

    def _toggle_running(self) -> None:
        if self.scheduler.status().running:
            self.scheduler.pause()
        else:
            try:
                interval_seconds, min_cm, max_cm, only_when_idle = self._parse_settings()
            except ValueError as exc:
                messagebox.showerror("Invalid Settings", str(exc), parent=self.root)
                return
            self.scheduler.configure(
                interval_seconds=interval_seconds,
                min_cm=min_cm,
                max_cm=max_cm,
                only_when_idle=only_when_idle,
            )
            self._update_scale_info(min_cm, max_cm)
            self.scheduler.start()

    def _jiggle_now(self) -> None:
        try:
            _, min_cm, max_cm, _ = self._parse_settings()
            self.scheduler.configure(
                interval_seconds=self.scheduler.status().interval_seconds,
                min_cm=min_cm,
                max_cm=max_cm,
            )
            self._update_scale_info(min_cm, max_cm)
        except ValueError as exc:
            messagebox.showerror("Invalid Settings", str(exc), parent=self.root)
            return
        self.scheduler.trigger_now(reset_timer=True)

    def _refresh_ui(self) -> None:
        st = self.scheduler.status()
        if st.running:
            self.state_var.set("Running")
            self.toggle_btn.config(text="Pause")
            self.countdown_var.set(format_duration(st.seconds_remaining))
        else:
            self.state_var.set("Paused")
            self.toggle_btn.config(text="Start")
            self.countdown_var.set("--:--")

        try:
            self.idle_var.set(format_duration(get_idle_seconds()))
        except Exception:
            self.idle_var.set("Unavailable")

        self.count_var.set(str(st.jiggle_count))
        if st.last_jiggle_at:
            dist_note = (
                f" ({st.last_distance_cm:.1f} cm)"
                if st.last_distance_cm is not None
                else ""
            )
            self.last_jiggle_var.set(st.last_jiggle_at.strftime("%H:%M:%S") + dist_note)
        else:
            self.last_jiggle_var.set("Never")
        self.error_var.set(f"Error: {st.last_error}" if st.last_error else "")

        self.root.after(250, self._refresh_ui)

    def _on_close(self) -> None:
        self.scheduler.stop()
        self.root.destroy()


CLI_COMMANDS_HELP = (
    "Commands: jiggle | pause | start | interval <time[s|m]> | "
    "distance <min> [max] | status | help | quit"
)


def handle_cli_command(line: str, scheduler: JigglerScheduler) -> tuple[bool, str]:
    """Process one interactive CLI command. Returns (should_continue, output)."""
    parts = line.strip().split()
    if not parts:
        return True, ""

    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in ("quit", "exit", "q"):
        return False, "Stopping Cursor Jiggler..."
    if cmd in ("help", "?"):
        return True, CLI_COMMANDS_HELP
    if cmd in ("jiggle", "now"):
        ok = scheduler.trigger_now(reset_timer=True)
        st = scheduler.status()
        if not ok:
            return True, f"Jiggle failed: {st.last_error}"
        dist = f"{st.last_distance_cm:.1f} cm" if st.last_distance_cm else ""
        return True, f"Jiggled cursor {dist} (total: {st.jiggle_count})"
    if cmd == "pause":
        scheduler.pause()
        return True, "Paused."
    if cmd in ("start", "resume"):
        scheduler.start()
        return True, "Resumed."
    if cmd == "status":
        st = scheduler.status()
        state = "Running" if st.running else "Paused"
        rem = format_duration(st.seconds_remaining) if st.running else "--:--"
        return True, (
            f"Status={state}, interval={st.interval_seconds:g}s, "
            f"next={rem}, total={st.jiggle_count}"
        )
    if cmd == "interval":
        if not args:
            return True, "Usage: interval <value[s|m]> [seconds|minutes]"
        try:
            if len(args) >= 2:
                val = float(args[0])
                secs = interval_to_seconds(val, args[1])
            else:
                _, _, secs = parse_interval_spec(args[0], DEFAULT_INTERVAL_UNIT)
            scheduler.configure(interval_seconds=secs)
        except ValueError as exc:
            return True, f"Invalid interval: {exc}"
        return True, f"Interval updated to {secs:g}s."
    if cmd == "distance":
        if not args:
            return True, "Usage: distance <min_cm> [max_cm]"
        try:
            min_cm = float(args[0])
            max_cm = float(args[1]) if len(args) >= 2 else min_cm
            if max_cm > 30:
                return True, "Max distance must be 30 cm or less."
            scheduler.configure(
                interval_seconds=scheduler.status().interval_seconds,
                min_cm=min_cm,
                max_cm=max_cm,
            )
        except ValueError as exc:
            return True, f"Invalid distance: {exc}"
        return True, f"Distance updated to {min_cm:g}-{max_cm:g} cm."

    return True, f"Unknown command {cmd!r}. {CLI_COMMANDS_HELP}"


def run_cli(
    interval_seconds: float,
    min_cm: float,
    max_cm: float,
    only_when_idle: bool,
) -> int:
    """Run the cursor jiggler in terminal mode until interrupted or quit."""
    scheduler = JigglerScheduler(
        interval_seconds=interval_seconds,
        min_cm=min_cm,
        max_cm=max_cm,
        only_when_idle=only_when_idle,
    )
    print(
        f"Cursor Jiggler started: every {format_duration(interval_seconds)} "
        f"({interval_seconds:g}s), distance={min_cm:g}-{max_cm:g} cm, "
        f"only_when_idle={only_when_idle}."
    )
    print(CLI_COMMANDS_HELP)
    scheduler.start()

    stop_event = threading.Event()
    print_lock = threading.Lock()

    def _stdin_reader() -> None:
        while not stop_event.is_set():
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if line == "":
                # EOF (e.g. non-interactive stdin): keep running until Ctrl+C.
                return
            keep_going, message = handle_cli_command(line, scheduler)
            if message:
                with print_lock:
                    print(message, flush=True)
            if not keep_going:
                stop_event.set()
                return

    if sys.stdin is not None:
        threading.Thread(target=_stdin_reader, name="CLIInput", daemon=True).start()

    last_count = 0
    last_error_seen: str | None = None
    try:
        while not stop_event.is_set():
            stop_event.wait(0.25)
            st = scheduler.status()
            if st.jiggle_count != last_count:
                last_count = st.jiggle_count
                ts = st.last_jiggle_at.strftime("%H:%M:%S") if st.last_jiggle_at else ""
                dist = f"{st.last_distance_cm:.1f} cm" if st.last_distance_cm else ""
                with print_lock:
                    print(
                        f"[{ts}] Jiggled cursor {dist} (total: {st.jiggle_count})",
                        flush=True,
                    )
            if st.last_error and st.last_error != last_error_seen:
                last_error_seen = st.last_error
                with print_lock:
                    print(f"[error] {st.last_error}", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        print("\nStopping Cursor Jiggler...")
    finally:
        stop_event.set()
        scheduler.stop()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Jiggle the Windows mouse cursor every specified interval "
            "(default: 10 seconds, 3 to 10 cm)."
        )
    )
    parser.add_argument(
        "--interval",
        type=str,
        default=f"{DEFAULT_INTERVAL_VALUE:g}",
        help="Interval between jiggles, e.g. '10', '10s', or '9m' (default: 10).",
    )
    parser.add_argument(
        "--unit",
        choices=("seconds", "minutes"),
        default=DEFAULT_INTERVAL_UNIT,
        help="Unit for --interval when no suffix is given (default: seconds).",
    )
    parser.add_argument(
        "--min-cm",
        type=float,
        default=DEFAULT_MIN_CM,
        help="Minimum jiggle distance in centimeters (default: 3).",
    )
    parser.add_argument(
        "--max-cm",
        type=float,
        default=DEFAULT_MAX_CM,
        help="Maximum jiggle distance in centimeters (default: 10).",
    )
    parser.add_argument(
        "--idle-only",
        action="store_true",
        help="Only jiggle if OS idle time is already >= the interval.",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Run in terminal/CLI mode with live interactive commands.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Jiggle the cursor once immediately and exit.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        interval_value, interval_unit, interval_seconds = parse_interval_spec(
            args.interval, args.unit
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.min_cm <= 0 or args.max_cm <= 0 or args.min_cm > args.max_cm:
        print("Error: --min-cm and --max-cm must be > 0 and min <= max", file=sys.stderr)
        return 2

    if args.once:
        try:
            moved_cm = jiggle_cursor(min_cm=args.min_cm, max_cm=args.max_cm)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"Jiggled cursor {moved_cm:.1f} cm.")
        return 0

    if args.cli:
        return run_cli(
            interval_seconds=interval_seconds,
            min_cm=args.min_cm,
            max_cm=args.max_cm,
            only_when_idle=args.idle_only,
        )

    if tk is None:
        print(
            "Error: tkinter is not available in this Python environment. Use --cli.",
            file=sys.stderr,
        )
        return 2

    scheduler = JigglerScheduler(
        interval_seconds=interval_seconds,
        min_cm=args.min_cm,
        max_cm=args.max_cm,
        only_when_idle=args.idle_only,
    )
    root = tk.Tk()
    CursorJigglerApp(
        root,
        scheduler,
        initial_interval=interval_value,
        initial_unit=interval_unit,
        initial_min_cm=args.min_cm,
        initial_max_cm=args.max_cm,
        initial_idle_only=args.idle_only,
    )
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
