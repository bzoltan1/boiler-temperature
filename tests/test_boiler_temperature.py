import configparser
import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import boiler_temperature as bt


def make_config():
    config = configparser.ConfigParser(interpolation=None)
    config.read_dict(bt.DEFAULTS)
    return config


def make_state():
    return {
        "version": 1,
        "last_web_upload": None,
        "channels": {name: {"history": [], "pending": None} for name in bt.CHANNELS},
    }


def make_logger():
    logger = logging.getLogger("boiler-test")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


class ToNumberTest(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(bt.to_number(25), 25.0)
        self.assertEqual(bt.to_number(25.5), 25.5)
        self.assertEqual(bt.to_number("-7 C"), -7.0)
        self.assertEqual(bt.to_number("12.25°C"), 12.25)
        self.assertIsNone(bt.to_number(None))
        self.assertIsNone(bt.to_number("not a number"))
        self.assertIsNone(bt.to_number(True))


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.logger = make_logger()

    def run_values(self, state, roof, boiler, now):
        return bt.validate_readings(
            {"s1": roof, "s2": boiler}, state, self.config, now, self.logger
        )

    def test_initial_reading_needs_confirmation(self):
        state = make_state()
        result = self.run_values(state, 25.0, 76.0, 100.0)
        self.assertFalse(result["roof"]["accepted"])
        self.assertIsNone(result["roof"]["last"])
        self.assertEqual(state["channels"]["roof"]["pending"]["v"], 25.0)
        result = self.run_values(state, 25.0, 76.0, 160.0)
        self.assertTrue(result["roof"]["accepted"])
        self.assertEqual(result["roof"]["last"], 25.0)
        self.assertEqual(result["boiler"]["last"], 76.0)

    def test_small_change_is_accepted(self):
        state = make_state()
        self.run_values(state, 25.0, 76.0, 100.0)
        self.run_values(state, 25.0, 76.0, 160.0)
        result = self.run_values(state, 29.0, 72.0, 220.0)
        self.assertTrue(result["roof"]["accepted"])
        self.assertTrue(result["boiler"]["accepted"])

    def test_single_spike_is_rejected(self):
        state = make_state()
        self.run_values(state, 25.0, 76.0, 100.0)
        self.run_values(state, 25.0, 76.0, 160.0)
        result = self.run_values(state, 45.0, 76.0, 220.0)
        self.assertFalse(result["roof"]["accepted"])
        self.assertEqual(result["roof"]["last"], 25.0)
        self.assertEqual(state["channels"]["roof"]["pending"]["v"], 45.0)
        result = self.run_values(state, 25.0, 76.0, 280.0)
        self.assertTrue(result["roof"]["accepted"])
        self.assertIsNone(state["channels"]["roof"]["pending"])

    def test_real_step_is_confirmed(self):
        state = make_state()
        self.run_values(state, 25.0, 76.0, 100.0)
        self.run_values(state, 25.0, 76.0, 160.0)
        result = self.run_values(state, 45.0, 76.0, 220.0)
        self.assertFalse(result["roof"]["accepted"])
        result = self.run_values(state, 45.5, 76.0, 280.0)
        self.assertTrue(result["roof"]["accepted"])
        self.assertEqual(result["roof"]["last"], 45.5)
        result = self.run_values(state, 45.5, 76.0, 340.0)
        self.assertTrue(result["roof"]["accepted"])

    def test_bad_baseline_recovers(self):
        state = make_state()
        state["channels"]["roof"]["history"] = [{"t": 0.0, "v": 32.0}]
        result = self.run_values(state, 25.0, 76.0, 100.0)
        self.assertFalse(result["roof"]["accepted"])
        self.assertEqual(result["roof"]["last"], 32.0)
        result = self.run_values(state, 25.0, 76.0, 160.0)
        self.assertTrue(result["roof"]["accepted"])
        self.assertEqual(result["roof"]["last"], 25.0)
        self.assertEqual(state["channels"]["roof"]["pending"], None)

    def test_out_of_range_is_rejected(self):
        state = make_state()
        self.run_values(state, 25.0, 76.0, 100.0)
        self.run_values(state, 25.0, 76.0, 160.0)
        result = self.run_values(state, 200.0, -20.0, 220.0)
        self.assertFalse(result["roof"]["accepted"])
        self.assertFalse(result["boiler"]["accepted"])
        self.assertEqual(result["roof"]["last"], 25.0)
        self.assertIsNone(state["channels"]["roof"]["pending"])

    def test_unreadable_is_rejected(self):
        state = make_state()
        self.run_values(state, 25.0, 76.0, 100.0)
        self.run_values(state, 25.0, 76.0, 160.0)
        result = self.run_values(state, None, None, 220.0)
        self.assertFalse(result["roof"]["accepted"])
        self.assertFalse(result["boiler"]["accepted"])
        self.assertEqual(result["roof"]["last"], 25.0)

    def test_channels_are_independent(self):
        state = make_state()
        self.run_values(state, 25.0, 76.0, 100.0)
        self.run_values(state, 25.0, 76.0, 160.0)
        result = self.run_values(state, 45.0, 77.0, 220.0)
        self.assertFalse(result["roof"]["accepted"])
        self.assertTrue(result["boiler"]["accepted"])
        self.assertEqual(result["roof"]["last"], 25.0)
        self.assertEqual(result["boiler"]["last"], 77.0)

    def test_negative_decimal_value(self):
        state = make_state()
        self.run_values(state, -5.5, 60.0, 100.0)
        result = self.run_values(state, -5.5, 60.0, 160.0)
        self.assertTrue(result["roof"]["accepted"])
        self.assertEqual(result["roof"]["last"], -5.5)


class StateTest(unittest.TestCase):
    def test_save_and_load(self):
        logger = make_logger()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            state = make_state()
            state["channels"]["roof"]["history"].append({"t": 1.0, "v": 25.0})
            bt.save_state(path, state)
            loaded = bt.load_state(path, logger)
            self.assertEqual(loaded["channels"]["roof"]["history"][0]["v"], 25.0)

    def test_corrupt_state_is_ignored(self):
        logger = make_logger()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            loaded = bt.load_state(path, logger)
            self.assertEqual(loaded["channels"], {})


if __name__ == "__main__":
    unittest.main()
