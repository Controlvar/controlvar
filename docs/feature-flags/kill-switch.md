# Turning a Flag Off

Turning a flag off is the kill switch: the switch on the flag's page, `"is_active": false` (or
`POST …/deactivate`), or a safety rollback to 0%. The server's answer changes at once: from the
next evaluation it answers `enabled: false` with `reason: "inactive"`, and it keeps no cache of
flags. Each client, though, keeps serving an answer it already has until that answer expires. The
same goes for any change to a flag: its rollout percentage, its rules, a rollback to a lower
percentage.

## How long each client takes

With the defaults, every client below serves the change within 5 minutes, the next time your code
asks it for the flag, except the edge SDK: 1 minute, or about 2 with a KV store, plus the store's
own propagation time (an instance can copy a KV entry up to 60 seconds old into its memory for
another 60).

| SDK | Cache lifetime (default) | Clear it from your code |
|---|---|---|
| JavaScript | `cacheTtlMs` (300000 ms) | `clearCache()` |
| React | `config.cacheTtlMs` (300000 ms) | `client.clearCache()`, with `client` from `useExperimentation()` |
| React Native | `cacheTtlMs` (300000 ms) | `clearCache()`, and `clearStorage()` for the copy in AsyncStorage |
| Python | `cache_ttl_seconds` (300) | `clear_cache()` |
| Go | `WithCacheTTL` (5m); 0 or less never expires | none: create a new client |
| Java | `cacheTtlMs` (300000); Spring `experimentation.cache-ttl-seconds` (300) | `clearCache()`, `invalidateCache(userId, key)` |
| .NET | `CacheTtlSeconds` (300) | `ClearCache()` |
| Ruby | `cache_ttl` (300) | `clear_cache` |
| PHP | `cacheTtl` (300), and never past the PHP process | `clearCache()` |
| Elixir | `:cache_ttl` (300) | `clear_cache(client)` |
| iOS | `cacheTTL` (300 seconds) | `clearCache()` |
| Android | `cacheTtlMs` (300000) | `clearCache()`, `invalidateCache(userId, key)` |
| Flutter | `cacheTtl` (5 minutes) | `clearCache()` |
| Edge | `cacheTtlMs` (60000 ms); with a KV store, `kvTtlSeconds` on Cloudflare (`cacheTtlMs` in seconds, at least 60) or `kvTtlMs` on Deno (`cacheTtlMs`) | `clearCache()` (this instance's memory only) |
| OpenFeature provider (JavaScript, Python) | the provider's `cacheTtlMs` (300000 ms) / `cache_ttl` (300), passed to the SDK | `provider.client.clearCache()` / `provider.client.clear_cache()` |

**A React or React Native component already on screen does not ask again.** It keeps the answer it
rendered until it mounts again (a screen opened anew, a reloaded page or a restarted app), so a
screen a navigator keeps mounted does not ask again until then; the SDKs neither poll nor stream.
Nothing reaches a screen that is already open until it mounts the component again.

## Local evaluation (JavaScript and Python, beta)

The SDK refreshes the ruleset every 30 seconds, give or take 10% (`refreshIntervalMs` /
`refresh_interval_seconds`, at least 5 seconds), so a flag it answers locally serves the change
within 33 seconds. A flag it sends to the server (see
[what the server still evaluates](../sdk/local-evaluation.md#what-is-evaluated-locally)) follows
the cache lifetime above.

## While a client cannot reach the API

| Client | What it serves |
|---|---|
| React Native, iOS | The last answer it stored for that user and flag, with no time limit, also after the app restarts (`offlineFallback` / `enableOfflineFallback`, on by default). A flag turned off while a device is offline stays on there until the device reaches the API once. Turn the fallback off if serving off is safer than serving a stale on. |
| Android, Flutter | The same, from the offline store: in memory by default, so until the app's process ends; across restarts with a persistent store (a `SharedPreferences` adapter on Android, `SharedPreferencesOfflineStore` on Flutter). |
| Every other SDK | The cached answer until its lifetime ends; then the call fails, and the SDK answers as for any failure (see its page). |
| Local evaluation | The last ruleset it loaded, with no time limit unless you set `maxStaleMs` / `max_stale_seconds`. A failed refresh is tried again later each time, up to 5 minutes apart, so once the API answers again a change can take up to 5 minutes more. A 401, 403 or 404 drops the ruleset at once: every flag then goes to the server, and the ruleset is asked for again every 10 minutes. |

## Making it faster

- A shorter cache lifetime. Each user and flag then asks again once per lifetime, and each SDK
  path, such as one flag's evaluate route, takes 6,000 requests a minute per IP address by default
  (`SDK_RATE_LIMIT_PER_MINUTE`).
- Clearing the cache from your code when you know a flag changed.
- A shorter `refreshIntervalMs` in local evaluation.

## Safety rollbacks

A safety rollback changes the flag on the server like any other change, and reaches clients on the
same timetable. The safety monitor looks for a breach every 5 minutes by default
(`SAFETY_CHECK_INTERVAL_MINUTES`), so from a breach to every client serving the rolled-back answer
can take that long plus the client's time above.
