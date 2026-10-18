plugins {
    id("com.android.library")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.controlvar.android"
    compileSdk = 34
    defaultConfig {
        minSdk = 21
        targetSdk = 34
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.7.3")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.6.3")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.junit.jupiter:junit-jupiter:5.10.5")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.7.3")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
    // The SDK uses the platform's org.json; android.jar only ships stubs for local
    // unit tests, so the real implementation is needed on the test classpath.
    testImplementation("org.json:json:20240303")
    testImplementation("io.mockk:mockk:1.13.17")
    testImplementation("org.assertj:assertj-core:3.24.2")
}

tasks.withType<Test> {
    useJUnitPlatform()
}
