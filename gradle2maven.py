#!/usr/bin/env python3
"""gradle2maven - build a self-contained local Maven repository from a Gradle build.

The tool statically parses Gradle build scripts (Groovy ``build.gradle`` and
Kotlin ``build.gradle.kts``), settings scripts, ``gradle.properties`` files and
version catalogs (``gradle/libs.versions.toml``).  It then resolves the full
transitive dependency graph using Maven POMs and Gradle Module Metadata and
lays every required file out as a standard Maven repository that can be used
for offline / air-gapped builds.

Before anything is downloaded the Gradle dependency cache
(``~/.gradle/caches/modules-2/files-2.1``) is consulted and cached files are
copied instead.  Downloads that fail with an SSL error are retried without
certificate verification (this can be disabled with ``--no-ssl-fallback``).

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import dataclasses
import fnmatch
import functools
import hashlib
import json
import logging
import os
import re
import shutil
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple

try:  # Python 3.11+
    import tomllib as _toml
except ImportError:  # pragma: no cover - depends on interpreter version
    _toml = None  # type: ignore[assignment]

__version__ = "1.0.0"

log = logging.getLogger("gradle2maven")

MAVEN_CENTRAL = "https://repo.maven.apache.org/maven2/"
GOOGLE = "https://dl.google.com/dl/android/maven2/"
GRADLE_PLUGIN_PORTAL = "https://plugins.gradle.org/m2/"
JCENTER = "https://jcenter.bintray.com/"
WELL_KNOWN_REPOS = {
    "mavenCentral": MAVEN_CENTRAL,
    "google": GOOGLE,
    "gradlePluginPortal": GRADLE_PLUGIN_PORTAL,
    "jcenter": JCENTER,
}

USER_AGENT = "gradle2maven/%s (+https://github.com/mehsayer/gradle2maven)" % __version__

DEFAULT_SCOPES = ("compile", "runtime")
DEFAULT_KOTLIN_PLATFORMS = ("jvm", "androidJvm", "common")
CHECKSUM_ALGORITHMS = {"md5": "md5", "sha1": "sha1", "sha256": "sha256", "sha512": "sha512"}

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INCOMPLETE = 3


class Gradle2MavenError(Exception):
    """Fatal, user-facing error."""


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

_SPECIAL_QUALIFIERS = {"dev": -1, "rc": 1, "snapshot": 2, "final": 3, "ga": 4, "release": 5, "sp": 6}
_VERSION_SPLIT = re.compile(r"[.\-_+]|(?<=\d)(?=\D)|(?<=\D)(?=\d)")


def _version_parts(version: str) -> List[str]:
    return [p for p in _VERSION_SPLIT.split(version) if p]


def _compare_part(x: str, y: str) -> int:
    xd, yd = x.isdigit(), y.isdigit()
    if xd and yd:
        xi, yi = int(x), int(y)
        return (xi > yi) - (xi < yi)
    if xd:
        return 1
    if yd:
        return -1
    xl, yl = x.lower(), y.lower()
    rx, ry = _SPECIAL_QUALIFIERS.get(xl, 0), _SPECIAL_QUALIFIERS.get(yl, 0)
    if rx != ry:
        return (rx > ry) - (rx < ry)
    return (xl > yl) - (xl < yl)


def compare_versions(a: str, b: str) -> int:
    """Compare two versions using Gradle's ordering rules.

    Numeric parts compare numerically and are greater than textual parts;
    ``dev`` < other qualifiers < ``rc`` < ``snapshot`` < ``final`` < ``ga`` <
    ``release`` < ``sp``.  An extra numeric part makes a version greater
    (1.0 < 1.0.1), an extra textual part makes it smaller (1.0-beta < 1.0).
    """
    pa, pb = _version_parts(a), _version_parts(b)
    for x, y in zip(pa, pb):
        c = _compare_part(x, y)
        if c:
            return c
    if len(pa) == len(pb):
        return 0
    if len(pa) > len(pb):
        return 1 if pa[len(pb)].isdigit() else -1
    return -1 if pb[len(pa)].isdigit() else 1


version_key = functools.cmp_to_key(compare_versions)

_RANGE_RE = re.compile(r"([\[(\]])\s*([^,\[\]()]*?)\s*(?:,\s*([^,\[\]()]*?)\s*)?([\])\[])")


def parse_version_ranges(spec: str) -> Optional[List[Tuple[Optional[str], bool, Optional[str], bool]]]:
    """Parse Maven/Gradle range notation into ``(low, low_inclusive, high, high_inclusive)`` tuples."""
    spec = spec.strip()
    if not spec or spec[0] not in "[(]":
        return None
    ranges = []
    pos = 0
    for m in _RANGE_RE.finditer(spec):
        if spec[pos:m.start()].strip(" ,"):
            return None
        pos = m.end()
        opening, low, high, closing = m.groups()
        if high is None:  # "[1.0]" - exact version
            if not low:
                return None
            ranges.append((low, True, low, True))
            continue
        ranges.append((low or None, opening == "[", high or None, closing == "]"))
    if not ranges or spec[pos:].strip(" ,"):
        return None
    return ranges


def is_dynamic_version(version: Optional[str]) -> bool:
    if not version:
        return False
    return (
        version.endswith("+")
        or version.startswith("latest.")
        or parse_version_ranges(version) is not None
    )


def normalize_requested_version(version: Optional[str]) -> Optional[str]:
    """Reduce Gradle rich-version shorthands (``1.0!!``, ``[1,2)!!1.5``) to a single request."""
    if not version:
        return None
    version = version.strip()
    if "!!" in version:
        required, _, preferred = version.partition("!!")
        version = preferred or required
    return version or None


def version_matches(spec: str, candidate: str) -> bool:
    if spec in ("+", "latest.integration"):
        return True
    if spec in ("latest.release", "latest.milestone"):
        return not candidate.endswith("-SNAPSHOT")
    if spec.endswith("+"):
        return candidate.startswith(spec[:-1])
    ranges = parse_version_ranges(spec)
    if ranges is None:
        return candidate == spec
    for low, low_inc, high, high_inc in ranges:
        if low is not None:
            c = compare_versions(candidate, low)
            if c < 0 or (c == 0 and not low_inc):
                continue
        if high is not None:
            c = compare_versions(candidate, high)
            if c > 0 or (c == 0 and not high_inc):
                continue
        return True
    return False


def select_version(spec: str, candidates: Iterable[str]) -> Optional[str]:
    """Pick the highest candidate satisfying a dynamic version or range (what Gradle does)."""
    for candidate in sorted(set(candidates), key=version_key, reverse=True):
        if version_matches(spec, candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Coordinates
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Dependency:
    group: str
    artifact: str
    version: Optional[str] = None
    classifier: Optional[str] = None
    ext: Optional[str] = None
    configuration: str = ""
    platform: bool = False
    transitive: bool = True
    excludes: frozenset = frozenset()
    origin: str = ""
    optional: bool = False  # implicit dependencies: missing ones are only warnings

    @property
    def ga(self) -> Tuple[str, str]:
        return self.group, self.artifact

    def notation(self) -> str:
        s = "%s:%s" % (self.group, self.artifact)
        if self.version:
            s += ":" + self.version
        if self.classifier:
            s += (":" if self.version else "::") + self.classifier
        if self.ext:
            s += "@" + self.ext
        return s

    def to_json(self) -> dict:
        d = {
            "group": self.group,
            "artifact": self.artifact,
            "version": self.version,
            "configuration": self.configuration,
            "notation": self.notation(),
        }
        if self.classifier:
            d["classifier"] = self.classifier
        if self.ext:
            d["extension"] = self.ext
        if self.platform:
            d["platform"] = True
        if not self.transitive:
            d["transitive"] = False
        if self.excludes:
            d["excludes"] = sorted("%s:%s" % e for e in self.excludes)
        if self.origin:
            d["origin"] = self.origin
        if self.optional:
            d["optional"] = True
        return d


_COORD_PART = re.compile(r"^[^\s:@'\"$/\\]+$")


def parse_coordinates(text: str) -> Optional[Dependency]:
    """Parse ``group:artifact[:version[:classifier]][@ext]``.  Returns None if not coordinates."""
    s = text.strip()
    ext = None
    if "@" in s:
        s, ext = s.rsplit("@", 1)
        if not ext or not _COORD_PART.match(ext):
            return None
    parts = s.split(":")
    if not 2 <= len(parts) <= 4:
        return None
    group, artifact = parts[0], parts[1]
    if not _COORD_PART.match(group) or not _COORD_PART.match(artifact):
        return None
    version = parts[2] if len(parts) > 2 and parts[2] else None
    classifier = parts[3] if len(parts) > 3 and parts[3] else None
    if version and ("$" in version or " " in version.strip()):
        return None
    if classifier and not _COORD_PART.match(classifier):
        return None
    return Dependency(group, artifact, version.strip() if version else None, classifier, ext)


def plugin_marker(plugin_id: str, version: str) -> Dependency:
    return Dependency(plugin_id, plugin_id + ".gradle.plugin", version, configuration="plugins")


# ---------------------------------------------------------------------------
# Groovy / Kotlin script scanning helpers
# ---------------------------------------------------------------------------

_PAIRS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = set(_PAIRS.values())


def skip_string(text: str, i: int) -> int:
    """Return the index just after the string literal starting at ``text[i]``."""
    n = len(text)
    q = text[i]
    if text.startswith(q * 3, i):
        end = text.find(q * 3, i + 3)
        return n if end < 0 else end + 3
    j = i + 1
    while j < n:
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if ch == q:
            return j + 1
        if ch == "\n":
            return j
        if q == '"' and ch == "$" and j + 1 < n and text[j + 1] == "{":
            end = find_matching(text, j + 1)
            j = n if end < 0 else end + 1
            continue
        j += 1
    return n


def find_matching(text: str, i: int) -> int:
    """Index of the bracket closing the one at ``text[i]`` (skipping strings), or -1."""
    stack = [_PAIRS[text[i]]]
    n = len(text)
    j = i + 1
    while j < n:
        ch = text[j]
        if ch in "\"'":
            j = skip_string(text, j)
            continue
        if ch in _PAIRS:
            stack.append(_PAIRS[ch])
        elif ch in _CLOSERS:
            if ch == stack[-1]:
                stack.pop()
                if not stack:
                    return j
            elif ch in stack:
                while stack and stack[-1] != ch:
                    stack.pop()
                stack.pop()
                if not stack:
                    return j
        j += 1
    return -1


def strip_comments(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments while leaving string literals (and line numbers) intact."""
    out: List[str] = []
    i, n = 0, len(text)
    if text.startswith("#!"):
        i = text.find("\n")
        i = n if i < 0 else i
    while i < n:
        ch = text[i]
        if ch == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("\n" * text.count("\n", i, j))
            i = j
            continue
        if ch in "\"'":
            j = skip_string(text, i)
            out.append(text[i:j])
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def split_top_level(text: str, sep: str = ",") -> List[str]:
    parts: List[str] = []
    depth = 0
    start = 0
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'":
            i = skip_string(text, i)
            continue
        if ch in _PAIRS:
            depth += 1
        elif ch in _CLOSERS:
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return [p for p in (p.strip() for p in parts) if p]


@dataclasses.dataclass
class Block:
    name: str
    start: int
    body_start: int
    body_end: int


def find_blocks(text: str, name: str) -> List[Block]:
    """Find every ``name { ... }`` (or ``name(...) { ... }``) block in ``text``."""
    blocks = []
    pattern = re.compile(r"(?<![\w$])" + re.escape(name) + r"\s*(?:\([^()]*\)\s*)?\{")
    for m in pattern.finditer(text):
        open_idx = m.end() - 1
        close = find_matching(text, open_idx)
        if close < 0:
            continue
        blocks.append(Block(name, m.start(), open_idx + 1, close))
    return blocks


def _inside(pos: int, spans: Iterable[Block]) -> bool:
    return any(b.body_start <= pos < b.body_end for b in spans)


def string_literal(text: str) -> Optional[Tuple[str, bool]]:
    """If ``text`` is exactly one string literal return ``(content, interpolating)``."""
    t = text.strip()
    if len(t) < 2 or t[0] not in "\"'" or skip_string(t, 0) != len(t):
        return None
    q = t[0]
    if t.startswith(q * 3):
        if len(t) < 6 or not t.endswith(q * 3):
            return None
        return t[3:-3], q == '"'
    if t[-1] != q:
        return None
    return t[1:-1], q == '"'


@dataclasses.dataclass
class Statement:
    name: str
    args: Optional[str]
    closure: Optional[str]


_IDENT_AT = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*|`[^`]+`")


def _skip_blanks(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t":
        i += 1
    return i


def iter_statements(body: str) -> Iterator[Statement]:
    """Split a closure body into ``name args { closure }`` statements (one level deep).

    Handles ``name 'arg', ...`` (Groovy command syntax), ``name(args)``, ``"name"(args)``
    (Kotlin string invocation) and an optional trailing closure on the same line.
    """
    i, n = 0, len(body)
    while i < n:
        while i < n and body[i] in " \t\r\n;":
            i += 1
        if i >= n:
            break
        ch = body[i]
        if ch in "\"'":
            end = skip_string(body, i)
            lit = string_literal(body[i:end])
            j = _skip_blanks(body, end)
            if not (lit and j < n and body[j] == "("):
                i = max(_skip_to_eol(body, end), i + 1)
                continue
            name = lit[0]
        else:
            m = _IDENT_AT.match(body, i)
            if not m:
                if ch in _PAIRS:
                    end = find_matching(body, i)
                    i = n if end < 0 else end + 1
                else:
                    i += 1
                continue
            name = m.group().strip("`")
            j = _skip_blanks(body, m.end())
        args: Optional[str] = None
        closure: Optional[str] = None
        if j < n and body[j] == "(":
            end = find_matching(body, j)
            if end < 0:
                break
            args = body[j + 1:end]
            j = _skip_blanks(body, end + 1)
            if j < n and body[j] not in "{\r\n;}":
                # chained call or trailing expression (``.because(..)``, ``version '1'``): ignore it
                j = _skip_to_eol(body, j, stop_at_brace=True)
        elif j < n and body[j] == "=" and not body.startswith("==", j):
            i = max(_skip_to_eol(body, j + 1), i + 1)  # assignment, not a call
            continue
        elif j < n and body[j] not in "{\r\n;}":
            end = _skip_to_eol(body, j, stop_at_brace=True)
            args = body[j:end].strip()
            j = end
        j = _skip_blanks(body, j)
        if j < n and body[j] == "{":
            end = find_matching(body, j)
            if end < 0:
                break
            closure = body[j + 1:end]
            j = end + 1
        yield Statement(name, args, closure)
        i = max(j, i + 1)


def _skip_to_eol(text: str, i: int, stop_at_brace: bool = False) -> int:
    """Advance to the end of a (possibly continued) statement line."""
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'":
            i = skip_string(text, i)
            continue
        if ch in "([":
            end = find_matching(text, i)
            i = n if end < 0 else end + 1
            continue
        if ch == "{":
            if stop_at_brace:
                return i
            end = find_matching(text, i)
            i = n if end < 0 else end + 1
            continue
        if ch in ";}":
            return i
        if ch == "\n":
            k = i - 1
            while k >= 0 and text[k] in " \t\r":
                k -= 1
            if k >= 0 and (text[k] in ",+=" or (text[k] == ":" and k > 0 and text[k - 1] == "?")):
                i += 1
                continue
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if text.startswith((".", "?.", "+ "), j) and not text.startswith("..", j):
                i = j
                continue
            return i
        i += 1
    return n


def first_expression(text: str) -> str:
    """The first complete expression of ``text`` (a literal, a call or a dotted name)."""
    t = text.strip()
    if not t:
        return t
    if t[0] in "\"'":
        return t[:skip_string(t, 0)]
    m = _IDENT_AT.match(t)
    if not m:
        return t
    j = m.end()
    if j < len(t) and t[j] == "(":
        end = find_matching(t, j)
        j = len(t) if end < 0 else end + 1
        while t.startswith(".", j):  # .get(), .orNull ...
            m2 = _IDENT_AT.match(t, j + 1)
            if not m2:
                break
            j = m2.end()
            if t.startswith("(", j):
                end = find_matching(t, j)
                j = len(t) if end < 0 else end + 1
    else:
        while j < len(t) and t[j] == "[":
            end = find_matching(t, j)
            if end < 0:
                break
            j = end + 1
    return t[:j]


def unwrap_call(expr: str, names: Iterable[str]) -> str:
    """``uri("x")`` -> ``"x"`` for the given function names (repeatedly)."""
    e = expr.strip()
    changed = True
    while changed:
        changed = False
        for name in names:
            if e.startswith(name + "(") and find_matching(e, len(name)) == len(e) - 1:
                args = split_top_level(e[len(name) + 1:-1])
                e = args[-1] if args else ""
                changed = True
    return e


# ---------------------------------------------------------------------------
# Variables, properties and interpolation
# ---------------------------------------------------------------------------

def read_properties(path: Path) -> Dict[str, str]:
    """Read a Java ``.properties`` file (good enough for gradle.properties)."""
    props: Dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return props
    lines = raw.splitlines()
    buf = ""
    for line in lines:
        stripped = line.strip() if not buf else line.lstrip()
        if not buf and (not stripped or stripped[0] in "#!"):
            continue
        if stripped.endswith("\\") and not stripped.endswith("\\\\"):
            buf += stripped[:-1]
            continue
        buf += stripped
        m = re.match(r"((?:[^=:\s\\]|\\.)+)\s*[=:\s]\s*(.*)$", buf)
        if m:
            props[m.group(1).replace("\\", "")] = m.group(2)
        elif buf:
            props[buf] = ""
        buf = ""
    return props


class Scope:
    """Variable scope: project variables -> parent (root project) -> properties."""

    def __init__(self, parent: Optional["Scope"] = None, properties: Optional[Dict[str, str]] = None,
                 overrides: Optional[Dict[str, str]] = None, catalogs: Optional[Dict[str, "VersionCatalog"]] = None,
                 builtins: Optional[Dict[str, str]] = None):
        self.parent = parent
        self.vars: Dict[str, str] = {}  # name -> raw expression
        self.properties = properties if properties is not None else {}
        self.overrides = overrides if overrides is not None else (parent.overrides if parent else {})
        self.catalogs = catalogs if catalogs is not None else (parent.catalogs if parent else {})
        self.builtins = builtins or {}

    def lookup_raw(self, name: str) -> Optional[str]:
        if name in self.overrides:
            return _quote(self.overrides[name])
        s: Optional[Scope] = self
        while s is not None:
            if name in s.builtins:
                return _quote(s.builtins[name])
            if name in s.vars:
                return s.vars[name]
            s = s.parent
        s = self
        while s is not None:
            if name in s.properties:
                return _quote(s.properties[name])
            s = s.parent
        return None

    def resolve(self, expr: str, _depth: int = 0) -> Optional[str]:
        return resolve_expr(expr, self, _depth)


def _quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


_ACCESSOR_PREFIXES = re.compile(r"^(?:(?:project|rootProject|this|it|ext|extra|properties|settings|gradle\.rootProject|"
                                r"project\.rootProject|rootProject\.ext|project\.ext)\.)+")
_STR = r"""(?:"[^"\n]*"|'[^'\n]*')"""


def normalize_name(expr: str) -> str:
    e = expr.strip()
    e = re.sub(r"\.get\(\)$|\.getOrNull\(\)$|\.orNull$|\.orElse\(.*\)$|!!$|\.toString\(\)$|\s+as\s+String$", "", e)
    while e.startswith("(") and find_matching(e, 0) == len(e) - 1:
        e = e[1:-1].strip()
    m = re.fullmatch(r"(?:.*\.)?(?:property|findProperty|getProperty|gradleProperty|extra\.get|ext\.get|ext\.getAt)"
                     r"\(\s*(" + _STR + r")\s*\)(?:\.get\(\)|\.orNull|\.getOrNull\(\))?(?:\s+as\s+String)?", e)
    if m:
        return m.group(1)[1:-1]
    e = re.sub(r"\[\s*(" + _STR + r")\s*\]", lambda mm: "." + mm.group(1)[1:-1], e)
    e = re.sub(r"\.getValue\(\s*(" + _STR + r")\s*\)", lambda mm: "." + mm.group(1)[1:-1], e)
    e = _ACCESSOR_PREFIXES.sub("", e)
    return e


def resolve_expr(expr: str, scope: Scope, depth: int = 0) -> Optional[str]:
    """Evaluate a (simple) Groovy/Kotlin string expression to a string, or None."""
    if depth > 25:
        return None
    e = expr.strip().rstrip(";").strip()
    if not e:
        return None
    if e.startswith("(") and find_matching(e, 0) == len(e) - 1:
        return resolve_expr(e[1:-1], scope, depth + 1)
    parts = split_top_level(e, "+")
    if len(parts) > 1:
        values = [resolve_expr(p, scope, depth + 1) for p in parts]
        if any(v is None for v in values):
            return None
        return "".join(values)  # type: ignore[arg-type]
    lit = string_literal(e)
    if lit is not None:
        content, interpolating = lit
        content = _unescape(content) if "\\" in content else content
        return interpolate(content, scope, depth + 1) if interpolating else content
    if re.fullmatch(r"-?\d+(?:\.\d+)?[LlFfDd]?", e):
        return e
    m = re.fullmatch(r"(?:providers\.)?(?:systemProperty|environmentVariable)\(.*\)|System\.getenv\((" + _STR + r")\)", e)
    if m:
        if m.group(1):
            return os.environ.get(m.group(1)[1:-1])
        return None
    m = re.fullmatch(r"(.*?)\s*\?:\s*(.*)", e, re.S)  # elvis: a ?: 'default'
    if m and "?:" in e:
        left = resolve_expr(m.group(1), scope, depth + 1)
        return left if left is not None else resolve_expr(m.group(2), scope, depth + 1)
    m = re.fullmatch(r"(.+?)\s*\?\s*(.+?)\s*:\s*(.+)", e)  # ternary: just pick the "true" branch
    if m and not e.startswith(("'", '"')):
        return resolve_expr(m.group(2), scope, depth + 1)
    name = normalize_name(e)
    if not re.fullmatch(r"[\w$.\-]+", name):
        return None
    m = re.fullmatch(r"(\w+)\.versions\.([\w.]+)", name)
    if m and m.group(1) in scope.catalogs:
        v = scope.catalogs[m.group(1)].version_ref(m.group(2))
        if v is not None:
            return v
    raw = scope.lookup_raw(name)
    if raw is None:
        return None
    return resolve_expr(raw, scope, depth + 1)


_TEMPLATE_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")


def interpolate(content: str, scope: Scope, depth: int = 0) -> str:
    """Expand ``${expr}`` and ``$name`` templates; unresolved templates are kept verbatim."""
    out: List[str] = []
    i, n = 0, len(content)
    while i < n:
        ch = content[i]
        if ch == "\\" and i + 1 < n:
            out.append(content[i:i + 2])
            i += 2
            continue
        if ch == "$" and i + 1 < n:
            if content[i + 1] == "{":
                end = find_matching(content, i + 1)
                if end > 0:
                    inner = content[i + 2:end]
                    value = resolve_expr(inner, scope, depth + 1)
                    out.append(value if value is not None else content[i:end + 1])
                    i = end + 1
                    continue
            m = _TEMPLATE_NAME.match(content, i + 1)
            if m:
                pieces = m.group().split(".")
                for k in range(len(pieces), 0, -1):
                    value = resolve_expr(".".join(pieces[:k]), scope, depth + 1)
                    if value is not None:
                        out.append(value)
                        i = m.start() + len(".".join(pieces[:k]))
                        break
                else:
                    out.append(content[i:m.end()])
                    i = m.end()
                continue
        out.append(ch)
        i += 1
    return "".join(out)


_VAR_DECL = re.compile(
    r"(?m)(?:^|(?<=[{;]))[ \t]*(?:(?:def|val|var|final|static|private|public|internal|const|String|Object)\s+)*"
    r"([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*(?:\[\s*" + _STR + r"\s*\])?)"
    r"\s*(?::\s*[\w<>?,. ]+?\s*)?=(?!=)\s*")
_EXT_SET = re.compile(r"(?<![\w.])(?:(?:project|rootProject)\.)?(?:(?:ext|extra)\.)?set(?=\(\s*" + _STR + r"\s*,)")
_BY_EXTRA = re.compile(r"(?m)\bva[lr]\s+(\w+)(?:\s*:\s*[\w<>?]+)?\s+by\s+(?:project\.|rootProject\.)?extra(?=\()")
_SKIP_VAR_NAMES = {"url", "isTransitive", "transitive", "isAllowInsecureProtocol", "allowInsecureProtocol",
                   "username", "password", "classifier", "extension", "type"}


def collect_variables(text: str, scope: Scope) -> None:
    """Record variable assignments (``def x = ..``, ``ext.x = ..``, ``val x by extra(..)``, maps, ...)."""
    for m in _VAR_DECL.finditer(text):
        name = normalize_name(m.group(1))
        if not name or name in _SKIP_VAR_NAMES or name.endswith(".url"):
            continue
        start = m.end()
        if start < len(text) and text[start] == "[":
            end = find_matching(text, start)
            if end > 0:
                _collect_groovy_collection(text[start + 1:end], name, scope)
                scope.vars.setdefault(name, text[start:end + 1])
            continue
        if text.startswith(("mapOf(", "mutableMapOf(", "hashMapOf(", "linkedMapOf("), start):
            open_idx = text.index("(", start)
            end = find_matching(text, open_idx)
            if end > 0:
                for pair in split_top_level(text[open_idx + 1:end]):
                    pm = re.fullmatch(r"(" + _STR + r")\s+to\s+(.+)", pair, re.S)
                    if pm:
                        scope.vars.setdefault(name + "." + pm.group(1)[1:-1], pm.group(2).strip())
            continue
        if text.startswith(("listOf(", "arrayOf(", "setOf("), start):
            open_idx = text.index("(", start)
            end = find_matching(text, open_idx)
            if end > 0:
                scope.vars.setdefault(name, "[" + text[open_idx + 1:end] + "]")
            continue
        end = _skip_to_eol(text, start)
        rhs = text[start:end].strip()
        if rhs and not rhs.startswith("{"):
            scope.vars.setdefault(name, rhs)
    for regex in (_EXT_SET, _BY_EXTRA):
        for m in regex.finditer(text):
            open_idx = m.end()
            close = find_matching(text, open_idx)
            if close < 0:
                continue
            args = split_top_level(text[open_idx + 1:close])
            if regex is _EXT_SET and len(args) == 2:
                scope.vars.setdefault(args[0][1:-1], args[1])
            elif regex is _BY_EXTRA and len(args) == 1:
                scope.vars.setdefault(m.group(1), args[0])


def _collect_groovy_collection(body: str, prefix: str, scope: Scope) -> None:
    for entry in split_top_level(body):
        m = re.match(r"(" + _STR + r"|[\w$]+)\s*:\s*", entry)
        if not m:
            continue
        key = m.group(1).strip("'\"")
        value = entry[m.end():].strip()
        full = prefix + "." + key
        if value.startswith("["):
            end = find_matching(value, 0)
            if end > 0:
                _collect_groovy_collection(value[1:end], full, scope)
                scope.vars.setdefault(full, value[:end + 1])
            continue
        scope.vars.setdefault(full, value)


# ---------------------------------------------------------------------------
# Minimal TOML reader (fallback for Python < 3.11, enough for version catalogs)
# ---------------------------------------------------------------------------

class TomlError(ValueError):
    pass


class _MiniToml:
    _BARE = re.compile(r"[A-Za-z0-9_-]+")
    _SCALAR = re.compile(r"[^\s,\]}#]+")

    def __init__(self, text: str):
        self.s = text.replace("\r\n", "\n")
        self.i = 0

    def error(self, msg: str) -> TomlError:
        line = self.s.count("\n", 0, self.i) + 1
        return TomlError("%s (line %d)" % (msg, line))

    def ws(self, newlines: bool = False) -> None:
        while self.i < len(self.s):
            c = self.s[self.i]
            if c in " \t" or (newlines and c == "\n"):
                self.i += 1
            elif c == "#":
                while self.i < len(self.s) and self.s[self.i] != "\n":
                    self.i += 1
            else:
                break

    def parse(self) -> dict:
        root: dict = {}
        table = root
        while True:
            self.ws(newlines=True)
            if self.i >= len(self.s):
                return root
            if self.s.startswith("[[", self.i):
                self.i += 2
                keys = self.key()
                self.expect("]]")
                parent = self._descend(root, keys[:-1])
                arr = parent.setdefault(keys[-1], [])
                if not isinstance(arr, list):
                    raise self.error("not an array of tables: %s" % ".".join(keys))
                table = {}
                arr.append(table)
            elif self.s[self.i] == "[":
                self.i += 1
                keys = self.key()
                self.expect("]")
                table = self._descend(root, keys)
            else:
                keys = self.key()
                self.expect("=")
                self._descend(table, keys[:-1])[keys[-1]] = self.value()
            self.ws()
            if self.i < len(self.s) and self.s[self.i] != "\n":
                raise self.error("unexpected %r" % self.s[self.i])

    def _descend(self, table: dict, keys: List[str]) -> dict:
        for k in keys:
            nxt = table.setdefault(k, {})
            if isinstance(nxt, list):
                nxt = nxt[-1]
            if not isinstance(nxt, dict):
                raise self.error("key %s is not a table" % k)
            table = nxt
        return table

    def expect(self, token: str) -> None:
        self.ws()
        if not self.s.startswith(token, self.i):
            raise self.error("expected %r" % token)
        self.i += len(token)

    def key(self) -> List[str]:
        keys = []
        while True:
            self.ws()
            if self.i < len(self.s) and self.s[self.i] in "\"'":
                keys.append(self.string())
            else:
                m = self._BARE.match(self.s, self.i)
                if not m:
                    raise self.error("invalid key")
                keys.append(m.group())
                self.i = m.end()
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == ".":
                self.i += 1
                continue
            return keys

    def string(self) -> str:
        s, i = self.s, self.i
        q = s[i]
        if s.startswith(q * 3, i):
            end = s.find(q * 3, i + 3)
            if end < 0:
                raise self.error("unterminated string")
            raw = s[i + 3:end]
            self.i = end + 3
            raw = raw[1:] if raw.startswith("\n") else raw
            return raw if q == "'" else self._escapes(raw)
        j = i + 1
        while j < len(s) and s[j] != q and s[j] != "\n":
            j += 2 if (s[j] == "\\" and q == '"') else 1
        if j >= len(s) or s[j] != q:
            raise self.error("unterminated string")
        self.i = j + 1
        raw = s[i + 1:j]
        return raw if q == "'" else self._escapes(raw)

    @staticmethod
    def _escapes(raw: str) -> str:
        def repl(m: "re.Match[str]") -> str:
            e = m.group(1)
            if e[0] in "uU":
                return chr(int(e[1:], 16))
            return {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}.get(e, e)
        return re.sub(r"\\(u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|.)", repl, raw)

    def value(self):
        self.ws()
        if self.i >= len(self.s):
            raise self.error("missing value")
        c = self.s[self.i]
        if c in "\"'":
            return self.string()
        if c == "[":
            self.i += 1
            items = []
            while True:
                self.ws(newlines=True)
                if self.s.startswith("]", self.i):
                    self.i += 1
                    return items
                items.append(self.value())
                self.ws(newlines=True)
                if self.s.startswith(",", self.i):
                    self.i += 1
                elif not self.s.startswith("]", self.i):
                    raise self.error("expected ',' or ']'")
        if c == "{":
            self.i += 1
            table: dict = {}
            self.ws()
            if self.s.startswith("}", self.i):
                self.i += 1
                return table
            while True:
                keys = self.key()
                self.expect("=")
                self._descend(table, keys[:-1])[keys[-1]] = self.value()
                self.ws()
                if self.s.startswith(",", self.i):
                    self.i += 1
                elif self.s.startswith("}", self.i):
                    self.i += 1
                    return table
                else:
                    raise self.error("expected ',' or '}'")
        m = self._SCALAR.match(self.s, self.i)
        if not m:
            raise self.error("invalid value")
        self.i = m.end()
        token = m.group()
        if token in ("true", "false"):
            return token == "true"
        try:
            return int(token.replace("_", ""), 0)
        except ValueError:
            pass
        try:
            return float(token.replace("_", ""))
        except ValueError:
            return token  # dates etc. are kept as text


def parse_toml(text: str) -> dict:
    """Parse TOML with :mod:`tomllib` when available, otherwise with a small built-in reader."""
    if _toml is not None:
        return _toml.loads(text)
    return _MiniToml(text).parse()


# ---------------------------------------------------------------------------
# Version catalogs
# ---------------------------------------------------------------------------

def _catalog_key(alias: str) -> str:
    return re.sub(r"[-_.]", ".", alias).lower()


class VersionCatalog:
    def __init__(self, name: str, path: Optional[Path] = None):
        self.name = name
        self.path = path
        self.versions: Dict[str, str] = {}
        self.libraries: Dict[str, Dependency] = {}
        self.bundles: Dict[str, List[str]] = {}
        self.plugins: Dict[str, Tuple[str, Optional[str]]] = {}

    @staticmethod
    def _version(value, versions: Dict[str, str]) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            if "ref" in value:
                return versions.get(_catalog_key(value["ref"]))
            for key in ("strictly", "require", "prefer"):
                if key in value:
                    return normalize_requested_version(str(value[key]))
        return None

    @classmethod
    def load(cls, name: str, path: Path) -> "VersionCatalog":
        data = parse_toml(path.read_text(encoding="utf-8"))
        cat = cls(name, path)
        for key, value in (data.get("versions") or {}).items():
            v = cls._version(value, {})
            if v is not None:
                cat.versions[_catalog_key(key)] = v
        for key, value in (data.get("libraries") or {}).items():
            dep = None
            if isinstance(value, str):
                dep = parse_coordinates(value)
            elif isinstance(value, dict):
                if "module" in value:
                    group, _, artifact = str(value["module"]).partition(":")
                else:
                    group, artifact = value.get("group", ""), value.get("name", "")
                if group and artifact:
                    dep = Dependency(group, artifact, normalize_requested_version(cls._version(value.get("version"), cat.versions)))
            if dep is not None:
                dep.origin = "%s.%s" % (name, key)
                cat.libraries[_catalog_key(key)] = dep
        for key, value in (data.get("bundles") or {}).items():
            if isinstance(value, list):
                cat.bundles[_catalog_key(key)] = [_catalog_key(v) for v in value]
        for key, value in (data.get("plugins") or {}).items():
            if isinstance(value, str):
                pid, _, ver = value.partition(":")
                cat.plugins[_catalog_key(key)] = (pid, ver or None)
            elif isinstance(value, dict) and "id" in value:
                cat.plugins[_catalog_key(key)] = (value["id"], cls._version(value.get("version"), cat.versions))
        return cat

    def version_ref(self, accessor: str) -> Optional[str]:
        return self.versions.get(_catalog_key(accessor))

    def lookup(self, accessor: str) -> Optional[List[Dependency]]:
        """Resolve ``foo.bar`` (``libs.foo.bar``) / ``bundles.x`` to dependencies."""
        acc = re.sub(r"\.(?:get|asProvider)\(\)", "", accessor).strip(".")
        if acc.startswith("bundles."):
            aliases = self.bundles.get(_catalog_key(acc[len("bundles."):]))
            if aliases is None:
                return None
            return [dataclasses.replace(self.libraries[a]) for a in aliases if a in self.libraries]
        dep = self.libraries.get(_catalog_key(acc))
        return [dataclasses.replace(dep)] if dep else None

    def plugin(self, accessor: str) -> Optional[Tuple[str, Optional[str]]]:
        acc = re.sub(r"\.(?:get|asProvider)\(\)", "", accessor).strip(".")
        if acc.startswith("plugins."):
            acc = acc[len("plugins."):]
        return self.plugins.get(_catalog_key(acc))


# ---------------------------------------------------------------------------
# Build script model
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class RepoSpec:
    url: str
    name: str = ""
    username: Optional[str] = None
    password: Optional[str] = None

    def to_json(self) -> dict:
        d = {"url": self.url}
        if self.name:
            d["name"] = self.name
        if self.username:
            d["username"] = self.username
        return d


@dataclasses.dataclass
class PluginSpec:
    id: str
    version: Optional[str]
    origin: str = ""

    def to_json(self) -> dict:
        return {"id": self.id, "version": self.version, "origin": self.origin}


@dataclasses.dataclass
class BuildModel:
    root_dir: Path
    build_files: List[Path] = dataclasses.field(default_factory=list)
    repositories: List[RepoSpec] = dataclasses.field(default_factory=list)
    plugin_repositories: List[RepoSpec] = dataclasses.field(default_factory=list)
    dependencies: List[Dependency] = dataclasses.field(default_factory=list)
    plugins: List[PluginSpec] = dataclasses.field(default_factory=list)
    uses_maven_local: bool = False
    warnings: List[str] = dataclasses.field(default_factory=list)

    def add_repo(self, repo: RepoSpec, plugin: bool = False) -> None:
        target = self.plugin_repositories if plugin else self.repositories
        url = repo.url.rstrip("/") + "/"
        if all(r.url.rstrip("/") + "/" != url for r in target):
            repo.url = url
            target.append(repo)

    def to_json(self) -> dict:
        return {
            "rootDir": str(self.root_dir),
            "buildFiles": [str(p) for p in self.build_files],
            "repositories": [r.to_json() for r in self.repositories],
            "pluginRepositories": [r.to_json() for r in self.plugin_repositories],
            "usesMavenLocal": self.uses_maven_local,
            "plugins": [p.to_json() for p in self.plugins],
            "dependencies": [d.to_json() for d in self.dependencies],
            "warnings": self.warnings,
        }


_NOT_CONFIGURATIONS = {
    "exclude", "because", "attributes", "capabilities", "version", "strictly", "require", "prefer", "reject",
    "rejectAll", "force", "transitive", "isTransitive", "artifact", "classifier", "extension", "type", "name", "id",
    "apply", "if", "else", "for", "while", "def", "val", "var", "return", "println", "print", "when", "try", "catch",
    "finally", "project", "files", "fileTree", "constraints", "components", "modules", "module", "registerTransform",
    "attributesSchema", "artifactTypes", "add", "configurations", "dependencies", "repositories", "plugins",
    "buildscript", "allprojects", "subprojects", "afterEvaluate", "task", "tasks", "test", "doLast", "doFirst",
    "logger", "throw", "assert", "import", "package", "sourceSets", "java", "kotlin", "android", "ext", "extra",
    "withModule", "all", "allVariants", "withVariant", "eachDependency", "useVersion", "useTarget", "details",
    "resolutionStrategy", "substitute", "dependencySubstitution", "enforcedPlatform", "platform", "testFixtures",
    "listOf", "setOf", "arrayOf", "mapOf", "forEach", "each", "with", "run", "also", "let", "set",
    "from", "into", "include", "implementationConfigurationName",
}
_SKIP_NOTATIONS = re.compile(r"^(?:project|files|fileTree|gradleApi|localGroovy|gradleTestKit|gradleKotlinDsl|"
                             r"layout\.files|kotlin\(\s*\"script-runtime\"|sourceSets|configurations|"
                             r"rootProject\.files|projects\.)", re.S)
_WRAPPERS = re.compile(r"^(platform|enforcedPlatform|testFixtures|dependencies\.create|create|project\.dependencies\.create|"
                       r"dependencies\.platform|dependencies\.enforcedPlatform|requireCapability|variantOf|"
                       r"dependencies\.testFixtures)\s*\(", re.S)
_KOTLIN_PLUGIN_PREFIX = "org.jetbrains.kotlin."


class GradleBuildParser:
    """Static (best effort) parser for Gradle Groovy and Kotlin DSL builds."""

    BUILD_FILE_NAMES = ("build.gradle.kts", "build.gradle")
    SETTINGS_FILE_NAMES = ("settings.gradle.kts", "settings.gradle")

    def __init__(self, gradle_home: Optional[Path] = None, cli_properties: Optional[Dict[str, str]] = None,
                 include_subprojects: bool = True, include_build_logic: bool = True,
                 configuration_filter: Optional[Callable[[str], bool]] = None):
        self.gradle_home = gradle_home
        self.cli_properties = dict(cli_properties or {})
        self.include_subprojects = include_subprojects
        self.include_build_logic = include_build_logic
        self.configuration_filter = configuration_filter or (lambda _c: True)
        self._seen_dep_keys: Set[tuple] = set()
        self._parsed_roots: Set[Tuple[Path, Path]] = set()
        self._plugin_defaults: Dict[str, str] = {}

    # -- entry point --------------------------------------------------------
    def parse(self, target: Path, model: Optional[BuildModel] = None) -> BuildModel:
        target = target.expanduser().resolve()
        if not target.exists():
            raise Gradle2MavenError("no such file or directory: %s" % target)
        build_file: Optional[Path]
        if target.is_dir():
            project_dir = target
            build_file = self._find_file(project_dir, self.BUILD_FILE_NAMES)
        elif target.name in self.SETTINGS_FILE_NAMES:
            project_dir = target.parent
            build_file = self._find_file(project_dir, self.BUILD_FILE_NAMES)
        else:
            project_dir = target.parent
            build_file = target
        settings_file = self._find_settings(project_dir)
        root_dir = settings_file.parent if settings_file else project_dir
        if model is None:
            model = BuildModel(root_dir=root_dir)
        if build_file is None and settings_file is None:
            raise Gradle2MavenError("no Gradle build or settings file found in %s" % target)
        self._parse_root(root_dir, settings_file, project_dir, build_file, model)
        return model

    # -- discovery helpers ------------------------------------------------
    @staticmethod
    def _find_file(directory: Path, names: Iterable[str]) -> Optional[Path]:
        for name in names:
            p = directory / name
            if p.is_file():
                return p
        return None

    def _find_settings(self, project_dir: Path) -> Optional[Path]:
        d = project_dir
        for _ in range(6):
            s = self._find_file(d, self.SETTINGS_FILE_NAMES)
            if s:
                return s
            if d.parent == d:
                break
            d = d.parent
        return None

    def _properties_for(self, directory: Path, root_dir: Path) -> Dict[str, str]:
        props: Dict[str, str] = {}
        if self.gradle_home:
            props.update(read_properties(self.gradle_home / "gradle.properties"))
        props.update(read_properties(root_dir / "gradle.properties"))
        if directory != root_dir:
            props.update(read_properties(directory / "gradle.properties"))
        return props

    def _read(self, path: Path, model: BuildModel) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            model.warnings.append("cannot read %s: %s" % (path, exc))
            return ""
        if path not in model.build_files:
            model.build_files.append(path)
        return strip_comments(text)

    def _texts_with_applied(self, path: Path, scope: Scope, model: BuildModel, seen: Optional[Set[Path]] = None) -> List[Tuple[Path, str]]:
        """Return the script text plus every script it pulls in with ``apply from:``."""
        seen = seen if seen is not None else set()
        if path in seen:
            return []
        seen.add(path)
        text = self._read(path, model)
        result = [(path, text)]
        collect_variables(text, scope)
        for m in re.finditer(r"\bapply\s*\(?\s*from\s*[:=]\s*(.+)", text):
            raw = unwrap_call(first_expression(m.group(1)), ("file", "project.file", "rootProject.file", "uri"))
            value = scope.resolve(raw)
            if not value:
                model.warnings.append("%s: cannot evaluate 'apply from: %s'" % (path, raw))
                continue
            if re.match(r"^[a-z]+://", value):
                model.warnings.append("%s: remote script %s is not processed" % (path, value))
                continue
            applied = Path(value)
            if not applied.is_absolute():
                applied = path.parent / applied
            applied = applied.resolve()
            if applied.is_file():
                result.extend(self._texts_with_applied(applied, scope, model, seen))
            else:
                model.warnings.append("%s: applied script %s not found" % (path, applied))
        return result

    # -- root / settings -------------------------------------------------
    def _parse_root(self, root_dir: Path, settings_file: Optional[Path], project_dir: Path,
                    build_file: Optional[Path], model: BuildModel) -> None:
        key = (root_dir, project_dir)
        if key in self._parsed_roots:
            return
        self._parsed_roots.add(key)
        root_props = self._properties_for(root_dir, root_dir)
        catalogs: Dict[str, VersionCatalog] = {}
        builtins = {"rootDir": str(root_dir), "projectDir": str(root_dir), "rootProject.projectDir": str(root_dir)}
        settings_scope = Scope(properties=root_props, overrides=self.cli_properties, catalogs=catalogs, builtins=builtins)
        includes: List[Tuple[str, Path]] = []
        included_builds: List[Path] = []
        settings_texts: List[Tuple[Path, str]] = []
        if settings_file:
            settings_texts = self._texts_with_applied(settings_file, settings_scope, model)
            for _path, text in settings_texts:
                includes.extend(self._settings_includes(text, root_dir, settings_scope))
                included_builds.extend(self._included_builds(text, root_dir, settings_scope))
        self._load_catalogs(root_dir, settings_texts, settings_scope, catalogs, model)

        for path, text in settings_texts:
            pm_blocks = find_blocks(text, "pluginManagement")
            self._collect_repositories(text, settings_scope, model, path, plugin_spans=pm_blocks)
            for block in pm_blocks:
                # pluginManagement { plugins { id 'x' version 'y' } } only declares default versions
                for pb in find_blocks(text[block.body_start:block.body_end], "plugins"):
                    body = text[block.body_start:block.body_end][pb.body_start:pb.body_end]
                    for plugin in self._parse_plugins_block(body, settings_scope, path, model):
                        if plugin.version:
                            self._plugin_defaults[plugin.id] = plugin.version
            outside = self._without_blocks(text, pm_blocks)
            for pb in find_blocks(outside, "plugins"):
                for plugin in self._parse_plugins_block(outside[pb.body_start:pb.body_end], settings_scope, path, model):
                    self._add_plugin(plugin, model, path)
        root_scope = Scope(parent=None, properties=root_props, overrides=self.cli_properties, catalogs=catalogs,
                           builtins=builtins)
        root_scope.vars.update(settings_scope.vars)
        root_build = self._find_file(root_dir, self.BUILD_FILE_NAMES)

        projects: List[Tuple[Path, Optional[Path], bool]] = []  # (dir, build file, collect deps)
        is_root_target = project_dir == root_dir
        if root_build:
            projects.append((root_dir, root_build, is_root_target))
        if is_root_target and self.include_subprojects:
            for _name, d in includes:
                projects.append((d, self._find_file(d, self.BUILD_FILE_NAMES), True))
        elif not is_root_target:
            projects.append((project_dir, build_file, True))

        root_texts: List[Tuple[Path, str]] = []
        if root_build:
            root_texts = self._texts_with_applied(root_build, root_scope, model)
        for d, bf, collect in projects:
            if bf is None:
                continue
            if d == root_dir:
                scope, texts = root_scope, root_texts
            else:
                props = self._properties_for(d, root_dir)
                scope = Scope(parent=root_scope, properties=props,
                              builtins={"projectDir": str(d), "rootDir": str(root_dir)})
                texts = self._texts_with_applied(bf, scope, model)
            for path, text in texts:
                if collect:
                    self._collect_from_script(text, scope, model, path)
                else:
                    # still honour shared configuration (repositories, subprojects { dependencies { .. } })
                    self._collect_repositories(text, scope, model, path)
                    for name in ("subprojects", "allprojects"):
                        for block in find_blocks(text, name):
                            self._collect_dependencies(text[block.body_start:block.body_end], scope, model, path)

        if self.include_build_logic:
            build_logic_dirs = [root_dir / "buildSrc"] + included_builds
            for d in build_logic_dirs:
                if d.resolve() == root_dir.resolve() or not d.is_dir():
                    continue
                self._parse_build_logic(d, model)

    @staticmethod
    def _without_blocks(text: str, blocks: List[Block]) -> str:
        chars = list(text)
        for b in blocks:
            for i in range(b.start, min(b.body_end + 1, len(chars))):
                if chars[i] != "\n":
                    chars[i] = " "
        return "".join(chars)

    def _parse_build_logic(self, directory: Path, model: BuildModel) -> None:
        settings = self._find_file(directory, self.SETTINGS_FILE_NAMES)
        build = self._find_file(directory, self.BUILD_FILE_NAMES)
        if settings is None and build is None:
            return
        log.debug("parsing build logic in %s", directory)
        sub = GradleBuildParser(self.gradle_home, self.cli_properties, True, False, self.configuration_filter)
        sub._seen_dep_keys = self._seen_dep_keys
        sub._plugin_defaults = dict(self._plugin_defaults)
        try:
            if settings is not None:
                sub._parse_root(directory, settings, directory, build, model)
            else:
                sub.parse(directory, model)
        except Gradle2MavenError as exc:
            model.warnings.append("build logic %s: %s" % (directory, exc))
            return
        # precompiled script plugins contain dependencies/plugins blocks as well
        scope = Scope(properties=self._properties_for(directory, directory), overrides=self.cli_properties)
        for src in sorted(directory.glob("**/src/main/*/**/*.gradle*")):
            if src.suffix in (".gradle", ".kts") and src.is_file():
                text = self._read(src, model)
                collect_variables(text, scope)
                sub._collect_dependencies_blocks(text, scope, model, src)

    def _settings_includes(self, text: str, root_dir: Path, scope: Scope) -> List[Tuple[str, Path]]:
        result: List[Tuple[str, Path]] = []
        overrides: Dict[str, Path] = {}
        for m in re.finditer(r"project\(\s*(" + _STR + r")\s*\)\.projectDir\s*=\s*(?:new\s+)?(?:file|File)\((.+?)\)\s*$",
                             text, re.M):
            args = split_top_level(m.group(2))
            value = scope.resolve(args[-1]) if args else None
            if value:
                overrides[m.group(1)[1:-1]] = (root_dir / value).resolve()
        for m in re.finditer(r"(?<![\w.])include(Flat)?\b\s*(\(?)", text):
            if m.group(2):
                end = find_matching(text, m.end() - 1)
                args_text = text[m.end():end] if end > 0 else ""
            else:
                args_text = text[m.end():_skip_to_eol(text, m.end())]
            for arg in split_top_level(args_text):
                value = scope.resolve(arg)
                if not value:
                    continue
                name = value if value.startswith(":") else ":" + value
                if name in overrides:
                    d = overrides[name]
                elif m.group(1):
                    d = (root_dir.parent / name.strip(":")).resolve()
                else:
                    d = (root_dir / name.strip(":").replace(":", os.sep)).resolve()
                result.append((name, d))
        return result

    def _included_builds(self, text: str, root_dir: Path, scope: Scope) -> List[Path]:
        result = []
        for m in re.finditer(r"(?<![\w.])includeBuild\s*\(?\s*(" + _STR + r")", text):
            value = scope.resolve(m.group(1))
            if value:
                result.append((root_dir / value).resolve())
        return result

    def _load_catalogs(self, root_dir: Path, settings_texts: List[Tuple[Path, str]], scope: Scope,
                       catalogs: Dict[str, VersionCatalog], model: BuildModel) -> None:
        sources: Dict[str, Path] = {}
        default = root_dir / "gradle" / "libs.versions.toml"
        if default.is_file():
            sources["libs"] = default
        for path, text in settings_texts:
            for block in find_blocks(text, "versionCatalogs"):
                body = text[block.body_start:block.body_end]
                for m in re.finditer(r"(?:create|register|maybeCreate)?\s*\(?\s*(" + _STR + r"|\w+)\s*\)?\s*\{", body):
                    name = m.group(1).strip("'\"")
                    close = find_matching(body, m.end() - 1)
                    inner = body[m.end():close] if close > 0 else ""
                    fm = re.search(r"from\s*\(?\s*files\(\s*(.+?)\s*\)", inner)
                    if fm:
                        value = scope.resolve(fm.group(1))
                        if value:
                            sources[name] = (path.parent / value).resolve()
                    elif re.search(r"from\s*\(?\s*" + _STR, inner):
                        model.warnings.append("%s: published version catalogs (from(\"g:a:v\")) are not supported" % path)
        for name, p in sources.items():
            if not p.is_file():
                model.warnings.append("version catalog %s not found: %s" % (name, p))
                continue
            try:
                catalogs[name] = VersionCatalog.load(name, p)
                if p not in model.build_files:
                    model.build_files.append(p)
            except Exception as exc:  # noqa: BLE001 - user files may be malformed in many ways
                model.warnings.append("cannot load version catalog %s: %s" % (p, exc))

    # -- build scripts ---------------------------------------------------
    def _collect_from_script(self, text: str, scope: Scope, model: BuildModel, path: Path) -> None:
        self._collect_repositories(text, scope, model, path)
        for block in find_blocks(text, "plugins"):
            for plugin in self._parse_plugins_block(text[block.body_start:block.body_end], scope, path, model):
                self._add_plugin(plugin, model, path)
        self._collect_dependencies_blocks(text, scope, model, path)
        for m in re.finditer(r"(?<![\w.])force\s*\(?\s*(" + _STR + r")", text):  # resolutionStrategy.force
            value = scope.resolve(m.group(1))
            dep = parse_coordinates(value) if value else None
            if dep and dep.version:
                dep.configuration, dep.origin = "resolutionStrategy.force", str(path)
                self._add_dependency(dep, model)
        for m in re.finditer(r"\bmavenBom\s*\(?\s*(.+)", text):  # io.spring.dependency-management
            value = scope.resolve(first_expression(m.group(1)))
            dep = parse_coordinates(value) if value else None
            if dep:
                dep.configuration, dep.platform, dep.origin = "dependencyManagement", True, str(path)
                self._add_dependency(dep, model)

    def _collect_dependencies_blocks(self, text: str, scope: Scope, model: BuildModel, path: Path) -> None:
        blocks = find_blocks(text, "dependencies")
        # nested dependencies blocks are scanned through their parent
        top = [b for b in blocks if not any(o is not b and o.body_start <= b.start < o.body_end for o in blocks)]
        for block in top:
            self._collect_dependencies(text[block.body_start:block.body_end], scope, model, path)

    def _add_plugin(self, plugin: PluginSpec, model: BuildModel, path: Path) -> None:
        if not plugin.version:
            plugin.version = self._plugin_defaults.get(plugin.id)
        for existing in model.plugins:
            if existing.id == plugin.id and (existing.version == plugin.version or not plugin.version):
                return
        # a version-less application (version comes from a parent project) is superseded by a versioned one
        model.plugins[:] = [p for p in model.plugins if p.id != plugin.id or p.version]
        model.plugins.append(plugin)

    def _parse_plugins_block(self, body: str, scope: Scope, path: Path, model: BuildModel) -> List[PluginSpec]:
        plugins = []
        for line in re.split(r"[\n;]", body):
            line = line.strip()
            if not line:
                continue
            pid = version = None
            m = re.match(r"id\s*\(?\s*(" + _STR + r")\s*\)?(.*)$", line)
            if m:
                pid = scope.resolve(m.group(1))
                rest = m.group(2)
            else:
                m = re.match(r"kotlin\s*\(\s*(" + _STR + r")\s*\)(.*)$", line)
                if m:
                    name = scope.resolve(m.group(1))
                    pid = _KOTLIN_PLUGIN_PREFIX + name if name else None
                    rest = m.group(2)
                else:
                    m = re.match(r"alias\s*\(\s*(\w+)\.plugins\.([\w.]+?)(?:\.get\(\))?\s*\)(.*)$", line)
                    if m and m.group(1) in scope.catalogs:
                        found = scope.catalogs[m.group(1)].plugin(m.group(2))
                        if found:
                            pid, version = found
                        else:
                            model.warnings.append("%s: unknown catalog plugin %s.plugins.%s" % (path, m.group(1), m.group(2)))
                        rest = m.group(3)
                    else:
                        continue
            vm = re.search(r"\bversion\s*\(?\s*(.+?)\s*\)?\s*(?:\bapply\b.*)?$", rest or "")
            if vm:
                version = scope.resolve(vm.group(1)) or version
                if version is None:
                    model.warnings.append("%s: cannot evaluate plugin version %r for %s" % (path, vm.group(1), pid))
            if pid:
                plugins.append(PluginSpec(pid, normalize_requested_version(version), str(path)))
        return plugins

    def _collect_repositories(self, text: str, scope: Scope, model: BuildModel, path: Path,
                              plugin_spans: Optional[List[Block]] = None) -> None:
        excluded = find_blocks(text, "publishing") + find_blocks(text, "uploadArchives")
        plugin_spans = plugin_spans or []
        buildscript = find_blocks(text, "buildscript")
        for block in find_blocks(text, "repositories"):
            if _inside(block.start, excluded):
                continue
            is_plugin = _inside(block.start, plugin_spans) or _inside(block.start, buildscript)
            body = text[block.body_start:block.body_end]
            for stmt in iter_statements(body):
                name = stmt.name
                if name in WELL_KNOWN_REPOS:
                    model.add_repo(RepoSpec(WELL_KNOWN_REPOS[name], name), plugin=is_plugin)
                elif name == "mavenLocal":
                    model.uses_maven_local = True
                elif name == "maven":
                    repo = self._parse_maven_repo(stmt, scope, path, model)
                    if repo:
                        model.add_repo(repo, plugin=is_plugin)
                elif name in ("ivy", "flatDir"):
                    model.warnings.append("%s: %s repositories are not supported" % (path, name))
                elif name in ("exclusiveContent", "forRepository", "forRepositories") and stmt.closure:
                    self._collect_repositories("repositories {" + stmt.closure + "}", scope, model, path)

    def _parse_maven_repo(self, stmt: Statement, scope: Scope, path: Path, model: BuildModel) -> Optional[RepoSpec]:
        url_expr = None
        for arg in split_top_level(stmt.args or ""):
            m = re.match(r"^(\w+)\s*[=:]\s*(.+)$", arg, re.S)
            if m and m.group(1) != "url":
                continue
            url_expr = m.group(2) if m else arg
            break
        closure = stmt.closure or ""
        if url_expr is None:
            m = re.search(r"(?<![\w.])(?:url\s*(?:=|\.set\()?|setUrl\s*\(?)\s*", closure)
            if m:
                url_expr = first_expression(closure[m.end():])
        if not url_expr:
            model.warnings.append("%s: maven repository without a URL" % path)
            return None
        url_expr = unwrap_call(first_expression(url_expr), ("uri", "file", "project.uri", "project.file", "URI",
                                                            "java.net.URI", "new URI"))
        url = scope.resolve(url_expr)
        if not url or "$" in url:
            model.warnings.append("%s: cannot evaluate maven repository URL %s" % (path, url_expr))
            return None
        if not re.match(r"^[a-zA-Z][\w+.-]*://", url) and not url.startswith("file:"):
            p = Path(url)
            p = p if p.is_absolute() else (path.parent / p)
            url = p.resolve().as_uri()
        repo = RepoSpec(url)
        nm = re.search(r"(?<![\w.])name\s*=?\s*(" + _STR + r")", closure)
        if nm:
            repo.name = nm.group(1)[1:-1]
        um = re.search(r"(?<![\w.])username\s*(?:=|\.set\()?\s*", closure)
        pm = re.search(r"(?<![\w.])password\s*(?:=|\.set\()?\s*", closure)
        if um and pm:
            repo.username = scope.resolve(first_expression(closure[um.end():]))
            repo.password = scope.resolve(first_expression(closure[pm.end():]))
        return repo

    # -- dependencies ----------------------------------------------------
    def _add_dependency(self, dep: Dependency, model: BuildModel) -> None:
        key = (dep.group, dep.artifact, dep.version, dep.classifier, dep.ext, dep.platform, dep.configuration)
        if key in self._seen_dep_keys:
            return
        self._seen_dep_keys.add(key)
        model.dependencies.append(dep)

    def _collect_dependencies(self, body: str, scope: Scope, model: BuildModel, path: Path) -> None:
        for stmt in iter_statements(body):
            name = stmt.name
            if name in _NOT_CONFIGURATIONS or stmt.args is None:
                if name == "add" and stmt.args:
                    args = split_top_level(stmt.args)
                    config = scope.resolve(args[0]) if args else None
                    if config and len(args) >= 2:
                        self._handle_notation(config, ", ".join(args[1:2]), stmt.closure, scope, model, path)
                    continue
                if stmt.closure and name not in ("exclude", "version", "attributes", "capabilities", "because"):
                    self._collect_dependencies(stmt.closure, scope, model, path)
                continue
            if "." in name:
                # e.g. ``commonMain.dependencies { }`` - recurse; other method calls are skipped
                if stmt.closure:
                    self._collect_dependencies(stmt.closure, scope, model, path)
                continue
            self._handle_notation(name, stmt.args, stmt.closure, scope, model, path)

    def _handle_notation(self, config: str, args: str, closure: Optional[str], scope: Scope,
                         model: BuildModel, path: Path) -> None:
        if not self.configuration_filter(config):
            return
        deps = self.parse_notation(args, scope, model, path)
        if not deps:
            return
        excludes, transitive, version = self._parse_dep_closure(closure, scope) if closure else (frozenset(), True, None)
        for dep in deps:
            dep.configuration = config
            dep.origin = dep.origin or str(path)
            if excludes:
                dep.excludes = dep.excludes | excludes
            if not transitive:
                dep.transitive = False
            if version and not dep.version:
                dep.version = version
            dep.version = normalize_requested_version(dep.version)
            self._add_dependency(dep, model)

    def parse_notation(self, args: str, scope: Scope, model: BuildModel, path: Path) -> List[Dependency]:
        args = args.strip()
        if not args:
            return []
        parts = split_top_level(args)
        named = {}
        for p in parts:
            m = re.match(r"^(\w+)\s*[:=]\s*(.+)$", p, re.S)
            if m and m.group(1) in ("group", "name", "version", "classifier", "ext", "extension", "configuration",
                                    "transitive", "module"):
                named[m.group(1)] = m.group(2)
        if named:
            values = {k: scope.resolve(v) for k, v in named.items()}
            group, artifact = values.get("group"), values.get("name")
            if not group or not artifact:
                model.warnings.append("%s: cannot evaluate dependency %s" % (path, args))
                return []
            dep = Dependency(group, artifact, values.get("version"), values.get("classifier"),
                             values.get("ext") or values.get("extension"))
            if (values.get("transitive") or "").lower() == "false":
                dep.transitive = False
            return [dep]
        if len(parts) == 3 and all(string_literal(p) for p in parts) and ":" not in args:
            # Kotlin DSL: implementation("group", "name", "version")
            values = [scope.resolve(p) for p in parts]
            if all(values):
                return [Dependency(values[0], values[1], values[2])]  # type: ignore[arg-type]
        result: List[Dependency] = []
        for part in parts:
            if re.match(r"^\w+\s*[:=]", part):  # unknown named argument
                continue
            result.extend(self._parse_single_notation(part, scope, model, path))
        return result

    def _parse_single_notation(self, part: str, scope: Scope, model: BuildModel, path: Path,
                               platform: bool = False) -> List[Dependency]:
        p = part.strip()
        if _SKIP_NOTATIONS.match(p) or p.startswith("{"):
            return []
        m = _WRAPPERS.match(p)
        if m:
            close = find_matching(p, m.end() - 1)
            inner = p[m.end():close] if close > 0 else p[m.end():]
            is_platform = platform or m.group(1).endswith(("platform", "Platform"))
            inner_parts = split_top_level(inner)
            if m.group(1) in ("variantOf",) or not inner_parts:
                inner_parts = inner_parts[:1]
            deps: List[Dependency] = []
            if len(inner_parts) == 1:
                deps = self._parse_single_notation(inner_parts[0], scope, model, path, is_platform)
            else:
                deps = self.parse_notation(inner, scope, model, path)
            if m.group(1) == "testFixtures":
                for d in deps:
                    d.classifier = d.classifier or "test-fixtures"
            if m.group(1) == "variantOf" and close > 0:
                cm = re.search(r"classifier\s*\(\s*(" + _STR + r")", p[close:])
                if cm:
                    for d in deps:
                        d.classifier = scope.resolve(cm.group(1))
                am = re.search(r"artifactType\s*\(\s*(" + _STR + r")", p[close:])
                if am:
                    for d in deps:
                        d.ext = scope.resolve(am.group(1))
            if is_platform:
                for d in deps:
                    d.platform = True
            return deps
        m = re.match(r"^kotlin\s*\(\s*(" + _STR + r")\s*(?:,\s*(.+?))?\s*\)$", p, re.S)
        if m:
            module = scope.resolve(m.group(1))
            version = scope.resolve(m.group(2)) if m.group(2) else self._kotlin_version(model, scope)
            if not module:
                return []
            dep = Dependency("org.jetbrains.kotlin", "kotlin-" + module, version)
            if not version:
                model.warnings.append("%s: no Kotlin version known for kotlin(\"%s\")" % (path, module))
            return [dep]
        # version catalog accessor
        m = re.match(r"^(\w+)\.([\w.]+?)(?:\.get\(\))?$", p)
        if m and m.group(1) in scope.catalogs:
            found = scope.catalogs[m.group(1)].lookup(m.group(2))
            if found is None:
                model.warnings.append("%s: unknown version catalog entry %s" % (path, p))
                return []
            return found
        value = scope.resolve(p)
        if value is None:
            raw = scope.lookup_raw(normalize_name(p)) if re.fullmatch(r"[\w.$\[\]'\"]+", p) else None
            if raw and raw.strip().startswith("["):  # list of notations held in a variable
                end = find_matching(raw.strip(), 0)
                items = split_top_level(raw.strip()[1:end]) if end > 0 else []
                deps = []
                for item in items:
                    deps.extend(self._parse_single_notation(item, scope, model, path, platform))
                return deps
            if raw:
                return self._parse_single_notation(raw, scope, model, path, platform)
            model.warnings.append("%s: cannot evaluate dependency notation %s" % (path, p))
            return []
        dep = parse_coordinates(value)
        if dep is None:
            if "$" in value:
                model.warnings.append("%s: unresolved variable in dependency %s" % (path, value))
            else:
                model.warnings.append("%s: not a dependency notation: %s" % (path, value))
            return []
        dep.platform = platform
        return [dep]

    def _kotlin_version(self, model: BuildModel, scope: Scope) -> Optional[str]:
        for plugin in model.plugins:
            if plugin.id.startswith(_KOTLIN_PLUGIN_PREFIX) and plugin.version:
                return plugin.version
        for name in ("kotlin_version", "kotlinVersion", "kotlin.version", "versions.kotlin"):
            v = scope.resolve(name)
            if v:
                return v
        for cat in scope.catalogs.values():
            v = cat.version_ref("kotlin")
            if v:
                return v
        return None

    def _parse_dep_closure(self, closure: str, scope: Scope) -> Tuple[frozenset, bool, Optional[str]]:
        excludes = set()
        for m in re.finditer(r"\bexclude\s*\(?\s*(?:mapOf\()?(.+)", closure):
            args = m.group(1).rstrip(") \t")
            group = module = None
            for a in split_top_level(args):
                km = re.match(r"(?:(\w+)\s*[:=]|(" + _STR + r")\s+to)\s*(.+)$", a)
                if not km:
                    continue
                key = km.group(1) or km.group(2)[1:-1]
                value = scope.resolve(km.group(3))
                if key == "group":
                    group = value
                elif key == "module":
                    module = value
            if group or module:
                excludes.add((group or "*", module or "*"))
        transitive = not re.search(r"\b(?:isTransitive|transitive)\s*=?\s*false\b", closure)
        version = None
        vm = re.search(r"\b(strictly|require|prefer)\s*\(?\s*(" + _STR + r")", closure)
        if vm:
            version = scope.resolve(vm.group(2))
        return frozenset(excludes), transitive, version


# Tool classpaths the Kotlin Gradle plugin resolves at build time (not visible in build scripts).
KOTLIN_IMPLICIT_ARTIFACTS = (
    ("kotlin-compiler-embeddable", "kotlinCompilerClasspath"),
    ("kotlin-build-tools-impl", "kotlinBuildToolsApiClasspath"),
    ("kotlin-scripting-compiler-embeddable", "kotlinCompilerPluginClasspath"),
)


def finalize_model(model: BuildModel, add_plugin_portal: bool, implicit: bool = True,
                   properties: Optional[Dict[str, str]] = None) -> None:
    """Add plugin markers and implicit dependencies, and fill versions known from elsewhere."""
    properties = properties or {}
    by_ga: Dict[Tuple[str, str], str] = {}
    for dep in model.dependencies:
        if dep.version and not is_dynamic_version(dep.version):
            by_ga.setdefault(dep.ga, dep.version)
    kotlin_version = None
    for plugin in model.plugins:
        if plugin.version:
            if plugin.id.startswith(_KOTLIN_PLUGIN_PREFIX) and kotlin_version is None:
                kotlin_version = plugin.version
            dep = plugin_marker(plugin.id, plugin.version)
            dep.origin = plugin.origin
            model.dependencies.append(dep)
    if implicit:
        ids = {p.id: p.version for p in model.plugins}
        boot = ids.get("org.springframework.boot")
        if boot and "io.spring.dependency-management" in ids:
            model.dependencies.append(Dependency("org.springframework.boot", "spring-boot-dependencies", boot,
                                                 configuration="dependencyManagement", platform=True,
                                                 origin="implicit: org.springframework.boot plugin"))
        kotlin_jvm = [p for p in ids if p in ("org.jetbrains.kotlin.jvm", "org.jetbrains.kotlin.android",
                                               "org.jetbrains.kotlin.multiplatform")]
        if kotlin_version and kotlin_jvm and properties.get("kotlin.stdlib.default.dependency", "true") != "false":
            if ("org.jetbrains.kotlin", "kotlin-stdlib") not in by_ga:
                model.dependencies.append(Dependency("org.jetbrains.kotlin", "kotlin-stdlib", kotlin_version,
                                                     configuration="implementation",
                                                     origin="implicit: Kotlin Gradle plugin"))
        if kotlin_version and kotlin_jvm:
            for artifact, config in KOTLIN_IMPLICIT_ARTIFACTS:
                model.dependencies.append(Dependency("org.jetbrains.kotlin", artifact, kotlin_version,
                                                     configuration=config, optional=True,
                                                     origin="implicit: Kotlin Gradle plugin"))
    if add_plugin_portal and any(p.version for p in model.plugins):
        if not model.plugin_repositories:
            model.add_repo(RepoSpec(GRADLE_PLUGIN_PORTAL, "gradlePluginPortal"), plugin=True)
    for dep in model.dependencies:
        if not dep.version and dep.ga in by_ga:
            dep.version = by_ga[dep.ga]


# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

class NotFound(Exception):
    pass


class FetchError(Exception):
    pass


def is_ssl_error(exc: BaseException) -> bool:
    seen = set()
    stack: List[Optional[BaseException]] = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, (ssl.SSLError, ssl.CertificateError)):
            return True
        reason = getattr(e, "reason", None)
        if isinstance(reason, BaseException):
            stack.append(reason)
        elif isinstance(reason, str) and "CERTIFICATE_VERIFY_FAILED" in reason:
            return True
        stack.extend([e.__cause__, e.__context__])
    return False


class HttpClient:
    """urllib based downloader with retries and an SSL "ignore certificate" fallback."""

    def __init__(self, timeout: float = 30.0, retries: int = 2, insecure: bool = False, ssl_fallback: bool = True,
                 credentials: Optional[Dict[str, Tuple[str, str]]] = None, ca_file: Optional[str] = None):
        self.timeout = timeout
        self.retries = retries
        self.insecure = insecure
        self.ssl_fallback = ssl_fallback
        self.credentials = credentials or {}
        self._verified = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
        self._unverified = ssl.create_default_context()
        self._unverified.check_hostname = False
        self._unverified.verify_mode = ssl.CERT_NONE
        self.insecure_hosts: Set[str] = set()
        self._lock = threading.Lock()
        self.requests = 0
        self.bytes = 0

    def _auth_for(self, url: str) -> Optional[Tuple[str, str]]:
        best = None
        for prefix, cred in self.credentials.items():
            if url.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
                best = (prefix, cred)
        return best[1] if best else None

    def download(self, url: str, dest: Path) -> bool:
        """Download ``url`` to ``dest``.  Returns False on 404, raises FetchError on failure."""
        host = urllib.parse.urlsplit(url).hostname or ""
        attempt = 0
        while True:
            insecure = self.insecure or host in self.insecure_hosts
            try:
                self._download_once(url, dest, insecure)
                return True
            except NotFound:
                return False
            except Exception as exc:  # noqa: BLE001
                if is_ssl_error(exc) and not insecure and self.ssl_fallback:
                    with self._lock:
                        first = host not in self.insecure_hosts
                        self.insecure_hosts.add(host)
                    if first:
                        log.warning("SSL error for %s (%s); retrying with certificate verification disabled",
                                    host, _describe(exc))
                    continue
                attempt += 1
                if attempt > self.retries or not _retryable(exc):
                    raise FetchError("%s: %s" % (url, _describe(exc))) from exc
                delay = min(0.5 * 2 ** (attempt - 1), 8.0)
                log.debug("retrying %s in %.1fs (%s)", url, delay, _describe(exc))
                time.sleep(delay)

    def _download_once(self, url: str, dest: Path, insecure: bool) -> None:
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        auth = self._auth_for(url)
        if auth:
            token = base64.b64encode(("%s:%s" % auth).encode()).decode()
            headers["Authorization"] = "Basic " + token
        req = urllib.request.Request(url, headers=headers)
        ctx = self._unverified if insecure else self._verified
        with self._lock:
            self.requests += 1
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout, context=ctx)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                raise NotFound(url) from None
            if exc.code == 403:
                log.debug("403 Forbidden for %s (treated as not found)", url)
                raise NotFound(url) from None
            raise
        with resp:
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".part-", dir=str(dest.parent))
            try:
                size = 0
                with os.fdopen(fd, "wb") as fh:
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        fh.write(chunk)
                        size += len(chunk)
                os.replace(tmp, dest)
                with self._lock:
                    self.bytes += size
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise


def _describe(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTP %s %s" % (exc.code, exc.reason)
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    return "%s: %s" % (type(exc).__name__, exc)


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 425, 429, 500, 502, 503, 504)
    return isinstance(exc, (urllib.error.URLError, OSError, TimeoutError, ConnectionError))


# ---------------------------------------------------------------------------
# Artifact sources
# ---------------------------------------------------------------------------

def gav_dir(group: str, artifact: str, version: str) -> str:
    return "%s/%s/%s" % (group.replace(".", "/"), artifact, version)


def artifact_filename(artifact: str, version: str, classifier: Optional[str], ext: str) -> str:
    return "%s-%s%s.%s" % (artifact, version, "-" + classifier if classifier else "", ext)


_TIMESTAMPED = r"\d{8}\.\d{6}-\d+"


class GradleCache:
    """Read access to ``$GRADLE_USER_HOME/caches/modules-2/files-2.1`` (and read-only caches)."""

    def __init__(self, roots: List[Path]):
        self.roots = [r for r in roots if r.is_dir()]

    @classmethod
    def from_gradle_home(cls, gradle_home: Path) -> "GradleCache":
        roots = [gradle_home / "caches" / "modules-2" / "files-2.1"]
        ro = os.environ.get("GRADLE_RO_DEP_CACHE")
        if ro:
            roots.append(Path(ro) / "modules-2" / "files-2.1")
        return cls(roots)

    def find(self, group: str, artifact: str, version: str, filename: str) -> Optional[Path]:
        for root in self.roots:
            d = root / group / artifact / version
            if not d.is_dir():
                continue
            for h in d.iterdir():
                p = h / filename
                if p.is_file():
                    return p
            if version.endswith("-SNAPSHOT"):
                base = version[:-len("SNAPSHOT")]
                rest = filename[len(artifact) + 1 + len(version):]
                rx = re.compile(re.escape(artifact + "-" + base) + _TIMESTAMPED + re.escape(rest) + "$")
                matches = sorted(p for h in d.iterdir() for p in h.iterdir() if rx.match(p.name))
                if matches:
                    return matches[-1]
        return None

    def versions(self, group: str, artifact: str) -> Set[str]:
        out: Set[str] = set()
        for root in self.roots:
            d = root / group / artifact
            if d.is_dir():
                out.update(p.name for p in d.iterdir() if p.is_dir())
        return out

    def iter_files(self) -> Iterator[Tuple[str, str, str, Path]]:
        for root in self.roots:
            for g in sorted(root.iterdir()):
                if not g.is_dir():
                    continue
                for a in sorted(g.iterdir()):
                    if not a.is_dir():
                        continue
                    for v in sorted(a.iterdir()):
                        if not v.is_dir():
                            continue
                        for h in v.iterdir():
                            if h.is_dir():
                                for f in h.iterdir():
                                    if f.is_file():
                                        yield g.name, a.name, v.name, f


class LocalMavenSource:
    """A Maven layout directory used as a source (``~/.m2/repository`` or a ``file://`` repo)."""

    def __init__(self, root: Path, label: str):
        self.root = root
        self.label = label

    def find(self, group: str, artifact: str, version: str, filename: str) -> Optional[Path]:
        p = self.root / gav_dir(group, artifact, version) / filename
        return p if p.is_file() else None

    def versions(self, group: str, artifact: str) -> Set[str]:
        d = self.root / group.replace(".", "/") / artifact
        if not d.is_dir():
            return set()
        return {p.name for p in d.iterdir() if p.is_dir()}


class RemoteRepository:
    def __init__(self, url: str, http: HttpClient, name: str = ""):
        self.url = url.rstrip("/") + "/"
        self.name = name or self.url
        self.http = http
        self._snapshot_meta: Dict[Tuple[str, str, str], Optional[ET.Element]] = {}
        self._lock = threading.Lock()
        self.errors = 0

    def file_url(self, group: str, artifact: str, version: str, filename: str) -> str:
        return self.url + gav_dir(group, artifact, version) + "/" + filename

    def fetch(self, group: str, artifact: str, version: str, filename: str, dest: Path) -> bool:
        if self.http.download(self.file_url(group, artifact, version, filename), dest):
            return True
        if version.endswith("-SNAPSHOT"):
            real = self._timestamped_name(group, artifact, version, filename)
            if real and real != filename:
                return self.http.download(self.file_url(group, artifact, version, real), dest)
        return False

    def _timestamped_name(self, group: str, artifact: str, version: str, filename: str) -> Optional[str]:
        key = (group, artifact, version)
        with self._lock:
            known = key in self._snapshot_meta
        if not known:
            root = None
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td) / "maven-metadata.xml"
                try:
                    if self.http.download(self.url + gav_dir(group, artifact, version) + "/maven-metadata.xml", tmp):
                        root = _parse_xml(tmp)
                except FetchError:
                    root = None
            with self._lock:
                self._snapshot_meta[key] = root
        root = self._snapshot_meta.get(key)
        if root is None:
            return None
        rest = filename[len(artifact) + 1 + len(version):]  # "-classifier.ext" or ".ext"
        ext = rest.rsplit(".", 1)[-1] if "." in rest else ""
        classifier = rest[1:].rsplit(".", 1)[0] if rest.startswith("-") else ""
        if rest.endswith((".pom", ".module")):
            classifier = ""
        for sv in root.iter("snapshotVersion"):
            if (_text(sv, "extension") or "") == ext and (_text(sv, "classifier") or "") == classifier:
                value = _text(sv, "value")
                if value:
                    return "%s-%s%s" % (artifact, value, rest)
        snap = root.find("versioning/snapshot")
        if snap is not None:
            ts, bn = _text(snap, "timestamp"), _text(snap, "buildNumber")
            if ts and bn:
                return "%s-%s%s-%s%s" % (artifact, version[:-len("SNAPSHOT")], ts, bn, rest)
        return None

    def versions(self, group: str, artifact: str) -> Set[str]:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "maven-metadata.xml"
            try:
                if not self.http.download(self.url + group.replace(".", "/") + "/" + artifact + "/maven-metadata.xml", tmp):
                    return set()
            except FetchError as exc:
                log.warning("cannot list versions of %s:%s in %s: %s", group, artifact, self.url, exc)
                return set()
            root = _parse_xml(tmp)
        if root is None:
            return set()
        return {v.text.strip() for v in root.iter("version") if v.text and v.text.strip()}


# ---------------------------------------------------------------------------
# POM / Gradle module metadata
# ---------------------------------------------------------------------------

def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _parse_xml(path: Path) -> Optional[ET.Element]:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        return _strip_ns(ET.fromstring(data))
    except ET.ParseError:
        # Some old POMs use HTML entities or stray '&'; make a best effort.
        text = data.decode("utf-8", errors="replace")
        text = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
        try:
            return _strip_ns(ET.fromstring(text))
        except ET.ParseError:
            return None


def _text(el: Optional[ET.Element], path: str) -> Optional[str]:
    if el is None:
        return None
    found = el.find(path)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


@dataclasses.dataclass
class PomDependency:
    group: str
    artifact: str
    version: Optional[str]
    type: str = "jar"
    classifier: Optional[str] = None
    scope: Optional[str] = None
    optional: bool = False
    exclusions: Tuple[Tuple[str, str], ...] = ()

    @property
    def key(self) -> Tuple[str, str, str, Optional[str]]:
        return self.group, self.artifact, self.type, self.classifier


@dataclasses.dataclass
class RawPom:
    group: Optional[str]
    artifact: str
    version: Optional[str]
    packaging: str
    parent: Optional[Tuple[str, str, str]]
    properties: Dict[str, str]
    managed: List[PomDependency]
    dependencies: List[PomDependency]
    relocation: Optional[Tuple[Optional[str], Optional[str], Optional[str]]]
    gradle_metadata: bool


def _pom_dependency(el: ET.Element) -> Optional[PomDependency]:
    g, a = _text(el, "groupId"), _text(el, "artifactId")
    if not g or not a:
        return None
    exclusions = []
    for ex in el.findall("exclusions/exclusion"):
        exclusions.append((_text(ex, "groupId") or "*", _text(ex, "artifactId") or "*"))
    return PomDependency(
        g, a, _text(el, "version"), _text(el, "type") or "jar", _text(el, "classifier"), _text(el, "scope"),
        (_text(el, "optional") or "false").lower() == "true", tuple(exclusions))


def parse_pom(path: Path) -> Optional[RawPom]:
    root = _parse_xml(path)
    if root is None or root.tag != "project":
        return None
    parent = None
    pe = root.find("parent")
    if pe is not None:
        pg, pa, pv = _text(pe, "groupId"), _text(pe, "artifactId"), _text(pe, "version")
        if pg and pa and pv:
            parent = (pg, pa, pv)
    props = {}
    pr = root.find("properties")
    if pr is not None:
        for child in pr:
            if isinstance(child.tag, str):
                props[child.tag] = (child.text or "").strip()
    managed = [d for d in (_pom_dependency(e) for e in root.findall("dependencyManagement/dependencies/dependency")) if d]
    deps = [d for d in (_pom_dependency(e) for e in root.findall("dependencies/dependency")) if d]
    relocation = None
    rel = root.find("distributionManagement/relocation")
    if rel is not None:
        relocation = (_text(rel, "groupId"), _text(rel, "artifactId"), _text(rel, "version"))
    raw = path.read_bytes()
    return RawPom(
        _text(root, "groupId"), _text(root, "artifactId") or "", _text(root, "version"),
        _text(root, "packaging") or "jar", parent, props, managed, deps, relocation,
        b"published-with-gradle-metadata" in raw)


@dataclasses.dataclass
class EffectivePom:
    group: str
    artifact: str
    version: str
    packaging: str
    dependencies: List[PomDependency]
    managed: Dict[Tuple[str, str, str, Optional[str]], PomDependency]
    relocation: Optional[Tuple[str, str, str]]
    gradle_metadata: bool


_PROP_RE = re.compile(r"\$\{([^}]+)\}")


def interpolate_pom(value: Optional[str], props: Dict[str, str]) -> Optional[str]:
    if value is None or "${" not in value:
        return value
    for _ in range(10):
        new = _PROP_RE.sub(lambda m: props.get(m.group(1), m.group(0)), value)
        if new == value:
            break
        value = new
    return value


PACKAGING_EXTENSIONS = {
    "jar": "jar", "bundle": "jar", "maven-plugin": "jar", "eclipse-plugin": "jar", "orbit": "jar",
    "hk2-jar": "jar", "takari-jar": "jar", "kotlin-plugin": "jar", "nbm": "jar", "jbi-component": "jar",
    "ejb": "jar", "test-jar": "jar", "aar": "aar", "war": "war", "ear": "ear", "rar": "rar", "klib": "klib",
}
TYPE_MAPPING = {  # dependency <type> -> (extension, classifier)
    "jar": ("jar", None), "bundle": ("jar", None), "test-jar": ("jar", "tests"), "ejb": ("jar", None),
    "ejb-client": ("jar", "client"), "java-source": ("jar", "sources"), "javadoc": ("jar", "javadoc"),
    "maven-plugin": ("jar", None), "aar": ("aar", None), "pom": ("pom", None),
}


# ---------------------------------------------------------------------------
# Output repository
# ---------------------------------------------------------------------------

class OutputRepository:
    def __init__(self, root: Path, checksums: Iterable[str] = ("sha1", "md5")):
        self.root = root
        self.checksums = list(checksums)

    def path(self, group: str, artifact: str, version: str, filename: str) -> Path:
        return self.root / gav_dir(group, artifact, version) / filename

    def write_checksums(self, path: Path) -> None:
        if not self.checksums or path.name.endswith(tuple("." + c for c in CHECKSUM_ALGORITHMS)):
            return
        hashes = {c: hashlib.new(CHECKSUM_ALGORITHMS[c]) for c in self.checksums}
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                for h in hashes.values():
                    h.update(chunk)
        for name, h in hashes.items():
            Path(str(path) + "." + name).write_text(h.hexdigest(), encoding="ascii")

    def copy_in(self, src: Path, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".part-", dir=str(dest.parent))
        os.close(fd)
        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def write_maven_metadata(self) -> int:
        """Write ``maven-metadata.xml`` for every ``group/artifact`` directory (enables dynamic versions)."""
        artifact_dirs = set()
        for pom in self.root.rglob("*.pom"):
            version_dir = pom.parent
            artifact_dir = version_dir.parent
            if pom.name == "%s-%s.pom" % (artifact_dir.name, version_dir.name):
                artifact_dirs.add(artifact_dir)
        stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
        for artifact_dir in sorted(artifact_dirs):
            versions = sorted(
                (d.name for d in artifact_dir.iterdir()
                 if d.is_dir() and (d / ("%s-%s.pom" % (artifact_dir.name, d.name))).is_file()),
                key=version_key)
            group = artifact_dir.parent.relative_to(self.root).as_posix().replace("/", ".")
            releases = [v for v in versions if not v.endswith("-SNAPSHOT")]
            lines = [
                '<?xml version="1.0" encoding="UTF-8"?>',
                "<metadata>",
                "  <groupId>%s</groupId>" % _xml_escape(group),
                "  <artifactId>%s</artifactId>" % _xml_escape(artifact_dir.name),
                "  <versioning>",
                "    <latest>%s</latest>" % _xml_escape(versions[-1]),
            ]
            if releases:
                lines.append("    <release>%s</release>" % _xml_escape(releases[-1]))
            lines.append("    <versions>")
            lines.extend("      <version>%s</version>" % _xml_escape(v) for v in versions)
            lines += ["    </versions>", "    <lastUpdated>%s</lastUpdated>" % stamp,
                      "  </versioning>", "</metadata>", ""]
            target = artifact_dir / "maven-metadata.xml"
            target.write_text("\n".join(lines), encoding="utf-8")
            self.write_checksums(target)
        return len(artifact_dirs)


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class ModuleRecord:
    group: str
    artifact: str
    version: str
    packaging: str = "jar"
    found: bool = False
    gradle_metadata: bool = False
    files: Dict[str, str] = dataclasses.field(default_factory=dict)  # filename -> source
    variant_files: List[str] = dataclasses.field(default_factory=list)
    extra_artifacts: Set[Tuple[Optional[str], str]] = dataclasses.field(default_factory=set)
    requested_by: Set[str] = dataclasses.field(default_factory=set)
    missing: List[str] = dataclasses.field(default_factory=list)
    processed: List[Tuple[frozenset, bool]] = dataclasses.field(default_factory=list)  # (excludes, transitive)
    depth: int = 0
    optional_only: bool = True  # only reached through optional (implicit) requests

    @property
    def coord(self) -> str:
        return "%s:%s:%s" % (self.group, self.artifact, self.version)


@dataclasses.dataclass
class WorkItem:
    group: str
    artifact: str
    version: str
    excludes: frozenset = frozenset()
    transitive: bool = True
    requested_by: str = ""
    depth: int = 0
    classifier: Optional[str] = None
    ext: Optional[str] = None
    artifact_only: bool = False
    optional: bool = False


@dataclasses.dataclass
class ResolverOptions:
    scopes: Tuple[str, ...] = DEFAULT_SCOPES
    transitive: bool = True
    include_optional: bool = False
    sources: bool = False
    javadoc: bool = False
    conflict_strategy: str = "all"
    kotlin_platforms: Optional[Tuple[str, ...]] = DEFAULT_KOTLIN_PLATFORMS
    offline: bool = False
    jobs: int = 8
    exclude_patterns: Tuple[str, ...] = ()
    refresh: bool = False
    gradle_metadata: bool = True


class Resolver:
    def __init__(self, output: OutputRepository, remotes: List[RemoteRepository], gradle_cache: Optional[GradleCache],
                 local_sources: List[LocalMavenSource], options: ResolverOptions):
        self.out = output
        self.remotes = remotes
        self.cache = gradle_cache
        self.local_sources = local_sources
        self.opts = options
        self.modules: Dict[Tuple[str, str, str], ModuleRecord] = {}
        self.failures: List[str] = []
        self.warnings: List[str] = []
        self.stats: Dict[str, int] = {"existing": 0, "gradle-cache": 0, "local": 0, "downloaded": 0}
        self.constraints: Dict[Tuple[str, str], Set[str]] = {}
        self.managed_versions: Dict[Tuple[str, str], str] = {}
        self._lock = threading.RLock()
        self._key_locks: Dict[object, threading.RLock] = {}
        self._raw_poms: Dict[Tuple[str, str, str], Optional[RawPom]] = {}
        self._effective: Dict[Tuple[str, str, str], Optional[EffectivePom]] = {}
        self._obtained: Dict[Path, Optional[str]] = {}
        self._versions: Dict[Tuple[str, str], Set[str]] = {}
        self._preferred_remote: Dict[Tuple[str, str], int] = {}
        self._unversioned: List[Dependency] = []
        self.last_errors: Dict[Tuple[str, str, str], str] = {}

    # -- locking helpers ---------------------------------------------------
    def _key_lock(self, key: object) -> threading.RLock:
        with self._lock:
            lk = self._key_locks.get(key)
            if lk is None:
                lk = self._key_locks[key] = threading.RLock()
            return lk

    def _memo(self, cache: dict, key: object, fn: Callable[[], object]):
        with self._lock:
            if key in cache:
                return cache[key]
        lk = self._key_lock(("memo", id(cache), key))
        if not lk.acquire(timeout=300):  # pragma: no cover - only on pathological cycles
            return fn()
        try:
            with self._lock:
                if key in cache:
                    return cache[key]
            value = fn()
            with self._lock:
                cache[key] = value
            return value
        finally:
            lk.release()

    # -- file acquisition ----------------------------------------------------
    def obtain(self, group: str, artifact: str, version: str, filename: str, quiet: bool = False) -> Optional[str]:
        """Make sure ``filename`` of ``group:artifact:version`` exists in the output repository.

        Lookup order: output repo -> Gradle cache -> local Maven sources -> remote repositories.
        Returns the source label, or None when the file could not be found anywhere.
        """
        dest = self.out.path(group, artifact, version, filename)
        return self._memo(self._obtained, dest, lambda: self._obtain(group, artifact, version, filename, dest, quiet))

    def _obtain(self, group: str, artifact: str, version: str, filename: str, dest: Path, quiet: bool) -> Optional[str]:
        if dest.is_file() and not self.opts.refresh:
            self._count("existing")
            return "existing"
        if self.cache is not None:
            src = self.cache.find(group, artifact, version, filename)
            if src is not None:
                self.out.copy_in(src, dest)
                self.out.write_checksums(dest)
                self._count("gradle-cache")
                log.debug("gradle cache  %s", dest.relative_to(self.out.root))
                return "gradle-cache"
        for source in self.local_sources:
            src = source.find(group, artifact, version, filename)
            if src is not None:
                self.out.copy_in(src, dest)
                self.out.write_checksums(dest)
                self._count("local")
                log.debug("%s  %s", source.label, dest.relative_to(self.out.root))
                return source.label
        if self.opts.offline:
            return None
        ga = (group, artifact)
        order = list(range(len(self.remotes)))
        pref = self._preferred_remote.get(ga)
        if pref is not None:
            order.remove(pref)
            order.insert(0, pref)
        for idx in order:
            remote = self.remotes[idx]
            try:
                if remote.fetch(group, artifact, version, filename, dest):
                    with self._lock:
                        self._preferred_remote.setdefault(ga, idx)
                    self.out.write_checksums(dest)
                    self._count("downloaded")
                    log.info("downloaded  %s  <- %s", dest.relative_to(self.out.root), remote.url)
                    return remote.url
            except FetchError as exc:
                # "not found" is silent for optional probes; real errors (TLS, auth, 5xx) are always reported
                with self._lock:
                    remote.errors += 1
                    count = remote.errors
                    self.last_errors[(group, artifact, version)] = str(exc)
                if count <= self.MAX_ERRORS_PER_REPO:
                    log.warning("%s", exc)
                elif count == self.MAX_ERRORS_PER_REPO + 1:
                    log.warning("further errors from %s are suppressed (use -v to see them)", remote.url)
                else:
                    log.debug("%s", exc)
        return None

    MAX_ERRORS_PER_REPO = 5

    def _count(self, key: str) -> None:
        with self._lock:
            self.stats[key] = self.stats.get(key, 0) + 1

    # -- versions ------------------------------------------------------------
    def available_versions(self, group: str, artifact: str) -> Set[str]:
        def compute() -> Set[str]:
            versions: Set[str] = set()
            if self.cache:
                versions |= self.cache.versions(group, artifact)
            for src in self.local_sources:
                versions |= src.versions(group, artifact)
            local = LocalMavenSource(self.out.root, "output")
            versions |= local.versions(group, artifact)
            if not self.opts.offline:
                for remote in self.remotes:
                    versions |= remote.versions(group, artifact)
            return versions
        return self._memo(self._versions, (group, artifact), compute)

    def resolve_version(self, group: str, artifact: str, spec: str) -> Optional[str]:
        spec = normalize_requested_version(spec) or spec
        if not is_dynamic_version(spec):
            return spec
        ranges = parse_version_ranges(spec)
        if ranges and len(ranges) == 1 and ranges[0][0] == ranges[0][2] and ranges[0][0]:
            return ranges[0][0]  # [1.0] - exact version
        return select_version(spec, self.available_versions(group, artifact))

    # -- POMs ----------------------------------------------------------------
    def raw_pom(self, group: str, artifact: str, version: str) -> Optional[RawPom]:
        def load() -> Optional[RawPom]:
            filename = "%s-%s.pom" % (artifact, version)
            if self.obtain(group, artifact, version, filename, quiet=True) is None:
                return None
            pom = parse_pom(self.out.path(group, artifact, version, filename))
            if pom is None:
                self.warnings.append("cannot parse POM %s:%s:%s" % (group, artifact, version))
            return pom
        return self._memo(self._raw_poms, (group, artifact, version), load)

    def _inherited(self, gav: Tuple[str, str, str], chain: Tuple) -> Optional[dict]:
        raw = self.raw_pom(*gav)
        if raw is None:
            return None
        model = {"properties": {}, "managed": {}, "dependencies": {}, "group": raw.group, "version": raw.version,
                 "parent": raw.parent}
        if raw.parent and raw.parent not in chain and len(chain) < 30:
            parent = self._inherited(raw.parent, chain + (gav,))
            if parent is None:
                self.warnings.append("parent POM %s:%s:%s of %s not found" % (raw.parent + (":".join(gav),)))
            else:
                model["properties"].update(parent["properties"])
                model["managed"].update(parent["managed"])
                model["dependencies"].update(parent["dependencies"])
                model["parent_props"] = parent
                model["group"] = model["group"] or raw.parent[0]
                model["version"] = model["version"] or raw.parent[2]
        model["group"] = model["group"] or (raw.parent[0] if raw.parent else gav[0])
        model["version"] = model["version"] or (raw.parent[2] if raw.parent else gav[2])
        model["properties"].update(raw.properties)
        for d in raw.managed:
            model["managed"][d.key] = d
        for d in raw.dependencies:
            model["dependencies"][d.key] = d
        return model

    def effective_pom(self, group: str, artifact: str, version: str, chain: Tuple = ()) -> Optional[EffectivePom]:
        key = (group, artifact, version)
        if key in chain:
            return None
        return self._memo(self._effective, key, lambda: self._build_effective(key, chain + (key,)))

    def _build_effective(self, gav: Tuple[str, str, str], chain: Tuple) -> Optional[EffectivePom]:
        raw = self.raw_pom(*gav)
        if raw is None:
            return None
        inh = self._inherited(gav, ())
        assert inh is not None
        props = dict(inh["properties"])
        g, v = inh["group"] or gav[0], inh["version"] or gav[2]
        builtins = {
            "project.groupId": g, "pom.groupId": g, "groupId": g,
            "project.artifactId": raw.artifact, "pom.artifactId": raw.artifact, "artifactId": raw.artifact,
            "project.version": v, "pom.version": v, "version": v,
        }
        if raw.parent:
            builtins.update({
                "project.parent.groupId": raw.parent[0], "parent.groupId": raw.parent[0],
                "project.parent.artifactId": raw.parent[1], "project.parent.version": raw.parent[2],
                "parent.version": raw.parent[2],
            })
        props.update(builtins)

        def interp(d: PomDependency) -> PomDependency:
            return dataclasses.replace(
                d, group=interpolate_pom(d.group, props) or d.group,
                artifact=interpolate_pom(d.artifact, props) or d.artifact,
                version=interpolate_pom(d.version, props), type=interpolate_pom(d.type, props) or "jar",
                classifier=interpolate_pom(d.classifier, props), scope=interpolate_pom(d.scope, props),
                exclusions=tuple((interpolate_pom(eg, props) or eg, interpolate_pom(ea, props) or ea)
                                 for eg, ea in d.exclusions))

        managed: Dict[Tuple, PomDependency] = {}
        imports: List[PomDependency] = []
        for d in inh["managed"].values():
            d = interp(d)
            if d.scope == "import" and d.type == "pom":
                imports.append(d)
            else:
                managed[d.key] = d
        for imp in imports:
            if not imp.version or "${" in imp.version:
                self.warnings.append("%s: cannot import BOM %s:%s (version %s)" % (":".join(gav), imp.group,
                                                                                    imp.artifact, imp.version))
                continue
            bom_version = self.resolve_version(imp.group, imp.artifact, imp.version) or imp.version
            bom = self.effective_pom(imp.group, imp.artifact, bom_version, chain)
            if bom is None:
                if (imp.group, imp.artifact, bom_version) not in chain:
                    self.warnings.append("%s: imported BOM %s:%s:%s not found" % (":".join(gav), imp.group,
                                                                                 imp.artifact, bom_version))
                continue
            for k, md in bom.managed.items():
                managed.setdefault(k, md)
        by_ga: Dict[Tuple[str, str], PomDependency] = {}
        for md in managed.values():
            by_ga.setdefault((md.group, md.artifact), md)
        deps = []
        for d in inh["dependencies"].values():
            d = interp(d)
            md = managed.get(d.key) or by_ga.get((d.group, d.artifact))
            if md is not None:
                if not d.version:
                    d.version = md.version
                if not d.scope:
                    d.scope = md.scope
                if md.exclusions:
                    d.exclusions = tuple(set(d.exclusions) | set(md.exclusions))
            d.scope = d.scope or "compile"
            deps.append(d)
        relocation = None
        if raw.relocation:
            rg, ra, rv = (interpolate_pom(x, props) for x in raw.relocation)
            relocation = (rg or g, ra or raw.artifact, rv or v)
        return EffectivePom(g, raw.artifact or gav[1], v, interpolate_pom(raw.packaging, props) or "jar", deps,
                            managed, relocation, raw.gradle_metadata)

    # -- module metadata -------------------------------------------------------
    def _keep_variant(self, attrs: dict) -> bool:
        if attrs.get("org.gradle.category") == "documentation":
            doc = attrs.get("org.gradle.docstype")
            return (doc == "sources" and self.opts.sources) or (doc == "javadoc" and self.opts.javadoc)
        platform = attrs.get("org.jetbrains.kotlin.platform.type")
        if platform and self.opts.kotlin_platforms is not None and platform not in self.opts.kotlin_platforms:
            return False
        return True

    def _module_metadata(self, rec: ModuleRecord, item: WorkItem) -> Optional[Tuple[List[WorkItem], List[WorkItem]]]:
        """Read Gradle module metadata: returns (dependencies, available-at modules) or None if absent."""
        filename = "%s-%s.module" % (rec.artifact, rec.version)
        if self.obtain(rec.group, rec.artifact, rec.version, filename, quiet=True) is None:
            return None
        path = self.out.path(rec.group, rec.artifact, rec.version, filename)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.warnings.append("cannot parse %s: %s" % (path, exc))
            return None
        rec.files.setdefault(filename, "")
        dependencies: List[WorkItem] = []
        available_at: List[WorkItem] = []
        files: List[str] = []
        for variant in data.get("variants") or []:
            if not self._keep_variant(variant.get("attributes") or {}):
                continue
            for f in variant.get("files") or []:
                url = f.get("url") or f.get("name")
                if url and url not in files:
                    files.append(url)
            at = variant.get("available-at")
            if at and at.get("group") and at.get("module") and at.get("version"):
                available_at.append(WorkItem(at["group"], at["module"], at["version"], item.excludes,
                                             item.transitive, rec.coord, item.depth + 1, optional=item.optional))
            for d in variant.get("dependencies") or []:
                g, m = d.get("group"), d.get("module")
                vinfo = d.get("version") or {}
                v = vinfo.get("strictly") or vinfo.get("requires") or vinfo.get("prefers")
                if not g or not m or not v or self._excluded(g, m, item.excludes):
                    continue
                excl = set(item.excludes)
                for e in d.get("excludes") or []:
                    excl.add((e.get("group") or "*", e.get("module") or "*"))
                dependencies.append(WorkItem(g, m, v, frozenset(excl), True, rec.coord, item.depth + 1,
                                             optional=item.optional))
            for c in variant.get("dependencyConstraints") or []:
                g, m = c.get("group"), c.get("module")
                vinfo = c.get("version") or {}
                v = vinfo.get("strictly") or vinfo.get("requires") or vinfo.get("prefers")
                if g and m and v and not is_dynamic_version(v):
                    with self._lock:
                        self.constraints.setdefault((g, m), set()).add(v)
        rec.variant_files = files
        rec.gradle_metadata = True
        return dependencies, available_at

    # -- graph traversal -------------------------------------------------------
    def _excluded(self, group: str, artifact: str, excludes: Iterable[Tuple[str, str]]) -> bool:
        for eg, ea in excludes:
            if (eg == "*" or fnmatch.fnmatchcase(group, eg)) and (ea == "*" or fnmatch.fnmatchcase(artifact, ea)):
                return True
        return False

    def _cli_excluded(self, group: str, artifact: str, version: str) -> bool:
        for pattern in self.opts.exclude_patterns:
            if pattern.count(":") >= 2:
                if fnmatch.fnmatchcase("%s:%s:%s" % (group, artifact, version), pattern):
                    return True
            elif fnmatch.fnmatchcase("%s:%s" % (group, artifact), pattern if ":" in pattern else pattern + ":*"):
                return True
        return False

    def _process(self, item: WorkItem) -> List[WorkItem]:
        version = self.resolve_version(item.group, item.artifact, item.version)
        if version is None:
            self._fail(item, "%s:%s:%s - no matching version found (requested by %s)" % (
                item.group, item.artifact, item.version, item.requested_by or "build script"))
            return []
        if self._cli_excluded(item.group, item.artifact, version):
            log.debug("excluded by --exclude: %s:%s:%s", item.group, item.artifact, version)
            return []
        key = (item.group, item.artifact, version)
        with self._lock:
            rec = self.modules.get(key)
            new = rec is None
            if new:
                rec = self.modules[key] = ModuleRecord(item.group, item.artifact, version, depth=item.depth)
            if item.requested_by:
                rec.requested_by.add(item.requested_by)
            if not item.optional:
                rec.optional_only = False
            if item.classifier or (item.ext and item.ext not in ("jar", "pom")):
                rec.extra_artifacts.add((item.classifier, item.ext or "jar"))
            # Revisit a module only if this path could reach more of the graph than earlier visits did.
            if any(ex <= item.excludes and (tr or not item.transitive) for ex, tr in rec.processed):
                return []
            rec.processed.append((item.excludes, item.transitive))
        transitive = item.transitive and self.opts.transitive
        pom = self.effective_pom(item.group, item.artifact, version)
        children: List[WorkItem] = []
        if pom is None:
            if item.artifact_only:
                rec.found = True
                rec.packaging = "pom"  # no metadata: only the explicitly requested artifact
            else:
                error = self.last_errors.get(key)
                self._fail(item, "%s - POM not found in any repository (requested by %s)%s" % (
                    rec.coord, item.requested_by or "build script", "; last error: " + error if error else ""))
            return []
        rec.found = True
        rec.packaging = pom.packaging
        rec.files.setdefault("%s-%s.pom" % (rec.artifact, rec.version), "")
        if item.depth == 0 and transitive:
            # Maven applies the root artifact's dependencyManagement to its whole graph (e.g. when it
            # resolves a plugin); record those versions so modules in the graph get them as well.
            with self._lock:
                for md in pom.managed.values():
                    if md.version and "${" not in md.version and not is_dynamic_version(md.version):
                        self.constraints.setdefault((md.group, md.artifact), set()).add(md.version)
        if pom.relocation and pom.relocation != key:
            children.append(WorkItem(*pom.relocation, excludes=item.excludes, requested_by=rec.coord,
                                     depth=item.depth + 1, optional=item.optional))
        meta = None
        if pom.gradle_metadata and self.opts.gradle_metadata:
            meta = self._module_metadata(rec, item)
        if meta is not None:
            dependencies, available_at = meta
            # "available-at" variants (e.g. Kotlin multiplatform -jvm modules) are part of the component itself
            children.extend(available_at)
            if transitive:
                children.extend(dependencies)
            return children
        if not transitive:
            return children
        for d in pom.dependencies:
            if d.scope not in self.opts.scopes or (d.optional and not self.opts.include_optional):
                continue
            if not d.version:
                self.warnings.append("%s: dependency %s:%s has no version" % (rec.coord, d.group, d.artifact))
                continue
            if "${" in (d.version or "") or "${" in d.group or "${" in d.artifact:
                self.warnings.append("%s: unresolved property in dependency %s:%s:%s" % (rec.coord, d.group,
                                                                                       d.artifact, d.version))
                continue
            if self._excluded(d.group, d.artifact, item.excludes):
                continue
            ext, cls = TYPE_MAPPING.get(d.type, (d.type, None))
            classifier = d.classifier or cls
            children.append(WorkItem(d.group, d.artifact, d.version, item.excludes | frozenset(d.exclusions), True,
                                     rec.coord, item.depth + 1, classifier, None if ext == "pom" else ext,
                                     optional=item.optional))
        return children

    def _fail(self, item: WorkItem, msg: str) -> None:
        if item.optional:
            log.debug("optional: %s", msg)
            return
        with self._lock:
            if msg in self.failures:
                return
            self.failures.append(msg)
        log.error("%s", msg)

    def resolve(self, dependencies: List[Dependency]) -> None:
        """Resolve the dependency graph (metadata only); see :meth:`fetch_artifacts`."""
        platforms = [d for d in dependencies if d.platform and d.version]
        # Versions managed by declared platforms/BOMs (used for version-less declarations + as constraints).
        for p in platforms:
            version = self.resolve_version(p.group, p.artifact, p.version)  # type: ignore[arg-type]
            if not version:
                continue
            pom = self.effective_pom(p.group, p.artifact, version)
            if pom is None:
                continue
            for md in pom.managed.values():
                if md.version and "${" not in md.version:
                    self.managed_versions.setdefault((md.group, md.artifact), md.version)
                    self.constraints.setdefault((md.group, md.artifact), set()).add(md.version)
        items: List[WorkItem] = []
        for d in dependencies:
            version = d.version or self.managed_versions.get(d.ga)
            if not version:
                self._unversioned.append(d)
                continue
            items.append(WorkItem(d.group, d.artifact, version, d.excludes, d.transitive, "", 0, d.classifier,
                                  d.ext, artifact_only=bool(d.ext) and not d.transitive, optional=d.optional))
        self._run(items)

        # Versionless declarations whose version only comes from the graph (constraints, transitive deps).
        still = []
        for d in self._unversioned:
            versions = [k[2] for k in self.modules if k[:2] == d.ga]
            if versions:
                if d.classifier or d.ext:
                    for v in versions:
                        self.modules[(d.group, d.artifact, v)].extra_artifacts.add((d.classifier, d.ext or "jar"))
                continue
            still.append(d)
        for d in still:
            self._fail(WorkItem(d.group, d.artifact, "", optional=d.optional),
                       "%s - no version declared and none found via platforms/constraints (%s)" % (
                           d.notation(), d.origin))

    def _run(self, items: List[WorkItem]) -> None:
        frontier = items
        level = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, self.opts.jobs)) as pool:
            while frontier:
                log.info("resolving level %d: %d module request(s)", level, len(frontier))
                next_frontier: List[WorkItem] = []
                for children in pool.map(self._process, frontier):
                    next_frontier.extend(children)
                # constraints (platforms, Gradle module metadata) may upgrade modules already in the graph
                with self._lock:
                    present = {}
                    for (g, a, v) in self.modules:
                        present.setdefault((g, a), set()).add(v)
                    queued = {(c.group, c.artifact, c.version) for c in next_frontier}
                    for ga, versions in self.constraints.items():
                        if ga in present:
                            for v in versions - present[ga]:
                                if (ga[0], ga[1], v) not in queued:
                                    next_frontier.append(WorkItem(ga[0], ga[1], v, frozenset(), True,
                                                                  "constraint", level + 1))
                frontier = next_frontier
                level += 1

    # -- artifacts ---------------------------------------------------------------
    def selected_modules(self) -> List[ModuleRecord]:
        found = [m for m in self.modules.values() if m.found]
        if self.opts.conflict_strategy != "highest":
            return found
        best: Dict[Tuple[str, str], ModuleRecord] = {}
        for m in found:
            cur = best.get((m.group, m.artifact))
            if cur is None or compare_versions(m.version, cur.version) > 0:
                best[(m.group, m.artifact)] = m
        return list(best.values())

    def fetch_artifacts(self) -> None:
        tasks: List[Tuple[ModuleRecord, str, bool]] = []  # (module, filename, optional)
        for rec in self.selected_modules():
            names = set()
            if rec.gradle_metadata:
                for f in rec.variant_files:
                    # files of another module directory (e.g. guava's "-android" jar) belong to variants that
                    # are only selected in other environments: fetch them when possible, don't fail otherwise
                    tasks.append((rec, f, f.startswith("../")))
                    names.add(f)
            elif rec.packaging == "pom":
                # Gradle still uses a jar next to a pom-packaged module if there is one
                jar = artifact_filename(rec.artifact, rec.version, None, "jar")
                tasks.append((rec, jar, True))
                names.add(jar)
            else:
                ext = PACKAGING_EXTENSIONS.get(rec.packaging, rec.packaging)
                name = artifact_filename(rec.artifact, rec.version, None, ext)
                tasks.append((rec, name, False))
                names.add(name)
            for classifier, ext in sorted(rec.extra_artifacts, key=lambda x: (x[0] or "", x[1])):
                name = artifact_filename(rec.artifact, rec.version, classifier, ext)
                if name not in names:
                    tasks.append((rec, name, False))
                    names.add(name)
            if not rec.gradle_metadata and rec.packaging != "pom":
                if self.opts.sources:
                    tasks.append((rec, artifact_filename(rec.artifact, rec.version, "sources", "jar"), True))
                if self.opts.javadoc:
                    tasks.append((rec, artifact_filename(rec.artifact, rec.version, "javadoc", "jar"), True))
        log.info("fetching %d artifact file(s) for %d module(s)", len(tasks), len(self.selected_modules()))

        def run(task: Tuple[ModuleRecord, str, bool]) -> None:
            rec, filename, optional = task
            if filename.startswith("../") or "/" in filename:
                rel = os.path.normpath(os.path.join(gav_dir(rec.group, rec.artifact, rec.version), filename))
                parts = Path(rel).parts
                if len(parts) < 4:
                    return
                version, artifact = parts[-2], parts[-3]
                group = ".".join(parts[:-3])
                source = self.obtain(group, artifact, version, parts[-1], quiet=optional)
            else:
                source = self.obtain(rec.group, rec.artifact, rec.version, filename, quiet=optional)
                if source is None and not optional and not rec.gradle_metadata and filename.endswith(
                        "." + PACKAGING_EXTENSIONS.get(rec.packaging, rec.packaging)) and \
                        not filename.endswith(".jar"):
                    # Gradle falls back to .jar when the packaging-derived artifact does not exist
                    jar = artifact_filename(rec.artifact, rec.version, None, "jar")
                    source = self.obtain(rec.group, rec.artifact, rec.version, jar, quiet=True)
                    if source is not None:
                        filename = jar
            with self._lock:
                if source is not None:
                    rec.files[filename] = source
                elif not optional:
                    rec.missing.append(filename)
                    self.failures.append("%s - artifact %s not found" % (rec.coord, filename))
                    log.error("%s - artifact %s not found", rec.coord, filename)
                    if rec.optional_only:
                        self.failures.pop()

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, self.opts.jobs)) as pool:
            list(pool.map(run, tasks))
        # record pom/module sources for the report
        for rec in self.modules.values():
            for fn in list(rec.files):
                if not rec.files[fn]:
                    dest = self.out.path(rec.group, rec.artifact, rec.version, fn)
                    rec.files[fn] = self._obtained.get(dest) or "existing"


def import_gradle_cache(cache: GradleCache, out: OutputRepository, exclude: Callable[[str, str, str], bool]) -> int:
    """Copy the whole Gradle module cache into the output repository."""
    count = 0
    for group, artifact, version, src in cache.iter_files():
        if exclude(group, artifact, version):
            continue
        dest = out.path(group, artifact, version, src.name)
        if dest.is_file():
            continue
        out.copy_in(src, dest)
        out.write_checksums(dest)
        count += 1
    return count


# ---------------------------------------------------------------------------
# Init script / snippets
# ---------------------------------------------------------------------------

INIT_SCRIPT_TEMPLATE = """\
// Generated by gradle2maven %(version)s
// Usage: gradle --offline --init-script %(name)s build
// Puts the local repository first for plugins, buildscript classpaths and project dependencies.
def g2mRepo = new File(%(path)s).toURI()
def g2mMode = null

def g2mFirst = { repos ->
    def repo = repos.maven { url = g2mRepo; name = 'gradle2maven' }
    repos.remove(repo)
    repos.addFirst(repo)
}

beforeSettings { settings ->
    g2mFirst(settings.pluginManagement.repositories)
    try {
        g2mFirst(settings.dependencyResolutionManagement.repositories)
    } catch (Throwable ignored) {
        // Gradle < 6.8 has no dependencyResolutionManagement
    }
}
settingsEvaluated { settings ->
    try {
        g2mMode = settings.dependencyResolutionManagement.repositoriesMode.get().toString()
    } catch (Throwable ignored) {
    }
}
allprojects {
    g2mFirst(buildscript.repositories)
    if (g2mMode != 'FAIL_ON_PROJECT_REPOS') {
        g2mFirst(repositories)
    }
}
"""


def write_init_script(path: Path, repo_root: Path) -> None:
    content = INIT_SCRIPT_TEMPLATE % {
        "version": __version__,
        "name": path.name,
        "path": json.dumps(str(repo_root.resolve())),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_kv(values: Optional[List[str]], what: str) -> Dict[str, str]:
    out = {}
    for item in values or []:
        if "=" not in item:
            raise Gradle2MavenError("%s must be KEY=VALUE, got %r" % (what, item))
        k, v = item.split("=", 1)
        out[k.strip()] = v
    return out


def _parse_credentials(values: Optional[List[str]]) -> Dict[str, Tuple[str, str]]:
    out = {}
    for item in values or []:
        m = re.match(r"^(.+?)=([^:]*):(.*)$", item)
        if not m:
            raise Gradle2MavenError("--credentials must be URL_PREFIX=USER:PASSWORD, got %r" % item)
        password = m.group(3)
        if password.startswith("env:"):
            password = os.environ.get(password[4:], "")
        out[m.group(1)] = (m.group(2), password)
    return out


def _csv(value: str) -> Tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gradle2maven",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Create a local Maven repository containing every dependency of a Gradle build.\n\n"
                    "Gradle build files (Groovy or Kotlin DSL), settings files, gradle.properties and version\n"
                    "catalogs are parsed statically; transitive dependencies are resolved from POMs and Gradle\n"
                    "module metadata. Files already present in the Gradle cache (~/.gradle) are copied instead\n"
                    "of downloaded. Downloads failing with SSL errors are retried without certificate checks.",
        epilog="examples:\n"
               "  gradle2maven build.gradle\n"
               "  gradle2maven path/to/project -o /srv/offline-repo --sources\n"
               "  gradle2maven app/build.gradle.kts --no-subprojects --exclude-configurations '^test'\n"
               "  gradle2maven build.gradle --offline                  # Gradle cache only, no network\n"
               "  gradle2maven build.gradle --list --format json        # just show what was parsed\n"
               "  gradle2maven build.gradle --init-script offline.gradle\n"
               "  gradle2maven build.gradle -r https://repo.example.com/maven --credentials \\\n"
               "      https://repo.example.com/=alice:env:REPO_PASSWORD\n\n"
               "exit status: 0 success, 1 error, 2 usage error, 3 some dependencies could not be resolved")
    p.add_argument("build_files", nargs="+", metavar="BUILD_FILE",
                   help="Gradle build file (build.gradle / build.gradle.kts), settings file or project directory")
    p.add_argument("-V", "--version", action="version", version="%(prog)s " + __version__)

    g = p.add_argument_group("output")
    g.add_argument("-o", "--output", default="local-maven-repo", metavar="DIR",
                   help="directory of the Maven repository to create/update (default: %(default)s)")
    g.add_argument("--clean", action="store_true",
                   help="delete the output directory first (only if it was created by gradle2maven)")
    g.add_argument("--refresh", action="store_true", help="re-fetch files that already exist in the output repository")
    g.add_argument("--checksums", default="sha1,md5", type=_csv, metavar="ALGOS",
                   help="checksum files to write next to every file: any of md5,sha1,sha256,sha512 or 'none' "
                        "(default: %(default)s)")
    g.add_argument("--no-metadata", action="store_true", help="do not generate maven-metadata.xml files")
    g.add_argument("--report", metavar="FILE", help="write a JSON report of everything that was resolved")
    g.add_argument("--init-script", metavar="FILE",
                   help="also write a Gradle init script that points builds at the generated repository")

    g = p.add_argument_group("build parsing")
    g.add_argument("-P", "--property", action="append", metavar="KEY=VALUE", default=[],
                   help="set/override a Gradle project property used while evaluating the scripts")
    g.add_argument("--no-subprojects", action="store_true",
                   help="do not follow include(...) in settings files to subprojects")
    g.add_argument("--no-build-logic", action="store_true", help="ignore buildSrc and included builds")
    g.add_argument("--configurations", metavar="REGEX",
                   help="only use configurations whose name matches REGEX (e.g. 'implementation|api')")
    g.add_argument("--exclude-configurations", metavar="REGEX",
                   help="skip configurations whose name matches REGEX (e.g. '^test')")
    g.add_argument("--no-plugins", action="store_true", help="do not resolve plugins from plugins { } blocks")
    g.add_argument("--no-implicit", action="store_true",
                   help="do not add implicit dependencies (Kotlin stdlib/compiler, Spring Boot BOM)")
    g.add_argument("-d", "--dependency", action="append", default=[], metavar="COORDS",
                   help="extra dependency group:artifact:version[:classifier][@ext] (repeatable)")
    g.add_argument("--dependencies-file", metavar="FILE",
                   help="file with extra dependency coordinates, one per line ('#' comments allowed)")
    g.add_argument("--list", action="store_true", help="only print what was parsed from the build and exit")
    g.add_argument("--format", choices=("text", "json"), default="text", help="output format for --list")

    g = p.add_argument_group("resolution")
    g.add_argument("--no-transitive", action="store_true", help="only fetch the declared dependencies")
    g.add_argument("--scopes", type=_csv, default=DEFAULT_SCOPES, metavar="LIST",
                   help="Maven scopes followed transitively (default: %s; also: provided,test)" % ",".join(DEFAULT_SCOPES))
    g.add_argument("--optional", action="store_true", help="also follow optional Maven dependencies")
    g.add_argument("--conflict-strategy", choices=("all", "highest"), default="all",
                   help="'all' keeps artifacts of every version seen in the graph (safest for offline builds); "
                        "'highest' keeps only POMs for losing versions (default: %(default)s)")
    g.add_argument("--kotlin-platforms", default=",".join(DEFAULT_KOTLIN_PLATFORMS), metavar="LIST",
                   help="Kotlin multiplatform variants to fetch from Gradle module metadata, or 'all' "
                        "(default: %(default)s)")
    g.add_argument("--no-gradle-metadata", action="store_true", help="ignore Gradle .module files, use POMs only")
    g.add_argument("--sources", action="store_true", help="also fetch -sources.jar files when available")
    g.add_argument("--javadoc", action="store_true", help="also fetch -javadoc.jar files when available")
    g.add_argument("-x", "--exclude", action="append", default=[], metavar="PATTERN",
                   help="skip modules matching group[:artifact[:version]] glob, e.g. 'com.android.*' (repeatable)")

    g = p.add_argument_group("sources")
    g.add_argument("--gradle-home", metavar="DIR",
                   help="Gradle user home to take cached files from (default: $GRADLE_USER_HOME or ~/.gradle)")
    g.add_argument("--no-gradle-cache", action="store_true", help="do not use the Gradle cache")
    g.add_argument("--copy-entire-cache", action="store_true",
                   help="copy every module in the Gradle cache into the repository before resolving")
    g.add_argument("--maven-local", action="store_true",
                   help="also use the local Maven repository (~/.m2/repository) as a source; "
                        "enabled automatically when the build declares mavenLocal()")
    g.add_argument("--maven-local-dir", metavar="DIR",
                   help="local Maven repository location used by --maven-local / mavenLocal() "
                        "(default: ~/.m2/repository)")
    g.add_argument("-r", "--repository", action="append", default=[], metavar="URL",
                   help="additional remote repository URL (repeatable; searched after the build's repositories)")
    g.add_argument("--no-build-repositories", action="store_true",
                   help="ignore repositories declared in the build scripts")
    g.add_argument("--no-default-repositories", action="store_true",
                   help="do not fall back to Maven Central / Gradle Plugin Portal when none are declared")
    g.add_argument("--offline", action="store_true", help="never use the network (Gradle cache / local sources only)")

    g = p.add_argument_group("network")
    g.add_argument("-j", "--jobs", type=int, default=8, metavar="N", help="parallel downloads (default: %(default)s)")
    g.add_argument("--timeout", type=float, default=30.0, metavar="SECONDS",
                   help="network timeout (default: %(default)s)")
    g.add_argument("--retries", type=int, default=2, metavar="N",
                   help="retries for transient network errors (default: %(default)s)")
    g.add_argument("-k", "--insecure", action="store_true", help="never verify TLS certificates")
    g.add_argument("--no-ssl-fallback", action="store_true",
                   help="do not retry without certificate verification after an SSL error")
    g.add_argument("--ca-file", metavar="FILE", help="PEM bundle of CA certificates to trust")
    g.add_argument("--credentials", action="append", default=[], metavar="URL_PREFIX=USER:PASSWORD",
                   help="basic-auth credentials for URLs starting with URL_PREFIX; PASSWORD may be env:VAR "
                        "(repeatable)")

    g = p.add_argument_group("diagnostics")
    g.add_argument("-v", "--verbose", action="count", default=0, help="debug output (every file and where it came from)")
    g.add_argument("-q", "--quiet", action="store_true", help="only print warnings and errors")
    g.add_argument("--allow-missing", action="store_true",
                   help="exit with status 0 even if some dependencies could not be resolved")
    return p


def _configure_logging(verbose: int, quiet: bool) -> None:
    level = logging.WARNING if quiet else (logging.DEBUG if verbose >= 1 else logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s" if verbose else "%(message)s"))

    class _Prefix(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            if not verbose and record.levelno >= logging.WARNING and not record.msg.startswith(record.levelname):
                record.msg = "%s: %s" % (record.levelname.lower(), record.msg)
            return True
    handler.addFilter(_Prefix())
    log.handlers[:] = [handler]
    log.setLevel(level)
    log.propagate = False


def _print_listing(model: BuildModel, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(model.to_json(), indent=2))
        return
    print("Root project: %s" % model.root_dir)
    print("\nScripts parsed (%d):" % len(model.build_files))
    for f in model.build_files:
        print("  " + str(f))
    print("\nRepositories (%d):" % len(model.repositories))
    for r in model.repositories:
        print("  " + r.url)
    if model.plugin_repositories:
        print("\nPlugin repositories (%d):" % len(model.plugin_repositories))
        for r in model.plugin_repositories:
            print("  " + r.url)
    if model.uses_maven_local:
        print("  (mavenLocal)")
    print("\nPlugins (%d):" % len(model.plugins))
    for pl in model.plugins:
        print("  %s %s" % (pl.id, pl.version or "(no version: core plugin or version from elsewhere)"))
    print("\nDependencies (%d):" % len(model.dependencies))
    width = max([len(d.configuration) for d in model.dependencies] + [0])
    for d in model.dependencies:
        flags = []
        if d.platform:
            flags.append("platform")
        if not d.transitive:
            flags.append("non-transitive")
        if d.excludes:
            flags.append("excludes " + ",".join("%s:%s" % e for e in sorted(d.excludes)))
        print("  %-*s  %s%s" % (width, d.configuration, d.notation(), ("  [" + "; ".join(flags) + "]") if flags else ""))
    if model.warnings:
        print("\nWarnings (%d):" % len(model.warnings))
        for w in model.warnings:
            print("  " + w)


def _human_size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024
    return "%d B" % n


def _dir_size(root: Path) -> Tuple[int, int]:
    files = size = 0
    for p in root.rglob("*"):
        if p.is_file():
            files += 1
            size += p.stat().st_size
    return files, size


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose, args.quiet)
    return execute(args)


def execute(args: argparse.Namespace) -> int:
    """Run with parsed arguments, mapping user-facing errors to exit codes."""
    try:
        return run(args)
    except Gradle2MavenError as exc:
        log.error("%s", exc)
        return EXIT_ERROR
    except re.error as exc:
        log.error("invalid regular expression: %s", exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130


def run(args: argparse.Namespace) -> int:
    gradle_home = Path(args.gradle_home or os.environ.get("GRADLE_USER_HOME") or Path.home() / ".gradle").expanduser()
    include_re = re.compile(args.configurations) if args.configurations else None
    exclude_re = re.compile(args.exclude_configurations) if args.exclude_configurations else None

    def config_filter(name: str) -> bool:
        if name in ("plugins", "dependencyManagement"):
            return True
        if include_re and not include_re.search(name):
            return False
        return not (exclude_re and exclude_re.search(name))

    cli_props = _parse_kv(args.property, "-P")
    parser = GradleBuildParser(gradle_home, cli_props, include_subprojects=not args.no_subprojects,
                               include_build_logic=not args.no_build_logic, configuration_filter=config_filter)
    model: Optional[BuildModel] = None
    for target in args.build_files:
        model = parser.parse(Path(target), model)
    assert model is not None
    if args.no_plugins:
        model.plugins = []
    props = dict(read_properties(model.root_dir / "gradle.properties"))
    props.update(cli_props)
    finalize_model(model, add_plugin_portal=not args.no_default_repositories, implicit=not args.no_implicit,
                   properties=props)
    for coords in list(args.dependency) + _read_dependencies_file(args.dependencies_file):
        dep = parse_coordinates(coords)
        if dep is None:
            raise Gradle2MavenError("invalid dependency coordinates: %r" % coords)
        dep.configuration, dep.origin = "cli", "command line"
        model.dependencies.append(dep)

    if args.list:
        _print_listing(model, args.format)
        return EXIT_OK
    for w in model.warnings:
        log.warning("%s", w)

    # repositories
    repos: List[RepoSpec] = []
    if not args.no_build_repositories:
        repos.extend(model.repositories)
        for r in model.plugin_repositories:
            if all(r.url != x.url for x in repos):
                repos.append(r)
    for url in args.repository:
        url = url if re.match(r"^[a-zA-Z][\w+.-]*://", url) else Path(url).resolve().as_uri()
        if all(url.rstrip("/") != x.url.rstrip("/") for x in repos):
            repos.append(RepoSpec(url.rstrip("/") + "/"))
    if not args.no_default_repositories:
        if not model.repositories or args.no_build_repositories:
            if all(x.url != MAVEN_CENTRAL for x in repos):
                log.info("no repositories declared - using Maven Central")
                repos.append(RepoSpec(MAVEN_CENTRAL, "mavenCentral"))
        if any(p.version for p in model.plugins) and all(x.url != GRADLE_PLUGIN_PORTAL for x in repos):
            repos.append(RepoSpec(GRADLE_PLUGIN_PORTAL, "gradlePluginPortal"))

    credentials = {r.url: (r.username, r.password) for r in repos if r.username and r.password}
    credentials.update(_parse_credentials(args.credentials))
    http = HttpClient(timeout=args.timeout, retries=args.retries, insecure=args.insecure,
                      ssl_fallback=not args.no_ssl_fallback, credentials=credentials, ca_file=args.ca_file)
    remotes: List[RemoteRepository] = []
    local_sources: List[LocalMavenSource] = []
    for r in repos:
        if r.url.startswith("file:"):
            local_sources.append(LocalMavenSource(Path(urllib.request.url2pathname(urllib.parse.urlsplit(r.url).path)),
                                                  r.url))
        else:
            remotes.append(RemoteRepository(r.url, http, r.name))
    if args.maven_local or args.maven_local_dir or model.uses_maven_local:
        m2 = Path(args.maven_local_dir).expanduser() if args.maven_local_dir else Path.home() / ".m2" / "repository"
        local_sources.insert(0, LocalMavenSource(m2, "maven-local"))

    out_root = Path(args.output).expanduser().resolve()
    if args.clean and out_root.exists():
        if not (out_root / ".gradle2maven").exists() and any(out_root.iterdir()):
            raise Gradle2MavenError("refusing to --clean %s: it was not created by gradle2maven" % out_root)
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / ".gradle2maven").write_text("This repository was generated by gradle2maven %s\n" % __version__,
                                            encoding="utf-8")
    checksums = [] if "none" in args.checksums else list(args.checksums)
    for c in checksums:
        if c not in CHECKSUM_ALGORITHMS:
            raise Gradle2MavenError("unknown checksum algorithm %r" % c)
    output = OutputRepository(out_root, checksums)

    cache = None if args.no_gradle_cache else GradleCache.from_gradle_home(gradle_home)
    if cache is not None and not cache.roots:
        log.info("no Gradle cache found under %s", gradle_home)
        cache = None
    kotlin_platforms = None if args.kotlin_platforms.strip() == "all" else _csv(args.kotlin_platforms)
    options = ResolverOptions(
        scopes=tuple(args.scopes), transitive=not args.no_transitive, include_optional=args.optional,
        sources=args.sources, javadoc=args.javadoc, conflict_strategy=args.conflict_strategy,
        kotlin_platforms=kotlin_platforms, offline=args.offline, jobs=args.jobs,
        exclude_patterns=tuple(args.exclude), refresh=args.refresh, gradle_metadata=not args.no_gradle_metadata)
    resolver = Resolver(output, remotes, cache, local_sources, options)

    if cache is not None:
        log.info("using Gradle cache: %s", ", ".join(str(r) for r in cache.roots))
        if args.copy_entire_cache:
            n = import_gradle_cache(cache, output, lambda g, a, v: resolver._cli_excluded(g, a, v))
            log.info("copied %d file(s) from the Gradle cache", n)
    if not remotes and not local_sources and cache is None:
        log.warning("no sources available (offline without a Gradle cache?)")
    elif not args.offline and remotes:
        log.info("remote repositories: %s", ", ".join(r.url for r in remotes))

    started = time.time()
    resolver.resolve(model.dependencies)
    resolver.fetch_artifacts()
    metadata_count = 0 if args.no_metadata else output.write_maven_metadata()

    for w in sorted(set(resolver.warnings)):
        log.warning("%s", w)
    files, size = _dir_size(out_root)
    found = [m for m in resolver.modules.values() if m.found]
    s = resolver.stats
    if not args.quiet:
        print("", file=sys.stderr)
    log.info("Repository: %s", out_root)
    log.info("  modules: %d resolved, %d file(s), %s on disk, %d maven-metadata.xml",
             len(found), files, _human_size(size), metadata_count)
    log.info("  files: %d copied from Gradle cache, %d downloaded, %d from local Maven repos, %d already present",
             s.get("gradle-cache", 0), s.get("downloaded", 0), s.get("local", 0), s.get("existing", 0))
    if http.insecure_hosts:
        log.warning("TLS certificate verification was disabled for: %s", ", ".join(sorted(http.insecure_hosts)))
    log.info("  time: %.1fs, %d HTTP request(s), %s downloaded", time.time() - started, http.requests,
             _human_size(http.bytes))
    if args.init_script:
        write_init_script(Path(args.init_script), out_root)
        log.info("Init script written: %s", Path(args.init_script).resolve())

    if args.report:
        report = {
            "version": __version__,
            "repository": str(out_root),
            "build": model.to_json(),
            "remoteRepositories": [r.url for r in remotes],
            "insecureHosts": sorted(http.insecure_hosts),
            "stats": s,
            "modules": [
                {
                    "coordinates": m.coord,
                    "packaging": m.packaging,
                    "gradleMetadata": m.gradle_metadata,
                    "depth": m.depth,
                    "requestedBy": sorted(m.requested_by),
                    "files": m.files,
                    "missing": m.missing,
                }
                for m in sorted(resolver.modules.values(), key=lambda m: (m.group, m.artifact, version_key(m.version)))
            ],
            "failures": resolver.failures,
            "warnings": sorted(set(resolver.warnings)) + model.warnings,
        }
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
        log.info("Report written: %s", Path(args.report).resolve())

    if resolver.failures:
        log.error("%d problem(s):", len(resolver.failures))
        for f in resolver.failures:
            log.error("  %s", f)
        return EXIT_OK if args.allow_missing else EXIT_INCOMPLETE
    return EXIT_OK


def _read_dependencies_file(path: Optional[str]) -> List[str]:
    if not path:
        return []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise Gradle2MavenError("cannot read %s: %s" % (path, exc)) from exc
    return [ln.split("#", 1)[0].strip() for ln in lines if ln.split("#", 1)[0].strip()]


if __name__ == "__main__":
    sys.exit(main())
