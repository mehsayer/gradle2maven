import unittest

from support import g2m


class CompareVersionsTest(unittest.TestCase):
    def assertOrdered(self, *versions):
        for lower, higher in zip(versions, versions[1:]):
            self.assertLess(g2m.compare_versions(lower, higher), 0, "%s < %s" % (lower, higher))
            self.assertGreater(g2m.compare_versions(higher, lower), 0, "%s > %s" % (higher, lower))

    def test_numeric(self):
        self.assertOrdered("1.0", "1.0.1", "1.1", "1.2", "1.10", "2", "10.0")

    def test_qualifiers(self):
        self.assertOrdered("1.0-dev", "1.0-alpha", "1.0-beta", "1.0-milestone", "1.0-rc1", "1.0-SNAPSHOT",
                           "1.0", "1.0.1")
        self.assertOrdered("1.0-rc-1", "1.0-rc-2", "1.0-final")
        self.assertOrdered("1.0-alpha1", "1.0-alpha2", "1.0-alpha10")

    def test_equal(self):
        self.assertEqual(g2m.compare_versions("1.0.0", "1.0.0"), 0)
        self.assertEqual(g2m.compare_versions("1-0", "1.0"), 0)

    def test_sorting_key(self):
        versions = ["2.0", "1.10", "1.9", "1.0-beta", "1.0"]
        self.assertEqual(sorted(versions, key=g2m.version_key), ["1.0-beta", "1.0", "1.9", "1.10", "2.0"])


class RangesTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(g2m.parse_version_ranges("[1.0,2.0)"), [("1.0", True, "2.0", False)])
        self.assertEqual(g2m.parse_version_ranges("(,1.5]"), [(None, False, "1.5", True)])
        self.assertEqual(g2m.parse_version_ranges("[1.0]"), [("1.0", True, "1.0", True)])
        self.assertEqual(g2m.parse_version_ranges("]1.0,2.0["), [("1.0", False, "2.0", False)])
        self.assertEqual(len(g2m.parse_version_ranges("[1,2),[3,4)")), 2)
        self.assertIsNone(g2m.parse_version_ranges("1.0"))
        self.assertIsNone(g2m.parse_version_ranges("[1.0"))

    def test_dynamic_detection(self):
        for v in ("1.+", "+", "latest.release", "latest.integration", "[1,2)", "(,3]"):
            self.assertTrue(g2m.is_dynamic_version(v), v)
        for v in ("1.0", "1.0-SNAPSHOT", None, ""):
            self.assertFalse(g2m.is_dynamic_version(v), v)

    def test_select(self):
        cands = ["1.0", "1.5", "1.9.1", "2.0", "2.1-SNAPSHOT", "2.0.1-rc1"]
        self.assertEqual(g2m.select_version("1.+", cands), "1.9.1")
        self.assertEqual(g2m.select_version("+", cands), "2.1-SNAPSHOT")
        self.assertEqual(g2m.select_version("latest.release", cands), "2.0.1-rc1")
        self.assertEqual(g2m.select_version("[1.0,2.0)", cands), "1.9.1")
        self.assertEqual(g2m.select_version("[1.0,2.0]", cands), "2.0")
        self.assertEqual(g2m.select_version("(,1.5)", cands), "1.0")
        self.assertEqual(g2m.select_version("[1.0,1.5),[2.0,2.0]", cands), "2.0")
        self.assertIsNone(g2m.select_version("[3,)", cands))
        self.assertEqual(g2m.select_version("1.5", cands), "1.5")

    def test_rich_versions(self):
        self.assertEqual(g2m.normalize_requested_version("1.2!!"), "1.2")
        self.assertEqual(g2m.normalize_requested_version("[1.0,2.0)!!1.5"), "1.5")
        self.assertEqual(g2m.normalize_requested_version(" 1.0 "), "1.0")
        self.assertIsNone(g2m.normalize_requested_version(""))


class CoordinatesTest(unittest.TestCase):
    def test_parse(self):
        d = g2m.parse_coordinates("org.example:lib:1.0:linux-x86_64@zip")
        self.assertEqual((d.group, d.artifact, d.version, d.classifier, d.ext),
                         ("org.example", "lib", "1.0", "linux-x86_64", "zip"))
        d = g2m.parse_coordinates("org.example:lib")
        self.assertEqual((d.group, d.artifact, d.version), ("org.example", "lib", None))
        d = g2m.parse_coordinates("org.example:lib:[1.0,2.0)")
        self.assertEqual(d.version, "[1.0,2.0)")
        self.assertEqual(g2m.parse_coordinates("g:a:1@jar").ext, "jar")

    def test_invalid(self):
        for s in ("lib", "g:a:$version", "g:a:1:2:3:4", ":a:1", "g a:b:1", "https://x/y", "g:a:1@"):
            self.assertIsNone(g2m.parse_coordinates(s), s)

    def test_notation_roundtrip(self):
        for s in ("g:a", "g:a:1", "g:a:1:cls", "g:a:1@zip", "g:a:1:cls@zip"):
            self.assertEqual(g2m.parse_coordinates(s).notation(), s)

    def test_plugin_marker(self):
        d = g2m.plugin_marker("org.jetbrains.kotlin.jvm", "2.0.0")
        self.assertEqual(d.notation(), "org.jetbrains.kotlin.jvm:org.jetbrains.kotlin.jvm.gradle.plugin:2.0.0")


if __name__ == "__main__":
    unittest.main()
