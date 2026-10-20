import json
import subprocess
import sys
import unittest

from support import ROOT, FakeRepo, RepoServer, TempDirTestCase, g2m

SCRIPT = str(ROOT / "gradle2maven.py")


def run_script(*args, env=None, cwd=None):
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, timeout=300,
                          env=env, cwd=cwd)


class EntryPointTest(TempDirTestCase):
    def test_help_and_version(self):
        p = run_script("--help")
        self.assertEqual(p.returncode, 0)
        for flag in ("--output", "--offline", "--insecure", "--no-ssl-fallback", "--gradle-home", "--list",
                     "--init-script", "--conflict-strategy", "--credentials", "exit status"):
            self.assertIn(flag, p.stdout)
        p = run_script("--version")
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "gradle2maven " + g2m.__version__))

    def test_usage_errors(self):
        self.assertEqual(run_script().returncode, 2)
        self.assertEqual(run_script("x", "--conflict-strategy", "nope").returncode, 2)
        p = run_script(str(self.tmp / "missing" / "build.gradle"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("no such file", p.stderr)

    def test_list_json_via_subprocess(self):
        build = self.write("p/build.gradle", "dependencies { implementation 'a:b:1' }")
        p = run_script(str(build), "--list", "--format", "json", "-P", "x=y")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["dependencies"][0]["notation"], "a:b:1")
        self.assertEqual(data["buildFiles"], [str(build.resolve())])

    def test_full_run_via_subprocess(self):
        remote = FakeRepo(self.tmp / "remote")
        remote.module("org.x:x:1")
        with RepoServer(remote.root) as server:
            build = self.write("p/build.gradle", "repositories { maven { url '%s' } }\n"
                                                 "dependencies { implementation 'org.x:x:1' }" % server.url)
            p = run_script(str(build), "-o", str(self.tmp / "out"), "--no-default-repositories",
                           "--gradle-home", str(self.gradle_home))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("downloaded", p.stderr)
        self.assertTrue((self.tmp / "out/org/x/x/1/x-1.jar").is_file())
        p = run_script(str(build), "-o", str(self.tmp / "out"), "-q", "--offline")
        self.assertEqual((p.returncode, p.stderr), (0, ""))


class CliOptionsTest(TempDirTestCase):
    def test_list_text(self):
        build = self.write("p/build.gradle", '''
            plugins { id 'org.example.plugin' version '1.0' }
            repositories { mavenCentral() }
            dependencies {
                implementation('a:b:1') { exclude group: 'x' }
                api platform('a:bom:1')
                implementation "a:c:$nope"
            }''')
        _, out, _ = self.run_cli(str(build), "--list", expect=0)
        self.assertIn("org.example.plugin 1.0", out)
        self.assertIn("a:b:1  [excludes x:*]", out)
        self.assertIn("a:bom:1  [platform]", out)
        self.assertIn("org.example.plugin:org.example.plugin.gradle.plugin:1.0", out)
        self.assertIn(g2m.MAVEN_CENTRAL, out)
        self.assertIn("Warnings (1)", out)
        self.assertIn("plugins.gradle.org", out)

    def test_list_flags(self):
        build = self.write("p/build.gradle", '''
            plugins { id 'org.example.plugin' version '1.0' }
            dependencies { implementation 'a:b:1'; testImplementation 'a:t:1' }''')
        _, out, _ = self.run_cli(str(build), "--list", "--format", "json", "--no-plugins",
                                 "--exclude-configurations", "^test", "-d", "extra:dep:2", expect=0)
        notations = [d["notation"] for d in json.loads(out)["dependencies"]]
        self.assertEqual(notations, ["a:b:1", "extra:dep:2"])
        _, out, _ = self.run_cli(str(build), "--list", "--format", "json", "--configurations", "^test", expect=0)
        notations = [d["notation"] for d in json.loads(out)["dependencies"]]
        self.assertEqual(notations[0], "a:t:1")

    def test_cli_property_override(self):
        self.write("p/gradle.properties", "v=1\n")
        build = self.write("p/build.gradle", "dependencies { implementation \"a:b:$v\" }")
        _, out, _ = self.run_cli(str(build), "--list", "--format", "json", "-P", "v=2", expect=0)
        self.assertEqual(json.loads(out)["dependencies"][0]["version"], "2")

    def test_dependencies_file_and_bad_coordinates(self):
        build = self.write("p/build.gradle", "")
        deps = self.write("deps.txt", "# extra\ng:a:1   # trailing\n\ng:b:2@zip\n")
        _, out, _ = self.run_cli(str(build), "--list", "--format", "json", "--dependencies-file", str(deps),
                                 expect=0)
        self.assertEqual([d["notation"] for d in json.loads(out)["dependencies"]], ["g:a:1", "g:b:2@zip"])
        self.assertEqual(self.run_cli(str(build), "--list", "-d", "not-coords")[0], 1)
        self.assertEqual(self.run_cli(str(build), "--list", "-P", "novalue")[0], 1)
        self.assertEqual(self.run_cli(str(build), "--credentials", "bad")[0], 1)

    def test_extra_repository_and_report_and_init_script(self):
        remote = FakeRepo(self.tmp / "remote")
        remote.module("org.x:x:1")
        out = self.tmp / "out"
        build = self.write("p/build.gradle", "dependencies { implementation 'org.x:x:1' }")
        with RepoServer(remote.root) as server:
            self.run_cli(str(build), "-o", str(out), "-r", server.url, "--no-default-repositories",
                         "--report", str(self.tmp / "report.json"), "--init-script", str(self.tmp / "init.gradle"),
                         "--checksums", "sha256,sha512", expect=0)
        self.assertTrue((out / "org/x/x/1/x-1.jar.sha256").is_file())
        self.assertTrue((out / "org/x/x/1/x-1.jar.sha512").is_file())
        self.assertFalse((out / "org/x/x/1/x-1.jar.sha1").exists())
        report = json.loads((self.tmp / "report.json").read_text())
        self.assertEqual(report["modules"][0]["coordinates"], "org.x:x:1")
        self.assertEqual(report["modules"][0]["files"]["x-1.jar"], server.url)
        self.assertEqual(report["stats"]["downloaded"], 2)
        init = (self.tmp / "init.gradle").read_text()
        self.assertIn(json.dumps(str(out)), init)
        self.assertIn("addFirst", init)

    def test_local_directory_as_repository(self):
        local = FakeRepo(self.tmp / "localrepo")
        local.module("org.l:l:1")
        build = self.write("p/build.gradle", "dependencies { implementation 'org.l:l:1' }")
        self.run_cli(str(build), "-o", str(self.tmp / "out"), "-r", str(local.root), "--offline",
                     "--checksums", "none", "--no-metadata", expect=0)
        self.assertTrue((self.tmp / "out/org/l/l/1/l-1.jar").is_file())
        self.assertFalse((self.tmp / "out/org/l/l/1/l-1.jar.sha1").exists())
        self.assertFalse((self.tmp / "out/org/l/l/maven-metadata.xml").exists())

    def test_clean_refuses_foreign_directory(self):
        foreign = self.tmp / "important"
        foreign.mkdir()
        (foreign / "data.txt").write_text("keep me")
        build = self.write("p/build.gradle", "")
        code, _, logs = self.run_cli(str(build), "-o", str(foreign), "--clean", "--offline")
        self.assertEqual(code, 1)
        self.assertIn("refusing to --clean", logs)
        self.assertTrue((foreign / "data.txt").is_file())
        # a directory created by gradle2maven may be cleaned
        own = self.tmp / "own"
        self.run_cli(str(build), "-o", str(own), "--offline", expect=0)
        (own / "stale.txt").write_text("x")
        self.run_cli(str(build), "-o", str(own), "--offline", "--clean", expect=0)
        self.assertFalse((own / "stale.txt").exists())

    def test_invalid_checksum(self):
        build = self.write("p/build.gradle", "")
        self.assertEqual(self.run_cli(str(build), "-o", str(self.tmp / "o"), "--checksums", "crc32")[0], 1)

    def test_multiple_build_files(self):
        a = self.write("a/build.gradle", "dependencies { implementation 'a:a:1' }")
        b = self.write("b/build.gradle.kts", 'dependencies { implementation("b:b:1") }')
        _, out, _ = self.run_cli(str(a), str(b.parent), "--list", "--format", "json", expect=0)
        self.assertEqual([d["notation"] for d in json.loads(out)["dependencies"]], ["a:a:1", "b:b:1"])


if __name__ == "__main__":
    unittest.main()
