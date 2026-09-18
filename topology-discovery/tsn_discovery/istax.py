"""ISTAX CLI transport — the fallback collector.

This exists because the NETCONF server on the KSwitch D10 stopped starting
after the GA-3.06 firmware update, even though `netconf server` is still
present in the running configuration. That is a firmware regression, not a
configuration problem, and it is the manufacturer's to fix. Until it is
fixed the testbed still needs its topology and capabilities, so this module
collects the same information over the ISTAX CLI.

**It is a fallback, not a replacement.** `cli.py` probes NETCONF first on
every switch and only drops to this path when NETCONF does not answer. When
NETCONF works it is used, because it is schema-defined, versioned, portable
and transactional, and this is none of those things — it is screen-scraping
a human interface whose format can change without notice between releases.

Three properties carry over from the NETCONF collector deliberately:

1. **It never raises at the caller.** Every failure becomes a structured
   record so one unreachable switch does not abort the other four.
2. **Every command's output is written to disk verbatim** before anything
   parses it, so a wrong parser costs a re-analysis and not another trip to
   the bench.
3. **Nothing is written to the switch.** Only `show` commands are issued.
   There is no configuration command anywhere in this module, and
   `_REFUSED` below is enforced at the point of execution rather than left
   to convention.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

try:
    import paramiko
    PARAMIKO_AVAILABLE = True
    PARAMIKO_IMPORT_ERROR = None
except Exception as exc:                                    # pragma: no cover
    PARAMIKO_AVAILABLE = False
    PARAMIKO_IMPORT_ERROR = str(exc)
    paramiko = None                                          # type: ignore


# ISTAX prompt: "KSwitchTSN-1# " at the start of a line, and "(config)#"
# variants which we should never see because we never enter config mode.
PROMPT_RE = re.compile(r"(?:^|[\r\n])([\w.\-]+(?:\([^)]*\))?[#>])\s*$")

# "-- more --, next page: Space, continue: g, quit: ^C"
# 'g' continues to the end of the output without further prompting, which is
# exactly what a scraper wants. Space would page one screen at a time.
MORE_RE = re.compile(r"--\s*more\s*--[^\r\n]*", re.IGNORECASE)
MORE_CONTINUE = "g"

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|[\x00\x07\x08]")

# SHA-1 era algorithms, prepended only on a retry after normal negotiation
# failed. See IstaxSession._open_legacy for why and when.
LEGACY_KEX = (
    "diffie-hellman-group14-sha1",
    "diffie-hellman-group-exchange-sha1",
    "diffie-hellman-group1-sha1",
)
LEGACY_HOST_KEYS = ("ssh-rsa", "ssh-dss")

# Any command that is not a read is refused before it reaches the wire.
_ALLOWED_PREFIXES = ("show ", "do show ")
_REFUSED = re.compile(
    r"^\s*(conf|configure|write|copy|reload|no\b|set\b|clear|erase|delete|"
    r"debug|platform|firmware|username|interface\b)", re.IGNORECASE)


class CommandRefused(Exception):
    """Raised when something asks this module to run a non-`show` command."""


@dataclass
class CliCapture:
    key: str
    command: str
    ok: bool
    text: Optional[str] = None
    error: Optional[str] = None
    error_kind: Optional[str] = None
    duration_s: float = 0.0
    optional: bool = False       # a failure here is informative, not a fault


@dataclass
class CliResult:
    """Everything one switch gave us over the CLI, shaped to mirror
    ``netconf.SwitchResult`` so downstream code can treat them alike."""
    name: str
    host: str
    port: int = 22
    transport: str = "cli"
    reachable: bool = False
    connect_error: Optional[str] = None
    connect_error_kind: Optional[str] = None
    prompt: Optional[str] = None
    firmware: Optional[str] = None
    captures: Dict[str, CliCapture] = field(default_factory=dict)
    raw_dir: Optional[str] = None
    duration_s: float = 0.0

    def text_of(self, key: str) -> Optional[str]:
        cap = self.captures.get(key)
        return cap.text if cap and cap.ok else None

    def errors(self) -> List[dict]:
        out = []
        if not self.reachable:
            out.append({
                "scope": "connect",
                "kind": self.connect_error_kind or "unknown",
                "message": self.connect_error or "not reachable",
                "transport": "cli",
            })
        for key, cap in self.captures.items():
            if not cap.ok:
                out.append({
                    "scope": key,
                    "kind": cap.error_kind or "unknown",
                    "message": cap.error or "failed",
                    "transport": "cli",
                })
        return out


def classify_exception(exc: Exception) -> str:
    name = type(exc).__name__
    msg = str(exc).lower()
    if PARAMIKO_AVAILABLE and isinstance(exc, paramiko.AuthenticationException):
        return "auth-failed"
    if "timed out" in msg or "timeout" in msg or name == "TimeoutError":
        return "timeout"
    if "refused" in msg:
        return "connection-refused"
    if "no route" in msg or "unreachable" in msg:
        return "unreachable"
    if "no matching" in msg or "kex" in msg or "key exchange" in msg:
        return "ssh-algorithm-mismatch"
    if "banner" in msg:
        return "ssh-banner"
    if PARAMIKO_AVAILABLE and isinstance(exc, paramiko.SSHException):
        return "ssh-error"
    return "error"


# --- interface naming ----------------------------------------------------
# The CLI is inconsistent with itself: `show interface * status` prints
# "Gi 1/1" and "2.5G 1/1", `show lldp neighbors` prints
# "GigabitEthernet 1/5", and `show mac address-table` prints ranges like
# "GigabitEthernet 1/1-6 2.5GigabitEthernet 1/1-2 CPU".
#
# NETCONF uses the short form ("Gi 1/5", "2.5G 1/1") as the ietf-interfaces
# key, so the short form is canonical here too. That is what makes a CLI
# record and a NETCONF record comparable port-for-port.

_LONG_TO_SHORT = [
    (re.compile(r"^2\.5\s*GigabitEthernet\s*", re.I), "2.5G "),
    (re.compile(r"^10\s*GigabitEthernet\s*", re.I), "10G "),
    (re.compile(r"^25\s*GigabitEthernet\s*", re.I), "25G "),
    (re.compile(r"^GigabitEthernet\s*", re.I), "Gi "),
    (re.compile(r"^FastEthernet\s*", re.I), "Fa "),
]
_SHORT_TO_LONG = [
    (re.compile(r"^2\.5G\s+", re.I), "2.5GigabitEthernet "),
    (re.compile(r"^10G\s+", re.I), "10GigabitEthernet "),
    (re.compile(r"^25G\s+", re.I), "25GigabitEthernet "),
    (re.compile(r"^Gi\s+", re.I), "GigabitEthernet "),
    (re.compile(r"^Fa\s+", re.I), "FastEthernet "),
]


def canonical_ifname(name: Optional[str]) -> Optional[str]:
    """Any CLI spelling -> the short form NETCONF uses ("Gi 1/5")."""
    if not name:
        return None
    n = " ".join(str(name).split())
    for pattern, repl in _LONG_TO_SHORT:
        if pattern.match(n):
            return pattern.sub(repl, n)
    return n


def cli_ifname(name: Optional[str]) -> Optional[str]:
    """Canonical short form -> the long form to type at the CLI.

    The CLI accepts abbreviations, but the long form is what the
    application notes use and is unambiguous, so commands are built with it.
    """
    if not name:
        return None
    n = " ".join(str(name).split())
    for pattern, repl in _SHORT_TO_LONG:
        if pattern.match(n):
            return pattern.sub(repl, n)
    return n


_RANGE_RE = re.compile(
    r"((?:\d+\.?\d*\s*)?[A-Za-z]+(?:Ethernet)?)\s+(\d+)/(\d+)(?:-(\d+))?", re.I)


def expand_port_list(value: Optional[str]) -> List[str]:
    """Expand "Gi 1/1-6 2.5G 1/1-2 CPU" into canonical interface names.

    Non-interface tokens such as ``CPU`` are dropped: they are not bridge
    ports and carrying them into the topology would invent adjacencies.
    """
    if not value:
        return []
    out: List[str] = []
    for m in _RANGE_RE.finditer(value):
        prefix, unit, first, last = m.group(1), m.group(2), m.group(3), m.group(4)
        base = canonical_ifname(f"{prefix} {unit}/{first}")
        if base is None:
            continue
        head = base.rsplit("/", 1)[0]
        lo, hi = int(first), int(last) if last else int(first)
        for n in range(lo, hi + 1):
            out.append(f"{head}/{n}")
    return out


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


class IstaxSession:
    """One SSH session to a switch's ISTAX CLI.

    ISTAX has no `exec_command` subsystem worth relying on, so this drives
    an interactive shell: send a command, read until the prompt returns,
    answering the pager along the way.
    """

    def __init__(
        self,
        name: str,
        host: str,
        port: int = 22,
        username: str = "admin",
        password: str = "",
        timeout: int = 30,
        command_timeout: int = 25,
        raw_dir: Optional[str] = None,
        logger=None,
        legacy_algorithms: bool = True,
    ):
        self.name = name
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout
        self.command_timeout = command_timeout
        self.raw_dir = raw_dir
        self.log = logger or (lambda *a, **k: None)
        self.legacy_algorithms = legacy_algorithms
        self.client = None
        self.chan = None
        self.result = CliResult(name=name, host=host, port=port, raw_dir=raw_dir)

    # --- lifecycle -------------------------------------------------------
    def __enter__(self) -> "IstaxSession":
        self.connect()
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    def _open_modern(self):
        """Normal path: paramiko's own algorithm preferences."""
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=self.host, port=self.port,
            username=self.username, password=self.password,
            timeout=self.timeout, allow_agent=False, look_for_keys=False,
        )
        return self.client.invoke_shell(width=200, height=1000)

    def _open_legacy(self):
        """Retry with the SHA-1 algorithms embedded switches still offer.

        Recent paramiko and OpenSSH de-prioritise or drop `ssh-rsa` and the
        SHA-1 key exchanges. Older switch firmware often offers nothing
        else, which is why `ssh admin@<switch>` from a current Ubuntu can
        fail outright on key exchange. Prepending them to the transport's
        preference lists is the same thing `-oKexAlgorithms=+...` does for
        OpenSSH.

        This reaches into paramiko's private preference attributes because
        there is no public API for it. It is guarded: it runs only after a
        normal negotiation has already failed, and only when
        `legacy_algorithms` is on. Appropriate for an isolated lab
        management network; it weakens negotiation and should not be used
        on a routed one.
        """
        transport = paramiko.Transport((self.host, self.port))
        try:
            for attr, extra in (
                ("_preferred_kex", LEGACY_KEX),
                ("_preferred_keys", LEGACY_HOST_KEYS),
            ):
                current = tuple(getattr(transport, attr, ()) or ())
                merged = tuple(dict.fromkeys(current + extra))
                setattr(transport, attr, merged)
            transport.banner_timeout = self.timeout
            transport.connect(username=self.username, password=self.password)
            self.client = transport            # closed the same way
            chan = transport.open_session()
            chan.get_pty(width=200, height=1000)
            chan.invoke_shell()
            return chan
        except Exception:
            try:
                transport.close()
            except Exception:
                pass
            raise

    def connect(self) -> bool:
        if not PARAMIKO_AVAILABLE:
            self.result.connect_error = (
                f"paramiko is not installed ({PARAMIKO_IMPORT_ERROR}). "
                "Run scripts/setup_env.sh.")
            self.result.connect_error_kind = "no-paramiko"
            return False

        t0 = time.time()
        first_error = None
        try:
            try:
                self.chan = self._open_modern()
            except Exception as exc:
                kind = classify_exception(exc)
                if not (self.legacy_algorithms
                        and kind in ("ssh-algorithm-mismatch", "ssh-error")):
                    raise
                first_error = exc
                self.log(f"  [{self.name}] SSH negotiation failed ({kind}); "
                         "retrying with legacy algorithms")
                self.close()
                self.chan = self._open_legacy()
                self.result.transport = "cli"

            self.chan.settimeout(self.command_timeout)
            banner = self._read_until_prompt(self.command_timeout)
            self.result.prompt = self._last_prompt(banner)
            self.result.reachable = True
            self.log(f"  [{self.name}] CLI session open "
                     f"(prompt {self.result.prompt!r})"
                     + (" via legacy algorithms" if first_error else ""))
            return True
        except Exception as exc:
            self.result.reachable = False
            self.result.connect_error = f"{type(exc).__name__}: {exc}"
            self.result.connect_error_kind = classify_exception(exc)
            self.log(f"  [{self.name}] CLI connect failed "
                     f"({self.result.connect_error_kind}): {exc}")
            return False
        finally:
            self.result.duration_s += time.time() - t0

    def close(self) -> None:
        for obj in (self.chan, self.client):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self.chan = None
        self.client = None

    # --- reading ---------------------------------------------------------
    @staticmethod
    def _last_prompt(text: str) -> Optional[str]:
        m = PROMPT_RE.search(text or "")
        return m.group(1) if m else None

    def _read_until_prompt(self, timeout: int) -> str:
        buf = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.chan.recv_ready():
                chunk = self.chan.recv(65535).decode("utf-8", errors="replace")
                buf += chunk
                clean = strip_ansi(buf)
                if MORE_RE.search(clean):
                    # Answer the pager and drop its prompt from the buffer,
                    # so it never reaches a parser.
                    self.chan.send(MORE_CONTINUE)
                    buf = MORE_RE.sub("", clean)
                    deadline = time.time() + timeout   # output is still coming
                    continue
                if PROMPT_RE.search(clean):
                    return clean
            else:
                time.sleep(0.05)
        raise TimeoutError(
            f"no prompt within {timeout}s (got {len(buf)} bytes)")

    # --- command execution -----------------------------------------------
    def _record(self, cap: CliCapture) -> CliCapture:
        self.result.captures[cap.key] = cap
        if cap.ok and cap.text is not None and self.raw_dir:
            os.makedirs(self.raw_dir, exist_ok=True)
            try:
                with open(os.path.join(self.raw_dir, f"{cap.key}.txt"),
                          "w", encoding="utf-8") as fh:
                    fh.write(f"! command: {cap.command}\n{cap.text}")
            except OSError as exc:
                cap.error = f"raw capture not written: {exc}"
        return cap

    def run(self, key: str, command: str,
            optional: bool = False) -> CliCapture:
        """Run one `show` command and capture its output.

        Refuses anything that is not a read. This module has no business
        changing a switch, and the check lives here rather than in a comment
        so that a future edit cannot quietly make it possible.
        """
        cmd = command.strip()
        if _REFUSED.match(cmd) or not cmd.lower().startswith(_ALLOWED_PREFIXES):
            raise CommandRefused(
                f"istax.py issues read-only commands; refused: {cmd!r}")

        cap = CliCapture(key=key, command=cmd, ok=False, optional=optional)
        if self.chan is None:
            cap.error = "no session"
            cap.error_kind = "no-session"
            return self._record(cap)

        t0 = time.time()
        try:
            while self.chan.recv_ready():          # drain anything stale
                self.chan.recv(65535)
            self.chan.send(cmd + "\n")
            raw = self._read_until_prompt(self.command_timeout)
            cap.text = self._clean(raw, cmd)
            if self._looks_like_cli_error(cap.text):
                # The firmware rejected the command. On an optional read
                # that is a discovery result in itself -- this release does
                # not have that command -- not a failure of the run.
                first = (cap.text.strip().splitlines() or ["rejected"])[0]
                cap.ok = False
                cap.error = first.strip()
                cap.error_kind = "command-unsupported"
            else:
                cap.ok = True
        except Exception as exc:
            cap.error = f"{type(exc).__name__}: {exc}"
            cap.error_kind = classify_exception(exc)
        finally:
            cap.duration_s = time.time() - t0
            self.result.duration_s += cap.duration_s
        return self._record(cap)

    @staticmethod
    def _clean(raw: str, command: str) -> str:
        """Strip the echoed command and the trailing prompt."""
        text = strip_ansi(raw)
        lines = text.split("\n")
        # drop the echo of the command itself (first line containing it)
        for i, line in enumerate(lines[:3]):
            if command in line:
                lines = lines[i + 1:]
                break
        # drop the trailing prompt line
        while lines and (PROMPT_RE.search(lines[-1]) or not lines[-1].strip()):
            lines.pop()
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _looks_like_cli_error(text: Optional[str]) -> bool:
        if not text:
            return False
        head = text.strip().lower()
        return head.startswith((
            "% invalid", "% unknown", "% incomplete", "% ambiguous",
            "invalid input", "unknown command", "syntax error",
        )) or "% invalid input" in head[:200]


# --- the standard collection --------------------------------------------

# Commands that describe the whole switch. `show running-config all-defaults`
# is requested as well because AN1185 §6 notes that port-level frame
# preemption "is disabled by default and not shown unless the 'all-defaults'
# option is used" -- so the plain running-config under-reports Qbu.
GLOBAL_COMMANDS = [
    ("running-config", "show running-config"),
    ("interface-status", "show interface * status"),
    ("lldp-neighbors", "show lldp neighbors"),
    ("mac-address-table", "show mac address-table"),
    ("vlan", "show vlan"),
    ("tas-status", "show tsn tas status"),
    ("frame-preemption-status", "show tsn frame-preemption status"),
    ("ptp-default", "show ptp 0 default"),
    ("ptp-current", "show ptp 0 current"),
    ("ptp-parent", "show ptp 0 parent"),
    ("ptp-time-property", "show ptp 0 time-property"),
    ("ptp-port-state", "show ptp 0 port-state"),
    ("ptp-slave", "show ptp 0 slave"),
    ("tsn-current-time", "show tsn current-time"),
    # Capability probes. These two have no YANG module on this platform at
    # all, so whether the command exists is the only way to learn whether
    # the feature does.
    ("psfp-status", "show tsn stream filter status"),
    ("frer-status", "show tsn frer"),
]

PER_INTERFACE_COMMANDS = [
    ("tas-status", "show tsn tas status interface {cli_if}"),
    ("frame-preemption-status",
     "show tsn frame-preemption status interface {cli_if}"),
    ("qos", "show qos interface {cli_if}"),
    ("ptp-port-state", "show ptp 0 port-state interface {cli_if}"),
]


def collect(session: IstaxSession, interface_lister=None,
            all_defaults: bool = False, per_interface: bool = True) -> CliResult:
    """Run the standard read set against one connected switch.

    Strategy for the per-port data (Qbv, Qbu, QoS, PTP port state): try the
    switch-wide form of each command first, because one round trip beats
    eight. Fall back to the per-interface form only for what the global form
    did not return. AN1185 documents only the per-interface spelling, so the
    global form may not exist on every release -- hence trying, not
    assuming.

    ``interface_lister`` is a callable taking this ``CliResult`` and
    returning canonical interface names. It is injected rather than
    imported so that the transport module stays free of the parsers, which
    import it.
    """
    if not session.result.reachable:
        return session.result

    for key, cmd in GLOBAL_COMMANDS:
        session.run(key, cmd, optional=True)

    if all_defaults:
        session.run("running-config-all-defaults",
                    "show running-config all-defaults", optional=True)

    # Firmware version, for the record: it is the thing that changed when
    # NETCONF stopped working, so every CLI run should state it.
    rc = session.result.text_of("running-config") or ""
    m = re.search(r"System Description\s*:\s*(\S+)", rc)
    if m:
        session.result.firmware = m.group(1)

    interfaces: List[str] = []
    if per_interface and interface_lister is not None:
        try:
            interfaces = list(interface_lister(session.result) or [])
        except Exception as exc:                       # a parser bug must not
            session.log(f"  [{session.name}] interface list unavailable: {exc}")
            interfaces = []

    if not interfaces:
        return session.result

    for canonical in interfaces:
        cli_if = cli_ifname(canonical)
        slug = canonical.replace(" ", "_").replace("/", "-")
        for key, template in PER_INTERFACE_COMMANDS:
            global_cap = session.result.captures.get(key)
            # If the switch-wide form already returned this interface's
            # block, do not ask again.
            if (global_cap and global_cap.ok and global_cap.text
                    and _mentions_interface(global_cap.text, canonical, cli_if)):
                continue
            session.run(f"{key}--{slug}", template.format(cli_if=cli_if),
                        optional=True)

    return session.result


def _mentions_interface(text: str, canonical: str, cli_if: Optional[str]) -> bool:
    for form in filter(None, (canonical, cli_if)):
        if re.search(rf"\b{re.escape(form)}\b", text):
            return True
    return False


def probe_reachable(host: str, port: int = 22, timeout: float = 3.0) -> bool:
    """Cheap TCP check before spending an SSH handshake."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
