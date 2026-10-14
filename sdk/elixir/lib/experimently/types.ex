defmodule Experimently.FlagEvaluation do
  @moduledoc """
  Result of a server-side feature flag evaluation
  (`GET /api/v1/feature-flags/evaluate/{key}?user_id=...`).

  - `key` — the flag key you asked for
  - `enabled` — the server's decision for this user
  - `config` — `nil` today: the evaluate route returns `config: null` for every flag
  """

  @derive Jason.Encoder
  @enforce_keys [:key, :enabled]
  defstruct [:key, :enabled, :config]

  @type t :: %__MODULE__{
          key: String.t(),
          enabled: boolean(),
          config: term() | nil
        }
end

defmodule Experimently.Assignment do
  @moduledoc """
  Result of a sticky experiment assignment (`POST /api/v1/tracking/assign`).

  - `experiment_key` — the experiment key you asked for
  - `variant_id` — UUID of the assigned variant (`nil` when the server did not send one)
  - `variant_name` — assigned variant name (e.g. `"control"`, `"treatment"`)
  - `is_control` — `true` for the control variant
  - `configuration` — the variant's configuration map (`nil` when none)
  """

  @derive Jason.Encoder
  @enforce_keys [:experiment_key, :variant_name, :is_control]
  defstruct [:experiment_key, :variant_id, :variant_name, :is_control, :configuration]

  @type t :: %__MODULE__{
          experiment_key: String.t(),
          variant_id: String.t() | nil,
          variant_name: String.t(),
          is_control: boolean(),
          configuration: map() | nil
        }
end

defmodule Experimently.BatchResult do
  @moduledoc """
  Aggregated result of `Experimently.track_batch/2`
  (`POST /api/v1/tracking/batch`).

  - `success_count` — events the server accepted
  - `failure_count` — events rejected by the server or malformed locally
  - `errors` — error details (server-provided or local), `nil` when none
  """

  @derive Jason.Encoder
  defstruct success_count: 0, failure_count: 0, errors: nil

  @type t :: %__MODULE__{
          success_count: non_neg_integer(),
          failure_count: non_neg_integer(),
          errors: [term()] | nil
        }

  @doc "`true` when no event failed."
  @spec ok?(t()) :: boolean()
  def ok?(%__MODULE__{failure_count: failure_count}), do: failure_count == 0
end
