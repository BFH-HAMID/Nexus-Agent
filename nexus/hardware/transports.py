"""Transports for hardware control: mock, serial, MQTT, GPIO.

Each transport performs exactly one bounded exchange and never retries: after
a failure the physical outcome is unknown, and repeating a command is the
operator's decision.  Optional dependencies are imported only when a transport
is used (``pip install "nexus-agent[hardware]"``).
"""
from __future__ import annotations

import importlib.util
import os
import time
import uuid
from typing import Any

PACKAGES = {"serial": ("serial", "pyserial"), "mqtt": ("paho.mqtt.client", "paho-mqtt"), "gpio": ("gpiozero", "gpiozero")}


class TransportError(ValueError):
    """The exchange failed; the device may or may not have acted."""


def missing(transports: set[str]) -> list[str]:
    """Pip names of optional packages needed by the configured transports but not installed."""
    out = []
    for name in sorted(transports):
        module, package = PACKAGES.get(name, ("", ""))
        if not module:
            continue
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            out.append(package)
    return out


def _need(name: str, module: Any):
    if module is not None:
        return module
    import importlib

    target, package = PACKAGES[name]
    try:
        return importlib.import_module(target)
    except ImportError:
        raise TransportError(f'{name} devices need: pip install "nexus-agent[hardware]" (missing {package})') from None


class MockTransport:
    """In-memory device for trying a configuration: records what it was sent, answers with ``reply``."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes]] = []

    def exchange(self, name: str, device: dict[str, Any], command: dict[str, Any], payload: bytes, *,
                 timeout: float, max_bytes: int) -> bytes | None:
        self.sent.append((name, payload))
        return device.get("reply", "").encode("utf-8")[:max_bytes] if command["expect_reply"] else None


class SerialTransport:
    def __init__(self, module: Any = None) -> None:
        self.module = module

    def exchange(self, name: str, device: dict[str, Any], command: dict[str, Any], payload: bytes, *,
                 timeout: float, max_bytes: int) -> bytes | None:
        serial = _need("serial", self.module)
        try:
            link = serial.serial_for_url(device["port"], baudrate=device["baud"], timeout=timeout,
                                         write_timeout=timeout, do_not_open=True)
            link.open()
        except Exception as exc:  # noqa: BLE001 - pyserial raises several unrelated types
            raise TransportError(f"Could not open {device['port']}: {type(exc).__name__}") from None
        try:
            if device.get("settle_ms"):
                time.sleep(device["settle_ms"] / 1000.0)
            link.reset_input_buffer()
            link.write(payload)
            link.flush()
            if not command["expect_reply"]:
                return None
            return bytes(link.read_until(b"\n", max_bytes))
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"Serial exchange failed ({type(exc).__name__}); the device may or may not have acted") from None
        finally:
            try:
                link.close()
            except Exception:  # noqa: BLE001
                pass


class MqttTransport:
    def __init__(self, module: Any = None, environ: Any = None) -> None:
        self.module = module
        self.environ = os.environ if environ is None else environ

    def exchange(self, name: str, device: dict[str, Any], command: dict[str, Any], payload: bytes, *,
                 timeout: float, max_bytes: int) -> bytes | None:
        mqtt = _need("mqtt", self.module)
        # A fresh, unique client id per exchange: no session is ever resumed or taken over.
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"nexus-hw-{uuid.uuid4().hex[:12]}",
                             clean_session=True, reconnect_on_failure=False)
        if device["username_env"]:
            username, password = self.environ.get(device["username_env"], ""), self.environ.get(device["password_env"], "")
            if not username or not password:
                raise TransportError(f"Set {device['username_env']} and {device['password_env']} in the environment")
            client.username_pw_set(username, password)
        if device["tls"]:
            client.tls_set()  # system trust store with host name verification
        connected: list[bool] = []
        received: list[bytes] = []
        client.on_connect = lambda _c, _u, _f, reason, _p=None: connected.append(reason == 0)

        def on_message(_client, _userdata, message) -> None:
            if message.topic == device["state_topic"]:  # only the one configured topic counts as the reply
                received.append(bytes(message.payload)[:max_bytes])

        client.on_message = on_message
        deadline = time.monotonic() + timeout
        sent = False        # whether the publish was handed to the client: before that, nothing can have reached the device

        def pump(done, problem: str) -> None:
            while not done():
                if time.monotonic() >= deadline or client.loop(timeout=0.05) != mqtt.MQTT_ERR_SUCCESS:
                    raise TransportError(problem)

        try:
            client.connect(device["host"], device["port"], keepalive=max(5, int(timeout) + 5))
            pump(lambda: bool(connected), "MQTT broker did not accept the connection in time; nothing was sent")
            if not connected[0]:
                raise TransportError("MQTT broker refused the connection; nothing was sent")
            if command["class"] == "read":
                client.subscribe(device["state_topic"], qos=device["qos"])
                pump(lambda: bool(received), "No message arrived on the state topic in time")
                return received[0]
            sent = True
            info = client.publish(device["topic"], payload, qos=device["qos"], retain=False)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise TransportError("MQTT publish was refused; nothing was sent")
            pump(info.is_published, "MQTT broker did not confirm the publish in time; the device may or may not have acted")
            return None
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001 - socket, TLS and paho errors
            if sent:
                raise TransportError(f"MQTT exchange failed ({type(exc).__name__}); the device may or may not have acted") from None
            raise TransportError(f"MQTT broker could not be reached ({type(exc).__name__}); nothing was sent") from None
        finally:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass


class GpioTransport:
    """GPIO through gpiozero.

    Output pins stay claimed by this process: a level set here holds only while
    the process that set it is running, and gpiozero releases the pin (back to
    an input) when the process exits.  ``pulse_ms`` is the one operation that is
    complete within a single call.
    """

    _held: dict[int, Any] = {}

    def __init__(self, module: Any = None, sleep=time.sleep) -> None:
        self.module = module
        self.sleep = sleep

    def exchange(self, name: str, device: dict[str, Any], command: dict[str, Any], payload: bytes, *,
                 timeout: float, max_bytes: int) -> bytes | None:
        gpio = _need("gpio", self.module)
        pin = device["pin"]
        try:
            if command["class"] == "read":
                held = self._held.get(pin)
                if held is not None:
                    return b"1" if held.value else b"0"
                probe = gpio.DigitalInputDevice(pin, pull_up=None, active_state=device["active_high"])
                try:
                    return b"1" if probe.value else b"0"
                finally:
                    probe.close()
            line = self._held.get(pin)
            if line is None:
                line = self._held[pin] = gpio.DigitalOutputDevice(pin, active_high=device["active_high"], initial_value=None)
            wanted = command["state"] == "on"
            (line.on if wanted else line.off)()
            if command.get("pulse_ms"):
                try:
                    self.sleep(command["pulse_ms"] / 1000.0)
                finally:
                    (line.off if wanted else line.on)()
            return None
        except Exception as exc:  # noqa: BLE001 - pin factory errors vary by board
            raise TransportError(f"GPIO pin {pin} failed ({type(exc).__name__}: {str(exc)[:80]})") from None

    @classmethod
    def release_all(cls) -> None:
        for pin, line in list(cls._held.items()):
            try:
                line.close()
            except Exception:  # noqa: BLE001
                pass
            cls._held.pop(pin, None)


def default_transports() -> dict[str, Any]:
    return {"mock": MockTransport(), "serial": SerialTransport(), "mqtt": MqttTransport(), "gpio": GpioTransport()}
