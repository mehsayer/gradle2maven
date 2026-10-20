import unittest

from support import g2m

CATALOG = '''
# comment
[versions]
kotlin = "2.0.21"       # trailing comment
jackson = { strictly = "2.17.2" }
"quoted-key" = 'literal\\no-escape'

[libraries]
jackson-databind = { module = "com.fasterxml.jackson.core:jackson-databind", version.ref = "jackson" }
plain = "g:a:1.0"
multi = { group = "g", name = "b", version = { require = "1.0", prefer = "1.1" } }

[bundles]
all = [
    "jackson-databind",
    "plain",   # comment inside array
]

[plugins]
kotlin-jvm = { id = "org.jetbrains.kotlin.jvm", version.ref = "kotlin" }

[misc]
flag = true
n = 1_000
f = 1.5
s = """
multi
line"""
date = 2024-01-01
empty = {}

[[arr]]
x = 1
[[arr]]
x = 2
'''


class MiniTomlTest(unittest.TestCase):
    def test_matches_expected_structure(self):
        data = g2m._MiniToml(CATALOG).parse()
        self.assertEqual(data["versions"]["kotlin"], "2.0.21")
        self.assertEqual(data["versions"]["jackson"], {"strictly": "2.17.2"})
        self.assertEqual(data["versions"]["quoted-key"], "literal\\no-escape")
        self.assertEqual(data["libraries"]["jackson-databind"]["version"], {"ref": "jackson"})
        self.assertEqual(data["libraries"]["multi"]["version"], {"require": "1.0", "prefer": "1.1"})
        self.assertEqual(data["bundles"]["all"], ["jackson-databind", "plain"])
        self.assertEqual(data["misc"], {"flag": True, "n": 1000, "f": 1.5, "s": "multi\nline",
                                        "date": "2024-01-01", "empty": {}})
        self.assertEqual(data["arr"], [{"x": 1}, {"x": 2}])

    def test_same_result_as_tomllib(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("tomllib not available (Python < 3.11)")
        expected = tomllib.loads(CATALOG)
        expected["misc"]["date"] = str(expected["misc"]["date"])
        self.assertEqual(g2m._MiniToml(CATALOG).parse(), expected)

    def test_escapes(self):
        self.assertEqual(g2m._MiniToml('a = "x\\ty\\u0041\\"q"').parse(), {"a": 'x\tyA"q'})

    def test_errors(self):
        for bad in ('a = "unterminated', "a = [1, 2", "[table", "= 1", "a = 1 b = 2"):
            with self.assertRaises(g2m.TomlError, msg=bad):
                g2m._MiniToml(bad).parse()


if __name__ == "__main__":
    unittest.main()
