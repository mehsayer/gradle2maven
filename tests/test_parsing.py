import unittest

from support import TempDirTestCase, deps_of, g2m


class ScannerTest(unittest.TestCase):
    def test_strip_comments_keeps_urls_in_strings(self):
        src = 'url "https://example.com/repo" // comment\n/* block\ncomment */ x = \'//not\'\n'
        out = g2m.strip_comments(src)
        self.assertIn('"https://example.com/repo"', out)
        self.assertNotIn("comment", out)
        self.assertIn("'//not'", out)
        self.assertEqual(out.count("\n"), src.count("\n"))

    def test_find_matching_skips_strings(self):
        text = 'a { b("}") { c } }'
        self.assertEqual(g2m.find_matching(text, 2), len(text) - 1)

    def test_iter_statements(self):
        body = '''
            implementation 'a:b:1'
            api("c:d:2") { exclude(group = "x") }
            "kapt"("e:f:3")
            constraints {
                implementation('g:h:4')
            }
            foo = bar
            implementation group: 'i', name: 'j',
                version: '5'
        '''
        stmts = list(g2m.iter_statements(body))
        self.assertEqual([s.name for s in stmts], ["implementation", "api", "kapt", "constraints", "implementation"])
        self.assertEqual(stmts[0].args, "'a:b:1'")
        self.assertEqual(stmts[1].closure.strip(), 'exclude(group = "x")')
        self.assertIn("version: '5'", stmts[4].args)

    def test_first_expression_and_unwrap(self):
        self.assertEqual(g2m.first_expression('"x" ; y'), '"x"')
        self.assertEqual(g2m.first_expression('uri("x") }'), 'uri("x")')
        self.assertEqual(g2m.unwrap_call('uri(file("x"))', ("uri", "file")), '"x"')


class VariablesTest(TempDirTestCase):
    def scope_for(self, text, **props):
        scope = g2m.Scope(properties=props)
        g2m.collect_variables(g2m.strip_comments(text), scope)
        return scope

    def test_groovy_variables(self):
        scope = self.scope_for('''
            ext.a = '1'
            ext { b = "2"; set('c', '3') }
            def d = "x${a}"
            project.ext['e'] = '5'
            versions = [spring: '6.0', nested: [x: 'y']]
            ''')
        self.assertEqual(scope.resolve("a"), "1")
        self.assertEqual(scope.resolve("b"), "2")
        self.assertEqual(scope.resolve("c"), "3")
        self.assertEqual(scope.resolve("d"), "x1")
        self.assertEqual(scope.resolve("e"), "5")
        self.assertEqual(scope.resolve("versions.spring"), "6.0")
        self.assertEqual(scope.resolve("versions['spring']"), "6.0")
        self.assertEqual(scope.resolve("versions.nested.x"), "y")

    def test_kotlin_variables(self):
        scope = self.scope_for('''
            val a = "1"
            val b: String = "$a.2"
            val c by extra("3")
            extra["d"] = "4"
            val m = mapOf("k" to "v")
            val p: String by project
            ''', p="from-props")
        self.assertEqual(scope.resolve("b"), "1.2")
        self.assertEqual(scope.resolve("c"), "3")
        self.assertEqual(scope.resolve('extra["d"]'), "4")
        self.assertEqual(scope.resolve('m["k"]'), "v")
        self.assertEqual(scope.resolve('"${m.getValue("k")}"'), "v")
        self.assertEqual(scope.resolve("p"), "from-props")
        self.assertEqual(scope.resolve('property("p")'), "from-props")
        self.assertEqual(scope.resolve('project.findProperty("p") as String'), "from-props")

    def test_expressions(self):
        scope = self.scope_for("def v = '1.0'")
        self.assertEqual(scope.resolve("'g:a:' + v"), "g:a:1.0")
        self.assertEqual(scope.resolve('"g:a:$v"'), "g:a:1.0")
        self.assertEqual(scope.resolve("'g:a:$v'"), "g:a:$v")  # no interpolation in single quotes
        self.assertEqual(scope.resolve('findProperty("missing") ?: "dflt"'), "dflt")
        self.assertIsNone(scope.resolve("unknownVar"))
        self.assertEqual(g2m.interpolate("x-${nope}-$v", scope), "x-${nope}-1.0")

    def test_overrides_win(self):
        scope = g2m.Scope(properties={"v": "props"}, overrides={"v": "cli"})
        scope.vars["v"] = "'script'"
        self.assertEqual(scope.resolve("v"), "cli")

    def test_read_properties(self):
        p = self.write("gradle.properties", "# c\na=1\nb : 2\nc 3\nlong=x\\\n   y\n! bang\n")
        self.assertEqual(g2m.read_properties(p), {"a": "1", "b": "2", "c": "3", "long": "xy"})


class GroovyBuildTest(TempDirTestCase):
    def test_dependency_notations(self):
        self.write("build.gradle", '''
            plugins { id 'java' }
            ext.junitVersion = '5.10.0'
            def libs2 = [guava: 'com.google.guava:guava:33.0.0-jre']
            def bundle = ['x:one:1', 'x:two:2']
            repositories { mavenCentral() }
            dependencies {
                implementation 'org.slf4j:slf4j-api:2.0.9'
                implementation "org.junit.jupiter:junit-jupiter-api:${junitVersion}"
                implementation group: 'commons-io', name: 'commons-io', version: '2.15.0'
                implementation('org.apache:lib:1.0:linux@zip')
                implementation libs2.guava
                implementation bundle
                runtimeOnly 'a:one:1', 'a:two:2'
                testImplementation platform('org.junit:junit-bom:5.10.0')
                testImplementation enforcedPlatform("org.e:bom:1")
                testImplementation testFixtures('org.t:fix:1')
                implementation project(':other')
                implementation files('libs/a.jar')
                implementation fileTree(dir: 'libs', include: ['*.jar'])
                implementation gradleApi()
                implementation('org.ex:excl:1') {
                    exclude group: 'bad.group', module: 'bad'
                    exclude group: 'whole.group'
                    transitive = false
                }
                implementation('org.rich:rich') { version { strictly '3.0' } }
                implementation 'org.strict:strict:2.0!!'
                if (true) {
                    implementation 'in.if:block:1'
                }
                constraints {
                    implementation 'org.c:constraint:9'
                }
                components { withModule('org.cm:x') { } }
                add('implementation', 'via.add:fn:1')
                compile 'old:compile:1'
                myCustomConfig 'custom:conf:1'
            }
            ''')
        model = self.parse(self.tmp / "build.gradle")
        self.assertEqual(deps_of(model), [
            "org.slf4j:slf4j-api:2.0.9",
            "org.junit.jupiter:junit-jupiter-api:5.10.0",
            "commons-io:commons-io:2.15.0",
            "org.apache:lib:1.0:linux@zip",
            "com.google.guava:guava:33.0.0-jre",
            "x:one:1", "x:two:2",
            "a:one:1", "a:two:2",
            "org.junit:junit-bom:5.10.0",
            "org.e:bom:1",
            "org.t:fix:1:test-fixtures",
            "org.ex:excl:1",
            "org.rich:rich:3.0",
            "org.strict:strict:2.0",
            "in.if:block:1",
            "org.c:constraint:9",
            "via.add:fn:1",
            "old:compile:1",
            "custom:conf:1",
        ])
        by = {d.artifact: d for d in model.dependencies}
        self.assertTrue(by["junit-bom"].platform)
        self.assertTrue(by["bom"].platform)
        self.assertEqual(by["excl"].excludes, frozenset({("bad.group", "bad"), ("whole.group", "*")}))
        self.assertFalse(by["excl"].transitive)
        self.assertEqual(by["junit-bom"].configuration, "testImplementation")
        self.assertEqual(by["conf"].configuration, "myCustomConfig")
        self.assertEqual(model.repositories[0].url, g2m.MAVEN_CENTRAL)
        self.assertEqual([p.id for p in model.plugins], ["java"])

    def test_buildscript_plugins_and_force(self):
        self.write("build.gradle", '''
            buildscript {
                ext.kotlin_version = '1.9.22'
                repositories { gradlePluginPortal() }
                dependencies { classpath "org.jetbrains.kotlin:kotlin-gradle-plugin:$kotlin_version" }
            }
            plugins {
                id 'org.springframework.boot' version '3.2.0' apply false
                id "com.github.ben-manes.versions" version "0.51.0"
            }
            configurations.all {
                resolutionStrategy { force 'com.forced:lib:1.2.3' }
            }
            dependencyManagement {
                imports { mavenBom "org.springframework.cloud:spring-cloud-dependencies:2023.0.0" }
            }
            ''')
        model = self.parse(self.tmp / "build.gradle")
        self.assertIn("org.jetbrains.kotlin:kotlin-gradle-plugin:1.9.22", deps_of(model))
        self.assertIn("com.forced:lib:1.2.3", deps_of(model))
        bom = [d for d in model.dependencies if d.artifact == "spring-cloud-dependencies"][0]
        self.assertTrue(bom.platform)
        self.assertEqual([(p.id, p.version) for p in model.plugins],
                         [("org.springframework.boot", "3.2.0"), ("com.github.ben-manes.versions", "0.51.0")])
        self.assertEqual(model.plugin_repositories[0].url, g2m.GRADLE_PLUGIN_PORTAL)

    def test_repositories(self):
        self.write("gradle.properties", "repoUser=bob\nrepoPass=secret\n")
        self.write("build.gradle", '''
            def nexus = 'https://nexus.example.com'
            repositories {
                mavenLocal()
                google()
                maven { url 'https://repo.spring.io/milestone' }
                maven {
                    url = "${nexus}/repository/public/"
                    credentials {
                        username = repoUser
                        password = project.findProperty('repoPass')
                    }
                }
                maven { url uri('local-repo') }
                maven {
                    name = 'withContent'
                    url "https://content.example.com/m2"; content { includeGroup "com.x" }
                }
                ivy { url 'https://ivy.example.com' }
            }
            publishing {
                repositories { maven { url 'https://publish.example.com/releases' } }
            }
            ''')
        model = self.parse(self.tmp / "build.gradle")
        urls = [r.url for r in model.repositories]
        self.assertEqual(urls, [
            g2m.GOOGLE, "https://repo.spring.io/milestone/", "https://nexus.example.com/repository/public/",
            (self.tmp / "local-repo").resolve().as_uri() + "/", "https://content.example.com/m2/"])
        self.assertTrue(model.uses_maven_local)
        nexus = model.repositories[2]
        self.assertEqual((nexus.username, nexus.password), ("bob", "secret"))
        self.assertEqual(model.repositories[4].name, "withContent")
        self.assertTrue(any("ivy" in w for w in model.warnings))

    def test_apply_from_and_subprojects(self):
        self.write("settings.gradle", "rootProject.name = 'x'\ninclude ':core', 'web'\ninclude 'deep:mod'\n")
        self.write("gradle/deps.gradle", "ext { versions = [lib: '1.0'] }\n")
        self.write("build.gradle", '''
            apply from: "$rootDir/gradle/deps.gradle"
            allprojects { repositories { mavenCentral() } }
            subprojects { dependencies { implementation "shared:lib:${versions.lib}" } }
            ''')
        self.write("core/build.gradle", "dependencies { implementation 'core:only:1' }")
        self.write("web/build.gradle", "dependencies { implementation \"web:dep:$versions.lib\" }")
        self.write("deep/mod/build.gradle", "dependencies { implementation 'deep:mod:2' }")
        model = self.parse(self.tmp)
        self.assertEqual(deps_of(model), ["shared:lib:1.0", "core:only:1", "web:dep:1.0", "deep:mod:2"])
        # only the subproject (but with root variables/repos)
        model = self.parse(self.tmp / "web" / "build.gradle")
        self.assertEqual(deps_of(model), ["shared:lib:1.0", "web:dep:1.0"])
        self.assertEqual(len(model.repositories), 1)
        # --no-subprojects
        model = self.parse(self.tmp, include_subprojects=False)
        self.assertEqual(deps_of(model), ["shared:lib:1.0"])

    def test_configuration_filter(self):
        self.write("build.gradle", '''
            dependencies {
                implementation 'a:main:1'
                testImplementation 'a:test:1'
                androidTestImplementation 'a:atest:1'
            }''')
        model = self.parse(self.tmp / "build.gradle", configuration_filter=lambda c: "test" not in c.lower())
        self.assertEqual(deps_of(model), ["a:main:1"])

    def test_unresolvable_notation_warns(self):
        self.write("build.gradle", "dependencies { implementation \"g:a:$missing\"\n implementation someVar }")
        model = self.parse(self.tmp / "build.gradle")
        self.assertEqual(model.dependencies, [])
        self.assertEqual(len(model.warnings), 2)


class KotlinBuildTest(TempDirTestCase):
    def test_kotlin_dsl(self):
        self.write("settings.gradle.kts", '''
            pluginManagement {
                repositories { gradlePluginPortal(); maven("https://plugins.example.com/m2") }
                plugins { id("com.example.managed") version "4.5" }
            }
            plugins { id("org.gradle.toolchains.foojay-resolver-convention") version "0.8.0" }
            dependencyResolutionManagement {
                repositories {
                    mavenCentral()
                    maven(url = "https://jitpack.io")
                    maven { url = uri("https://kts.example.com/repo") }
                    maven { setUrl("https://seturl.example.com") }
                }
            }
            rootProject.name = "k"
            include(
                ":app",
            )
            ''')
        self.write("gradle.properties", "okhttpVersion=4.12.0\n")
        self.write("app/build.gradle.kts", '''
            plugins {
                kotlin("jvm") version "2.0.21"
                id("com.example.managed")
                `java-library`
            }
            val okhttpVersion: String by project
            val retrofit = "2.11.0"
            dependencies {
                implementation(kotlin("stdlib"))
                implementation(kotlin("reflect", "1.9.0"))
                implementation("com.squareup.okhttp3:okhttp:$okhttpVersion")
                implementation("com.squareup.retrofit2:retrofit:${retrofit}") {
                    exclude(group = "com.squareup.okhttp3", module = "okhttp")
                    isTransitive = false
                }
                implementation(group = "org.named", name = "args", version = "1.0")
                implementation("org.pos", "args", "2.0")
                api(platform("org.bom:bom:1.0"))
                "kapt"("com.google.dagger:dagger-compiler:2.51")
                add("ksp", "io.ksp:proc:1.0")
                testImplementation("org.rich:dep") { version { strictly("3.3") } }
                implementation(variantOf(libsx.something) { classifier("x") })
            }
            ''')
        model = self.parse(self.tmp)
        self.assertEqual(deps_of(model), [
            "org.jetbrains.kotlin:kotlin-stdlib:2.0.21",
            "org.jetbrains.kotlin:kotlin-reflect:1.9.0",
            "com.squareup.okhttp3:okhttp:4.12.0",
            "com.squareup.retrofit2:retrofit:2.11.0",
            "org.named:args:1.0",
            "org.pos:args:2.0",
            "org.bom:bom:1.0",
            "com.google.dagger:dagger-compiler:2.51",
            "io.ksp:proc:1.0",
            "org.rich:dep:3.3",
        ])
        retrofit = [d for d in model.dependencies if d.artifact == "retrofit"][0]
        self.assertEqual(retrofit.excludes, frozenset({("com.squareup.okhttp3", "okhttp")}))
        self.assertFalse(retrofit.transitive)
        self.assertEqual({(p.id, p.version) for p in model.plugins}, {
            ("org.gradle.toolchains.foojay-resolver-convention", "0.8.0"),
            ("org.jetbrains.kotlin.jvm", "2.0.21"),
            ("com.example.managed", "4.5"),
        })
        self.assertEqual([r.url for r in model.repositories], [
            g2m.MAVEN_CENTRAL, "https://jitpack.io/", "https://kts.example.com/repo/", "https://seturl.example.com/"])
        self.assertEqual([r.url for r in model.plugin_repositories],
                         [g2m.GRADLE_PLUGIN_PORTAL, "https://plugins.example.com/m2/"])

    def test_version_catalog(self):
        self.write("settings.gradle.kts", '''
            rootProject.name = "c"
            dependencyResolutionManagement {
                versionCatalogs {
                    create("tools") { from(files("gradle/tools.toml")) }
                }
            }''')
        self.write("gradle/libs.versions.toml", '''
            [versions]
            kotlin = "2.0.0"
            [libraries]
            core-ktx = { module = "androidx.core:core-ktx", version = "1.13.1" }
            ok_http = { group = "com.squareup.okhttp3", name = "okhttp", version.ref = "kotlin" }
            bom = "org.bom:bom:7"
            from-bom = { module = "org.x:managed" }
            [bundles]
            net = ["ok_http", "core-ktx"]
            [plugins]
            kotlin-jvm = { id = "org.jetbrains.kotlin.jvm", version.ref = "kotlin" }
            shorthand = "com.example.short:1.0"
            ''')
        self.write("gradle/tools.toml", '[libraries]\nlint = "com.lint:lint:3"\n')
        self.write("build.gradle.kts", '''
            plugins {
                alias(libs.plugins.kotlin.jvm)
                alias(libs.plugins.shorthand) apply false
            }
            dependencies {
                implementation(libs.core.ktx)
                implementation(libs.bundles.net)
                implementation(platform(libs.bom))
                implementation(libs.from.bom)
                implementation(tools.lint)
                implementation("x:y:${libs.versions.kotlin.get()}")
                implementation(libs.does.not.exist)
            }''')
        model = self.parse(self.tmp)
        self.assertEqual(deps_of(model), [
            "androidx.core:core-ktx:1.13.1", "com.squareup.okhttp3:okhttp:2.0.0", "org.bom:bom:7",
            "org.x:managed", "com.lint:lint:3", "x:y:2.0.0"])
        self.assertTrue([d for d in model.dependencies if d.artifact == "bom"][0].platform)
        self.assertEqual({(p.id, p.version) for p in model.plugins},
                         {("org.jetbrains.kotlin.jvm", "2.0.0"), ("com.example.short", "1.0")})
        self.assertTrue(any("does.not.exist" in w for w in model.warnings))

    def test_kotlin_multiplatform_source_sets(self):
        self.write("build.gradle.kts", '''
            plugins { kotlin("multiplatform") version "2.0.0" }
            kotlin {
                jvm()
                sourceSets {
                    commonMain.dependencies {
                        implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.8.1")
                    }
                    val jvmTest by getting {
                        dependencies { implementation(kotlin("test-junit5")) }
                    }
                }
            }''')
        model = self.parse(self.tmp / "build.gradle.kts")
        self.assertEqual(deps_of(model), ["org.jetbrains.kotlinx:kotlinx-coroutines-core:1.8.1",
                                          "org.jetbrains.kotlin:kotlin-test-junit5:2.0.0"])

    def test_build_src_and_included_build(self):
        self.write("settings.gradle.kts", 'pluginManagement { includeBuild("build-logic") }\nrootProject.name = "r"')
        self.write("build.gradle.kts", 'plugins { id("my.convention") }')
        self.write("buildSrc/build.gradle.kts", '''
            plugins { `kotlin-dsl` }
            repositories { gradlePluginPortal() }
            dependencies { implementation("com.diffplug.spotless:spotless-plugin-gradle:6.25.0") }''')
        self.write("build-logic/settings.gradle.kts", 'rootProject.name = "build-logic"')
        self.write("build-logic/build.gradle.kts", '''
            plugins { `kotlin-dsl` }
            dependencies { implementation("org.logic:plugin:1.0") }''')
        self.write("build-logic/src/main/kotlin/my.convention.gradle.kts", '''
            plugins { java }
            dependencies { "implementation"("org.conv:dep:2.0") }''')
        model = self.parse(self.tmp)
        self.assertEqual(sorted(deps_of(model)), ["com.diffplug.spotless:spotless-plugin-gradle:6.25.0",
                                                  "org.conv:dep:2.0", "org.logic:plugin:1.0"])
        self.assertIn(g2m.GRADLE_PLUGIN_PORTAL, [r.url for r in model.repositories])
        # disabled
        model = self.parse(self.tmp, include_build_logic=False)
        self.assertEqual(deps_of(model), [])

    def test_finalize_model(self):
        self.write("build.gradle.kts", '''
            plugins {
                kotlin("jvm") version "2.0.21"
                id("org.springframework.boot") version "3.3.4"
                id("io.spring.dependency-management") version "1.1.6"
            }
            dependencies {
                implementation("org.springframework.boot:spring-boot-starter")
                implementation("a:b:1.0")
                testImplementation("a:b")
            }''')
        model = self.parse(self.tmp / "build.gradle.kts")
        g2m.finalize_model(model, add_plugin_portal=True)
        notations = deps_of(model)
        for expected in (
                "org.jetbrains.kotlin.jvm:org.jetbrains.kotlin.jvm.gradle.plugin:2.0.21",
                "org.springframework.boot:org.springframework.boot.gradle.plugin:3.3.4",
                "org.springframework.boot:spring-boot-dependencies:3.3.4",
                "org.jetbrains.kotlin:kotlin-stdlib:2.0.21",
                "org.jetbrains.kotlin:kotlin-compiler-embeddable:2.0.21"):
            self.assertIn(expected, notations)
        self.assertEqual(notations.count("a:b:1.0"), 2)  # version-less declaration filled from the other one
        self.assertEqual(model.plugin_repositories[0].url, g2m.GRADLE_PLUGIN_PORTAL)
        model = self.parse(self.tmp / "build.gradle.kts")
        g2m.finalize_model(model, add_plugin_portal=False, implicit=False)
        self.assertNotIn("org.jetbrains.kotlin:kotlin-stdlib:2.0.21", deps_of(model))
        self.assertEqual(model.plugin_repositories, [])

    def test_errors(self):
        with self.assertRaises(g2m.Gradle2MavenError):
            self.parse(self.tmp / "missing.gradle")
        with self.assertRaises(g2m.Gradle2MavenError):
            self.parse(self.tmp)  # empty directory


if __name__ == "__main__":
    unittest.main()
