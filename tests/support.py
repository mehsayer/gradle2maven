"""Shared helpers for the gradle2maven test-suite: a fake Maven repository server and builders."""

from __future__ import annotations

import contextlib
import functools
import http.server
import io
import json
import logging
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gradle2maven as g2m  # noqa: E402


class FakeRepo:
    """Builds a Maven layout directory with POMs, jars and module metadata."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def dir(self, group: str, artifact: str, version: str) -> Path:
        d = self.root / group.replace(".", "/") / artifact / version
        d.mkdir(parents=True, exist_ok=True)
        return d

    def module(self, coords: str, deps: Optional[List[dict]] = None, parent: Optional[str] = None,
               packaging: Optional[str] = None, properties: Optional[Dict[str, str]] = None,
               managed: Optional[List[dict]] = None, extra: str = "", jar: bool = True,
               ext: Optional[str] = None, gradle_metadata: Optional[dict] = None, classifiers: List[str] = (),
               omit_version: bool = False, omit_group: bool = False) -> Path:
        group, artifact, version = coords.split(":")
        d = self.dir(group, artifact, version)
        parts = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<project xmlns="http://maven.apache.org/POM/4.0.0">', "<modelVersion>4.0.0</modelVersion>"]
        if gradle_metadata is not None:
            parts.append("<!-- do_not_remove: published-with-gradle-metadata -->")
        if parent:
            pg, pa, pv = parent.split(":")
            parts.append("<parent><groupId>%s</groupId><artifactId>%s</artifactId><version>%s</version></parent>"
                         % (pg, pa, pv))
        if not omit_group:
            parts.append("<groupId>%s</groupId>" % group)
        parts.append("<artifactId>%s</artifactId>" % artifact)
        if not omit_version:
            parts.append("<version>%s</version>" % version)
        if packaging:
            parts.append("<packaging>%s</packaging>" % packaging)
        if properties:
            parts.append("<properties>%s</properties>" % "".join("<%s>%s</%s>" % (k, v, k) for k, v in properties.items()))
        if managed:
            parts.append("<dependencyManagement><dependencies>%s</dependencies></dependencyManagement>"
                         % "".join(_dep_xml(m) for m in managed))
        if deps:
            parts.append("<dependencies>%s</dependencies>" % "".join(_dep_xml(x) for x in deps))
        parts.append(extra)
        parts.append("</project>")
        (d / ("%s-%s.pom" % (artifact, version))).write_text("\n".join(parts), encoding="utf-8")
        if jar and packaging != "pom":
            e = ext or g2m.PACKAGING_EXTENSIONS.get(packaging or "jar", packaging or "jar")
            (d / ("%s-%s.%s" % (artifact, version, e))).write_bytes(("jar of %s" % coords).encode())
        for c in classifiers:
            (d / ("%s-%s-%s.jar" % (artifact, version, c))).write_bytes(("%s jar of %s" % (c, coords)).encode())
        if gradle_metadata is not None:
            (d / ("%s-%s.module" % (artifact, version))).write_text(json.dumps(gradle_metadata), encoding="utf-8")
        return d

    def metadata(self, group: str, artifact: str, versions: List[str]) -> None:
        d = self.root / group.replace(".", "/") / artifact
        d.mkdir(parents=True, exist_ok=True)
        (d / "maven-metadata.xml").write_text(
            "<metadata><groupId>%s</groupId><artifactId>%s</artifactId><versioning><versions>%s</versions>"
            "</versioning></metadata>" % (group, artifact, "".join("<version>%s</version>" % v for v in versions)),
            encoding="utf-8")


def _dep_xml(d: dict) -> str:
    g, a, *rest = d["coords"].split(":")
    out = "<dependency><groupId>%s</groupId><artifactId>%s</artifactId>" % (g, a)
    if rest and rest[0]:
        out += "<version>%s</version>" % rest[0]
    for key in ("scope", "type", "classifier", "optional"):
        if key in d:
            out += "<%s>%s</%s>" % (key, d[key], key)
    if d.get("exclusions"):
        out += "<exclusions>%s</exclusions>" % "".join(
            "<exclusion><groupId>%s</groupId><artifactId>%s</artifactId></exclusion>" % tuple(e.split(":"))
            for e in d["exclusions"])
    return out + "</dependency>"


class _Handler(http.server.SimpleHTTPRequestHandler):
    server: "_Server"

    def log_message(self, *args) -> None:  # silence
        pass

    def do_GET(self) -> None:  # noqa: N802
        srv = self.server
        with srv.lock:
            srv.requests.append(self.path)
            srv.headers.append(dict(self.headers))
            failures = srv.fail.get(self.path)
            status = failures.pop(0) if failures else None
        if srv.auth and self.headers.get("Authorization") != srv.auth:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="test"')
            self.end_headers()
            return
        if status:
            self.send_response(status)
            self.end_headers()
            return
        super().do_GET()


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lock = threading.Lock()
        self.requests: List[str] = []
        self.headers: List[dict] = []
        self.fail: Dict[str, List[int]] = {}
        self.auth: Optional[str] = None


class RepoServer:
    """Serves a directory over HTTP (or HTTPS with ``certfile``) on 127.0.0.1."""

    def __init__(self, directory: Path, certfile: Optional[Path] = None, keyfile: Optional[Path] = None,
                 host: str = "127.0.0.1"):
        handler = functools.partial(_Handler, directory=str(directory))
        self.httpd = _Server((host, 0), handler)
        scheme = "http"
        if certfile:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(certfile), str(keyfile))
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
            scheme = "https"
        self.url = "%s://%s:%d/" % (scheme, host, self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "RepoServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def requests(self) -> List[str]:
        return list(self.httpd.requests)


def make_self_signed_cert(directory: Path) -> Optional[tuple]:
    """Create a self-signed certificate for localhost/127.0.0.1 with openssl, or None if unavailable."""
    if not shutil.which("openssl"):
        return None
    cert, key = directory / "cert.pem", directory / "key.pem"
    cmd = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
           "-days", "2", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    except (subprocess.SubprocessError, OSError):
        return None
    return cert, key


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="g2m-test-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # never touch the real ~/.gradle or ~/.m2 of the machine running the tests
        self.gradle_home = self.tmp / "gradle-home"
        self.gradle_home.mkdir()
        self._env = mock_env(GRADLE_USER_HOME=str(self.gradle_home), GRADLE_RO_DEP_CACHE=None)
        self._env.__enter__()
        self.addCleanup(self._env.__exit__, None, None, None)

    def write(self, rel: str, content: str) -> Path:
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def parse(self, target: Path, **kwargs) -> "g2m.BuildModel":
        parser = g2m.GradleBuildParser(self.gradle_home, **kwargs)
        return parser.parse(target)

    def run_cli(self, *argv: str, expect: Optional[int] = None):
        """Run the CLI in-process; returns (exit code, stdout, log text)."""
        args = g2m.build_arg_parser().parse_args(list(argv))
        out = io.StringIO()
        logs = io.StringIO()
        handler = logging.StreamHandler(logs)
        logger = g2m.log
        old = (logger.handlers[:], logger.level, logger.propagate)
        logger.handlers[:] = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = g2m.execute(args)
        finally:
            logger.handlers[:], logger.level, logger.propagate = old
        if expect is not None:
            self.assertEqual(code, expect, "unexpected exit code; log:\n" + logs.getvalue())
        return code, out.getvalue(), logs.getvalue()


@contextlib.contextmanager
def _env_ctx(values):
    saved = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def mock_env(**values):
    return _env_ctx(values)


def deps_of(model: "g2m.BuildModel") -> List[str]:
    return [d.notation() for d in model.dependencies]
