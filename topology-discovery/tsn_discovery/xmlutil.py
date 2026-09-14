"""Namespace-agnostic XML helpers.

Every element in a NETCONF reply is namespaced, and the prefixes a vendor
chooses are not stable across firmware revisions or YANG module revisions.
Matching on namespace URIs therefore makes a parser that works today and
breaks on the next switch release.

All lookups here match on *local-name only*. That is deliberate: the YANG
node names (``gate-parameters``, ``remote-systems-data``, ``vlan``) are
defined by the IEEE/IETF standard and are stable; the namespaces are not
what we want to bind to.
"""

from __future__ import annotations

from typing import Iterable, Iterator, List, Optional

from lxml import etree


def lname(el) -> str:
    """Local name of an element, namespace stripped."""
    tag = el.tag
    if not isinstance(tag, str):          # comments, PIs
        return ""
    return tag.rsplit("}", 1)[-1]


def namespace(el) -> str:
    tag = el.tag
    if not isinstance(tag, str) or "}" not in tag:
        return ""
    return tag.split("}", 1)[0].lstrip("{")


def children(el, name: str) -> List:
    """Direct children with the given local name."""
    return [c for c in el if lname(c) == name]


def child(el, name: str):
    """First direct child with the given local name, or None."""
    for c in el:
        if lname(c) == name:
            return c
    return None


def descendants(root, name: str) -> Iterator:
    """All descendants (any depth) with the given local name."""
    for el in root.iter():
        if lname(el) == name:
            yield el


def first(root, name: str):
    for el in descendants(root, name):
        return el
    return None


def text(el, name: str, default: Optional[str] = None) -> Optional[str]:
    """Text of the first direct child with the given local name.

    Returns ``default`` if the child is absent, and ``""`` if the child is
    present but empty -- the distinction matters: an empty ``<location/>``
    means "configured empty", a missing one means "not supported".
    """
    c = child(el, name)
    if c is None:
        return default
    return (c.text or "").strip()


def deep_text(root, name: str, default: Optional[str] = None) -> Optional[str]:
    """Text of the first descendant with the given local name."""
    el = first(root, name)
    if el is None:
        return default
    return (el.text or "").strip()


def path(root, *names: str) -> Iterator:
    """Walk a chain of local names from ``root``, yielding the leaf elements.

    ``path(data, 'interfaces', 'interface')`` yields every ``interface``
    element that is a direct child of an ``interfaces`` element that is a
    direct child of ``data``.
    """
    level: Iterable = [root]
    for n in names:
        nxt: List = []
        for el in level:
            nxt.extend(children(el, n))
        level = nxt
    for el in level:
        yield el


def to_int(value, default=None):
    """Tolerant int conversion. YANG counters arrive as strings and vendors
    occasionally emit an empty element instead of omitting the node."""
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def to_bool(value, default=None):
    if value is None:
        return default
    v = str(value).strip().lower()
    if v in ("true", "1", "yes", "enabled", "up"):
        return True
    if v in ("false", "0", "no", "disabled", "down"):
        return False
    return default


def strip_identity(value: Optional[str]) -> Optional[str]:
    """YANG identityref values arrive prefixed: ``dot1q:c-vlan-component``,
    ``sched:set-gate-states``. Strip the prefix for readability; the full
    value is kept in the raw XML capture."""
    if value is None:
        return None
    return value.split(":")[-1].strip()


def parse(xml_text: str):
    """Parse a NETCONF reply into an element tree root.

    ncclient hands back the whole ``<rpc-reply>``. Some servers wrap the
    payload in ``<data>``, some return the payload directly. Callers always
    want the payload, so unwrap one level of ``rpc-reply`` and ``data`` if
    present.
    """
    if isinstance(xml_text, str):
        xml_text = xml_text.encode("utf-8")
    parser = etree.XMLParser(recover=True, huge_tree=True)
    root = etree.fromstring(xml_text, parser=parser)
    if root is None:
        raise ValueError("empty or unparseable XML")
    if lname(root) == "rpc-reply":
        inner = child(root, "data")
        if inner is not None:
            return inner
        return root
    return root


def pretty(el) -> str:
    return etree.tostring(el, pretty_print=True, encoding="unicode")


# --- MAC address normalisation -------------------------------------------
# The testbed has three spellings in play at once: SYSTEM.md uses both
# colon-separated lowercase (RPIs) and dash-separated uppercase (NXP boards),
# the switches report bridge addresses as 00-A0-A5-5C-6D-B0, and LLDP
# chassis-ids arrive as raw hex or as colon-separated. Everything is folded
# to lowercase colon-separated before comparison.

def norm_mac(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    hexchars = "".join(ch for ch in str(value).lower() if ch in "0123456789abcdef")
    if len(hexchars) != 12:
        return None
    return ":".join(hexchars[i:i + 2] for i in range(0, 12, 2))


def looks_like_mac(value: Optional[str]) -> bool:
    return norm_mac(value) is not None
