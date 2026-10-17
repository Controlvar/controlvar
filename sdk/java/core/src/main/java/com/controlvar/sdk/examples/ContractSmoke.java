package com.controlvar.sdk.examples;

import com.controlvar.sdk.ExperimentationClient;
import com.controlvar.sdk.config.SdkConfig;
import com.controlvar.sdk.model.ExperimentAssignment;
import com.controlvar.sdk.model.FlagEvaluation;
import com.controlvar.sdk.model.TrackEvent;
import com.controlvar.sdk.model.User;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

/**
 * Contract smoke: exercises the Java SDK against a live backend using only its public
 * API and prints exactly one JSON line on success (exit 0); on failure prints one line
 * to stderr and exits 1.
 *
 * <p>Environment: {@code CONTROLVAR_API_URL} (default {@code http://localhost:8000}),
 * {@code CONTROLVAR_API_KEY} (required), {@code CONTRACT_EXPERIMENT_KEY} (default
 * {@code sdk_contract_ab}), {@code CONTRACT_FLAG_KEY} (default {@code sdk_contract_flag}),
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
