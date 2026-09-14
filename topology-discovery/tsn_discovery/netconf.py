"""NETCONF session handling.

Wraps ncclient with the three properties the rest of this tool depends on:

1. **It never raises at the caller.** A switch that is powered off, has the
   NETCONF server disabled, or drops the session mid-transfer must not abort
   discovery of the other four. Every failure is captured as a structured
   error record and discovery continues.

2. **Every reply is written to disk verbatim** before anything tries to
   parse it. If a parser is wrong -- and against a firmware we cannot test
   here, some parser eventually will be -- the raw capture is what lets the
   run be re-analysed offline without touching the hardware again.

3. **Filters are built from namespaces the switch itself advertised** in its
   NETCONF ``<hello>``, falling back to the standard URIs only when the
   switch says nothing. Hard-coding vendor namespaces is how this kind of
   tool rots between firmware releases.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:
    from ncclient import manager
    from ncclient.operations import RPCError
    from ncclient.transport.errors import (
        AuthenticationError,
        SSHError,
        TransportError,
    )
    NCCLIENT_AVAILABLE = True
    NCCLIENT_IMPORT_ERROR = None
except Exception as exc:                                   # pragma: no cover
    NCCLIENT_AVAILABLE = False
    NCCLIENT_IMPORT_ERROR = str(exc)
    manager = None                                          # type: ignore
    RPCError = Exception                                    # type: ignore
    AuthenticationError = SSHError = TransportError = Exception  # type: ignore


# Standard namespaces, used only when the switch's <hello> does not name one.
DEFAULT_NAMESPACES: Dict[str, str] = {
    "ietf-system": "urn:ietf:params:xml:ns:yang:ietf-system",
    "ietf-interfaces": "urn:ietf:params:xml:ns:yang:ietf-interfaces",
    "ietf-ip": "urn:ietf:params:xml:ns:yang:ietf-ip",
    "ietf-netconf-monitoring": "urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring",
    "ieee802-dot1ab-lldp": "urn:ieee:std:802.1AB:yang:ieee802-dot1ab-lldp",
    "ieee802-dot1q-bridge": "urn:ieee:std:802.1Q:yang:ieee802-dot1q-bridge",
    "ieee802-dot1q-sched": "urn:ieee:std:802.1Q:yang:ieee802-dot1q-sched",
    "ieee802-dot1q-preemption": "urn:ieee:std:802.1Q:yang:ieee802-dot1q-preemption",
    "ieee802-ethernet-interface": "urn:ieee:std:802.3:yang:ieee802-ethernet-interface",
}

# Capability URIs carry the module name and revision as query parameters:
#   urn:ieee:std:802.1AB:yang:ieee802-dot1ab-lldp?module=ieee802-dot1ab-lldp&revision=2018-11-13
CAP_MODULE_RE = re.compile(r"[?&]module=([^&]+)")
CAP_REVISION_RE = re.compile(r"[?&]revision=([^&]+)")
CAP_FEATURES_RE = re.compile(r"[?&]features=([^&]+)")
CAP_DEVIATIONS_RE = re.compile(r"[?&]deviations=([^&]+)")


@dataclass
class Capture:
    """One NETCONF exchange and its result."""
    key: str
    operation: str                       # "get" | "get-config"
    filter_xml: Optional[str]
    ok: bool
    xml: Optional[str] = None
    error: Optional[str] = None
    error_kind: Optional[str] = None
    duration_s: float = 0.0
    bytes: int = 0


@dataclass
class SwitchResult:
    """Everything one switch gave us in one run."""
    name: str
    host: str
    port: int
    reachable: bool = False
    connect_error: Optional[str] = None
    connect_error_kind: Optional[str] = None
    session_id: Optional[str] = None
    server_capabilities: List[str] = field(default_factory=list)
    modules: Dict[str, dict] = field(default_factory=dict)
    namespaces: Dict[str, str] = field(default_factory=dict)
    captures: Dict[str, Capture] = field(default_factory=dict)
    raw_dir: Optional[str] = None
    duration_s: float = 0.0

    def xml_of(self, key: str) -> Optional[str]:
        cap = self.captures.get(key)
        return cap.xml if cap and cap.ok else None

    def errors(self) -> List[dict]:
        out = []
        if not self.reachable:
            out.append({
                "scope": "connect",
                "kind": self.connect_error_kind or "unknown",
                "message": self.connect_error or "not reachable",
            })
        for key, cap in self.captures.items():
            if not cap.ok:
                out.append({
                    "scope": key,
                    "kind": cap.error_kind or "unknown",
                    "message": cap.error or "failed",
                })
        return out


def classify_exception(exc: Exception) -> str:
    """Map a transport failure onto something an operator can act on."""
    if isinstance(exc, AuthenticationError):
        return "auth-failed"
    if isinstance(exc, RPCError):
        return "rpc-error"
    name = type(exc).__name__
    msg = str(exc).lower()
    if "timed out" in msg or "timeout" in msg or name == "TimeoutExpiredError":
        return "timeout"
    if "refused" in msg:
        return "connection-refused"
    if "no route" in msg or "unreachable" in msg:
        return "unreachable"
    if "host key" in msg or "hostkey" in msg:
        return "hostkey-rejected"
    if isinstance(exc, (SSHError, TransportError)):
        return "ssh-error"
    return "error"


def parse_capabilities(caps: List[str]) -> Tuple[Dict[str, dict], Dict[str, str]]:
    """Split a ``<hello>`` capability list into modules and namespaces.

    Returns ``(modules, namespaces)`` where *modules* maps module name to
    ``{revision, features, deviations, capability}`` and *namespaces* maps
    module name to the namespace URI the switch uses for it.
    """
    modules: Dict[str, dict] = {}
    namespaces: Dict[str, str] = {}
    for cap in caps:
        m = CAP_MODULE_RE.search(cap)
        if not m:
            continue
        module = m.group(1)
        rev = CAP_REVISION_RE.search(cap)
        feats = CAP_FEATURES_RE.search(cap)
        devs = CAP_DEVIATIONS_RE.search(cap)
        ns = cap.split("?", 1)[0]
        modules[module] = {
            "revision": rev.group(1) if rev else None,
            "features": feats.group(1).split(",") if feats else [],
            "deviations": devs.group(1).split(",") if devs else [],
            "namespace": ns,
            "capability": cap,
        }
        namespaces[module] = ns
    return modules, namespaces


class SwitchSession:
    """One NETCONF session against one switch."""

    def __init__(
        self,
        name: str,
        host: str,
        port: int = 830,
        username: str = "netconf",
        password: str = "geheim",
        timeout: int = 30,
        raw_dir: Optional[str] = None,
        logger=None,
    ):
        self.name = name
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout
        self.raw_dir = raw_dir
        self.log = logger or (lambda *a, **k: None)
        self.mgr = None
        self.result = SwitchResult(name=name, host=host, port=port, raw_dir=raw_dir)

    # --- lifecycle -------------------------------------------------------
    def __enter__(self) -> "SwitchSession":
        self.connect()
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False                      # never swallow caller exceptions

    def connect(self) -> bool:
        if not NCCLIENT_AVAILABLE:
            self.result.connect_error = (
                f"ncclient is not installed ({NCCLIENT_IMPORT_ERROR}). "
                "Run scripts/setup_env.sh."
            )
            self.result.connect_error_kind = "no-ncclient"
            return False

        t0 = time.time()
        try:
            self.mgr = manager.connect(
                host=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=self.timeout,
                hostkey_verify=False,     # lab network, switch keys are not managed
                allow_agent=False,
                look_for_keys=False,
                device_params={"name": "default"},
            )
            self.result.reachable = True
            self.result.session_id = str(getattr(self.mgr, "session_id", "") or "")
            caps = [str(c) for c in self.mgr.server_capabilities]
            self.result.server_capabilities = sorted(caps)
            modules, namespaces = parse_capabilities(caps)
            self.result.modules = modules
            self.result.namespaces = namespaces
            self._save_hello()
            self.log(f"  [{self.name}] connected, session {self.result.session_id}, "
                     f"{len(modules)} YANG modules advertised")
            return True
        except Exception as exc:
            self.result.reachable = False
            self.result.connect_error = f"{type(exc).__name__}: {exc}"
            self.result.connect_error_kind = classify_exception(exc)
            self.log(f"  [{self.name}] connect failed "
                     f"({self.result.connect_error_kind}): {exc}")
            return False
        finally:
            self.result.duration_s += time.time() - t0

    def _save_hello(self) -> None:
        """Persist the ``<hello>`` capability list alongside the raw XML.

        The capability list is the primary evidence for which YANG modules a
        switch implements, and it arrives on the transport rather than in an
        RPC reply -- so it would be lost from the raw capture. Saving it here
        is what makes ``--reanalyse`` produce the same answer as the live run.
        """
        if not self.raw_dir:
            return
        try:
            import json
            os.makedirs(self.raw_dir, exist_ok=True)
            with open(os.path.join(self.raw_dir, "hello-capabilities.json"),
                      "w", encoding="utf-8") as fh:
                json.dump({
                    "host": self.host,
                    "session_id": self.result.session_id,
                    "capabilities": self.result.server_capabilities,
                }, fh, indent=2)
                fh.write("\n")
        except OSError:
            pass

    def close(self) -> None:
        if self.mgr is not None:
            try:
                self.mgr.close_session()
            except Exception:
                pass                      # a switch that hung up is not an error
            self.mgr = None

    # --- filters ---------------------------------------------------------
    def ns(self, module: str) -> str:
        """Namespace for a module: what the switch advertised, else standard."""
        return self.result.namespaces.get(module) or DEFAULT_NAMESPACES.get(module, "")

    def has_module(self, module: str) -> bool:
        return module in self.result.modules

    def top_filter(self, module: str, container: str) -> str:
        ns = self.ns(module)
        if ns:
            return f'<{container} xmlns="{ns}"/>'
        # RFC 6241 6.2.2: a filter node without a namespace matches any
        # namespace. Last resort -- some servers are stricter than the RFC.
        return f"<{container}/>"

    # --- operations ------------------------------------------------------
    def _record(self, cap: Capture) -> Capture:
        self.result.captures[cap.key] = cap
        if cap.ok and cap.xml and self.raw_dir:
            os.makedirs(self.raw_dir, exist_ok=True)
            fname = os.path.join(self.raw_dir, f"{cap.key}.xml")
            try:
                with open(fname, "w", encoding="utf-8") as fh:
                    fh.write(cap.xml)
            except OSError as exc:
                cap.error = f"raw capture not written: {exc}"
        return cap

    def get(self, key: str, filter_xml: Optional[str] = None) -> Capture:
        return self._rpc(key, "get", filter_xml)

    def get_config(self, key: str, filter_xml: Optional[str] = None,
                   source: str = "running") -> Capture:
        return self._rpc(key, "get-config", filter_xml, source=source)

    def _rpc(self, key: str, operation: str, filter_xml: Optional[str],
             source: str = "running") -> Capture:
        cap = Capture(key=key, operation=operation, filter_xml=filter_xml, ok=False)
        if self.mgr is None:
            cap.error = "no session"
            cap.error_kind = "no-session"
            return self._record(cap)

        t0 = time.time()
        try:
            flt = ("subtree", filter_xml) if filter_xml else None
            if operation == "get":
                reply = self.mgr.get(filter=flt) if flt else self.mgr.get()
            else:
                reply = (self.mgr.get_config(source=source, filter=flt)
                         if flt else self.mgr.get_config(source=source))
            xml = reply.xml if hasattr(reply, "xml") else str(reply)
            cap.ok = True
            cap.xml = xml
            cap.bytes = len(xml or "")
        except RPCError as exc:
            # An <rpc-error> is a legitimate answer: usually "unknown element",
            # meaning the switch does not implement that model. Recorded, not
            # fatal -- absence of a model is itself a discovery result.
            cap.error = str(exc).strip() or "rpc-error"
            cap.error_kind = "rpc-error"
        except Exception as exc:
            cap.error = f"{type(exc).__name__}: {exc}"
            cap.error_kind = classify_exception(exc)
        finally:
            cap.duration_s = time.time() - t0
            self.result.duration_s += cap.duration_s
        return self._record(cap)


# --- the standard collection --------------------------------------------

def collect(session: SwitchSession, full_dump: bool = False) -> SwitchResult:
    """Run the standard set of reads against one connected switch.

    ``get`` is used rather than ``get-config`` wherever operational state is
    what we are after. This matters for capabilities: the Qbv limits
    (``supported-list-max``, ``supported-cycle-max``, ``supported-interval-max``)
    are ``config false`` nodes and are absent from ``get-config`` output --
    they only appear in a ``get``. Those three numbers are precisely what a
    CNC needs in order to know whether a schedule it computed will fit.

    The intended configuration is collected separately with ``get-config``,
    because on this platform the two can legitimately differ: the app note
    (§Data Stores) states the plugin reads the running datastore from the
    switch management software only at plugin start, so CLI or web-UI changes
    are not immediately visible over NETCONF.
    """
    if not session.result.reachable:
        return session.result

    # Operational reads -- these carry the capability envelope.
    session.get("system",
                session.top_filter("ietf-system", "system"))
    session.get("system-state",
                session.top_filter("ietf-system", "system-state"))
    session.get("interfaces",
                session.top_filter("ietf-interfaces", "interfaces"))
    session.get("interfaces-state",
                session.top_filter("ietf-interfaces", "interfaces-state"))
    session.get("bridges",
                session.top_filter("ieee802-dot1q-bridge", "bridges"))
    session.get("lldp",
                session.top_filter("ieee802-dot1ab-lldp", "lldp"))

    # Schema list from ietf-netconf-monitoring. Belt and braces next to the
    # <hello> capability list: a server may implement a module without
    # advertising it as a capability.
    ns_mon = session.ns("ietf-netconf-monitoring")
    session.get(
        "netconf-state-schemas",
        f'<netconf-state xmlns="{ns_mon}"><schemas/></netconf-state>'
        if ns_mon else "<netconf-state><schemas/></netconf-state>",
    )

    # Intended configuration, for the running-vs-operational comparison.
    session.get_config("config-interfaces",
                       session.top_filter("ietf-interfaces", "interfaces"))
    session.get_config("config-bridges",
                       session.top_filter("ieee802-dot1q-bridge", "bridges"))

    if full_dump:
        # Unfiltered <get>. Can be large; only on request. Useful when a
        # model turns out to live somewhere this tool did not look.
        session.get("full-dump", None)

    return session.result
