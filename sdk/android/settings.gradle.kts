// Where Gradle resolves plugins and dependencies from. Without this the
// module build fails with "Cannot resolve external dependency
// org.jetbrains.kotlin:kotlin-stdlib ... because no repositories are defined":
// the `buildscript` block in build.gradle.kts only covers the build's own
// classpath, not the projects'.
pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.PREFER_SETTINGS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "controlvar-android"
include(":sdk")
