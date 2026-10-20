import hashlib
import json
import unittest
import xml.etree.ElementTree as ET

from support import FakeRepo, RepoServer, TempDirTestCase


def module_json(variants):
    return {"formatVersion": "1.1", "component": {}, "variants": variants}


class ResolverTest(TempDirTestCase):
    """Full CLI runs against a fake remote repository served over HTTP."""

    def setUp(self):
        super().setUp()
        self.remote = FakeRepo(self.tmp / "remote")
        self.out = self.tmp / "out"

    def build(self, deps, repos=None, extra=""):
        repo_lines = "\n".join("maven { url '%s' }" % r for r in (repos or [self.server.url]))
        lines = "\n".join("    implementation '%s'" % d for d in deps)
        return self.write("proj/build.gradle", "repositories {\n%s\n}\ndependencies {\n%s\n}\n%s" % (
            repo_lines, lines, extra))

    def run_g2m(self, build, *extra, expect=0):
        return self.run_cli(str(build), "-o", str(self.out), "--no-default-repositories", "--retries", "1",
                            "--timeout", "10", *extra, expect=expect)

    def out_file(self, coords, filename):
        g, a, v = coords.split(":")
        return self.out / g.replace(".", "/") / a / v / filename

    def assertHas(self, coords, *filenames):
        for f in filenames:
            self.assertTrue(self.out_file(coords, f).is_file(), "%s missing for %s" % (f, coords))

    def assertHasNot(self, coords, *filenames):
        for f in filenames:
            self.assertFalse(self.out_file(coords, f).exists(), "%s unexpectedly present for %s" % (f, coords))

    # ------------------------------------------------------------------
    def test_transitive_parent_properties_bom_exclusions_scopes(self):
        r = self.remote
        r.module("org.parent:parent:1", packaging="pom", properties={"dep.version": "2.0"},
                 managed=[{"coords": "org.managed:managed:3.0"}],
                 deps=[{"coords": "org.inherited:inh:1.0"}])
        r.module("org.bom:bom:1", packaging="pom", managed=[{"coords": "org.frombom:fb:4.0"}])
        r.module("org.app:app:1.0", parent="org.parent:parent:1", omit_group=True, deps=[
            {"coords": "org.prop:prop:${dep.version}"},
            {"coords": "org.managed:managed"},
            {"coords": "org.frombom:fb"},
            {"coords": "org.self:self:${project.version}"},
            {"coords": "org.excl:excl:1.0", "exclusions": ["org.gone:gone"]},
            {"coords": "org.test:test:1.0", "scope": "test"},
            {"coords": "org.prov:prov:1.0", "scope": "provided"},
            {"coords": "org.opt:opt:1.0", "optional": "true"},
            {"coords": "org.rt:rt:1.0", "scope": "runtime"},
            {"coords": "org.cls:cls:1.0", "classifier": "natives"},
            {"coords": "org.tj:tj:1.0", "type": "test-jar", "scope": "compile"},
        ], managed=[{"coords": "org.bom:bom:1", "type": "pom", "scope": "import"}])
        for c in ("org.prop:prop:2.0", "org.managed:managed:3.0", "org.frombom:fb:4.0", "org.self:self:1.0",
                  "org.test:test:1.0", "org.prov:prov:1.0", "org.opt:opt:1.0", "org.rt:rt:1.0",
                  "org.inherited:inh:1.0", "org.gone:gone:1.0"):
            r.module(c)
        r.module("org.excl:excl:1.0", deps=[{"coords": "org.gone:gone:1.0"}, {"coords": "org.kept:kept:1.0"}])
        r.module("org.kept:kept:1.0")
        r.module("org.cls:cls:1.0", classifiers=["natives"])
        r.module("org.tj:tj:1.0", classifiers=["tests"])
        with RepoServer(r.root) as self.server:
            code, _, logs = self.run_g2m(self.build(["org.app:app:1.0"]))
        self.assertHas("org.app:app:1.0", "app-1.0.pom", "app-1.0.jar", "app-1.0.jar.sha1", "app-1.0.jar.md5")
        self.assertHas("org.parent:parent:1", "parent-1.pom")
        self.assertHasNot("org.parent:parent:1", "parent-1.jar")
        self.assertHas("org.bom:bom:1", "bom-1.pom")
        for c in ("org.prop:prop:2.0", "org.managed:managed:3.0", "org.frombom:fb:4.0", "org.self:self:1.0",
                  "org.rt:rt:1.0", "org.inherited:inh:1.0", "org.excl:excl:1.0", "org.kept:kept:1.0"):
            g, a, v = c.split(":")
            self.assertHas(c, "%s-%s.jar" % (a, v), "%s-%s.pom" % (a, v))
        for c in ("org.test:test:1.0", "org.prov:prov:1.0", "org.opt:opt:1.0", "org.gone:gone:1.0"):
            self.assertFalse(self.out_file(c, "x").parent.exists(), c)
        self.assertHas("org.cls:cls:1.0", "cls-1.0.jar", "cls-1.0-natives.jar")
        self.assertHas("org.tj:tj:1.0", "tj-1.0-tests.jar")
        # checksums are correct
        jar = self.out_file("org.app:app:1.0", "app-1.0.jar")
        self.assertEqual(self.out_file("org.app:app:1.0", "app-1.0.jar.sha1").read_text(),
                         hashlib.sha1(jar.read_bytes()).hexdigest())
        self.assertEqual(self.out_file("org.app:app:1.0", "app-1.0.jar.md5").read_text(),
                         hashlib.md5(jar.read_bytes()).hexdigest())

    def test_scopes_and_optional_flags(self):
        r = self.remote
        r.module("org.app:app:1.0", deps=[{"coords": "org.test:test:1.0", "scope": "test"},
                                          {"coords": "org.opt:opt:1.0", "optional": "true"}])
        r.module("org.test:test:1.0")
        r.module("org.opt:opt:1.0")
        with RepoServer(r.root) as self.server:
            self.run_g2m(self.build(["org.app:app:1.0"]), "--scopes", "compile,runtime,test", "--optional")
        self.assertHas("org.test:test:1.0", "test-1.0.jar")
        self.assertHas("org.opt:opt:1.0", "opt-1.0.jar")

    def test_packaging_variants_and_relocation(self):
        r = self.remote
        r.module("org.b:bundle:1", packaging="bundle")
        r.module("org.a:aar:1", packaging="aar")
        r.module("org.w:weird:1", packaging="custom-pkg", ext="jar")  # falls back to .jar like Gradle
        r.module("org.p:pom-with-jar:1", packaging="pom")
        (r.dir("org.p", "pom-with-jar", "1") / "pom-with-jar-1.jar").write_bytes(b"jar")
        r.module("org.old:old:1", packaging="pom", jar=False,
                 extra="<distributionManagement><relocation><groupId>org.new</groupId></relocation>"
                       "</distributionManagement>")
        r.module("org.new:old:1")
        with RepoServer(r.root) as self.server:
            self.run_g2m(self.build(["org.b:bundle:1", "org.a:aar:1", "org.w:weird:1", "org.p:pom-with-jar:1",
                                     "org.old:old:1"]))
        self.assertHas("org.b:bundle:1", "bundle-1.jar")
        self.assertHas("org.a:aar:1", "aar-1.aar")
        self.assertHas("org.w:weird:1", "weird-1.jar")
        self.assertHas("org.p:pom-with-jar:1", "pom-with-jar-1.jar")
        self.assertHas("org.new:old:1", "old-1.jar")

    def test_dynamic_versions_and_ranges(self):
        r = self.remote
        for v in ("1.0", "1.5", "2.0"):
            r.module("org.d:dyn:%s" % v)
        r.metadata("org.d", "dyn", ["1.0", "1.5", "2.0"])
        r.module("org.r:ranged:1.0", deps=[{"coords": "org.d:dyn:[1.0,2.0)"}])
        with RepoServer(r.root) as self.server:
            self.run_g2m(self.build(["org.d:dyn:1.+", "org.r:ranged:1.0", "org.d:dyn:latest.release"]))
        self.assertHas("org.d:dyn:1.5", "dyn-1.5.jar")
        self.assertHas("org.d:dyn:2.0", "dyn-2.0.jar")
        self.assertHasNot("org.d:dyn:1.0", "dyn-1.0.jar")
        meta = ET.parse(str(self.out / "org/d/dyn/maven-metadata.xml")).getroot()
        self.assertEqual([v.text for v in meta.iter("version")], ["1.5", "2.0"])
        self.assertEqual(meta.findtext("versioning/release"), "2.0")

    def test_unmatched_dynamic_version_fails(self):
        self.remote.metadata("org.d", "dyn", ["1.0"])
        with RepoServer(self.remote.root) as self.server:
            _, _, logs = self.run_g2m(self.build(["org.d:dyn:5.+"]), expect=3)
        self.assertIn("no matching version", logs)

    def test_snapshot(self):
        d = self.remote.dir("org.s", "snap", "1.0-SNAPSHOT")
        (d / "snap-1.0-20240101.120000-3.pom").write_text(
            "<project><groupId>org.s</groupId><artifactId>snap</artifactId><version>1.0-SNAPSHOT</version></project>")
        (d / "snap-1.0-20240101.120000-3.jar").write_bytes(b"snapshot jar")
        (d / "maven-metadata.xml").write_text(
            "<metadata><versioning><snapshot><timestamp>20240101.120000</timestamp><buildNumber>3</buildNumber>"
            "</snapshot><snapshotVersions><snapshotVersion><extension>jar</extension>"
            "<value>1.0-20240101.120000-3</value></snapshotVersion></snapshotVersions></versioning></metadata>")
        with RepoServer(self.remote.root) as self.server:
            self.run_g2m(self.build(["org.s:snap:1.0-SNAPSHOT"]))
        self.assertHas("org.s:snap:1.0-SNAPSHOT", "snap-1.0-SNAPSHOT.pom", "snap-1.0-SNAPSHOT.jar")
        self.assertEqual(self.out_file("org.s:snap:1.0-SNAPSHOT", "snap-1.0-SNAPSHOT.jar").read_bytes(),
                         b"snapshot jar")

    def test_gradle_module_metadata(self):
        r = self.remote
        r.module("org.k:kmp:1.0", jar=False, packaging="pom", gradle_metadata=module_json([
            {"name": "jvmApiElements", "attributes": {"org.jetbrains.kotlin.platform.type": "jvm"},
             "available-at": {"url": "../../kmp-jvm/1.0/kmp-jvm-1.0.module", "group": "org.k",
                              "module": "kmp-jvm", "version": "1.0"}},
            {"name": "jsApiElements", "attributes": {"org.jetbrains.kotlin.platform.type": "js"},
             "available-at": {"url": "../../kmp-js/1.0/kmp-js-1.0.module", "group": "org.k",
                              "module": "kmp-js", "version": "1.0"}},
            {"name": "metadataApiElements", "attributes": {"org.jetbrains.kotlin.platform.type": "common"},
             "files": [{"name": "kmp-metadata-1.0.jar", "url": "kmp-1.0.jar"}]},
        ]))
        (r.dir("org.k", "kmp", "1.0") / "kmp-1.0.jar").write_bytes(b"metadata")
        r.module("org.k:kmp-jvm:1.0", gradle_metadata=module_json([
            {"name": "apiElements", "attributes": {"org.gradle.category": "library"},
             "files": [{"name": "kmp-jvm-1.0.jar", "url": "kmp-jvm-1.0.jar"}],
             "dependencies": [{"group": "org.dep", "module": "dep", "version": {"requires": "1.0"},
                               "excludes": [{"group": "org.never", "module": "*"}]},
                              {"group": "org.strict", "module": "s", "version": {"strictly": "2.0",
                                                                                   "requires": "1.0"}}],
             "dependencyConstraints": [{"group": "org.cons", "module": "c", "version": {"requires": "9.0"}}]},
            {"name": "sourcesElements", "attributes": {"org.gradle.category": "documentation",
                                                       "org.gradle.docstype": "sources"},
             "files": [{"name": "kmp-jvm-1.0-sources.jar", "url": "kmp-jvm-1.0-sources.jar"}]},
        ]), classifiers=["sources"])
        r.module("org.k:kmp-js:1.0")
        r.module("org.dep:dep:1.0", deps=[{"coords": "org.never:never:1.0"}, {"coords": "org.cons:c:1.0"}])
        r.module("org.never:never:1.0")
        r.module("org.strict:s:2.0")
        r.module("org.cons:c:1.0")
        r.module("org.cons:c:9.0")
        with RepoServer(r.root) as self.server:
            self.run_g2m(self.build(["org.k:kmp:1.0"]))
        self.assertHas("org.k:kmp:1.0", "kmp-1.0.module", "kmp-1.0.pom", "kmp-1.0.jar")
        self.assertHas("org.k:kmp-jvm:1.0", "kmp-jvm-1.0.module", "kmp-jvm-1.0.jar")
        self.assertHasNot("org.k:kmp-jvm:1.0", "kmp-jvm-1.0-sources.jar")
        self.assertFalse((self.out / "org/k/kmp-js").exists(), "js platform should be filtered out")
        self.assertHas("org.dep:dep:1.0", "dep-1.0.jar")
        self.assertFalse((self.out / "org/never").exists(), "module metadata excludes must apply")
        self.assertHas("org.strict:s:2.0", "s-2.0.jar")
        self.assertHas("org.cons:c:1.0", "c-1.0.jar")
        self.assertHas("org.cons:c:9.0", "c-9.0.jar")  # upgraded by the dependency constraint

        # all platforms + sources
        with RepoServer(r.root) as self.server:
            self.run_g2m(self.build(["org.k:kmp:1.0"]), "--kotlin-platforms", "all", "--sources")
        self.assertHas("org.k:kmp-js:1.0", "kmp-js-1.0.jar")
        self.assertHas("org.k:kmp-jvm:1.0", "kmp-jvm-1.0-sources.jar")

    def test_platform_constraints_and_versionless(self):
        r = self.remote
        r.module("org.bom:bom:1", packaging="pom", managed=[{"coords": "org.lib:a:2.0"},
                                                            {"coords": "org.lib:b:3.0"}])
        r.module("org.lib:a:2.0", deps=[{"coords": "org.lib:b:1.0"}])
        r.module("org.lib:b:1.0")
        r.module("org.lib:b:3.0")
        with RepoServer(r.root) as self.server:
            build = self.write("proj/build.gradle", '''
                repositories { maven { url '%s' } }
                dependencies {
                    implementation platform('org.bom:bom:1')
                    implementation 'org.lib:a'
                }''' % self.server.url)
            self.run_g2m(build)
        self.assertHas("org.lib:a:2.0", "a-2.0.jar")
        self.assertHas("org.lib:b:1.0", "b-1.0.jar")
        self.assertHas("org.lib:b:3.0", "b-3.0.jar")  # Gradle would pick the platform's version

    def test_root_dependency_management_applies_to_graph(self):
        # Maven semantics: the requested artifact's (inherited) dependencyManagement pins transitive versions
        r = self.remote
        r.module("org.p:parent:1", packaging="pom", managed=[{"coords": "org.t:t:2.0"}, {"coords": "org.u:u:9"}])
        r.module("org.plugin:plugin:1", parent="org.p:parent:1", deps=[{"coords": "org.mid:mid:1"}])
        r.module("org.mid:mid:1", deps=[{"coords": "org.t:t:1.0"}])
        r.module("org.t:t:1.0")
        r.module("org.t:t:2.0")
        with RepoServer(r.root) as self.server:
            self.run_g2m(self.build(["org.plugin:plugin:1"]))
        self.assertHas("org.t:t:1.0", "t-1.0.jar")
        self.assertHas("org.t:t:2.0", "t-2.0.jar")
        self.assertFalse((self.out / "org/u").exists(), "managed modules outside the graph are not added")

    def test_versionless_without_platform_fails(self):
        with RepoServer(self.remote.root) as self.server:
            _, _, logs = self.run_g2m(self.build(["org.lib:nover"]), expect=3)
        self.assertIn("no version declared", logs)

    def test_conflict_strategy_highest(self):
        r = self.remote
        r.module("org.x:one:1", deps=[{"coords": "org.c:c:1.0"}])
        r.module("org.x:two:1", deps=[{"coords": "org.c:c:2.0"}])
        r.module("org.c:c:1.0")
        r.module("org.c:c:2.0")
        with RepoServer(r.root) as self.server:
            self.run_g2m(self.build(["org.x:one:1", "org.x:two:1"]), "--conflict-strategy", "highest")
        self.assertHas("org.c:c:2.0", "c-2.0.jar")
        self.assertHas("org.c:c:1.0", "c-1.0.pom")
        self.assertHasNot("org.c:c:1.0", "c-1.0.jar")

    def test_exclude_option_and_no_transitive(self):
        r = self.remote
        r.module("org.x:top:1", deps=[{"coords": "com.android:tools:1"}, {"coords": "org.y:y:1"}])
        r.module("com.android:tools:1")
        r.module("org.y:y:1")
        with RepoServer(r.root) as self.server:
            self.run_g2m(self.build(["org.x:top:1"]), "--exclude", "com.android*")
        self.assertFalse((self.out / "com/android").exists())
        self.assertHas("org.y:y:1", "y-1.jar")
        with RepoServer(r.root) as self.server:
            self.run_g2m(self.build(["org.x:top:1"]), "--no-transitive", "--clean")
        self.assertHas("org.x:top:1", "top-1.jar")
        self.assertFalse((self.out / "org/y").exists())

    def test_multiple_repositories_and_failures(self):
        other = FakeRepo(self.tmp / "other")
        other.module("org.only:other:1")
        self.remote.module("org.x:main:1", deps=[{"coords": "org.only:other:1"}])
        with RepoServer(self.remote.root) as self.server, RepoServer(other.root) as second:
            self.server.httpd.fail["/org/x/main/1/main-1.jar"] = [503]  # transient: retried
            code, _, logs = self.run_g2m(self.build(["org.x:main:1", "org.missing:gone:1"],
                                                    repos=[self.server.url, second.url]), expect=3)
            self.assertGreaterEqual(self.server.requests.count("/org/x/main/1/main-1.jar"), 2)
        self.assertHas("org.x:main:1", "main-1.jar")
        self.assertHas("org.only:other:1", "other-1.jar")
        self.assertIn("org.missing:gone:1 - POM not found", logs)
        # --allow-missing turns it into success
        with RepoServer(self.remote.root) as self.server:
            self.run_g2m(self.build(["org.missing:gone:1"]), "--allow-missing", expect=0)

    def test_missing_artifact_reported(self):
        self.remote.module("org.x:nojar:1", jar=False)
        with RepoServer(self.remote.root) as self.server:
            _, _, logs = self.run_g2m(self.build(["org.x:nojar:1"]), "--report", str(self.tmp / "r.json"),
                                      expect=3)
        report = json.loads((self.tmp / "r.json").read_text())
        mod = [m for m in report["modules"] if m["coordinates"] == "org.x:nojar:1"][0]
        self.assertEqual(mod["missing"], ["nojar-1.jar"])
        self.assertEqual(len(report["failures"]), 1)

    def test_credentials(self):
        self.remote.module("org.sec:sec:1")
        with RepoServer(self.remote.root) as self.server:
            self.server.httpd.auth = "Basic " + __import__("base64").b64encode(b"alice:s3cr3t").decode()
            self.run_g2m(self.build(["org.sec:sec:1"]), expect=3)
            with __import__("support").mock_env(REPO_PW="s3cr3t"):
                self.run_g2m(self.build(["org.sec:sec:1"]), "--credentials",
                             "%s=alice:env:REPO_PW" % self.server.url, expect=0)
        self.assertHas("org.sec:sec:1", "sec-1.jar")

    def test_credentials_from_build_script(self):
        self.remote.module("org.sec:sec:1")
        with RepoServer(self.remote.root) as self.server:
            self.server.httpd.auth = "Basic " + __import__("base64").b64encode(b"bob:pw").decode()
            self.write("proj/gradle.properties", "user=bob\npass=pw\n")
            build = self.write("proj/build.gradle", '''
                repositories { maven { url '%s'; credentials { username user; password pass } } }
                dependencies { implementation 'org.sec:sec:1' }''' % self.server.url)
            self.run_g2m(build)
        self.assertHas("org.sec:sec:1", "sec-1.jar")


class SourcesTest(TempDirTestCase):
    """Gradle cache first, then local Maven repositories, then the network."""

    def setUp(self):
        super().setUp()
        self.remote = FakeRepo(self.tmp / "remote")
        self.out = self.tmp / "out"
        self.cache = self.gradle_home / "caches" / "modules-2" / "files-2.1"

    def cache_module(self, coords, files):
        g, a, v = coords.split(":")
        for i, (name, content) in enumerate(files.items()):
            d = self.cache / g / a / v / ("%040x" % (i + 1))
            d.mkdir(parents=True, exist_ok=True)
            (d / name).write_text(content)

    def build(self, url, deps):
        return self.write("proj/build.gradle", "repositories { maven { url '%s' } }\ndependencies {\n%s\n}" % (
            url, "\n".join("implementation '%s'" % d for d in deps)))

    def test_gradle_cache_is_used_before_network(self):
        pom = "<project><groupId>org.c</groupId><artifactId>cached</artifactId><version>1</version>" \
              "<dependencies><dependency><groupId>org.r</groupId><artifactId>remote</artifactId>" \
              "<version>1</version></dependency></dependencies></project>"
        self.cache_module("org.c:cached:1", {"cached-1.pom": pom, "cached-1.jar": "cached jar"})
        self.remote.module("org.c:cached:1")  # different content remotely
        self.remote.module("org.r:remote:1")
        with RepoServer(self.remote.root) as server:
            code, _, logs = self.run_cli(str(self.build(server.url, ["org.c:cached:1"])), "-o", str(self.out),
                                         "--no-default-repositories", expect=0)
            requests = server.requests
        self.assertFalse([r for r in requests if "/org/c/" in r], requests)
        self.assertIn("/org/r/remote/1/remote-1.jar", requests)
        self.assertEqual((self.out / "org/c/cached/1/cached-1.jar").read_text(), "cached jar")
        self.assertTrue((self.out / "org/r/remote/1/remote-1.jar").is_file())
        self.assertIn("2 copied from Gradle cache", logs)

    def test_offline_uses_cache_only(self):
        for v in ("1.0", "2.0"):
            self.cache_module("org.c:cached:" + v, {
                "cached-%s.pom" % v: "<project><groupId>org.c</groupId><artifactId>cached</artifactId>"
                                     "<version>%s</version></project>" % v,
                "cached-%s.jar" % v: v})
        with RepoServer(self.remote.root) as server:
            build = self.build(server.url, ["org.c:cached:1.+", "org.c:missing:1"])
            code, _, logs = self.run_cli(str(build), "-o", str(self.out), "--offline", expect=3)
            self.assertEqual(server.requests, [])
        self.assertTrue((self.out / "org/c/cached/1.0/cached-1.0.jar").is_file())
        self.assertFalse((self.out / "org/c/cached/2.0").exists())
        self.assertIn("org.c:missing:1", logs)

    def test_no_gradle_cache(self):
        self.cache_module("org.c:cached:1", {"cached-1.pom": "<project/>", "cached-1.jar": "cached"})
        self.remote.module("org.c:cached:1")
        with RepoServer(self.remote.root) as server:
            self.run_cli(str(self.build(server.url, ["org.c:cached:1"])), "-o", str(self.out),
                         "--no-gradle-cache", "--no-default-repositories", expect=0)
            self.assertIn("/org/c/cached/1/cached-1.jar", server.requests)
        self.assertEqual((self.out / "org/c/cached/1/cached-1.jar").read_text(), "jar of org.c:cached:1")

    def test_copy_entire_cache(self):
        self.cache_module("org.unrelated:u:1", {"u-1.jar": "u", "u-1.pom": "<project/>"})
        self.cache_module("com.skip:s:1", {"s-1.jar": "s"})
        with RepoServer(self.remote.root) as server:
            self.run_cli(str(self.build(server.url, [])), "-o", str(self.out), "--copy-entire-cache",
                         "--exclude", "com.skip", "--no-default-repositories", expect=0)
        self.assertTrue((self.out / "org/unrelated/u/1/u-1.jar").is_file())
        self.assertTrue((self.out / "org/unrelated/u/1/u-1.jar.sha1").is_file())
        self.assertTrue((self.out / "org/unrelated/u/maven-metadata.xml").is_file())
        self.assertFalse((self.out / "com/skip").exists())

    def test_maven_local_and_file_repositories(self):
        m2 = FakeRepo(self.tmp / "m2")
        m2.module("org.m2:local:1")
        filerepo = FakeRepo(self.tmp / "proj" / "libs-repo")
        filerepo.module("org.file:repo:1")
        build = self.write("proj/build.gradle", '''
            repositories { mavenLocal(); maven { url 'libs-repo' } }
            dependencies { implementation 'org.m2:local:1'; implementation 'org.file:repo:1' }''')
        self.run_cli(str(build), "-o", str(self.out), "--maven-local-dir", str(m2.root), "--offline", expect=0)
        self.assertTrue((self.out / "org/m2/local/1/local-1.jar").is_file())
        self.assertTrue((self.out / "org/file/repo/1/repo-1.jar").is_file())

    def test_existing_output_is_reused_and_refresh(self):
        self.remote.module("org.x:x:1")
        with RepoServer(self.remote.root) as server:
            build = self.build(server.url, ["org.x:x:1"])
            self.run_cli(str(build), "-o", str(self.out), "--no-default-repositories", expect=0)
            n = len(server.requests)
            _, _, logs = self.run_cli(str(build), "-o", str(self.out), "--no-default-repositories", expect=0)
            self.assertEqual(len(server.requests), n, "second run must not hit the network")
            self.assertIn("2 already present", logs)
            self.run_cli(str(build), "-o", str(self.out), "--no-default-repositories", "--refresh", expect=0)
            self.assertGreater(len(server.requests), n)


if __name__ == "__main__":
    unittest.main()
