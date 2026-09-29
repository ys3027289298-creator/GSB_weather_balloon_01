import json
import os
import tempfile
import unittest

from weather.core import (
    ARCHIVE_CAPACITY,
    BALLOON_INVENTORY,
    LATE_WINDOW,
    Game,
    GameError,
    from_celsius,
    to_celsius,
)
from weather.__main__ import DEMO_SCRIPT, execute, run_demo


class StableOrderingTest(unittest.TestCase):
    def test_same_sample_time_keeps_insertion_order(self):
        game = Game.default()
        game.ingest("s2", "wind", 5, "kn", 1)
        game.ingest("s1", "wind", 7, "kn", 1)
        game.ingest("s3", "wind", 9, "kn", 1)
        ordered = game.ordered_readings()
        self.assertEqual([r.sensor for r in ordered], ["s2", "s1", "s3"])

    def test_ordering_is_repeatable(self):
        game = Game.default()
        game.ingest("s3", "wind", 9, "kn", 2)
        game.ingest("s1", "wind", 7, "kn", 1)
        game.ingest("s2", "wind", 5, "kn", 1)
        first = [r.seq for r in game.ordered_readings()]
        second = [r.seq for r in game.ordered_readings()]
        self.assertEqual(first, second)
        self.assertEqual(
            [r.sampled_at for r in game.ordered_readings()], [1, 1, 2]
        )


class DuplicateReadingTest(unittest.TestCase):
    def test_duplicate_ingest_is_idempotent(self):
        game = Game.default()
        first = game.ingest("s1", "temperature", 20, "C", 0)
        second = game.ingest("s1", "temperature", 20, "C", 0)
        self.assertIs(first, second)
        self.assertEqual(len(game.pending), 1)

    def test_duplicate_readings_counted_once_in_average(self):
        game = Game.default()
        game.ingest("s1", "temperature", 20, "C", 0)
        game.ingest("s1", "temperature", 20, "C", 0)
        game.ingest("s2", "temperature", 40, "C", 0)
        self.assertEqual(game.temperature_average(), 30.0)


class OfflineSensorTest(unittest.TestCase):
    def test_offline_sensor_excluded_from_wind_average(self):
        game = Game.default()
        game.ingest("s1", "wind", 10, "kn", 0)
        game.ingest("s2", "wind", 20, "kn", 0)
        game.sensors["s2"].online = False
        self.assertEqual(game.wind_average(), 10.0)

    def test_all_offline_gives_none(self):
        game = Game.default()
        game.ingest("s1", "wind", 10, "kn", 0)
        game.sensors["s1"].online = False
        self.assertIsNone(game.wind_average())


class UnitConversionTest(unittest.TestCase):
    def test_negative_fahrenheit_to_celsius(self):
        self.assertEqual(to_celsius(-4, "F"), -20.0)
        self.assertEqual(to_celsius(-40, "F"), -40.0)

    def test_negative_celsius_to_fahrenheit(self):
        self.assertEqual(from_celsius(-20, "F"), -4.0)
        self.assertEqual(from_celsius(-40, "F"), -40.0)

    def test_kelvin_and_round_trip(self):
        self.assertEqual(to_celsius(273.15, "K"), 0.0)
        self.assertAlmostEqual(to_celsius(from_celsius(-12.5, "K"), "K"), -12.5)

    def test_unknown_unit_rejected(self):
        with self.assertRaises(GameError):
            to_celsius(1, "rankine")

    def test_negative_temperature_average(self):
        game = Game.default()
        game.ingest("s1", "temperature", -4, "F", 0)
        game.ingest("s2", "temperature", -22, "F", 0)
        self.assertEqual(game.temperature_average(), -25.0)


class LateWindowTest(unittest.TestCase):
    def test_late_reading_does_not_pollute_current_round(self):
        game = Game.default()
        game.ingest("s1", "temperature", 100, "C", -LATE_WINDOW - 5)
        game.ingest("s2", "temperature", 20, "C", 0)
        summary = game.run_round()
        self.assertEqual(summary.count, 1)
        self.assertEqual(summary.temperature, 20.0)

    def test_late_reading_raises_alert(self):
        game = Game.default()
        game.ingest("s1", "wind", 50, "kn", -LATE_WINDOW - 1)
        game.run_round()
        self.assertTrue(any("late reading dropped" in a for a in game.alerts))

    def test_reading_inside_window_is_kept(self):
        game = Game.default()
        game.ingest("s1", "temperature", 18, "C", 1 - LATE_WINDOW)
        summary = game.run_round()
        self.assertEqual(summary.count, 1)
        self.assertEqual(summary.temperature, 18.0)


class CalibrationTest(unittest.TestCase):
    def test_failed_calibration_restores_old_coefficients(self):
        game = Game.default()
        ok, _ = game.calibrate("s1", 0, 5)
        self.assertFalse(ok)
        self.assertEqual(game.sensors["s1"].scale, 1.0)
        self.assertEqual(game.sensors["s1"].offset, 0.0)
        self.assertTrue(any("calibration failed" in a for a in game.alerts))

    def test_out_of_range_calibration_rolled_back(self):
        game = Game.default()
        game.calibrate("s1", 1.0, 0.5)
        ok, _ = game.calibrate("s1", 10.0, 0.0)
        self.assertFalse(ok)
        self.assertEqual(game.sensors["s1"].scale, 1.0)
        self.assertEqual(game.sensors["s1"].offset, 0.5)

    def test_successful_calibration_applies(self):
        game = Game.default()
        ok, _ = game.calibrate("s1", 1.0, 2.0)
        self.assertTrue(ok)
        game.ingest("s1", "temperature", 20, "C", 0)
        self.assertEqual(game.temperature_average(), 22.0)

    def test_unknown_sensor_calibration_rejected(self):
        game = Game.default()
        with self.assertRaises(GameError):
            game.calibrate("ghost", 1.0, 0.0)


class LaunchCancelTest(unittest.TestCase):
    def test_cancel_does_not_consume_balloon(self):
        game = Game.default()
        game.launch()
        ok, _ = game.cancel()
        self.assertTrue(ok)
        self.assertEqual(game.balloons, BALLOON_INVENTORY)

    def test_launch_without_balloon_fails(self):
        game = Game.default()
        game.launch()
        game.launch()
        with self.assertRaises(GameError):
            game.launch()

    def test_cancel_without_active_launch(self):
        game = Game.default()
        ok, msg = game.cancel()
        self.assertFalse(ok)
        self.assertIn("no active launch", msg)

    def test_balloons_never_exceed_capacity(self):
        game = Game.default()
        game.launch()
        game.cancel()
        game.cancel()
        self.assertEqual(game.balloons, BALLOON_INVENTORY)


class SnapshotTest(unittest.TestCase):
    def test_round_trip_preserves_state(self):
        game = Game.default()
        game.ingest("s1", "temperature", 20, "C", 0)
        game.run_round()
        game.launch()
        restored = Game.from_dict(game.to_dict())
        self.assertEqual(restored.to_dict(), game.to_dict())

    def test_sequence_does_not_regress_after_restore(self):
        game = Game.default()
        reading = game.ingest("s1", "wind", 10, "kn", 0)
        restored = Game.from_dict(game.to_dict())
        new_reading = restored.ingest("s2", "wind", 12, "kn", 0)
        self.assertGreater(new_reading.seq, reading.seq)

    def test_sequence_uses_max_of_snapshot_sources(self):
        game = Game.default()
        record = game.launch()
        data = game.to_dict()
        data["seq"] = 0  # simulate a stale snapshot header
        restored = Game.from_dict(data)
        self.assertGreaterEqual(restored._seq, record["seq"])

    def test_save_and_load_json_archive(self):
        game = Game.default()
        game.ingest("s1", "temperature", 20, "C", 0)
        game.run_round()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "save.json")
            game.save(path)
            with open(path, encoding="utf-8") as handle:
                json.load(handle)  # must be valid JSON
            restored = Game.load(path)
        self.assertEqual(restored.to_dict(), game.to_dict())


class CommandTest(unittest.TestCase):
    def test_unknown_command_rejected(self):
        game = Game.default()
        ok, msg = execute(game, "explode now")
        self.assertFalse(ok)
        self.assertIn("unknown command", msg)

    def test_empty_command_rejected(self):
        game = Game.default()
        ok, msg = execute(game, "   ")
        self.assertFalse(ok)
        self.assertIn("empty", msg)

    def test_ingest_with_bad_sensor_rejected(self):
        game = Game.default()
        ok, msg = execute(game, "ingest ghost wind 5 kn 0")
        self.assertFalse(ok)
        self.assertIn("unknown sensor", msg)

    def test_ingest_with_bad_arguments_rejected(self):
        game = Game.default()
        ok, _ = execute(game, "ingest s1 wind")
        self.assertFalse(ok)
        ok, _ = execute(game, "ingest s1 wind abc kn 0")
        self.assertFalse(ok)

    def test_duplicate_input_via_command(self):
        game = Game.default()
        execute(game, "ingest s1 wind 5 kn 0")
        execute(game, "ingest s1 wind 5 kn 0")
        self.assertEqual(len(game.pending), 1)

    def test_round_command(self):
        game = Game.default()
        execute(game, "ingest s1 wind 10 kn 0")
        ok, msg = execute(game, "round")
        self.assertTrue(ok)
        self.assertIn("round 1", msg)


class EmptyDataTest(unittest.TestCase):
    def test_round_with_no_readings(self):
        game = Game.default()
        summary = game.run_round()
        self.assertEqual(summary.count, 0)
        self.assertIsNone(summary.wind)
        self.assertIsNone(summary.temperature)

    def test_averages_with_no_readings(self):
        game = Game.default()
        self.assertIsNone(game.wind_average())
        self.assertIsNone(game.temperature_average())


class CapacityBoundaryTest(unittest.TestCase):
    def test_archive_never_exceeds_capacity(self):
        game = Game.default()
        for _ in range(ARCHIVE_CAPACITY + 2):
            game.run_round()
        self.assertEqual(len(game.archive), ARCHIVE_CAPACITY)
        self.assertEqual(game.archive[-1].tick, ARCHIVE_CAPACITY + 2)
        self.assertTrue(any("archive full" in a for a in game.alerts))

    def test_archive_at_exact_capacity_has_no_alert(self):
        game = Game.default()
        for _ in range(ARCHIVE_CAPACITY):
            game.run_round()
        self.assertEqual(len(game.archive), ARCHIVE_CAPACITY)
        self.assertFalse(any("archive full" in a for a in game.alerts))


class DemoRepeatabilityTest(unittest.TestCase):
    def test_demo_output_is_deterministic(self):
        self.assertEqual(run_demo(), run_demo())

    def test_demo_alerts_calibration_window_and_seq_repeatable(self):
        def snapshot():
            game = Game.default()
            for command in DEMO_SCRIPT:
                execute(game, command)
            return game.to_dict()

        first, second = snapshot(), snapshot()
        self.assertEqual(first["alerts"], second["alerts"])
        self.assertEqual(first["sensors"], second["sensors"])
        self.assertEqual(first["archive"], second["archive"])
        self.assertEqual(first["seq"], second["seq"])

    def test_demo_covers_fixed_data(self):
        game = Game.default()
        for command in DEMO_SCRIPT:
            execute(game, command)
        self.assertEqual(game.tick, 1)
        self.assertEqual(game.balloons, BALLOON_INVENTORY)
        self.assertEqual(game.sensors["s1"].offset, 0.5)
        self.assertEqual(game.sensors["s2"].scale, 1.0)


if __name__ == "__main__":
    unittest.main()
