# Compose Example

This example demonstrates using the Experimently Android SDK with Jetpack Compose.

## Setup

**Not yet published.** `com.getexperimently:experimently-android` is not on Maven Central, so
build the SDK from source: include `sdk/android/sdk` of this repository in your Gradle build (see
[`sdk/android/README.md`](../../README.md) for the `settings.gradle.kts` lines) and add the
dependency to your app module's `build.gradle.kts`:

```kotlin
implementation(project(":sdk"))
```

## Usage

```kotlin
val client = ExperimentationClient(
    SdkConfig(baseUrl = "http://10.0.2.2:8000", apiKey = "your-api-key")   // sent as X-API-Key
)

// In a Composable:
var flagEnabled by remember { mutableStateOf(false) }
var variant by remember { mutableStateOf("") }

LaunchedEffect(Unit) {
    val user = User(id = "user-123")
    flagEnabled = client.evaluateFlag("new-feature", user).enabled        // server decides
    variant = client.getAssignment("checkout-flow", user).variantName     // sticky server-side
}
```

## Running the Example

1. Start the backend server from the repo root: `ENVIRONMENT=development uvicorn backend.app.main:app --port 8000`
   and create an API key (for example with `python backend/scripts/seed_sdk_contract.py`, which
   writes one to `tests/sdk-contract/live/.api_key`).
2. Create an ACTIVE feature flag with the key `new-dashboard` and an ACTIVE experiment with the key
   `checkout-flow`. The seed script above creates neither: its flag is `sdk_contract_flag` and its
   experiment `sdk_contract_ab`.
3. This directory holds `ExampleActivity.kt` and no Android Studio project: add the file to an app
   module of your own that depends on the SDK (Setup above), and paste the key into it.
4. Run on an emulator (use `http://10.0.2.2:8000` to reach localhost).
5. The app evaluates the `new-dashboard` flag and assigns `user-123` to `checkout-flow` on
   launch; the button tracks a `purchase_clicked` event attributed to that experiment.
