"""End-to-end tests with a real Gradle installation and the network.

They are skipped unless ``G2M_E2E=1`` is set.  Gradle is taken from ``$G2M_GRADLE`` or ``PATH``
and must be able to run (``JAVA_HOME`` set or ``java`` on ``PATH``).

For every fixture project the test
  1. runs gradle2maven with an empty Gradle cache (everything is downloaded), then
  2. builds the project with ``gradle --offline`` and a *fresh, empty* GRADLE_USER_HOME,
     using only the generated repository (wired in through the generated init script).
A second test populates a Gradle cache with a normal online build and checks that gradle2maven
can then create a working repository from that cache alone (``--offline``).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import ROOT

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "e2e"
GRADLE = os.environ.get("G2M_GRADLE") or shutil.which("gradle")
ENABLED = os.environ.get("G2M_E2E") == "1"


@unittest.skipUnless(ENABLED, "set G2M_E2E=1 to run end-to-end tests with real Gradle (needs network)")
@unittest.skipUnless(GRADLE, "gradle not found (set G2M_GRADLE)")
class GradleEndToEndTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="g2m-e2e-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def project(self, name):
        dest = self.tmp / name
        shutil.copytree(str(FIXTURES / name), str(dest))
        return dest

    def gradle(self, project, *args, gradle_home, check=True):
        env = dict(os.environ, GRADLE_USER_HOME=str(gradle_home))
        env.pop("GRADLE_RO_DEP_CACHE", None)
        p = subprocess.run([GRADLE, "--no-daemon", "--console=plain", *args], cwd=str(project), env=env,
                           capture_output=True, text=True, timeout=1800)
        if check:
            self.assertEqual(p.returncode, 0, "gradle %s failed:\n%s\n%s" % (" ".join(args), p.stdout[-5000:],
                                                                             p.stderr[-5000:]))
        return p

    def g2m(self, project, repo, init, *extra):
        p = subprocess.run([sys.executable, str(ROOT / "gradle2maven.py"), str(project), "-o", str(repo),
                            "--init-script", str(init), *extra], capture_output=True, text=True, timeout=1800)
        self.assertEqual(p.returncode, 0, p.stderr[-5000:])
        return p

    def build_offline(self, project, init, *tasks):
        home = self.tmp / ("offline-home-" + project.name)
        if home.exists():
            shutil.rmtree(str(home))
        home.mkdir()
        return self.gradle(project, "--offline", "--init-script", str(init), *tasks, gradle_home=home)

    def check_fixture(self, name, *tasks, verify=None):
        project = self.project(name)
        empty_home = self.tmp / "empty-home"
        empty_home.mkdir(exist_ok=True)
        repo, init = self.tmp / ("repo-" + name), self.tmp / ("init-" + name + ".gradle")
        self.g2m(project, repo, init, "--gradle-home", str(empty_home))
        p = self.build_offline(project, init, *tasks)
        self.assertIn("BUILD SUCCESSFUL", p.stdout)
        if verify:
            verify(project)

        # Negative control: an empty repository must make the same offline build fail.
        empty_repo = self.tmp / "empty-repo"
        empty_repo.mkdir(exist_ok=True)
        empty_init = self.tmp / "init-empty.gradle"
        empty_init.write_text(init.read_text().replace(str(repo.resolve()), str(empty_repo.resolve())))
        for build_dir in project.glob("**/build"):
            shutil.rmtree(str(build_dir), ignore_errors=True)
        home = self.tmp / "offline-home-negative"
        shutil.rmtree(str(home), ignore_errors=True)
        home.mkdir()
        p = self.gradle(project, "--offline", "--init-script", str(empty_init), *tasks, gradle_home=home,
                        check=False)
        self.assertNotEqual(p.returncode, 0)
        return project

    def test_groovy_java(self):
        self.check_fixture("groovy-java", "build", verify=lambda project: self.assertTrue(
            list((project / "build" / "test-results" / "test").glob("*.xml"))))

    def test_kotlin_multiproject(self):
        p = self.build_offline(self.check_fixture("kotlin-multiproject", "build"),
                               self.tmp / "init-kotlin-multiproject.gradle", ":app:run")
        self.assertIn('{"name":"x"}', p.stdout)

    def test_groovy_spring_boot(self):
        self.check_fixture("groovy-spring-boot", "build", verify=lambda project: self.assertTrue(
            list((project / "web" / "build" / "test-results" / "test").glob("*.xml"))
            and list((project / "web" / "build" / "libs").glob("*.jar"))))

    def test_repository_from_populated_gradle_cache(self):
        project = self.project("groovy-java")
        home = self.tmp / "populated-home"
        home.mkdir()
        self.gradle(project, "build", gradle_home=home)  # normal online build fills the cache
        shutil.rmtree(str(project / "build"))
        repo, init = self.tmp / "repo-cache", self.tmp / "init-cache.gradle"
        p = self.g2m(project, repo, init, "--gradle-home", str(home), "--offline")
        self.assertIn("0 downloaded", p.stderr)
        self.assertIn("0 HTTP request(s)", p.stderr)
        self.build_offline(project, init, "build")


if __name__ == "__main__":
    unittest.main()
