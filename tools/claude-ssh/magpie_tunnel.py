#!/usr/bin/env python3
"""Run the Magpie SSH tunnel, quota display, Claude setup, and settings rollback.

Dependencies: Python 3.11+ standard library and OpenSSH; no sibling Python modules.
Effects: loopback forwarding and metadata queries; optional backed-up settings updates.
"""

import argparse
import asyncio
from contextlib import suppress
from datetime import datetime
import fcntl
import http.client
import io
import json
import math
import os
import re
from pathlib import Path
import shlex
import shutil
import signal
import socket
import sys
import tempfile
import time


QUOTA_INTERVAL = 180
STALE_AFTER = 600
MAX_RESPONSE = 1024 * 1024
WINDOWS = (("5 hours", "5H"), ("7 days", "Weekly"))


class QuotaError(Exception):
    """A safe diagnostic that contains no gateway response or account identity."""


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def parse_quota(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise QuotaError("invalid quota response")
    cards = [item for item in payload["data"] if isinstance(item, dict)
             and item.get("provider") == "claude" and item.get("kind") == "subscription"]
    if not cards:
        raise QuotaError("no Claude subscription quota")
    card = max(cards, key=lambda item: (item.get("last") is True,
                                      timestamp(item.get("lastServedAt")) or 0))
    entries = card.get("windows")
    if not isinstance(entries, list):
        raise QuotaError("invalid Claude quota windows")
    windows = {}
    for name, label in WINDOWS:
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("name") != name:
                continue
            used = entry.get("used")
            if type(used) not in (int, float) or not 0 <= used <= 100 or not math.isfinite(used):
                continue
            windows[label] = {"used": used, "reset": timestamp(entry.get("resetsAt"))}
            break
    if not windows:
        raise QuotaError("no 5H or Weekly quota")
    return {"windows": windows, "read_at": timestamp(card.get("readAt")),
            "accounts": len(cards), "source_error": bool(card.get("error"))}


class BufferedSocket:
    def __init__(self, data):
        self.data = data

    def makefile(self, *_args):
        return io.BytesIO(self.data)


async def fetch_gateway(port, path="/v1/magpie/quotas", owner_check=None, timeout=5):
    if path not in ("/v1/magpie/quotas", "/v1/models"):
        raise ValueError("unsupported gateway path")
    writer = None
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            if owner_check is not None and not owner_check():
                raise QuotaError("tunnel disconnected")
            writer.write((f"GET {path} HTTP/1.1\r\n"
                          f"Host: 127.0.0.1:{port}\r\n"
                          "Accept: application/json\r\nAccept-Encoding: identity\r\n"
                          "Connection: close\r\n\r\n").encode("ascii"))
            await writer.drain()
            data = bytearray()
            while block := await reader.read(65536):
                data.extend(block)
                if len(data) > MAX_RESPONSE:
                    raise QuotaError("quota response too large")
            # Bound network time and size, then let the standard HTTP parser decode chunks.
            with http.client.HTTPResponse(BufferedSocket(data)) as response:
                response.begin()
                if response.status != 200:
                    raise QuotaError(f"quota HTTP {response.status}")
                return json.loads(response.read())
    except TimeoutError as error:
        raise QuotaError("quota query timed out") from error
    except OSError as error:
        raise QuotaError("gateway unavailable") from error
    except (ValueError, http.client.HTTPException) as error:
        raise QuotaError("invalid quota response") from error
    finally:
        if writer is not None:
            writer.close()
            writer.transport.abort()


async def fetch_quota(port, owner_check=None, timeout=5):
    return parse_quota(await fetch_gateway(port, owner_check=owner_check, timeout=timeout))


def duration(seconds):
    minutes = max(0, math.ceil(seconds / 60))
    if minutes >= 1440:
        return f"{minutes // 1440}d{minutes % 1440 // 60}h"
    if minutes >= 60:
        return f"{minutes // 60}h{minutes % 60:02d}m"
    return f"{minutes}m"


class QuotaState:
    def __init__(self):
        self.snapshot = None
        self.error = ""

    def update(self, snapshot):
        self.snapshot = snapshot
        self.error = ""

    def stale(self, now):
        if self.snapshot is None:
            return False
        read_at = self.snapshot["read_at"]
        return (bool(self.error) or self.snapshot["source_error"] or read_at is None
                or now - read_at > STALE_AFTER or read_at > now + 60
                or any(w["reset"] is not None and w["reset"] <= now
                       for w in self.snapshot["windows"].values()))

    def summary(self, countdown=True, now=None):
        now = time.time() if now is None else now
        windows = self.snapshot["windows"] if self.snapshot is not None else {}
        parts = []
        for _name, label in WINDOWS:
            window = windows.get(label)
            text = f"{label} {window['used']:g}%" if window else f"{label} N/A"
            if window and countdown and window["reset"] is not None:
                remaining = window["reset"] - now
                text += f" ({duration(remaining) if remaining > 0 else 'reset due'})"
            parts.append(text)
        result = " | ".join(parts)
        if self.stale(now):
            result += " [stale]"
        elif self.error:
            result += " [unavailable]"
        return result

    def details(self, now=None):
        now = time.time() if now is None else now
        title = "Claude quota (percent used)"
        if self.snapshot is not None and self.snapshot["accounts"] > 1:
            title += " - last used account"
        lines = [title, self.summary(now=now)]
        if self.snapshot is not None:
            for label, window in self.snapshot["windows"].items():
                if window["reset"] is not None:
                    reset = datetime.fromtimestamp(window["reset"]).astimezone()
                    lines.append(f"{label} resets: {reset:%Y-%m-%d %H:%M:%S %z}")
            read_at = self.snapshot["read_at"]
            if read_at is not None:
                read = datetime.fromtimestamp(read_at).astimezone()
                lines.append(f"Read at: {read:%Y-%m-%d %H:%M:%S %z}")
            else:
                lines.append("Read at: unknown")
        if self.error:
            lines.append(f"Quota unavailable: {self.error}. Keep the tunnel running.")
        return lines


async def print_status(port):
    state = QuotaState()
    try:
        state.update(await fetch_quota(port))
    except QuotaError as error:
        state.error = str(error)
    print("\n".join(state.details()))
    return 0 if state.snapshot is not None else 1


async def print_statusline(port):
    state = QuotaState()
    try:
        state.update(await fetch_quota(port, timeout=2))
    except QuotaError as error:
        state.error = str(error)
    print(state.summary())



MODEL_FAMILIES = ("OPUS", "SONNET", "HAIKU", "FABLE")


def select_primary_models(models):
    primary = {}
    for family in MODEL_FAMILIES:
        candidates = []
        for model in models:
            match = re.fullmatch(r"claude/claude-" + family.lower()
                                 + r"-([0-9]+(?:-[0-9]+)*)(?:-latest)?", model)
            if match is None:
                continue
            parts = match.group(1).split("-")
            # A dated snapshot is not a larger semantic model version.
            date = int(parts.pop()) if len(parts[-1]) == 8 else 0
            version = tuple(int(part) for part in parts)
            candidates.append((version, date == 0, date, model))
        if not candidates:
            raise ValueError(f"no versioned {family} model in catalog; settings were not changed")
        primary[family] = max(candidates)[-1]
    return primary


def state_directory():
    state_root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    if not state_root.is_absolute():
        state_root = Path.home() / ".local/state"
    return state_root / "claude-magpie"


def atomic_json(path, data):
    payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return payload


def configure_claude(catalog, port, settings=None, manifest=None):
    settings = Path(settings) if settings is not None else Path.home() / ".claude/settings.json"
    manifest = (Path(manifest) if manifest is not None
                else state_directory() / "settings-changes.json")
    if not isinstance(catalog, dict) or not isinstance(catalog.get("data"), list):
        raise ValueError("invalid model catalog; settings were not changed")
    models = {item["id"]: item for item in catalog.get("data", [])
              if isinstance(item, dict) and isinstance(item.get("id"), str)
              and item["id"].startswith("claude/")}
    primary = select_primary_models(models)
    original = settings.read_bytes() if settings.exists() else None
    state_original = manifest.read_bytes() if manifest.exists() else None
    data = json.loads(original) if original is not None else {}
    state = json.loads(state_original) if state_original is not None else {"changes": []}
    if not isinstance(data, dict) or not isinstance(data.get("env", {}), dict):
        raise ValueError("Claude settings and env must be JSON objects")
    if not isinstance(state, dict) or not isinstance(state.get("changes"), list):
        raise ValueError("invalid rollback manifest; settings were not changed")
    paths = set()
    for change in state["changes"]:
        if (not isinstance(change, dict) or not isinstance(change.get("path"), list)
                or not change["path"] or not all(isinstance(key, str) for key in change["path"])
                or type(change.get("before_present")) is not bool
                or type(change.get("after_present")) is not bool
                or "before" not in change or "after" not in change
                or tuple(change["path"]) in paths):
            raise ValueError("invalid rollback manifest; settings were not changed")
        paths.add(tuple(change["path"]))

    def set_field(path, value, present=True):
        parent = data
        for key in path[:-1]:
            parent = parent.setdefault(key, {})
            if not isinstance(parent, dict):
                raise ValueError("managed settings parent must be an object")
        key = path[-1]
        before = (key in parent, parent.get(key))
        if before == (present, value):
            return
        change = next((item for item in state["changes"] if item["path"] == path), None)
        if change is None:
            change = {"path": path, "before_present": before[0], "before": before[1]}
            state["changes"].append(change)
        change.update(after_present=present, after=value)
        if present:
            parent[key] = value
        else:
            parent.pop(key, None)

    set_field(["env", "ANTHROPIC_BASE_URL"], f"http://127.0.0.1:{port}")
    set_field(["env", "ANTHROPIC_AUTH_TOKEN"], "magpie")
    for family, model in primary.items():
        set_field(["env", f"ANTHROPIC_DEFAULT_{family}_MODEL"], model)
    for name in ("ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_MODEL_CAPABILITIES"):
        set_field(["env", name], None, present=False)
    set_field(["model"], primary["OPUS"])
    ordered = list(primary.values()) + sorted(set(models) - set(primary.values()))
    overrides = data.get("modelOverrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("modelOverrides must be an object")
    set_field(["modelOverrides"], {**overrides, **{model[7:]: model for model in ordered}})
    options = [{"model": model, "label": models[model].get("display_name") or model,
                "description": "Magpie via SSH"} for model in ordered]
    set_field(["modelPicker", "options"], options)

    statusline_action = "added" if "statusLine" not in data else "preserved"
    managed = any(change.get("path") == ["statusLine"] and change.get("after_present")
                  and data.get("statusLine") == change.get("after") for change in state["changes"])
    if statusline_action == "added" or managed:
        if managed:
            statusline_action = "updated"
        helper = Path(__file__).resolve()
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(helper))} statusline --port {port}"
        set_field(["statusLine"], {"type": "command", "command": command})

    current = settings.read_bytes() if settings.exists() else None
    current_state = manifest.read_bytes() if manifest.exists() else None
    if current != original or current_state != state_original:
        raise ValueError("configuration changed concurrently; settings were not replaced")
    # Persist the undo record before publishing the corresponding settings.
    published_state = atomic_json(manifest, state)
    try:
        atomic_json(settings, data)
    except OSError:
        if manifest.read_bytes() == published_state:
            if state_original is None:
                manifest.unlink()
            else:
                atomic_json(manifest, json.loads(state_original))
        raise
    return statusline_action


def restore_claude(settings=None, manifest=None):
    manifest = Path(manifest) if manifest is not None else state_directory() / 'settings-changes.json'
    settings = Path(settings) if settings is not None else Path.home() / '.claude/settings.json'
    state = json.loads(manifest.read_text())
    original = settings.read_bytes()
    data = json.loads(original)
    conflicts = []
    for change in state['changes']:
        parent = data
        for key in change['path'][:-1]:
            parent = parent.get(key, {})
        key = change['path'][-1]
        current = (key in parent, parent.get(key))
        before = (change['before_present'], change['before'])
        after = (change['after_present'], change['after'])
        if current == before:
            continue
        if current != after:
            conflicts.append('.'.join(change['path']))
            continue
        if change['before_present']:
            parent[key] = change['before']
        else:
            parent.pop(key, None)
    if settings.read_bytes() != original:
        raise SystemExit('Settings changed concurrently; nothing restored.')
    temporary = settings.with_name('settings.json.magpie-restore')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    os.replace(temporary, settings)
    print('Restored unchanged managed fields; other settings preserved.')
    if conflicts:
        print('Later edits preserved: ' + ', '.join(conflicts))
        return 1
    return 0


def byte_size(value):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{value:.0f}B"
        value /= 1024


class Console:
    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self.interactive = self.stream.isatty() and os.environ.get("TERM") != "dumb"
        self.status = ""
        self.drawn = 0

    def width(self):
        return max(0, shutil.get_terminal_size((80, 24)).columns - 1)

    def clear(self):
        if self.drawn:
            self.stream.write("\r" + " " * min(self.drawn, self.width()) + "\r")
            self.stream.flush()
            self.drawn = 0

    def render(self):
        if not self.interactive:
            return
        text = self.status[:self.width()]
        self.stream.write("\r" + text + " " * max(0, min(self.drawn, self.width()) - len(text)))
        self.stream.flush()
        self.drawn = len(text)

    def log(self, message):
        self.clear()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S %z")
        for line in str(message).splitlines():
            self.stream.write(f"[{stamp}] {line}\n")
        self.stream.flush()
        self.render()

    def traffic(self, uploaded, downloaded, up_rate, down_rate, quota=None):
        up = "\u2191" if up_rate > 0 else "-"
        down = "\u2193" if down_rate > 0 else "-"
        self.status = (f"UP {up} {byte_size(up_rate)}/s ({byte_size(uploaded)})  "
                       f"DN {down} {byte_size(down_rate)}/s ({byte_size(downloaded)})")
        if quota is not None:
            rates = f"UP {up} {byte_size(up_rate)}/s  DN {down} {byte_size(down_rate)}/s"
            full, short = quota.summary(), quota.summary(countdown=False)
            options = [self.status + " | " + full, rates + " | " + full,
                       rates + " | " + short, short]
            self.status = next((text for text in options if len(text) <= self.width()), short)
        self.render()


class TrafficRelay:
    def __init__(self, port):
        self.port = port
        self.backend_port = None
        self.backend_owner = None
        self.uploaded = 0
        self.downloaded = 0
        self.server = None
        self.connections = set()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", self.port,
                                                 reuse_address=True)
        self.port = self.server.sockets[0].getsockname()[1]

    async def copy(self, reader, writer, direction):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
            if direction == "up":
                self.uploaded += len(data)
            else:
                self.downloaded += len(data)
        # Preserve responses arriving after a client finishes sending its request.
        writer.write_eof()
        await writer.drain()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        writers = [writer]
        copies = []
        complete = False
        try:
            port, owner = self.backend_port, self.backend_owner
            if port is None or (owner is not None and not owns_listener(owner, port)):
                return
            backend_reader, backend_writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=3)
            writers.append(backend_writer)
            if (port != self.backend_port or owner != self.backend_owner or
                    (owner is not None and not owns_listener(owner, port))):
                return
            copies = [asyncio.create_task(self.copy(reader, backend_writer, "up")),
                      asyncio.create_task(self.copy(backend_reader, writer, "down"))]
            await asyncio.gather(*copies)
            complete = True
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            for copy in copies:
                copy.cancel()
            await asyncio.gather(*copies, return_exceptions=True)
            for stream in writers:
                if complete:
                    stream.close()
                else:
                    stream.transport.abort()
            for stream in writers:
                try:
                    await asyncio.wait_for(stream.wait_closed(), timeout=1)
                except (OSError, asyncio.TimeoutError):
                    stream.transport.abort()
            self.connections.discard(task)

    def disable_backend(self):
        self.backend_port = None
        self.backend_owner = None

    async def reset_backend(self):
        self.disable_backend()
        connections = list(self.connections)
        for connection in connections:
            connection.cancel()
        await asyncio.gather(*connections, return_exceptions=True)

    async def stop(self):
        if self.server is not None:
            self.server.close()
        await self.reset_backend()
        if self.server is not None:
            await self.server.wait_closed()


def owns_listener(pid, port):
    try:
        directory = Path(f"/proc/{pid}")
        sockets = set()
        for descriptor in (directory / "fd").iterdir():
            with suppress(OSError):
                target = descriptor.readlink().as_posix()
                if target.startswith("socket:["):
                    sockets.add(target[8:-1])
        address = f"0100007F:{port:04X}"
        for line in (directory / "net/tcp").read_text().splitlines()[1:]:
            columns = line.split()
            if columns[1] == address and columns[3] == "0A" and columns[9] in sockets:
                return True
    except OSError:
        pass
    return False


def choose_backend_port():
    # The port is only a candidate; SSH must still bind it with ExitOnForwardFailure.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def load_connection(config=None):
    if config is None:
        config = os.environ.get("CLAUDE_MAGPIE_CONFIG")
    if config is None:
        beside_script = Path(__file__).resolve().with_name("connection.json")
        config = beside_script if beside_script.is_file() else state_directory() / "connection.json"
    config = Path(config).expanduser()
    try:
        with config.open() as source:
            data = json.load(source)
        if not all(isinstance(data[key], str) and data[key] and not data[key].startswith("-")
                   and all(c.isalnum() or c in "._-:" for c in data[key])
                   for key in ("host", "user")):
            raise ValueError("invalid host or user")
        if not all(type(data[key]) is int and 1 <= data[key] <= 65535
                   for key in ("ssh_port", "local_port", "remote_port")):
            raise ValueError("invalid port")
        return data
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f"Invalid connection configuration: {error}") from error


async def stop_process(process):
    if process is not None and process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


async def run_tunnel(connection, console, show_usage=True, configure=False):
    stop = asyncio.Event()
    exit_code = 0

    def request_stop(sig):
        nonlocal exit_code
        exit_code = 128 + sig
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, request_stop, sig)
    relay = TrafficRelay(connection["local_port"])
    process = None
    display_task = None
    quota_task = None
    quota = QuotaState()
    quota_refresh = asyncio.Event()
    configure_pending = configure
    stop_task = asyncio.create_task(stop.wait())

    async def display():
        previous = (0, 0, time.monotonic())
        while not stop.is_set():
            up, down, now = relay.uploaded, relay.downloaded, time.monotonic()
            elapsed = max(now - previous[2], 0.001)
            console.traffic(up, down, (up - previous[0]) / elapsed, (down - previous[1]) / elapsed,
                            quota if quota_task is not None else None)
            previous = (up, down, now)
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=0.5)

    async def read_logs(stream):
        while line := await stream.readline():
            console.log(line.decode("utf-8", errors="replace").rstrip("\r\n"))

    async def enable_backend(process, port):
        nonlocal configure_pending
        while process.returncode is None and not stop.is_set():
            if owns_listener(process.pid, port):
                relay.backend_owner = process.pid
                relay.backend_port = port
                console.log("SSH tunnel ready.")
                quota_refresh.set()
                if configure_pending:
                    def current_backend():
                        return ((port, process.pid) == (relay.backend_port, relay.backend_owner)
                                and owns_listener(process.pid, port))

                    for attempt in range(3):
                        try:
                            catalog = await fetch_gateway(port, "/v1/models", owner_check=current_backend)
                            if not current_backend():
                                return
                            action = configure_claude(catalog, relay.port)
                            console.log(f"Claude gateway/models configured; status line {action}. "
                                        "Private field-level rollback recorded.")
                            configure_pending = False
                            break
                        except QuotaError as error:
                            if attempt == 2:
                                console.log(f"Claude configuration pending: {error}. "
                                            "Retry --configure-claude after checking the gateway.")
                            else:
                                await asyncio.sleep(1)
                        except (OSError, ValueError) as error:
                            configure_pending = False
                            console.log(f"Claude configuration failed: {error}")
                            break
                return
            await asyncio.sleep(0.05)

    async def monitor_quota():
        while not stop.is_set():
            quota_refresh.clear()
            port, owner = relay.backend_port, relay.backend_owner

            def current_backend():
                return (port is not None and owner is not None
                        and (port, owner) == (relay.backend_port, relay.backend_owner)
                        and owns_listener(owner, port))

            if current_backend():
                try:
                    snapshot = await fetch_quota(port, owner_check=current_backend)
                    if current_backend():
                        quota.update(snapshot)
                except QuotaError as error:
                    if current_backend():
                        quota.error = str(error)
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(quota_refresh.wait(), timeout=QUOTA_INTERVAL)

    try:
        await relay.start()
        backend_port = choose_backend_port()
        console.log(f"Forwarding localhost:{relay.port} to remote Magpie. Keep this terminal open.")
        console.log("In another terminal run: claude\nCtrl+C stops the tunnel.")
        if show_usage and console.interactive:
            quota_task = asyncio.create_task(monitor_quota())
            console.log("Claude quota: percent used; parentheses show time until reset. Refresh every 3 minutes.")
        display_task = asyncio.create_task(display())
        command = ["ssh", "-NT", "-p", str(connection["ssh_port"]),
                   "-o", "ControlMaster=no", "-o", "ControlPath=none",
                   "-o", "ControlPersist=no", "-o", "ForkAfterAuthentication=no",
                   "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                   "-o", "ConnectTimeout=10", "-o", "ExitOnForwardFailure=yes",
                   "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                   "-L", f"127.0.0.1:{backend_port}:127.0.0.1:{connection['remote_port']}",
                   f"{connection['user']}@{connection['host']}"]
        if connection.get("identity_file"):
            command[1:1] = ["-i", connection["identity_file"], "-o", "IdentitiesOnly=yes"]
        while not stop.is_set():
            process = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            logs = asyncio.create_task(read_logs(process.stdout))
            waiting = asyncio.create_task(process.wait())
            ready = asyncio.create_task(enable_backend(process, backend_port))
            try:
                await asyncio.wait((waiting, stop_task), return_when=asyncio.FIRST_COMPLETED)
                await stop_process(process)
                # Existing sockets can still contain a complete response after SSH exits.
                relay.disable_backend()
                quota.error = "tunnel disconnected"
                quota_refresh.set()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(logs, timeout=1)
            finally:
                for task in (logs, waiting, ready):
                    task.cancel()
                await asyncio.gather(logs, waiting, ready, return_exceptions=True)
            if stop.is_set():
                break
            console.log(f"SSH exited ({process.returncode}); reconnecting in 5 seconds. Ctrl+C to stop.")
            process = None
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=5)
    finally:
        stop.set()
        await stop_process(process)
        await relay.stop()
        tasks = [task for task in (display_task, quota_task, stop_task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        console.clear()
        console.status = ""
        if display_task is not None:
            console.log(f"Traffic total: UP {byte_size(relay.uploaded)}, DN {byte_size(relay.downloaded)}")
    return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("start", "status", "statusline", "restore"),
                        default="start", help="start tunnel, read quota, render Claude status line, or restore settings")
    parser.add_argument("--config", help="connection JSON path; otherwise use environment, sibling file, or saved config")
    parser.add_argument("--port", type=int, help="local gateway port for status/statusline without a connection file")
    parser.add_argument("--no-usage", action="store_true", help="show only traffic on the tunnel status line")
    parser.add_argument("--configure-claude", action="store_true",
                        help="configure Claude gateway, model menu, and quota status line after connecting")
    parser.add_argument("--identity-file", help="SSH private key path; encrypted keys must be loaded in ssh-agent")
    args = parser.parse_args(argv)
    if args.command != "start" and args.configure_claude:
        parser.error("--configure-claude is only available when starting the tunnel")
    if args.port is not None:
        if not 1 <= args.port <= 65535:
            parser.error("port must be between 1 and 65535")
        if args.command not in ("status", "statusline"):
            parser.error("--port is only available with status or statusline")
    console = Console()
    try:
        if args.command == "restore":
            return restore_claude()
        if args.command in ("status", "statusline"):
            port = args.port if args.port is not None else load_connection(args.config)["local_port"]
            if args.command == "statusline":
                asyncio.run(print_statusline(port))
                return 0
            return asyncio.run(print_status(port))
        connection = load_connection(args.config)
        if args.identity_file is not None:
            key = Path(args.identity_file).expanduser().resolve(strict=True)
            if not key.is_file() or not os.access(key, os.R_OK) or key.suffix.lower() == ".pub":
                raise ValueError("--identity-file must be a readable private key file, not a .pub file")
            if key.stat().st_mode & 0o077:
                raise ValueError("Private key permissions are too open; run chmod 600 on the key file")
            connection["identity_file"] = str(key)
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
        lock = runtime / f"claude-magpie-{os.getuid()}-{connection['local_port']}.lock"
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                console.log("This tunnel is already running.")
                return 1
            return asyncio.run(run_tunnel(connection, console, show_usage=not args.no_usage,
                                          configure=args.configure_claude))
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as error:
        console.log(error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
