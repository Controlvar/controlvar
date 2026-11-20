/**
 * The default A/B create request of `/experiments/new`, byte for byte (#1956).
 *
 * The name below typed, every other answer left at its default, and Create
 * pressed. AB_LITERAL is the `JSON.stringify` of the body main's form sent
 * before the Bandit type existed, captured by running the steps of
 * experiment-new-ab-body.test.tsx against that form in both views (the two
 * strings were identical) and copied here, not typed by hand.
 *
 * experiment-new-ab-body.test.tsx holds both views to it, and the bandit tests
 * hold a form that chose Bandit and came back to A/B to it: an added key (an
 * `optimization_type` of "fixed", say), a changed value or a reordered key all
 * fail.
 */
export const AB_NAME = 'Default A/B body';

export const AB_LITERAL =
  '{"name":"Default A/B body","key":"default_a_b_body","experiment_type":"a_b","targeting_rules":null,"variants":[{"name":"Control","is_control":true,"traffic_allocation":50},{"name":"Treatment","is_control":false,"traffic_allocation":50}],"metrics":[{"name":"Conversion","event_name":"conversion","metric_type":"conversion","is_primary":true}],"confidence_level":0.95,"correction_method":"benjamini_hochberg"}';
