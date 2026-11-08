# Send exposures to your analytics tool

Controlvar records an exposure each time it enrols a user in an experiment. The JavaScript,
Python, Go, React, React Native, Java and .NET SDKs can also call a function of yours at that
moment, the exposure hook, so you can send the exposure to Segment, Google Analytics 4, Amplitude,
Mixpanel, RudderStack or your own warehouse.

## Which SDKs have it

| SDK | Set it with | Details |
|---|---|---|
| JavaScript | `onExposure` in `new ExperimentationClient({ ... })` | [Exposure hook](javascript.md#exposure-hook) |
| Python | `on_exposure=` in `ExperimentationClient(...)` | [Exposure hook](python.md#exposure-hook) |
| Go | `exp.WithOnExposure(fn)` in `exp.New(...)` | [Exposure hook](go.md#exposure-hook) |
| React | `onExposure` in the `config` of `ExperimentationProvider`, and of `ServerClient` | [Exposure hook](react.md#exposure-hook) |
| React Native | `onExposure` in `new ExperimentationClient({ ... })` | [Exposure hook](react-native.md#exposure-hook) |
| Java | `exposureListener(...)` on `SdkConfig.builder(...)`, or an `ExposureListener` bean with the Spring Boot starter | [Exposure hook](java.md#exposure-hook) |
| .NET | `OnExposure` on `SdkConfig` | [Exposure hook](dotnet.md#exposure-hook) |

Each SDK's page describes its hook in full, and says whether that SDK is published yet.

## When it is called

The server records one exposure for each assignment request it answers with `assigned: true`. The
SDK calls the hook at most once for each such answer, when it stores the answer for a user and
experiment that had no unexpired cached answer in that client. Within one cache lifetime (5
minutes by default), one client therefore calls it once per user and experiment, however often the
answer is asked for.

It is not called for:

- a cached answer;
- the second of two calls for the same new user and experiment made at the same time;
- a call that fails;
- an answer with `assigned: false`: a user in the global holdout, in another experiment of the same
  mutual exclusion group or outside the experiment's targeting, or an experiment that is not
  running, for which the server records no exposure;
- a feature flag.

React's server render makes no request and calls nothing; hydration in the browser then makes the
one request and the one call. React's `ServerClient.bootstrap()` reads and fills no cache, so a
`ServerClient` shared across requests calls its own hook on every `bootstrap()` call, once for each
experiment the server enrols the user in ([What the bootstrap records](react.md#what-the-bootstrap-records)).

It is called again, and the server records another exposure, whenever the SDK asks the server
again: after the cache lifetime, after `clearCache()` (Java's `invalidateCache` too; Go has
neither), after the answer is evicted from a full cache, and in every new client, such as each page
load in a browser or each process or worker on a server; except that React Native, with its
default `offlineFallback`, keeps the answer in AsyncStorage for the cache lifetime, so on one
device neither `clearCache()` nor a new client calls it again until that stored answer expires,
unless you set `offlineFallback: false` or call `clearStorage()`.

So your tool can receive several exposures for one user and experiment: count users, not events.
It never receives an exposure Controlvar did not record. It can receive fewer: two calls for the
same new user and experiment at the same time in Python, Go, Java or .NET each record one and call
the hook once, and a request that times out after the server recorded it calls nothing.

It is called when your code asks for the assignment, whether or not you then show the variant, so
ask for it where the variant is shown.

## Keep it quick

It runs inside the call that asked for the assignment, after the answer is cached and before the
call returns; in JavaScript, React and React Native, before that call's Promise resolves. Whatever
your tool, a call that sends before it returns adds that request to the assignment, once per user
and experiment per cache lifetime; put the exposure on your own queue, as the Python, Go, Java and
.NET pages show.

If it throws (in Go, panics), the answer is unchanged, in every SDK.

## Examples

Each example is a JavaScript function for the browser. It receives the `Exposure` of the
JavaScript, React and React Native SDKs: `experimentKey`, `userId`, `variantId` (`null` when the
server sends none) and `variantName`.

Pass it as `onExposure` when you create the client: in the options of `ExperimentationClient` in
the JavaScript and React Native SDKs, or in the `config` of React's `ExperimentationProvider`. Each
example assumes the tool's library is already loaded and set up with your account, as the tool's
own documentation describes.

The event and property names are examples: use the ones your team reports on. If your tool
identifies people, identify each one with the user id you give Controlvar, so you can join the
exposures to their other events.

### Segment

```js
function onExposure(exposure) {
  analytics.track('Experiment Viewed', {
    experiment_key: exposure.experimentKey,
    variant_name: exposure.variantName,
    variant_id: exposure.variantId,
  });
}
```

### Google Analytics 4

```js
function onExposure(exposure) {
  gtag('event', 'experiment_exposure', {
    experiment_key: exposure.experimentKey,
    variant_name: exposure.variantName,
  });
}
```

### Amplitude

```js
function onExposure(exposure) {
  amplitude.track('Experiment Viewed', {
    experiment_key: exposure.experimentKey,
    variant_name: exposure.variantName,
    variant_id: exposure.variantId,
  });
}
```

### Mixpanel

```js
function onExposure(exposure) {
  mixpanel.track('Experiment Viewed', {
    experiment_key: exposure.experimentKey,
    variant_name: exposure.variantName,
    variant_id: exposure.variantId,
  });
}
```

### RudderStack

```js
function onExposure(exposure) {
  rudderanalytics.track('Experiment Viewed', {
    experiment_key: exposure.experimentKey,
    variant_name: exposure.variantName,
    variant_id: exposure.variantId,
  });
}
```

## When nothing arrives

1. Is the experiment running? A draft, paused, completed or archived experiment enrols nobody.
2. Is the user enrolled? A user in the global holdout, in another experiment of the same mutual
   exclusion group, or outside the experiment's targeting is not.
3. Has this client already reported it? A cached answer, for 5 minutes by default, is not reported
   again. In React Native, with the default `offlineFallback`, neither `clearCache()` nor a new
   client reports it again until the answer stored in AsyncStorage expires.
4. Is your function failing? In React, Go, Java and .NET nothing reports it: catch and log inside
   it. In JavaScript and React Native, look at `onError`, with the operation `'exposure'`; in
   Python, at `on_error` and the `controlvar.client` logger.
5. In a test, does your faked `/assign` answer include `"assigned": true`? Without it, the hook is
   not called.
6. Is your SDK new enough? Use the JavaScript or Python SDK 0.3.0 or later, or the Go SDK v0.1.0 or
   later; for React, React Native, Java and .NET, see that SDK's page.
