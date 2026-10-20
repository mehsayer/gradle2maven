import json
import unittest

from support import FakeRepo, RepoServer, TempDirTestCase, g2m, make_self_signed_cert


class SslFallbackTest(TempDirTestCase):
    """Downloads over HTTPS from a server with a self-signed (untrusted) certificate."""

    def setUp(self):
        super().setUp()
        certs = make_self_signed_cert(self.tmp)
        if certs is None:
            self.skipTest("openssl is not available")
        self.cert, self.key = certs
        self.remote = FakeRepo(self.tmp / "remote")
        self.remote.module("org.tls:lib:1.0", deps=[{"coords": "org.tls:dep:1.0"}])
        self.remote.module("org.tls:dep:1.0")
        self.out = self.tmp / "out"

    def run_against_https(self, *extra, expect):
        with RepoServer(self.remote.root, certfile=self.cert, keyfile=self.key) as server:
            self.assertTrue(server.url.startswith("https://"))
            build = self.write("proj/build.gradle", "repositories { maven { url '%s' } }\n"
                                                    "dependencies { implementation 'org.tls:lib:1.0' }" % server.url)
            return self.run_cli(str(build), "-o", str(self.out), "--no-default-repositories", "--retries", "0",
                                "--report", str(self.tmp / "report.json"), *extra, expect=expect)

    def test_retries_without_certificate_check(self):
        _, _, logs = self.run_against_https(expect=0)
        self.assertIn("SSL error for 127.0.0.1", logs)
        self.assertIn("CERTIFICATE_VERIFY_FAILED", logs)
        self.assertIn("retrying with certificate verification disabled", logs)
        self.assertEqual(logs.count("SSL error for"), 1, "warn once per host")
        self.assertIn("TLS certificate verification was disabled for: 127.0.0.1", logs)
        self.assertTrue((self.out / "org/tls/lib/1.0/lib-1.0.jar").is_file())
        self.assertTrue((self.out / "org/tls/dep/1.0/dep-1.0.jar").is_file())
        report = json.loads((self.tmp / "report.json").read_text())
        self.assertEqual(report["insecureHosts"], ["127.0.0.1"])

    def test_no_ssl_fallback_fails(self):
        _, _, logs = self.run_against_https("--no-ssl-fallback", expect=3)
        self.assertIn("CERTIFICATE_VERIFY_FAILED", logs)
        self.assertFalse((self.out / "org/tls/lib/1.0/lib-1.0.jar").exists())

    def test_insecure_skips_verification_silently(self):
        _, _, logs = self.run_against_https("--insecure", expect=0)
        self.assertNotIn("SSL error", logs)
        self.assertTrue((self.out / "org/tls/lib/1.0/lib-1.0.jar").is_file())

    def test_ca_file_makes_certificate_trusted(self):
        _, _, logs = self.run_against_https("--ca-file", str(self.cert), "--no-ssl-fallback", expect=0)
        self.assertNotIn("SSL error", logs)
        report = json.loads((self.tmp / "report.json").read_text())
        self.assertEqual(report["insecureHosts"], [])

    def test_http_client_directly(self):
        with RepoServer(self.remote.root, certfile=self.cert, keyfile=self.key) as server:
            url = server.url + "org/tls/lib/1.0/lib-1.0.jar"
            client = g2m.HttpClient(retries=0)
            with self.assertLogs(g2m.log, "WARNING") as cm:
                self.assertTrue(client.download(url, self.tmp / "a.jar"))
            self.assertIn("retrying with certificate verification disabled", cm.output[0])
            self.assertEqual(client.insecure_hosts, {"127.0.0.1"})
            self.assertFalse(client.download(server.url + "nope.jar", self.tmp / "b.jar"))  # 404 -> False
            strict = g2m.HttpClient(retries=0, ssl_fallback=False)
            with self.assertRaises(g2m.FetchError):
                strict.download(url, self.tmp / "c.jar")
            self.assertFalse((self.tmp / "c.jar").exists())

    def test_is_ssl_error(self):
        import ssl
        import urllib.error
        self.assertTrue(g2m.is_ssl_error(ssl.SSLCertVerificationError("bad")))
        self.assertTrue(g2m.is_ssl_error(urllib.error.URLError(ssl.SSLError("x"))))
        self.assertTrue(g2m.is_ssl_error(urllib.error.URLError("[SSL: CERTIFICATE_VERIFY_FAILED] x")))
        self.assertFalse(g2m.is_ssl_error(urllib.error.URLError(ConnectionRefusedError())))
        self.assertFalse(g2m.is_ssl_error(ValueError("x")))


if __name__ == "__main__":
    unittest.main()
