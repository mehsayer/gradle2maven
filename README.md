# gradle2maven

Gradle caches/build files to local Maven repository converter.

`gradle2maven` reads a Gradle build (Groovy `build.gradle` or Kotlin `build.gradle.kts`) and produces a
**self-contained local Maven repository** with every dependency the build needs, including transitive
dependencies, parent POMs, BOMs, Gradle Module Metadata and Gradle plugins. Point Gradle (or Maven)
at that directory and the project builds **fully offline**, for example on an air-gapped machine, in a
locked-down CI runner, or for reproducible archives.

* **Single file, standard library only.** Python 3.9+, nothing to install, and Gradle/Java not required.
* **Gradle cache first.** Before anything is downloaded, `~/.gradle/caches/modules-2/files-2.1`
  (or `$GRADLE_USER_HOME`, plus `$GRADLE_RO_DEP_CACHE`) is checked. Cached files are copied instead of
  downloaded. With `--offline` the repository is built from the cache alone.
* **SSL fallback.** If a download fails with an SSL error (self-signed, expired or mismatched
  certificate, TLS-intercepting proxy, ...), it is retried *without certificate verification*. A
  warning is printed once per host, and the summary/report lists every host that was accessed insecurely.
  Use `--no-ssl-fallback` to disable this, `--ca-file` to trust a corporate CA properly, or `-k` to
  never verify.
* **Understands real builds**:
  * Groovy and Kotlin DSL; all dependency notations (`'g:a:v'`, `group:/name:/version:` maps,
    `kotlin("x")`, `platform()`, `enforcedPlatform()`, `testFixtures()`, `add("cfg", ...)`,
    `"kapt"(...)`, rich versions `strictly/require/prefer`, `1.0!!`, closures with `exclude`/`transitive`).
  * Variables from `ext`, `extra`, `def`/`val`, `by project`, `by extra`, Groovy maps and Kotlin
    `mapOf`, `gradle.properties` (project, root and Gradle home), string templates and concatenation,
    and `-P key=value` overrides.
  * Version catalogs (`gradle/libs.versions.toml` plus catalogs declared in settings): libraries,
    bundles, plugin aliases and `libs.versions.x`.
  * `settings.gradle(.kts)`: `include(...)` subprojects, `pluginManagement`,
    `dependencyResolutionManagement`, settings plugins, `buildSrc` and `includeBuild(...)` build
    logic (including precompiled script plugins).
  * `plugins { }` blocks (resolved through plugin marker artifacts), `buildscript { classpath ... }`,
    `apply from: 'other.gradle'`, `allprojects`/`subprojects` blocks, `resolutionStrategy.force`, and
    Spring `dependencyManagement { imports { mavenBom ... } }`.
  * Repositories: `mavenCentral()`, `google()`, `gradlePluginPortal()`, `mavenLocal()`,
    `maven { url ... }` in all its forms, with credentials. Publishing repositories are ignored.
  * Implicit dependencies that plugins add: the Kotlin stdlib and compiler, and the Spring Boot BOM.
* **Faithful resolution.** Parent POMs, property interpolation, `dependencyManagement`, `import`-scoped
  BOMs, exclusions, optional and scoped dependencies, relocations, classifiers and types, packaging
  (`bundle`, `aar`, pom-with-jar, ...), version ranges and dynamic versions (`1.+`,
  `latest.release`, `[1.0,2.0)`), SNAPSHOT timestamps, Gradle Module Metadata (`.module`) variants,
  `available-at` (Kotlin Multiplatform) and dependency constraints.
* **A usable repository.** `maven-metadata.xml` is generated per artifact, so dynamic versions keep
  working. `.sha1`/`.md5` (optionally `.sha256`/`.sha512`) checksums are written for every file, and
  there's a ready-made Gradle init script (`--init-script`) plus a JSON report (`--report`).

## Installation

```sh
# run it directly
python3 gradle2maven.py --help

# or install the `gradle2maven` command
pip install .
```

## Quick start

```sh
# 1. collect everything the build needs into ./local-maven-repo
gradle2maven path/to/project/build.gradle --init-script offline.gradle

# 2. build offline, using nothing but that repository
gradle --offline --init-script offline.gradle build
```

The argument can be a `build.gradle`, `build.gradle.kts`, a settings file or a project directory.
For a root project, all subprojects from `settings.gradle(.kts)` are included. Several builds can be
merged into one repository by passing several arguments.

Sample output (the Spring Boot fixture in `tests/fixtures/e2e`, with a partly filled Gradle cache):

```
using Gradle cache: /home/me/.gradle/caches/modules-2/files-2.1
remote repositories: https://repo.maven.apache.org/maven2/, https://plugins.gradle.org/m2/
resolving level 0: 12 module request(s)
resolving level 1: 90 module request(s)
...
resolving level 7: 3 module request(s)
fetching 207 artifact file(s) for 206 module(s)

Repository: /home/me/project/local-maven-repo
  modules: 206 resolved, 2500 file(s), 94.2 MiB on disk, 222 maven-metadata.xml
  files: 20 copied from Gradle cache, 591 downloaded, 0 from local Maven repos, 0 already present
  time: 35.4s, 604 HTTP request(s), 93.9 MiB downloaded
```

Running it again only fetches what's missing, because existing files are reused.

## More examples

```sh
# inspect what the parser sees (no network); JSON for scripting
gradle2maven build.gradle.kts --list
gradle2maven build.gradle.kts --list --format json | jq '.dependencies[].notation'

# custom output dir, include sources and javadoc jars, write a report
gradle2maven . -o /srv/offline-repo --sources --javadoc --report deps.json

# only from the Gradle cache, no network at all
gradle2maven build.gradle --offline

# start from scratch, and copy the entire Gradle cache in as well
gradle2maven build.gradle --clean --copy-entire-cache

# a single subproject, without test dependencies
gradle2maven app/build.gradle.kts --no-subprojects --exclude-configurations '(?i)test'

# extra repositories (searched after the build's own), with credentials from the environment
gradle2maven build.gradle -r https://nexus.example.com/repository/public/ \
    --credentials https://nexus.example.com/=deploy:env:NEXUS_PASSWORD

# corporate TLS proxy: trust its CA instead of disabling verification
gradle2maven build.gradle --ca-file /etc/ssl/certs/corp-ca.pem --no-ssl-fallback

# things the build resolves at execution time (tool classpaths added by plugins, etc.)
gradle2maven build.gradle -d org.jacoco:org.jacoco.agent:0.8.12:runtime -d org.jacoco:org.jacoco.ant:0.8.12
gradle2maven build.gradle --dependencies-file extra-deps.txt     # one coordinate per line

# property values that are normally passed on the Gradle command line
gradle2maven build.gradle -P springBootVersion=3.3.4

# smaller repo: keep only the winning version's jars (POMs of all versions are kept)
gradle2maven build.gradle --conflict-strategy highest
```

## Wiring the local repository into an existing project

All examples assume the repository lives at `/opt/local-maven-repo` (absolute path) or at
`local-maven-repo/` inside the project (relative path). Add `--offline` to the Gradle command line to
make sure nothing is fetched from the network.

### Option 1: init script (no changes to the project)

`--init-script FILE` writes a Gradle init script that puts the local repository **first** for
plugins, `buildscript` classpaths and project dependencies. The project's own repositories stay in
place but are never contacted with `--offline`.

```sh
gradle2maven . -o /opt/local-maven-repo --init-script /opt/local-maven-repo.gradle

gradle --offline --init-script /opt/local-maven-repo.gradle build
./gradlew --offline -I /opt/local-maven-repo.gradle build
```

To apply it to every build on a machine, copy it to `~/.gradle/init.d/local-maven-repo.gradle`.

The generated script looks like this, so you can also write your own:

```groovy
def g2mRepo = new File("/opt/local-maven-repo").toURI()
def g2mMode = null
def g2mFirst = { repos ->
    def repo = repos.maven { url = g2mRepo; name = 'gradle2maven' }
    repos.remove(repo)
    repos.addFirst(repo)
}
beforeSettings { settings ->
    g2mFirst(settings.pluginManagement.repositories)
    try { g2mFirst(settings.dependencyResolutionManagement.repositories) } catch (Throwable ignored) { }
}
settingsEvaluated { settings ->
    try { g2mMode = settings.dependencyResolutionManagement.repositoriesMode.get().toString() } catch (Throwable ignored) { }
}
allprojects {
    g2mFirst(buildscript.repositories)
    if (g2mMode != 'FAIL_ON_PROJECT_REPOS') {
        g2mFirst(repositories)
    }
}
```

### Option 2: `settings.gradle` / `settings.gradle.kts` (Gradle 6.8+)

Groovy DSL, `settings.gradle`:

```groovy
pluginManagement {
    repositories {
        maven { url = uri("$rootDir/local-maven-repo") }   // or uri('/opt/local-maven-repo')
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositories {
        maven { url = uri("$rootDir/local-maven-repo") }
        mavenCentral()
    }
}
```

Kotlin DSL, `settings.gradle.kts`:

```kotlin
pluginManagement {
    repositories {
        maven { url = uri("${rootDir}/local-maven-repo") }   // or uri("/opt/local-maven-repo")
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositories {
        maven { url = uri("${rootDir}/local-maven-repo") }
        mavenCentral()
    }
}
```

Keep `pluginManagement { }` as the first block in the settings file.

### Option 3: `build.gradle` / `build.gradle.kts` (project-level repositories)

Groovy DSL:

```groovy
buildscript {                       // only needed for legacy `apply plugin:` + classpath
    repositories {
        maven { url = uri("$rootDir/local-maven-repo") }
    }
}
allprojects {                       // or just `repositories { }` in a single-project build
    repositories {
        maven { url = uri("$rootDir/local-maven-repo") }
        mavenCentral()
    }
}
```

Kotlin DSL:

```kotlin
allprojects {
    repositories {
        maven { url = uri("${rootDir}/local-maven-repo") }
        mavenCentral()
    }
}
```

Plugins from `plugins { }` blocks are resolved through `pluginManagement` (option 2) or an init script
(option 1). Project repositories alone don't cover them.

### Option 4: make it switchable with a property

```kotlin
// settings.gradle.kts
pluginManagement {
    repositories {
        providers.gradleProperty("offlineRepo").orNull?.let { maven { url = uri(it) } }
        gradlePluginPortal()
    }
}
val offlineRepo = providers.gradleProperty("offlineRepo").orNull
dependencyResolutionManagement {
    repositories {
        if (offlineRepo != null) maven { url = uri(offlineRepo) }
        mavenCentral()
    }
}
```

```sh
gradle --offline -PofflineRepo=/opt/local-maven-repo build
```

### Option 5: serve it over HTTP for a team or CI

```sh
cd /opt/local-maven-repo && python3 -m http.server 8081
```

```kotlin
repositories {
    maven {
        url = uri("http://build-host:8081/")
        isAllowInsecureProtocol = true      // Groovy: allowInsecureProtocol = true
    }
}
```

A copy can also be uploaded to Nexus or Artifactory, since it uses the standard Maven layout.

### Option 6: Maven projects

A Maven build also needs Maven's own build plugins, which a Gradle build doesn't list. Add them with
`-d`. These are the plugins `mvn package` uses for a jar project on Maven 3.9.9; check
`mvn help:effective-pom` for yours:

```sh
gradle2maven build.gradle -o /opt/local-maven-repo \
    -d org.apache.maven.plugins:maven-resources-plugin:3.3.1 \
    -d org.apache.maven.plugins:maven-compiler-plugin:3.13.0 \
    -d org.apache.maven.plugins:maven-surefire-plugin:3.2.5 \
    -d org.apache.maven.plugins:maven-jar-plugin:3.4.1
```

The `dependencyManagement` of these top-level coordinates is applied to their dependency graph, as
Maven does when it resolves a plugin.

**Mirror** (`~/.m2/settings.xml`). Every repository request is routed to the directory, so the
network is never used. Don't add `-o`: Maven's offline mode also blocks `file://` repositories.

```xml
<settings>
  <mirrors>
    <mirror>
      <id>gradle2maven</id>
      <mirrorOf>*</mirrorOf>
      <url>file:///opt/local-maven-repo</url>
    </mirror>
  </mirrors>
</settings>
```

```sh
mvn package
```

**As the local repository.** Maven reads it directly and also writes into it. gradle2maven doesn't
write `_remote.repositories` files, so Maven treats every artifact as locally installed:

```sh
mvn -o -Dmaven.repo.local=/opt/local-maven-repo package
```

**Per project** (`pom.xml`). The repository is searched in addition to Maven Central:

```xml
<repositories>
  <repository>
    <id>gradle2maven</id>
    <url>file://${project.basedir}/local-maven-repo</url>
  </repository>
</repositories>
```

### Verifying that a build is really self-contained

Use an empty Gradle home so the regular cache can't hide missing files:

```sh
GRADLE_USER_HOME=$(mktemp -d) gradle --offline --init-script offline.gradle build
```

If Gradle reports `No cached version of g:a:v available for offline mode`, add that coordinate with
`-d g:a:v` (or `--dependencies-file`) and run gradle2maven again.

## How it works

1. **Parse.** Build, settings, applied scripts, `gradle.properties` and version catalogs are analysed
   statically. Nothing from the build is executed, so it's safe on untrusted projects and doesn't need
   Java or Gradle.
2. **Resolve.** The dependency graph is walked breadth-first in parallel (`-j`). For every module the
   POM (and the `.module` file when the POM says it was published with Gradle metadata) is read.
   Versions requested by platforms/BOMs and Gradle dependency constraints are added for modules in the
   graph, because Gradle may pick those versions. By default, artifacts of *every* version seen are
   kept (`--conflict-strategy all`) so whatever Gradle's conflict resolution picks is present.
3. **Fetch.** Each file is looked up in this order: the output repository (already there) → the
   **Gradle cache** → `mavenLocal()`/`file:` repositories → remote repositories, in declaration order.
   The first repository that has a module is tried first for the rest of its files. Transient errors
   (timeouts, 5xx, 429) are retried with backoff. SSL failures are retried without certificate
   verification unless `--no-ssl-fallback` is given.
4. **Finish.** Checksums and `maven-metadata.xml` are written, along with an optional init script
   and JSON report.

Exit status: `0` success, `1` error, `2` usage error, `3` some dependencies could not be resolved (all
other files are still written; `--allow-missing` returns `0`).

## Limitations

The build is not executed, so anything computed at execution time can't be seen. The usual cases are:

* **Tool classpaths added by plugins at execution time**, such as JaCoCo agent, Checkstyle, PMD,
  detekt, ktlint, protoc and the Android `aapt2`. Add them with `-d`/`--dependencies-file`. The Kotlin
  compiler classpath and the Spring Boot BOM are handled automatically.
* Dependencies built with arbitrary code (loops over lists, `if` on environment, custom functions):
  values that can't be evaluated are reported as warnings (see `--list`).
* Convention plugins that add dependencies programmatically (`project.dependencies.add(...)` inside
  Kotlin/Groovy classes) aren't seen. Their `*.gradle(.kts)` precompiled script plugins are.
* Ivy and `flatDir` repositories, and published version catalogs (`from("g:a:v")`), aren't supported.
* The Gradle distribution itself (wrapper) and JDK toolchains aren't part of a Maven repository.

## Development

```sh
python3 -m unittest discover -s tests          # unit + integration tests (local HTTP/HTTPS servers)

# end-to-end: generate repositories for the projects in tests/fixtures/e2e and build them with real
# Gradle using --offline and an empty GRADLE_USER_HOME (needs network, Gradle and a JDK)
G2M_E2E=1 G2M_GRADLE=/path/to/gradle python3 -m unittest discover -s tests -p 'test_e2e*.py' -v
```

The test suite covers parsing (Groovy, Kotlin DSL, catalogs, settings, build logic), version
semantics, POM/BOM/module-metadata resolution, Gradle-cache precedence, offline mode, retries,
credentials, the SSL fallback (against a self-signed HTTPS server), and the CLI.

## License

MIT
