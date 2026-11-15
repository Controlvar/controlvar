package com.controlvar.sdk.examples;

import com.controlvar.sdk.ExperimentationClient;
import com.controlvar.sdk.config.SdkConfig;
import com.controlvar.sdk.model.ExperimentAssignment;
import com.controlvar.sdk.model.Exposure;
import com.controlvar.sdk.model.FlagEvaluation;
import com.controlvar.sdk.model.TrackEvent;
import com.controlvar.sdk.model.User;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import okhttp3.OkHttpClient;
import okhttp3.Response;

import java.lang.reflect.Constructor;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.TimeUnit;

/**
 * Contract smoke: exercises the Java SDK against a live backend using its public API
 * (step 4c also builds one client through the package-private constructor the SDK's own
 * tests use; see there) and prints exactly one JSON line on success (exit 0); on failure
 * prints one line to stderr and exits 1.
 *
 * <p>Environment: {@code CONTROLVAR_API_URL} (default {@code http://localhost:8000}),
 * {@code CONTROLVAR_API_KEY} (required), {@code CONTRACT_EXPERIMENT_KEY} (default
 * {@code sdk_contract_ab}), {@code CONTRACT_FLAG_KEY} (default {@code sdk_contract_flag}),
 * {@code CONTRACT_TARGETED_FLAG_KEY} (default {@code sdk_contract_targeted}),
 * {@code CONTRACT_USER_ID} (default {@code smoke-<uuid>}).
 *
 * <p>Run from the repo root with {@code bash sdk/java/examples/contract_smoke.sh}.
 */
public final class ContractSmoke {

    private ContractSmoke() {}

    private static String env(String key, String fallback) {
        String value = System.getenv(key);
        return (value == null || value.isEmpty()) ? fallback : value;
    }

    private static void fail(String message) {
        System.err.println("contract_smoke: " + message);
        System.exit(1);
    }

    public static void main(String[] args) {
        String apiKey = env("CONTROLVAR_API_KEY", env("EXPERIMENTLY_API_KEY", ""));
        if (apiKey.isEmpty()) {
            fail("CONTROLVAR_API_KEY is required");
        }
        String baseUrl = env("CONTROLVAR_API_URL", env("EXPERIMENTLY_API_URL", "http://localhost:8000"));
        String experimentKey = env("CONTRACT_EXPERIMENT_KEY", "sdk_contract_ab");
        String flagKey = env("CONTRACT_FLAG_KEY", "sdk_contract_flag");
        String targetedKey = env("CONTRACT_TARGETED_FLAG_KEY", "sdk_contract_targeted");
        String userId = env("CONTRACT_USER_ID", "smoke-" + UUID.randomUUID());

        SdkConfig config = SdkConfig.builder(apiKey, baseUrl).timeoutMs(10_000).build();
        User user = User.builder(userId).attribute("source", "contract_smoke").build();

        try (ExperimentationClient client = new ExperimentationClient(config)) {
            // 1. Assign twice (second call is a cache hit); a fresh client verifies the
            //    server itself is sticky.
            ExperimentAssignment first = client.getExperimentAssignment(user, experimentKey);
            ExperimentAssignment second = client.getExperimentAssignment(user, experimentKey);
            ExperimentAssignment third;
            try (ExperimentationClient fresh = new ExperimentationClient(config)) {
                third = fresh.getExperimentAssignment(user, experimentKey);
            }
            String variant = first.getVariantName();
            if (!"control".equals(variant) && !"treatment".equals(variant)) {
                fail("unexpected variant_name '" + variant + "'");
            }
            boolean sticky = sameVariant(first, second) && sameVariant(first, third);
            if (!sticky) {
                fail("assignment not sticky: " + first.getVariantName() + " / "
                        + second.getVariantName() + " / " + third.getVariantName());
            }

            // 2. Evaluate the flag.
            FlagEvaluation flag = client.evaluateFeatureFlag(user, flagKey);

            // 3. Track a conversion attributed to the experiment.
            client.trackEventSync(TrackEvent.builder(userId, "purchase")
                    .experimentKey(experimentKey)
                    .value(12.5)
                    .property("sdk", "java")
                    .build());

            // 4. Track without a key (fans out to the cached assignment + flag), then a
            //    2-event batch.
            client.trackEventSync(TrackEvent.builder(userId, "page_view").build());
            client.trackBatchSync(Arrays.asList(
                    TrackEvent.builder(userId, "page_view").experimentKey(experimentKey).build(),
                    TrackEvent.builder(userId, "page_view").featureFlagKey(flagKey).build()));

            // 4b. One keyed event sent twice with one fixed event_id (#1543, C4). The server is to
            //     store it once: the runner counts the rows for this user and event name.
            String dedupName = "contract_dedup_" + UUID.randomUUID();
            String dedupId = "contract-" + UUID.randomUUID();
            for (int i = 0; i < 2; i++) {
                client.trackEventSync(TrackEvent.builder(userId, dedupName)
                        .experimentKey(experimentKey)
                        .eventId(dedupId)
                        .build());
            }

            // 4c. The attributes reach flag evaluation (#1520, #1718). The seeded targeted flag is
            //     at 0% with one rule, plan equals pro, at 100%. A NEW client (so the fan-out above
            //     is untouched) asks for it with plan=pro, then, after clearCache() (the cache key
            //     is user + flag), with plan=free. FlagEvaluation carries no reason, so the
            //     server's reason is read from the answer to the client's own request: the client
            //     is built through the package-private constructor the SDK's own tests use, with
            //     the public constructor's three timeouts and an interceptor that only reads the
            //     targeted flag's answers. The runner requires on with targeting_rule, then off.
            List<String> reasons = Collections.synchronizedList(new ArrayList<>());
            ObjectMapper answers = new ObjectMapper();
            OkHttpClient recording = new OkHttpClient.Builder()
                    .connectTimeout(config.getTimeoutMs(), TimeUnit.MILLISECONDS)
                    .readTimeout(config.getTimeoutMs(), TimeUnit.MILLISECONDS)
                    .writeTimeout(config.getTimeoutMs(), TimeUnit.MILLISECONDS)
                    .addInterceptor(chain -> {
                        Response response = chain.proceed(chain.request());
                        List<String> path = chain.request().url().pathSegments();
                        int last = path.size() - 1;
                        if (last >= 1 && "evaluate".equals(path.get(last - 1)) && targetedKey.equals(path.get(last))) {
                            String reason = null;
                            try {
                                JsonNode answer = answers.readTree(response.peekBody(1 << 20).string());
                                JsonNode value = answer == null ? null : answer.get("reason");
                                reason = (value != null && value.isTextual()) ? value.asText() : null;
                            } catch (JsonProcessingException e) {
                                // Not JSON: no reason.
                            }
                            reasons.add(reason);
                        }
                        return response;
                    })
                    .build();
            Constructor<ExperimentationClient> withHttpClient =
                    ExperimentationClient.class.getDeclaredConstructor(SdkConfig.class, OkHttpClient.class);
            withHttpClient.setAccessible(true);
            FlagEvaluation pro;
            FlagEvaluation free;
            try (ExperimentationClient targeting = withHttpClient.newInstance(config, recording)) {
                pro = targeting.evaluateFeatureFlag(User.builder(userId).attribute("plan", "pro").build(), targetedKey);
                targeting.clearCache();
                free = targeting.evaluateFeatureFlag(User.builder(userId).attribute("plan", "free").build(), targetedKey);
            }
            if (reasons.size() != 2) {
                fail("the targeted flag was asked " + reasons.size() + " time(s), expected 2");
            }

            // 5. The exposure listener (#1499): a NEW client with a listener assigns the same
            //    user twice, with no clearCache() in between. The server records an exposure for
            //    the first request (assigned: true) and the second answer comes from the cache,
            //    so the listener is called once. The runner checks the count, the experiment key
            //    and the variant; the user id is checked here, the one place that knows it.
            List<Exposure> exposures = Collections.synchronizedList(new ArrayList<>());
            SdkConfig listened = SdkConfig.builder(apiKey, baseUrl).timeoutMs(10_000)
                    .exposureListener(exposures::add).build();
            try (ExperimentationClient hooked = new ExperimentationClient(listened)) {
                hooked.getExperimentAssignment(user, experimentKey);
                hooked.getExperimentAssignment(user, experimentKey);
            }
            for (Exposure e : exposures) {
                if (!userId.equals(e.getUserId())) {
                    fail("the exposure listener received user \"" + e.getUserId() + "\", expected \"" + userId + "\"");
                }
            }

            // 5b. A listener that throws an Error, as a missing analytics library does: the
            //     client must catch it (an Error escapes this smoke's catch and exits 1) and
            //     answer the same variant.
            SdkConfig throwing = SdkConfig.builder(apiKey, baseUrl).timeoutMs(10_000)
                    .exposureListener(e -> {
                        throw new NoClassDefFoundError("com/example/analytics/Analytics");
                    }).build();
            ExperimentAssignment fromThrowing;
            try (ExperimentationClient failing = new ExperimentationClient(throwing)) {
                fromThrowing = failing.getExperimentAssignment(user, experimentKey);
            }
            if (!sameVariant(first, fromThrowing)) {
                fail("the client whose exposure listener throws answered " + fromThrowing.getVariantName()
                        + ", not " + first.getVariantName());
            }

            // 6. Flag values (#1810): a NEW client (so the fan-out above is untouched) reads the
            //    seed's three value flags, each on at 100% while the SDK rows run, with defaults
            //    that are no flag value, so a getter that falls back shows. The report is what
            //    they returned; the runner requires the seed's on values.
            Map<String, Object> values = new LinkedHashMap<>();
            try (ExperimentationClient valued = new ExperimentationClient(config)) {
                values.put("string", valued.getStringValue(user, "sdk_contract_value_string", "smoke-default"));
                values.put("number", valued.getNumberValue(user, "sdk_contract_value_number", -1));
                values.put("json", valued.getJsonValue(user, "sdk_contract_value_json",
                        Collections.singletonList("smoke-default")));
            }

            Map<String, Object> assign = new LinkedHashMap<>();
            assign.put("variant_name", first.getVariantName());
            assign.put("is_control", first.isControl());
            assign.put("sticky", sticky);

            Map<String, Object> report = new LinkedHashMap<>();
            report.put("sdk", "java");
            report.put("assign", assign);
            report.put("flag", Map.of("enabled", flag.isEnabled()));
            report.put("track", Map.of("ok", true));
            report.put("fanout", Map.of("ok", true));
            Map<String, Object> targeted = new LinkedHashMap<>(); // LinkedHashMap: reason may be null
            targeted.put("pro", pro.isEnabled());
            targeted.put("free", free.isEnabled());
            targeted.put("reason", reasons.get(0));
            report.put("targeted", targeted);

            Map<String, Object> payload = null;
            if (!exposures.isEmpty()) {
                Exposure e = exposures.get(0);
                payload = new LinkedHashMap<>(); // LinkedHashMap: variant_id may be null
                payload.put("experiment_key", e.getExperimentKey());
                payload.put("variant_name", e.getVariantName());
                payload.put("variant_id", e.getVariantId());
                payload.put("user_id", e.getUserId());
            }
            Map<String, Object> exposure = new LinkedHashMap<>();
            exposure.put("calls", exposures.size());
            exposure.put("payload", payload);
            report.put("exposure", exposure);

            Map<String, Object> eventId = new LinkedHashMap<>();
            eventId.put("user_id", userId);
            eventId.put("event_name", dedupName);
            eventId.put("id", dedupId);
            eventId.put("sends", 2);
            report.put("event_id", eventId);
            report.put("values", values);

            System.out.println(new ObjectMapper().writeValueAsString(report));
        } catch (Exception e) {
            fail(e.getClass().getSimpleName() + ": " + e.getMessage());
        }
        System.exit(0);
    }

    private static boolean sameVariant(ExperimentAssignment a, ExperimentAssignment b) {
        return a.getVariantName().equals(b.getVariantName())
                && String.valueOf(a.getVariantId()).equals(String.valueOf(b.getVariantId()));
    }
}
