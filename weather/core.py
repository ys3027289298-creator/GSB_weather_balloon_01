"""Weather balloon observation game core logic.

Rules encoded here (see tests/test_core.py):
- readings sharing a sample time sort deterministically (stable order);
- duplicate readings are counted once in averages;
- offline sensors never participate in wind averages;
- unit conversion is exact for negative temperatures;
- readings older than LATE_WINDOW ticks never pollute the current round;
- a failed calibration restores the previous coefficients;
- cancelling a launch never consumes a balloon;
- restoring a snapshot never moves the sequence counter backwards.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

LATE_WINDOW = 3
ARCHIVE_CAPACITY = 4
BALLOON_INVENTORY = 2
DEFAULT_SENSORS = ("s1", "s2", "s3")
KINDS = ("temperature", "wind", "humidity", "pressure")
REFERENCE_POINTS = (-40.0, 0.0, 25.0)
VALID_RANGE = (-90.0, 60.0)


class GameError(Exception):
    """Raised when a game rule is violated."""


def to_celsius(value, unit):
    """Convert a temperature to Celsius, exact for negative values."""
    name = str(unit).strip().upper()
    number = float(value)
    if name in ("C", "CELSIUS"):
        return number
    if name in ("F", "FAHRENHEIT"):
        return (number - 32.0) * 5.0 / 9.0
    if name in ("K", "KELVIN"):
        return number - 273.15
    raise GameError(f"unknown unit: {unit}")


def from_celsius(value, unit):
    """Convert a Celsius temperature to the requested unit."""
    name = str(unit).strip().upper()
    number = float(value)
    if name in ("C", "CELSIUS"):
        return number
    if name in ("F", "FAHRENHEIT"):
        return number * 9.0 / 5.0 + 32.0
    if name in ("K", "KELVIN"):
        return number + 273.15
    raise GameError(f"unknown unit: {unit}")


@dataclass
class Sensor:
    sid: str
    online: bool = True
    scale: float = 1.0
    offset: float = 0.0

    def apply(self, value):
        return value * self.scale + self.offset


@dataclass
class Reading:
    seq: int
    sensor: str
    kind: str
    value: float
    unit: str
    sampled_at: int

    def celsius(self):
        return to_celsius(self.value, self.unit)


@dataclass
class RoundSummary:
    tick: int
    count: int
    wind: float | None
    temperature: float | None


class Game:
    def __init__(self, sensors=None, balloons=BALLOON_INVENTORY):
        self.sensors = sensors if sensors is not None else {
            sid: Sensor(sid) for sid in DEFAULT_SENSORS
        }
        self.balloons = balloons
        self.balloon_capacity = balloons
        self.tick = 0
        self._seq = 0
        self.pending = []
        self.archive = []
        self.launches = []
        self.alerts = []

    @classmethod
    def default(cls):
        return cls()

    def _next_seq(self):
        self._seq += 1
        return self._seq

    def _alert(self, message):
        self.alerts.append(message)

    def ingest(self, sensor, kind, value, unit, sampled_at):
        """Record a reading; duplicate input returns the existing reading."""
        if sensor not in self.sensors:
            raise GameError(f"unknown sensor: {sensor}")
        if kind not in KINDS:
            raise GameError(f"unknown kind: {kind}")
        sampled_at = int(sampled_at)
        for existing in self.pending:
            if (existing.sensor, existing.kind, existing.sampled_at) == (
                sensor,
                kind,
                sampled_at,
            ):
                return existing
        reading = Reading(
            seq=self._next_seq(),
            sensor=sensor,
            kind=kind,
            value=float(value),
            unit=str(unit).strip().upper(),
            sampled_at=sampled_at,
        )
        self.pending.append(reading)
        return reading

    def ordered_readings(self):
        """Pending readings sorted by sample time with a stable tie-break."""
        return sorted(self.pending, key=lambda r: (r.sampled_at, r.seq))

    @staticmethod
    def _dedupe(readings):
        seen = {}
        for reading in readings:
            key = (reading.sensor, reading.kind, reading.sampled_at)
            if key not in seen:
                seen[key] = reading
        return list(seen.values())

    def _average(self, readings, kind):
        values = []
        for reading in self._dedupe(readings):
            if reading.kind != kind:
                continue
            sensor = self.sensors[reading.sensor]
            if not sensor.online:
                continue
            value = reading.celsius() if kind == "temperature" else reading.value
            values.append(sensor.apply(value))
        if not values:
            return None
        return round(sum(values) / len(values), 2)

    def wind_average(self, readings=None):
        return self._average(
            self.pending if readings is None else readings, "wind"
        )

    def temperature_average(self, readings=None):
        return self._average(
            self.pending if readings is None else readings, "temperature"
        )

    def run_round(self):
        """Close the current round; late readings are dropped with an alert."""
        self.tick += 1
        valid, late = [], []
        for reading in self.pending:
            if self.tick - reading.sampled_at <= LATE_WINDOW:
                valid.append(reading)
            else:
                late.append(reading)
        for reading in late:
            self._alert(
                "late reading dropped: seq=%d sensor=%s sampled_at=%d"
                % (reading.seq, reading.sensor, reading.sampled_at)
            )
        summary = RoundSummary(
            tick=self.tick,
            count=len(valid),
            wind=self._average(valid, "wind"),
            temperature=self._average(valid, "temperature"),
        )
        self.archive.append(summary)
        if len(self.archive) > ARCHIVE_CAPACITY:
            dropped = self.archive.pop(0)
            self._alert(f"archive full: dropped round {dropped.tick}")
        self.pending = []
        return summary

    def calibrate(self, sid, scale, offset):
        """Apply new coefficients; on validation failure restore the old ones."""
        if sid not in self.sensors:
            raise GameError(f"unknown sensor: {sid}")
        sensor = self.sensors[sid]
        old_scale, old_offset = sensor.scale, sensor.offset
        sensor.scale = float(scale)
        sensor.offset = float(offset)
        ok, reason = self._validate_calibration(sensor)
        if not ok:
            sensor.scale, sensor.offset = old_scale, old_offset
            self._alert(f"calibration failed for {sid}: {reason}")
            return False, f"calibration failed for {sid}: {reason}"
        return True, f"calibrated {sid}: scale={sensor.scale} offset={sensor.offset}"

    @staticmethod
    def _validate_calibration(sensor):
        if not sensor.scale > 0:
            return False, "scale must be positive"
        for reference in REFERENCE_POINTS:
            outcome = sensor.apply(reference)
            if not VALID_RANGE[0] <= outcome <= VALID_RANGE[1]:
                return False, f"reference {reference} out of range"
        return True, "ok"

    def launch(self):
        if self.balloons <= 0:
            raise GameError("no balloons left")
        self.balloons -= 1
        record = {"seq": self._next_seq(), "tick": self.tick, "status": "launched"}
        self.launches.append(record)
        return record

    def cancel(self):
        """Cancel the most recent active launch and return its balloon."""
        for record in reversed(self.launches):
            if record["status"] == "launched":
                record["status"] = "cancelled"
                self.balloons = min(self.balloons + 1, self.balloon_capacity)
                self._alert(f"launch cancelled: seq={record['seq']}")
                return True, "launch cancelled"
        return False, "no active launch"

    def to_dict(self):
        return {
            "tick": self.tick,
            "seq": self._seq,
            "balloons": self.balloons,
            "balloon_capacity": self.balloon_capacity,
            "sensors": {sid: asdict(s) for sid, s in self.sensors.items()},
            "pending": [asdict(r) for r in self.pending],
            "archive": [asdict(a) for a in self.archive],
            "launches": [dict(record) for record in self.launches],
            "alerts": list(self.alerts),
        }

    @classmethod
    def from_dict(cls, data):
        sensors = {sid: Sensor(**raw) for sid, raw in data["sensors"].items()}
        game = cls(sensors=sensors, balloons=data["balloons"])
        game.balloon_capacity = data.get("balloon_capacity", game.balloon_capacity)
        game.tick = data["tick"]
        game.pending = [Reading(**raw) for raw in data.get("pending", [])]
        game.archive = [RoundSummary(**raw) for raw in data.get("archive", [])]
        game.launches = [dict(record) for record in data.get("launches", [])]
        game.alerts = list(data.get("alerts", []))
        game._seq = max(
            [data.get("seq", 0)]
            + [reading.seq for reading in game.pending]
            + [record.get("seq", 0) for record in game.launches]
        )
        return game

    def save(self, path):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))
