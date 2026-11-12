"""Tests for cursor_jiggler."""

from __future__ import annotations

import ctypes
import os
import sys
import time
import unittest
from unittest import mock

import cursor_jiggler as cj

HAS_TK_DISPLAY = cj.tk is not None and (
    sys.platform == "win32" or bool(os.environ.get("DISPLAY"))
)


class TestHelpers(unittest.TestCase):
    def test_interval_to_seconds(self) -> None:
        self.assertEqual(cj.interval_to_seconds(10, "seconds"), 10.0)
        self.assertEqual(cj.interval_to_seconds(9, "minutes"), 540.0)
        self.assertEqual(cj.interval_to_seconds(1.5, "min"), 90.0)
        with self.assertRaises(ValueError):
            cj.interval_to_seconds(0, "seconds")
        with self.assertRaises(ValueError):
            cj.interval_to_seconds(-5, "seconds")
        with self.assertRaises(ValueError):
            cj.interval_to_seconds(5, "hours")

    def test_cm_to_pixels(self) -> None:
        self.assertEqual(cj.cm_to_pixels(3.0, px_per_cm=50.0), 150)
        self.assertEqual(cj.cm_to_pixels(10.0, px_per_cm=50.0), 500)
        with self.assertRaises(ValueError):
            cj.cm_to_pixels(0, px_per_cm=50.0)

    def test_split_total_into_steps(self) -> None:
        chunks = cj._split_total_into_steps(100, 6)
        self.assertEqual(len(chunks), 6)
        self.assertEqual(sum(chunks), 100)

    def test_format_duration(self) -> None:
        self.assertEqual(cj.format_duration(0), "00:00")
        self.assertEqual(cj.format_duration(10), "00:10")
        self.assertEqual(cj.format_duration(540), "09:00")
        self.assertEqual(cj.format_duration(3661), "01:01:01")

    def test_default_cli_args(self) -> None:
        args = cj.parse_args([])
        self.assertEqual(args.interval, "10")
        self.assertEqual(args.unit, "seconds")
        self.assertEqual(args.min_cm, 3.0)
        self.assertEqual(args.max_cm, 10.0)
        self.assertFalse(args.idle_only)
        self.assertFalse(args.cli)
        self.assertFalse(args.once)

    def test_parse_interval_spec(self) -> None:
        self.assertEqual(cj.parse_interval_spec("10s"), (10.0, "seconds", 10.0))
        self.assertEqual(cj.parse_interval_spec("9m"), (9.0, "minutes", 540.0))
        self.assertEqual(
            cj.parse_interval_spec("15", default_unit="seconds"),
            (15.0, "seconds", 15.0),
        )
        with self.assertRaises(ValueError):
            cj.parse_interval_spec("abc")


class TestWin32Structures(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows-only test")
    def test_input_struct_size_matches_win32(self) -> None:
        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(ctypes.sizeof(cj.INPUT), expected)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only test")
    def test_get_idle_seconds_and_px_per_cm_live(self) -> None:
        idle = cj.get_idle_seconds()
        self.assertIsInstance(idle, float)
        self.assertGreaterEqual(idle, 0.0)
        px_per_cm = cj.get_pixels_per_cm()
        self.assertGreater(px_per_cm, 10.0)

    def test_jiggle_cursor_absolute_returns_to_start(self) -> None:
        sent_coords: list[tuple[int, int, int]] = []

        class FakeUser32:
            def GetCursorPos(self, pt_ptr):
                pt = ctypes.cast(pt_ptr, ctypes.POINTER(cj.wintypes.POINT)).contents
                pt.x = 500
                pt.y = 400
                return 1

            def GetSystemMetrics(self, idx):
                return {
                    cj.SM_XVIRTUALSCREEN: 0,
                    cj.SM_YVIRTUALSCREEN: 0,
                    cj.SM_CXVIRTUALSCREEN: 1920,
                    cj.SM_CYVIRTUALSCREEN: 1200,
                }[idx]

            def SendInput(self, count, ptr, size):
                inp = ctypes.cast(ptr, ctypes.POINTER(cj.INPUT)).contents
                sent_coords.append(
                    (inp.union.mi.dx, inp.union.mi.dy, inp.union.mi.dwFlags)
                )
                return 1

        with mock.patch.object(
            cj, "_load_win32_apis", return_value=(FakeUser32(), None, None)
        ):
            moved_cm = cj.jiggle_cursor(
                min_cm=4.0,
                max_cm=4.0,
                steps=4,
                move_duration=0,
                hold_delay=0,
                px_per_cm=50.0,
            )

        self.assertEqual(moved_cm, 4.0)
        self.assertEqual(len(sent_coords), 8)
        start_norm = cj._to_virtual_absolute(500, 400, 0, 0, 1920, 1200)
        peak_norm = cj._to_virtual_absolute(700, 400, 0, 0, 1920, 1200)
        expected_flags = (
            cj.MOUSEEVENTF_MOVE | cj.MOUSEEVENTF_ABSOLUTE | cj.MOUSEEVENTF_VIRTUALDESK
        )
        self.assertEqual(sent_coords[3], (*peak_norm, expected_flags))
        self.assertEqual(sent_coords[-1], (*start_norm, expected_flags))

    def test_jiggle_cursor_relative_fallback_sums_to_zero(self) -> None:
        recorded_dx: list[int] = []

        class FallbackUser32:
            def GetCursorPos(self, pt_ptr):
                return 0

            def SendInput(self, count, ptr, size):
                inp = ctypes.cast(ptr, ctypes.POINTER(cj.INPUT)).contents
                recorded_dx.append(inp.union.mi.dx)
                return 1

        with mock.patch.object(
            cj, "_load_win32_apis", return_value=(FallbackUser32(), None, None)
        ):
            cj.jiggle_cursor(
                min_cm=3.0,
                max_cm=3.0,
                steps=5,
                move_duration=0,
                hold_delay=0,
                px_per_cm=50.0,
            )

        self.assertEqual(len(recorded_dx), 10)
        self.assertEqual(sum(recorded_dx[:5]), 150)
        self.assertEqual(sum(recorded_dx[5:]), -150)
        self.assertEqual(sum(recorded_dx), 0)

    def test_jiggle_cursor_raises_when_sendinput_fails(self) -> None:
        class FailingUser32:
            def GetCursorPos(self, pt_ptr):
                return 0

            def SendInput(self, count, ptr, size):
                return 0

        with mock.patch.object(
            cj, "_load_win32_apis", return_value=(FailingUser32(), None, None)
        ):
            with self.assertRaises(OSError):
                cj.jiggle_cursor(
                    min_cm=3.0,
                    max_cm=3.0,
                    steps=2,
                    move_duration=0,
                    hold_delay=0,
                    px_per_cm=50.0,
                )


class TestJigglerScheduler(unittest.TestCase):
    def test_scheduler_jiggles_at_interval(self) -> None:
        calls: list[tuple[float, float]] = []

        def fake_jiggle(min_cm: float, max_cm: float) -> float:
            calls.append((min_cm, max_cm))
            return 5.5

        scheduler = cj.JigglerScheduler(
            interval_seconds=0.08,
            min_cm=3.0,
            max_cm=10.0,
            jiggle_fn=fake_jiggle,
            idle_fn=lambda: 0.0,
        )
        try:
            scheduler.start()
            deadline = time.monotonic() + 1.5
            while len(calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(len(calls), 2)
            self.assertTrue(all(c == (3.0, 10.0) for c in calls))
            st = scheduler.status()
            self.assertTrue(st.running)
            self.assertGreaterEqual(st.jiggle_count, 2)
            self.assertEqual(st.last_distance_cm, 5.5)
            self.assertIsNotNone(st.last_jiggle_at)
            self.assertIsNone(st.last_error)
        finally:
            scheduler.stop()

    def test_pause_and_resume(self) -> None:
        calls: list[float] = []
        scheduler = cj.JigglerScheduler(
            interval_seconds=0.05,
            jiggle_fn=lambda min_cm, max_cm: calls.append(min_cm) or min_cm,
            idle_fn=lambda: 0.0,
        )
        try:
            scheduler.start()
            time.sleep(0.08)
            scheduler.pause()
            count_at_pause = len(calls)
            time.sleep(0.15)
            self.assertEqual(len(calls), count_at_pause)
            self.assertFalse(scheduler.status().running)
        finally:
            scheduler.stop()

    def test_only_when_idle_skips_active_user(self) -> None:
        calls: list[float] = []
        idle_value = 0.01
        scheduler = cj.JigglerScheduler(
            interval_seconds=0.08,
            only_when_idle=True,
            jiggle_fn=lambda min_cm, max_cm: calls.append(min_cm) or min_cm,
            idle_fn=lambda: idle_value,
        )
        try:
            scheduler.start()
            time.sleep(0.18)
            self.assertEqual(len(calls), 0)

            idle_value = 1.0
            deadline = time.monotonic() + 1.0
            while len(calls) == 0 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(len(calls), 1)
        finally:
            scheduler.stop()

    def test_trigger_now_and_error_capture(self) -> None:
        def failing_jiggle(min_cm: float, max_cm: float) -> float:
            raise RuntimeError("boom")

        scheduler = cj.JigglerScheduler(
            interval_seconds=10.0,
            jiggle_fn=failing_jiggle,
            idle_fn=lambda: 0.0,
        )
        ok = scheduler.trigger_now()
        self.assertFalse(ok)
        st = scheduler.status()
        self.assertEqual(st.jiggle_count, 0)
        self.assertEqual(st.last_error, "boom")


class TestDesktopApp(unittest.TestCase):
    @unittest.skipUnless(HAS_TK_DISPLAY, "Tkinter/display unavailable")
    def test_app_initializes_and_closes_cleanly(self) -> None:
        root = cj.tk.Tk()
        root.withdraw()
        scheduler = cj.JigglerScheduler(
            interval_seconds=10.0,
            jiggle_fn=lambda min_cm, max_cm: min_cm,
            idle_fn=lambda: 0.0,
        )
        app = cj.CursorJigglerApp(root, scheduler)
        self.assertEqual(app.interval_var.get(), "10")
        self.assertEqual(app.unit_var.get(), "seconds")
        self.assertEqual(app.min_cm_var.get(), "3")
        self.assertEqual(app.max_cm_var.get(), "10")
        self.assertTrue(scheduler.status().running)
        app._on_close()
        self.assertFalse(scheduler.status().running)


class TestCLICommands(unittest.TestCase):
    def test_interactive_cli_commands(self) -> None:
        calls: list[tuple[float, float]] = []

        def fake_jiggle(min_cm: float, max_cm: float) -> float:
            calls.append((min_cm, max_cm))
            return min_cm

        scheduler = cj.JigglerScheduler(
            interval_seconds=10.0,
            min_cm=3.0,
            max_cm=10.0,
            jiggle_fn=fake_jiggle,
            idle_fn=lambda: 0.0,
        )
        try:
            scheduler.start()
            cont, msg = cj.handle_cli_command("jiggle", scheduler)
            self.assertTrue(cont)
            self.assertIn("Jiggled cursor", msg)
            self.assertEqual(len(calls), 1)

            cont, msg = cj.handle_cli_command("interval 9m", scheduler)
            self.assertTrue(cont)
            self.assertEqual(scheduler.status().interval_seconds, 540.0)
            self.assertIn("540s", msg)

            cont, msg = cj.handle_cli_command("distance 4 8", scheduler)
            self.assertTrue(cont)
            self.assertIn("4-8 cm", msg)

            cont, _ = cj.handle_cli_command("pause", scheduler)
            self.assertTrue(cont)
            self.assertFalse(scheduler.status().running)

            cont, _ = cj.handle_cli_command("start", scheduler)
            self.assertTrue(cont)
            self.assertTrue(scheduler.status().running)

            cont, msg = cj.handle_cli_command("quit", scheduler)
            self.assertFalse(cont)
            self.assertIn("Stopping", msg)
        finally:
            scheduler.stop()

    def test_once_flag_invokes_jiggle_and_exits(self) -> None:
        with mock.patch.object(cj, "jiggle_cursor", return_value=6.0) as mocked:
            rc = cj.main(["--once", "--min-cm", "3", "--max-cm", "10"])
        self.assertEqual(rc, 0)
        mocked.assert_called_once_with(min_cm=3.0, max_cm=10.0)


if __name__ == "__main__":
    unittest.main()
