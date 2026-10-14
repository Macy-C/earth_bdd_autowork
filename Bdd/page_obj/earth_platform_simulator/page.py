from __future__ import annotations
from autowork_core.page import WindowPage


class EarthPlatformSimulatorPage(WindowPage):
    root_locator_file = "earth_platform_simulator/window.yaml"
    root_locator = "EarthPlatformSimulator_window"

    def __init__(self, context):
        super().__init__(context)
        from Bdd.simulator_client import SimulatorClient

        self._client = SimulatorClient()

    def get_position(self, *, timeout=None):
        """Get a fresh snapshot only while stationary and not scanning."""
        return self._client.get_position(timeout=timeout)

    def move_to_horizontal(self, target, *, mode="jump", speed=100,
                           timeout=30.0, tolerance=0.1):
        return self._client.move_to_horizontal(
            target, mode=mode, speed=speed, timeout=timeout, tolerance=tolerance
        )

    def move_to_vertical(self, target, *, mode="jump", speed=100,
                         timeout=30.0, tolerance=0.1):
        return self._client.move_to_vertical(
            target, mode=mode, speed=speed, timeout=timeout, tolerance=tolerance
        )

    def move_up(self, duration=0.5, *, speed=100):
        return self._client.move_up(duration, speed=speed)

    def move_down(self, duration=0.5, *, speed=100):
        return self._client.move_down(duration, speed=speed)

    def move_in(self, duration=0.5, *, speed=100):
        return self._client.move_in(duration, speed=speed)

    def move_out(self, duration=0.5, *, speed=100):
        return self._client.move_out(duration, speed=speed)

    def stop(self):
        return self._client.stop()

    def prepare(self):
        return self._client.prepare()

    def scan(self):
        """Send start-scan; receipt acknowledgement is not scan completion."""
        return self._client.scan()

    def abort(self):
        return self._client.abort()

    def close(self):
        """Release this Page's connections, not the simulator process."""
        self._client.close()