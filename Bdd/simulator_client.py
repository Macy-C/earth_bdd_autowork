"""Local GPC simulator control via its existing TCP and iStation protocols.

Dependencies: pyzmq and protobuf. Importing this module sends no commands.
connect() (also called by the first move) registers an iStation client and
changes simulator client state. Do not use alongside another iStation client.
Replies acknowledge receipt, not execution or physical completion. Movement
duration is approximate and includes communication/simulator scheduling delays.
This synchronous client is not thread-safe; use one instance from one thread.
"""

import math
import socket
import struct
import time
from dataclasses import dataclass
from decimal import Decimal

import zmq
from google.protobuf import descriptor_pb2, message_factory
from zmq.utils.monitor import recv_monitor_message


def _message_types():
    descriptor = descriptor_pb2.FileDescriptorProto(
        name="simulator_client.proto", package="simulator_client", syntax="proto2"
    )
    command_type = descriptor.enum_type.add(name="CommandType")
    command_type.value.add(name="TrySend", number=1)
    command_type.value.add(name="Send", number=2)
    definitions = {
        "MsgKey": [("CmdId", 1, 9, 2, "")],
        "MsgBody": [
            ("Type", 1, 14, 2, ".simulator_client.CommandType"),
            ("Command", 2, 12, 2, ""),
        ],
        "CommunicationMsg": [
            ("Key", 1, 11, 2, ".simulator_client.MsgKey"),
            ("Body", 3, 11, 1, ".simulator_client.MsgBody"),
        ],
        "CommunicationErrCode": [
            ("Key", 1, 11, 2, ".simulator_client.MsgKey"),
            ("Code", 2, 5, 2, ""),
        ],
        "iStationMovementCmd": [
            ("ISpeed", 1, 5, 2, ""),
            ("IvPos", 2, 5, 2, ""),
            ("IhPos", 3, 5, 2, ""),
            ("IaPos", 4, 5, 2, ""),
            ("IIndex", 5, 5, 2, ""),
        ],
        "iStationMovementPos": [
            ("IhMaxPos", 1, 5, 2, ""),
            ("IhPos", 2, 5, 2, ""),
            ("IhMinPos", 3, 5, 2, ""),
            ("IvMaxPos", 4, 5, 2, ""),
            ("IvPos", 5, 5, 2, ""),
            ("IvMinPos", 6, 5, 2, ""),
            ("IaMaxPos", 7, 5, 2, ""),
            ("IaPos", 8, 5, 2, ""),
            ("IaMinPos", 9, 5, 2, ""),
        ],
        "iStationCouchIncrement": [("CouchIncrementDistance", 1, 5, 2, "")],
    }
    for name, fields in definitions.items():
        message = descriptor.message_type.add(name=name)
        for field_name, number, field_type, label, type_name in fields:
            field = message.field.add(
                name=field_name, number=number, type=field_type, label=label
            )
            if type_name:
                field.type_name = type_name
    return message_factory.GetMessages([descriptor])


_MESSAGES = _message_types()
_CommunicationMsg = _MESSAGES["simulator_client.CommunicationMsg"]
_CommunicationErrCode = _MESSAGES["simulator_client.CommunicationErrCode"]
_MovementCmd = _MESSAGES["simulator_client.iStationMovementCmd"]
_MovementPos = _MESSAGES["simulator_client.iStationMovementPos"]
_CouchIncrement = _MESSAGES["simulator_client.iStationCouchIncrement"]


@dataclass(frozen=True)
class CouchPosition:
    """Coordinates and limits in simulator UI units; horizontal is Zero-relative."""

    horizontal: float
    vertical: float
    horizontal_min: float
    horizontal_max: float
    vertical_min: float
    vertical_max: float

    @classmethod
    def _from_message(cls, message):
        return cls(
            message.IhPos / 10,
            message.IvPos / 10,
            message.IhMinPos / 10,
            message.IhMaxPos / 10,
            message.IvMinPos / 10,
            message.IvMaxPos / 10,
        )


def _positive_seconds(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a positive number of seconds")
    try:
        seconds = float(value)
    except OverflowError as error:
        raise ValueError(f"{name} must be finite") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return seconds


def _raw_coordinate(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number in simulator UI units")
    scaled = Decimal(str(value)) * 10
    if not scaled.is_finite() or scaled != scaled.to_integral_value():
        raise ValueError(f"{name} must be finite with at most one decimal place")
    if not -2_147_483_648 <= scaled <= 2_147_483_647:
        raise ValueError(f"{name} exceeds the simulator coordinate range")
    return int(scaled)


class SimulatorClient:
    """Control only the simulator on 127.0.0.1; never resend motion or scan.

    move_* methods hold a direction for duration seconds, maintain the movement
    heartbeat, then send stop. speed is a raw simulator integer, not a declared
    mm/s unit. The model advances speed / 10 internal coordinate units per tick;
    speed=1 can produce no visible change during a short movement. Start with
    speed=100 and a short duration while observing the simulator UI.
    Timed moves and scan do not verify completion. move_to_* methods instead
    return position feedback verified within tolerance and stable after stop.
    """

    def __init__(self, *, timeout=2.0):
        self.timeout = _positive_seconds(timeout, "timeout")
        if self.timeout * 1000 > 2_147_483_647:
            raise ValueError("timeout exceeds the ZeroMQ timeout range")
        self._context = None
        self._tcp_socket = None
        self._position_socket = None
        self._vertical_increment = None
        self._registered = False
        self._closed = False
        self._pending_stop = None

    def _check_open(self):
        if self._closed:
            raise RuntimeError("SimulatorClient is closed")

    def _bed_request(self, command, payload=b""):
        self._check_open()
        if self._context is None:
            self._context = zmq.Context()
        message = _CommunicationMsg()
        message.Key.CmdId = command
        message.Body.Type = 2
        message.Body.Command = payload
        timeout_ms = max(1, math.ceil(self.timeout * 1000))
        request_socket = self._context.socket(zmq.REQ)
        try:
            request_socket.setsockopt(zmq.LINGER, 0)
            request_socket.setsockopt(zmq.IMMEDIATE, 1)
            request_socket.setsockopt(zmq.SNDTIMEO, timeout_ms)
            request_socket.setsockopt(zmq.RCVTIMEO, timeout_ms)
            request_socket.connect("tcp://127.0.0.1:5555")
            request_socket.send_multipart(
                [b"127.0.0.1", message.SerializeToString()]
            )
            frames = request_socket.recv_multipart()
            if len(frames) != 1:
                raise RuntimeError("Unexpected iStation reply frame count")
            response = _CommunicationMsg.FromString(frames[0])
            if (
                not response.IsInitialized()
                or response.Key.CmdId != "GantryErrCode"
                or not response.HasField("Body")
            ):
                raise RuntimeError("Unexpected iStation reply envelope")
            acknowledgement = _CommunicationErrCode.FromString(response.Body.Command)
            if (
                not acknowledgement.IsInitialized()
                or acknowledgement.Key.CmdId != command
                or acknowledgement.Code != 0
            ):
                raise RuntimeError(f"Invalid iStation acknowledgement for {command}")
        except zmq.Again as error:
            raise TimeoutError(
                f"iStation {command} timed out; execution is unknown, not resent"
            ) from error
        finally:
            request_socket.close(linger=0)

    def connect(self):
        """Register the bed-control client; this changes simulator state."""
        self._check_open()
        if not self._registered:
            self._bed_request("NewClientConnected2Gantry")
            self._registered = True
        return self

    def _remaining(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Target-position operation timed out")
        return remaining

    def _subscribe_position(self, deadline):
        self._check_open()
        if self._position_socket is not None:
            return
        if self._context is None:
            self._context = zmq.Context()
        subscriber = self._context.socket(zmq.SUB)
        monitor = None
        try:
            subscriber.setsockopt(zmq.LINGER, 0)
            subscriber.setsockopt(zmq.SUBSCRIBE, b"")
            monitor = subscriber.get_monitor_socket(
                events=zmq.EVENT_HANDSHAKE_SUCCEEDED
            )
            subscriber.connect("tcp://127.0.0.1:5556")
            if not monitor.poll(max(1, math.ceil(self._remaining(deadline) * 1000))):
                raise TimeoutError("Position publisher did not connect")
            if recv_monitor_message(monitor)["event"] != zmq.EVENT_HANDSHAKE_SUCCEEDED:
                raise ConnectionError("Position publisher handshake failed")
            time.sleep(min(0.1, self._remaining(deadline)))
            self._position_socket = subscriber
        finally:
            try:
                if monitor is not None:
                    try:
                        subscriber.disable_monitor()
                    finally:
                        monitor.close(linger=0)
            finally:
                if self._position_socket is not subscriber:
                    subscriber.close(linger=0)

    def _next_feedback(self, deadline, *, heartbeat=False):
        while True:
            remaining = self._remaining(deadline)
            if heartbeat:
                self._bed_request("Gantry_hb_movement")
            if not self._position_socket.poll(
                max(1, math.ceil(min(remaining, 0.05) * 1000))
            ):
                continue
            frames = self._position_socket.recv_multipart()
            if len(frames) != 1:
                raise RuntimeError("Unexpected position broadcast frame count")
            envelope = _CommunicationMsg.FromString(frames[0])
            if not envelope.IsInitialized():
                raise RuntimeError("Incomplete position broadcast envelope")
            self._remaining(deadline)
            if envelope.Key.CmdId == "MarkVerticalIncrement":
                if not envelope.HasField("Body"):
                    raise RuntimeError("Vertical increment broadcast has no body")
                increment = _CouchIncrement.FromString(envelope.Body.Command)
                if not increment.IsInitialized():
                    raise RuntimeError("Incomplete vertical increment feedback")
                self._vertical_increment = increment.CouchIncrementDistance
            return envelope

    def _next_position(self, deadline, *, heartbeat=False):
        feedback_deadline = min(deadline, time.monotonic() + self.timeout)
        while True:
            envelope = self._next_feedback(feedback_deadline, heartbeat=heartbeat)
            if envelope.Key.CmdId != "Gantry_pt":
                continue
            if not envelope.HasField("Body"):
                raise RuntimeError("Position broadcast has no body")
            position = _MovementPos.FromString(envelope.Body.Command)
            if not position.IsInitialized():
                raise RuntimeError("Incomplete position coordinates")
            if not (
                position.IhMinPos <= position.IhPos <= position.IhMaxPos
                and position.IvMinPos <= position.IvPos <= position.IvMaxPos
            ):
                raise RuntimeError("Position feedback violates simulator limits")
            self._remaining(feedback_deadline)
            return position

    def _position_snapshot(self, deadline):
        if self._pending_stop is not None:
            raise RuntimeError("A fresh snapshot requires bed motion to be stopped")
        self._subscribe_position(deadline)
        while self._position_socket.poll(0):
            self._position_socket.recv_multipart()
            self._remaining(deadline)
        self._vertical_increment = None
        self._bed_request("NewClientConnected2Gantry")
        self._registered = True
        return self._next_position(deadline)

    def get_position(self, *, timeout=None):
        """Get a fresh UI-coordinate snapshot while the bed is stationary.

        Re-registers this iStation client to request publication, resetting the
        simulator's movement state. Do not call during scans or other motion.
        """
        timeout = self.timeout if timeout is None else _positive_seconds(timeout, "timeout")
        return CouchPosition._from_message(
            self._position_snapshot(time.monotonic() + timeout)
        )

    def _settled_position(self, deadline):
        previous = self._position_snapshot(deadline)
        while True:
            time.sleep(min(0.12, self._remaining(deadline)))
            current = self._position_snapshot(deadline)
            if (previous.IhPos, previous.IvPos) == (current.IhPos, current.IvPos):
                return current
            previous = current

    def _wait_vertical_increment(self, expected, deadline):
        deadline = min(deadline, time.monotonic() + self.timeout)
        while self._vertical_increment is None or (
            expected is not None and self._vertical_increment != expected
        ):
            self._next_feedback(deadline)
        return self._vertical_increment

    def _set_vertical_increment(self, increment, deadline):
        self._remaining(deadline)
        self._vertical_increment = None
        message = _CouchIncrement(CouchIncrementDistance=increment)
        self._bed_request("MarkVerticalIncrement", message.SerializeToString())
        self._wait_vertical_increment(increment, deadline)

    def _jump_vertical(self, target, current, deadline, tolerance):
        delta = target - current.IvPos
        if abs(delta) > 2_147_483_647:
            raise ValueError("Vertical displacement exceeds Protobuf int32")
        original_increment = self._wait_vertical_increment(None, deadline)
        direction = "u" if delta > 0 else "d"
        try:
            self._set_vertical_increment(abs(delta), deadline)
            self._pending_stop = f"Stoptable_{direction}"
            try:
                movement = _MovementCmd(ISpeed=0, IvPos=0, IhPos=0, IaPos=0, IIndex=0)
                self._bed_request(f"Movetable_s{direction}", movement.SerializeToString())
                while True:
                    position = self._next_position(deadline)
                    if not position.IvMinPos <= target <= position.IvMaxPos:
                        raise RuntimeError("Simulator limits changed during vertical jump")
                    if abs(target - position.IvPos) <= tolerance:
                        break
            finally:
                self.stop()
        finally:
            self._set_vertical_increment(
                original_increment, time.monotonic() + self.timeout
            )
        position = self._settled_position(deadline)
        if abs(target - position.IvPos) > tolerance:
            raise RuntimeError("Vertical target not reached; command not resent")
        return CouchPosition._from_message(position)

    def _move_to(self, axis, target, timeout, tolerance, speed, mode):
        if mode not in ("jump", "step"):
            raise ValueError("mode must be 'jump' or 'step'")
        target = _raw_coordinate(target, "target")
        tolerance = _raw_coordinate(tolerance, "tolerance")
        if tolerance < 0:
            raise ValueError("tolerance must be nonnegative")
        timeout = _positive_seconds(timeout, "timeout")
        horizontal = axis == "horizontal"
        jump = mode == "jump"
        if not jump:
            if isinstance(speed, bool) or not isinstance(speed, int):
                raise TypeError("speed must be an integer")
            if not 10 <= speed <= 2_147_483_647:
                raise ValueError("Target movement speed must be a Protobuf int32 >= 10")
        field = "IhPos" if horizontal else "IvPos"
        minimum = "IhMinPos" if horizontal else "IvMinPos"
        maximum = "IhMaxPos" if horizontal else "IvMaxPos"
        deadline = time.monotonic() + timeout
        current = self._position_snapshot(deadline)
        while True:
            self._remaining(deadline)
            if not getattr(current, minimum) <= target <= getattr(current, maximum):
                raise ValueError(
                    f"{axis} target {target / 10:g} is outside simulator limits "
                    f"[{getattr(current, minimum) / 10:g}, "
                    f"{getattr(current, maximum) / 10:g}] (UI units); "
                    f"current={getattr(current, field) / 10:g}"
                )
            delta = target - getattr(current, field)
            if abs(delta) <= tolerance:
                current = self._settled_position(deadline)
                if abs(target - getattr(current, field)) > tolerance:
                    raise RuntimeError("Position changed while verifying arrival")
                return CouchPosition._from_message(current)
            if jump and not horizontal:
                return self._jump_vertical(target, current, deadline, tolerance)
            direction = ("i" if delta > 0 else "o") if horizontal else (
                "u" if delta > 0 else "d"
            )
            movement = _MovementCmd(ISpeed=0, IvPos=0, IhPos=0, IaPos=0, IIndex=0)
            if jump:
                if abs(delta) > 2_147_483_647:
                    raise ValueError("Horizontal displacement exceeds Protobuf int32")
                movement.IhPos = abs(delta)
                command = f"Movetable_s{direction}"
                stop_distance = tolerance
            else:
                step = max(1, min(speed // 10, abs(delta) // 3))
                movement.ISpeed = step * 10
                command = f"Movetable_{direction}"
                stop_distance = max(tolerance, 2 * step) if step > 1 else tolerance
            self._bed_request("Gantry_hb_movement")
            self._pending_stop = f"Stoptable_{direction}"
            try:
                self._bed_request(command, movement.SerializeToString())
                while True:
                    current = self._next_position(deadline, heartbeat=not jump)
                    if not getattr(current, minimum) <= target <= getattr(current, maximum):
                        raise RuntimeError("Simulator limits changed during movement")
                    remaining = target - getattr(current, field)
                    if jump:
                        if abs(remaining) <= tolerance:
                            break
                    elif remaining * (1 if delta > 0 else -1) <= stop_distance:
                        break
            finally:
                self.stop()
            current = self._settled_position(deadline)
            if jump:
                if abs(target - current.IhPos) > tolerance:
                    raise RuntimeError("Horizontal target not reached; command not resent")
                return CouchPosition._from_message(current)

    def move_to_horizontal(self, target, *, mode="jump", speed=100,
                           timeout=30.0, tolerance=0.1):
        """Move to UI Hor target (Zero-relative) and return verified position.

        mode='jump' sends one relative-step command. mode='step' uses continuous
        movement with feedback-controlled approach; speed applies only to step.
        Requires exclusive control; do not run concurrently with scanning.
        """
        return self._move_to("horizontal", target, timeout, tolerance, speed, mode)

    def move_to_vertical(self, target, *, mode="jump", speed=100,
                         timeout=30.0, tolerance=0.1):
        """Move to UI height and return stable feedback within tolerance.

        mode='jump' temporarily sets vertical increment, executes one step and
        restores the original increment with feedback verification. Restoration
        has a separate cleanup timeout and may extend the operation deadline.
        mode='step' uses continuous movement; speed is the maximum raw ISpeed.
        Unknown/failed commands are never retried. Requires exclusive control
        and a stationary bed on entry; never use alongside another iStation.
        """
        return self._move_to("vertical", target, timeout, tolerance, speed, mode)

    def _move(self, command, stop_command, duration, speed):
        duration = _positive_seconds(duration, "duration")
        if isinstance(speed, bool) or not isinstance(speed, int):
            raise TypeError("speed must be an integer")
        if not 1 <= speed <= 2_147_483_647:
            raise ValueError("speed must be a positive Protobuf int32 value")
        self.connect()
        movement = _MovementCmd(ISpeed=speed, IvPos=0, IhPos=0, IaPos=0, IIndex=0)
        self._bed_request("Gantry_hb_movement")
        self._pending_stop = stop_command
        try:
            self._bed_request(command, movement.SerializeToString())
            deadline = time.monotonic() + duration
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.05, remaining))
                if time.monotonic() < deadline:
                    self._bed_request("Gantry_hb_movement")
        finally:
            self.stop()

    def move_up(self, duration=0.5, *, speed=100):
        """Move up for approximately duration seconds, then send bed stop."""
        self._move("Movetable_u", "Stoptable_u", duration, speed)

    def move_down(self, duration=0.5, *, speed=100):
        """Move down for approximately duration seconds, then send bed stop."""
        self._move("Movetable_d", "Stoptable_d", duration, speed)

    def move_in(self, duration=0.5, *, speed=100):
        """Move into the gantry for approximately duration seconds, then stop."""
        self._move("Movetable_i", "Stoptable_i", duration, speed)

    def move_out(self, duration=0.5, *, speed=100):
        """Move out of the gantry for approximately duration seconds, then stop."""
        self._move("Movetable_o", "Stoptable_o", duration, speed)

    def stop(self):
        """Send bed stop; acknowledgement alone does not prove it has stopped."""
        self._check_open()
        if not self._registered:
            raise RuntimeError("Call connect() before stop()")
        self._bed_request(self._pending_stop or "Stoptable_u")
        self._pending_stop = None

    def _scan_request(self, command):
        self._check_open()
        try:
            with socket.create_connection(
                ("127.0.0.1", 12345), timeout=self.timeout
            ) as connection:
                connection.sendall(struct.pack("<HH", command, 0))
                acknowledgement = connection.recv(1)
                if acknowledgement != b"\x10":
                    raise RuntimeError(
                        f"Unexpected scan acknowledgement: {acknowledgement!r}"
                    )
        except Exception as error:
            raise RuntimeError(
                "Scan command failed; execution is unknown, not resent"
            ) from error

    def scan(self):
        """Send start-scan (TCP command 2); does not wait for completion."""
        self._scan_request(2)

    def abort(self):
        """Send abort-scan (TCP command 4); does not verify the final state."""
        self._scan_request(4)

    def prepare(self):
        """Send prepare/bed-enable (TCP command 3) before scan when needed."""
        self._scan_request(3)

    def close(self):
        """Attempt any pending bed stop, then release local connections."""
        if self._closed:
            return
        try:
            if self._pending_stop is not None:
                self.stop()
        finally:
            if self._tcp_socket is not None:
                self._tcp_socket.close()
                self._tcp_socket = None
            if self._position_socket is not None:
                self._position_socket.close(linger=0)
                self._position_socket = None
            if self._context is not None:
                self._context.term()
                self._context = None
            self._registered = False
            self._closed = True

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

if __name__ == "__main__":
    with SimulatorClient() as sim:
        sim.move_to_horizontal(100)
        sim.move_to_vertical(220)
        # print(sim.move_to_vertical(350, speed=100, timeout=30))
