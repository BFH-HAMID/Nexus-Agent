"""Operator policy for hardware control (``hardware.yaml``): an exact device and command allowlist."""
from __future__ import annotations

import ipaddress
import re
from typing import Any

from .. import policyfile

NAME = "hardware.yaml"
TRANSPORTS = ("serial", "mqtt", "gpio", "mock")
CLASSES = ("read", "actuate", "critical")
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]{0,23})\}")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_PORT = re.compile(r"^(?:/dev/(?:tty|cu\.)[A-Za-z0-9._-]{1,40}|/dev/serial/by-id/[A-Za-z0-9._:-]{1,120}|COM\d{1,3})$")
_HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}))*$")
_TOPIC = re.compile(r"^[^\s#+\x00-\x1f]{1,200}$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_CHOICE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_HEX = re.compile(r"^(?:[0-9a-fA-F]{2}){1,500}$")

DEFAULTS = {
    "schema": 1,
    "enabled": False,
    "dry_run": True,
    "safe_on_stop": True,
    "rate": {"max_per_minute": 20},
    "response": {"max_bytes": 2000, "timeout_ms": 1500},
    "devices": {},
}
_DEVICE_KEYS = {
    "serial": {"port", "baud", "settle_ms"},
    "mqtt": {"host", "port", "tls", "topic", "state_topic", "username_env", "password_env", "qos"},
    "gpio": {"pin", "active_high"},
    "mock": {"reply"},
}
_COMMON_KEYS = {"transport", "description", "commands", "safe"}
_COMMAND_KEYS = {"class", "payload", "payload_hex", "expect_reply", "params", "cooldown_seconds", "state", "pulse_ms"}


class HardwarePolicyError(policyfile.PolicyError):
    """hardware.yaml is invalid or hardware control is OFF."""


def _fail(message: str) -> None:
    raise HardwarePolicyError(f"{NAME}: {message}")


def _keys(value: Any, allowed: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{where} must be a mapping")
    unknown = sorted(str(key) for key in set(value) - allowed)
    if unknown:
        _fail(f"unknown setting '{unknown[0]}' in {where}")
    return value


def _number(value: Any, low: int, high: int, where: str) -> int:
    if type(value) is not int or not low <= value <= high:
        _fail(f"{where} must be a whole number between {low} and {high}")
    return value


def _flag(value: Any, where: str) -> bool:
    if type(value) is not bool:
        _fail(f"{where} must be true or false")
    return value


def _local_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback or address.is_private or address.is_link_local
    except ValueError:
        lowered = host.lower()
        return lowered == "localhost" or "." not in lowered or lowered.endswith((".local", ".lan", ".home.arpa"))


def _params(raw: Any, where: str) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > 4:
        _fail(f"{where} params must be a mapping of at most 4 parameters")
    out = {}
    for name, rule in raw.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", name):
            _fail(f"{where} parameter names use a-z, 0-9 and _")
        rule = _keys(rule, {"min", "max", "choices"}, f"{where} parameter '{name}'")
        if "choices" in rule:
            choices = rule["choices"]
            if ("min" in rule or "max" in rule or not isinstance(choices, list) or not 1 <= len(choices) <= 16
                    or any(not isinstance(item, str) or not _CHOICE.match(item) for item in choices)):
                _fail(f"{where} parameter '{name}' choices must be 1-16 plain words (and no min/max)")
            out[name] = {"choices": list(choices)}
        else:
            low = _number(rule.get("min"), -1_000_000_000, 1_000_000_000, f"{where} parameter '{name}' min")
            high = _number(rule.get("max"), low, 1_000_000_000, f"{where} parameter '{name}' max")
            out[name] = {"min": low, "max": high}
    return out


def _command(transport: str, device: dict[str, Any], name: str, raw: Any, where: str) -> dict[str, Any]:
    raw = _keys(raw, _COMMAND_KEYS, where)
    kind = raw.get("class", "critical")
    if kind not in CLASSES:
        _fail(f"{where} class must be one of {', '.join(CLASSES)}")
    params = _params(raw.get("params"), where)
    out: dict[str, Any] = {"class": kind, "params": params, "payload": None, "payload_hex": None,
                           "cooldown_seconds": _number(raw.get("cooldown_seconds", 0), 0, 3600, f"{where} cooldown_seconds"),
                           "expect_reply": kind == "read" or _flag(raw.get("expect_reply", False), f"{where} expect_reply")}
    if transport == "gpio":
        if set(raw) & {"payload", "payload_hex", "params", "expect_reply"}:
            _fail(f"{where}: gpio commands take 'state' (and optional 'pulse_ms'), not payloads or params")
        if kind == "read":
            if "state" in raw or "pulse_ms" in raw:
                _fail(f"{where}: a gpio read command takes no state")
        else:
            if raw.get("state") not in ("on", "off"):
                _fail(f"{where} state must be \"on\" or \"off\" (quote it)")
            out["state"] = raw["state"]
            out["pulse_ms"] = _number(raw["pulse_ms"], 10, 10_000, f"{where} pulse_ms") if "pulse_ms" in raw else 0
        return out
    if "state" in raw or "pulse_ms" in raw:
        _fail(f"{where}: 'state' and 'pulse_ms' are for gpio devices")
    text, hexed = raw.get("payload"), raw.get("payload_hex")
    if text is not None and hexed is not None:
        _fail(f"{where}: give payload or payload_hex, not both")
    if hexed is not None:
        if not isinstance(hexed, str) or not _HEX.match(hexed) or params:
            _fail(f"{where} payload_hex must be 1-500 bytes of hex and takes no params")
        out["payload_hex"] = hexed.lower()
    elif text is not None:
        if not isinstance(text, str) or not 1 <= len(text) <= 500 or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
            _fail(f"{where} payload must be 1-500 characters of text (use payload_hex for binary)")
        out["payload"] = text
    elif not (kind == "read" and transport in {"mqtt", "mock"}):
        _fail(f"{where} needs a payload")
    used = set(PLACEHOLDER.findall(text or ""))
    if used != set(params):
        _fail(f"{where}: placeholders {sorted(used)} and params {sorted(params)} must match exactly")
    if kind == "read" and transport == "mqtt" and not device.get("state_topic"):
        _fail(f"{where}: an mqtt read command needs the device's state_topic")
    return out


def _device(name: str, raw: Any) -> dict[str, Any]:
    where = f"device '{name}'"
    if not isinstance(raw, dict) or raw.get("transport") not in TRANSPORTS:
        _fail(f"{where} transport must be one of {', '.join(TRANSPORTS)}")
    transport = raw["transport"]
    raw = _keys(raw, _COMMON_KEYS | _DEVICE_KEYS[transport], where)
    out: dict[str, Any] = {"transport": transport, "description": policyfile.clean_text(
        raw.get("description", ""), 120, label=f"{NAME}: {where} description", error=HardwarePolicyError)}
    if transport == "serial":
        if not isinstance(raw.get("port"), str) or not _PORT.match(raw["port"]):
            _fail(f"{where} port must be a local serial device such as /dev/ttyUSB0 or COM3")
        out.update(port=raw["port"], baud=_number(raw.get("baud", 9600), 300, 1_000_000, f"{where} baud"),
                   settle_ms=_number(raw.get("settle_ms", 0), 0, 5000, f"{where} settle_ms"))
    elif transport == "mqtt":
        host = raw.get("host")
        if not isinstance(host, str) or not _HOST.match(host):
            _fail(f"{where} host must be a host name or address")
        tls = _flag(raw.get("tls", True), f"{where} tls")
        if not tls and not _local_host(host):
            _fail(f"{where}: tls: false is only accepted for a broker on this machine or the local network")
        for key in ("topic", "state_topic"):
            value = raw.get(key, "")
            if (value or key == "topic") and (not isinstance(value, str) or not _TOPIC.match(value)):
                _fail(f"{where} {key} must be one exact topic (no wildcards or spaces)")
        for key in ("username_env", "password_env"):
            value = raw.get(key, "")
            if value and (not isinstance(value, str) or not _ENV.match(value)):
                _fail(f"{where} {key} must name an environment variable (for example MQTT_PASSWORD)")
        if bool(raw.get("username_env")) != bool(raw.get("password_env")):
            _fail(f"{where}: give both username_env and password_env, or neither")
        out.update(host=host, port=_number(raw.get("port", 8883 if tls else 1883), 1, 65535, f"{where} port"), tls=tls,
                   topic=raw["topic"], state_topic=raw.get("state_topic", ""), username_env=raw.get("username_env", ""),
                   password_env=raw.get("password_env", ""), qos=_number(raw.get("qos", 1), 0, 1, f"{where} qos"))
    elif transport == "gpio":
        out.update(pin=_number(raw.get("pin"), 0, 40, f"{where} pin"),
                   active_high=_flag(raw.get("active_high", True), f"{where} active_high"))
    else:
        out["reply"] = policyfile.clean_text(raw.get("reply", ""), 500, label=f"{NAME}: {where} reply",
                                             error=HardwarePolicyError)
    commands = raw.get("commands")
    if not isinstance(commands, dict) or not 1 <= len(commands) <= 16:
        _fail(f"{where} needs 1-16 commands")
    out["commands"] = {}
    for command, body in commands.items():
        if not isinstance(command, str) or not _NAME.match(command):
            _fail(f"{where}: command names use a-z, 0-9, - and _ (quote on/off/yes/no: \"on\")")
        out["commands"][command] = _command(transport, out, command, body, f"{where} command '{command}'")
    out["safe"] = ""
    if "safe" in raw:  # YAML reads a bare `off` as false: never let that silently mean "no safe command"
        safe = raw["safe"]
        target = out["commands"].get(safe) if isinstance(safe, str) and safe else None
        if target is None or target["class"] == "read" or target["params"]:
            _fail(f"{where} safe must name one of its own non-read commands without params (quote it: \"off\")")
        out["safe"] = safe
    return out


def _validate(cfg: dict[str, Any]) -> None:
    devices = cfg["devices"]
    if len(devices) > 16:
        _fail("at most 16 devices")
    clean = {}
    for name, raw in devices.items():
        if not isinstance(name, str) or not _NAME.match(name):
            _fail("device names use a-z, 0-9, - and _")
        clean[name] = _device(name, raw)
    cfg["devices"] = clean


POLICY = policyfile.Policy(
    NAME, DEFAULTS, label="Hardware control", error=HardwarePolicyError, free=("devices",), validate=_validate,
    ranges={"rate.max_per_minute": (1, 600), "response.max_bytes": (16, 65_536), "response.timeout_ms": (100, 30_000)},
    limit=64_000,
)
load = POLICY.load
require = POLICY.require
enabled = POLICY.enabled
digest = policyfile.digest
