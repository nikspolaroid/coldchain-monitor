"""
Cold Chain Monitoring - Sensor Simulator
=========================================

Simulates three cold storage units publishing telemetry over MQTT.

Each unit models:
  - temperature      (heat leaks in from ambient, compressor pulls it out)
  - humidity         (rises while the door is open)
  - door_open        (random door events - normal operational noise)
  - vibration        (compressor health signal, in mm/s RMS)
  - compressor_on    (thermostat state)

Two failure modes are deliberately built in:

  1. DOOR EVENTS      -> short, sharp temperature spikes that recover on their
                         own. These are NORMAL. A good dashboard should not
                         page anyone at 3am for these.

  2. COMPRESSOR WEAR  -> vibration slowly creeps up while cooling power fades,
                         so the room drifts away from its target over hours.
                         This is the PREDICTIVE MAINTENANCE signal.

Telling those two apart - a spike versus a trend - is the actual engineering
problem this project solves.

Note: the internal `wear` value is deliberately NOT published. The dashboard
has to infer machine health from vibration and temperature drift, exactly as
it would with real equipment.

Run with:  python simulator/coldchain_sim.py
Stop with: Ctrl+C
"""

import json
import random
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt


# ---------------------------------------------------------------------------
# CONFIGURATION - change these to tune the simulation
# ---------------------------------------------------------------------------

BROKER_HOST = "localhost"   # Mosquitto is reachable on your Mac via port 1883
BROKER_PORT = 1883
PUBLISH_INTERVAL = 5        # seconds between readings

AMBIENT_TEMP = 22.0         # warehouse air temperature outside the units

# How fast the failing compressor degrades, per reading.
# Higher = faster failure, so you don't wait hours to see the trend.
DEGRADATION_RATE = 0.004


# ---------------------------------------------------------------------------
# UNIT MODEL
# ---------------------------------------------------------------------------

class ColdStorageUnit:
    """One cold storage room with its own thermostat and compressor."""

    def __init__(self, unit_id, name, target_temp, tolerance, cooling_power,
                 base_humidity, door_chance, degrading=False):
        self.unit_id = unit_id
        self.name = name

        # Thermostat settings
        self.target_temp = target_temp      # degrees C we are aiming for
        self.tolerance = tolerance          # +/- band before the compressor acts
        self.cooling_power = cooling_power  # degrees C removed per reading

        # Live state
        self.temperature = target_temp      # start at target
        self.humidity = base_humidity
        self.base_humidity = base_humidity
        self.door_chance = door_chance
        self.compressor_on = False
        self.door_open = False
        self.door_timer = 0                 # readings left with the door open

        # Compressor health
        self.degrading = degrading
        self.wear = 0.0                     # 0.0 = new, 1.0 = worn out
        self.base_vibration = 2.0           # mm/s RMS when healthy

    # -- internal steps -----------------------------------------------------

    def _update_door(self):
        """Randomly open the door, then count down until it shuts again."""
        if self.door_open:
            self.door_timer -= 1
            if self.door_timer <= 0:
                self.door_open = False
        elif random.random() < self.door_chance:
            self.door_open = True
            self.door_timer = random.randint(2, 5)

    def _update_temperature(self):
        """
        Newton's law of cooling.

        Heat leaks in at a rate proportional to how much colder the room is
        than the warehouse. That means the model is self-limiting: a broken
        unit drifts up towards ambient and stops, instead of running away to
        impossible temperatures.
        """

        # 1. Heat leaking in. An open door leaks far faster.
        leak_rate = 0.05 if self.door_open else 0.02
        heat_gain = leak_rate * (AMBIENT_TEMP - self.temperature)

        # 2. Thermostat with hysteresis (stops rapid on/off cycling)
        if self.temperature > self.target_temp + self.tolerance:
            self.compressor_on = True
        elif self.temperature < self.target_temp - self.tolerance:
            self.compressor_on = False

        # 3. Cooling. A worn compressor moves less heat.
        cooling = 0.0
        if self.compressor_on:
            efficiency = max(0.15, 1.0 - self.wear)
            cooling = self.cooling_power * efficiency

        # 4. Apply the change, plus a little sensor noise
        self.temperature += heat_gain - cooling
        self.temperature += random.uniform(-0.05, 0.05)
        self.temperature = round(min(self.temperature, AMBIENT_TEMP), 2)

    def _update_humidity(self):
        """Humidity climbs while the door is open, then settles back."""
        target = self.base_humidity + (12 if self.door_open else 0)
        self.humidity += (target - self.humidity) * 0.3
        self.humidity += random.uniform(-0.4, 0.4)
        self.humidity = round(max(0.0, min(100.0, self.humidity)), 1)

    def _read_vibration(self):
        """
        Compressor vibration in mm/s RMS.

        Rough ISO 10816 bands for small machines:
            below 2.8   healthy
            2.8 - 4.5   acceptable
            4.5 - 7.1   warning
            above 7.1   critical
        """
        if not self.compressor_on:
            return round(random.uniform(0.1, 0.3), 2)

        vibration = self.base_vibration + (self.wear * 8.0)
        vibration += random.uniform(-0.25, 0.25)
        return round(max(0.0, vibration), 2)

    def _age(self):
        """Advance wear on the one unit with a failing compressor."""
        if self.degrading:
            self.wear = min(1.0, self.wear + DEGRADATION_RATE)

    # -- public entry point -------------------------------------------------

    def step(self):
        """Advance the simulation by one interval and return a reading."""
        self._update_door()
        self._update_temperature()
        self._update_humidity()
        self._age()

        return {
            "unit_id": self.unit_id,
            "name": self.name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "temperature_c": self.temperature,
            "target_temp_c": self.target_temp,
            "humidity_pct": self.humidity,
            "door_open": self.door_open,
            "compressor_on": self.compressor_on,
            "vibration_mms": self._read_vibration(),
        }


# ---------------------------------------------------------------------------
# THE THREE UNITS
# ---------------------------------------------------------------------------

UNITS = [
    ColdStorageUnit(
        unit_id="freezer",
        name="Freezer Room",
        target_temp=-20.0,
        tolerance=1.5,
        cooling_power=1.30,
        base_humidity=55.0,
        door_chance=0.05,        # rarely opened
    ),
    ColdStorageUnit(
        unit_id="chiller",
        name="Chiller Room",
        target_temp=2.0,
        tolerance=1.0,
        cooling_power=0.90,
        base_humidity=78.0,
        door_chance=0.08,
        degrading=True,          # <-- this compressor is slowly failing
    ),
    ColdStorageUnit(
        unit_id="dock",
        name="Loading Dock Buffer",
        target_temp=8.0,
        tolerance=2.0,
        cooling_power=0.70,
        base_humidity=65.0,
        door_chance=0.15,        # opened constantly - it is a loading bay
    ),
]


# ---------------------------------------------------------------------------
# MQTT PUBLISHING LOOP
# ---------------------------------------------------------------------------

def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    print(f"Connecting to MQTT broker at {BROKER_HOST}:{BROKER_PORT} ...")
    client.connect(BROKER_HOST, BROKER_PORT, 60)
    client.loop_start()
    print(f"Connected. Publishing every {PUBLISH_INTERVAL}s. "
          "Press Ctrl+C to stop.\n")

    try:
        while True:
            for unit in UNITS:
                reading = unit.step()
                topic = f"coldchain/{unit.unit_id}/telemetry"

                client.publish(topic, json.dumps(reading))

                # Console output so you can watch it live
                door = "OPEN" if reading["door_open"] else "shut"
                comp = "ON " if reading["compressor_on"] else "off"
                print(
                    f"{unit.unit_id:<8} "
                    f"{reading['temperature_c']:>7.2f}C  "
                    f"RH {reading['humidity_pct']:>4.1f}%  "
                    f"door {door}  "
                    f"comp {comp}  "
                    f"vib {reading['vibration_mms']:>5.2f} mm/s"
                )

            print("-" * 72)
            time.sleep(PUBLISH_INTERVAL)

    except KeyboardInterrupt:
        print("\nStopping simulator ...")
    finally:
        client.loop_stop()
        client.disconnect()
        print("Disconnected cleanly.")


if __name__ == "__main__":
    main()
