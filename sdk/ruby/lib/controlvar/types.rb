module Controlvar
  # Result of a server-side feature flag evaluation
  # (GET /api/v1/feature-flags/evaluate/{key}?user_id=...).
  #
  #   key     - String  : the flag key you asked for
  #   enabled - Boolean : the server's decision for this user
  #   config  - Object  : nil today: the evaluate route returns config: null for every flag
  FlagEvaluation = Struct.new(:key, :enabled, :config, keyword_init: true) do
    # @return [Boolean] true only when the server reported the flag as enabled
    def enabled?
      enabled == true
    end
  end

  # Result of a sticky experiment assignment (POST /api/v1/tracking/assign).
  #
  #   experiment_key - String       : the experiment key you asked for
  #   variant_id     - String, nil  : UUID of the assigned variant
  #   variant_name   - String       : assigned variant name (e.g. "control", "treatment")
  #   is_control     - Boolean      : true for the control variant
  #   configuration  - Hash, nil    : the variant's configuration JSON
  Assignment = Struct.new(:experiment_key, :variant_id, :variant_name, :is_control, :configuration,
                          keyword_init: true) do
    # @return [Boolean]
    def control?
      is_control == true
    end
  end

  # Aggregated result of Client#track_batch (POST /api/v1/tracking/batch).
  #
  #   success_count - Integer     : events the server accepted
  #   failure_count - Integer     : events rejected by the server or never delivered
  #   errors        - Array, nil  : error details (server-provided or local), nil when none
  BatchResult = Struct.new(:success_count, :failure_count, :errors, keyword_init: true) do
    # @return [Boolean] true when no event failed
    def ok?
      failure_count.to_i.zero?
    end
  end
end
