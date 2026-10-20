plugins {
    kotlin("jvm")
    application
}
val commonsVersion: String by project
dependencies {
    implementation(project(":lib"))
    implementation("org.apache.commons:commons-lang3:$commonsVersion")
    testImplementation(kotlin("test"))
}
application {
    mainClass.set("app.MainKt")
}
tasks.test {
    useJUnitPlatform()
}
