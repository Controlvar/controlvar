defmodule Controlvar.Error do
  @moduledoc """
  Base error struct for the Controlvar SDK.
  """

  defexception [:message, :reason, :details]

  @type t :: %__MODULE__{
          message: String.t(),
          reason: atom(),
          details: any()
        }

  @impl true
  def message(%__MODULE__{message: msg}), do: msg
end

defmodule Controlvar.AuthError do
  @moduledoc """
  Raised when authentication fails (401 Unauthorized).
  """

  defexception [:message, :status_code]

  @type t :: %__MODULE__{
          message: String.t(),
          status_code: non_neg_integer()
        }

  @impl true
  def message(%__MODULE__{message: msg}), do: msg
end

defmodule Controlvar.ApiError do
  @moduledoc """
  Raised when the API returns a non-success status code.
  """

  defexception [:message, :status_code, :body]

  @type t :: %__MODULE__{
          message: String.t(),
          status_code: non_neg_integer(),
          body: String.t() | nil
        }

  @impl true
  def message(%__MODULE__{message: msg}), do: msg
end

defmodule Controlvar.NetworkError do
  @moduledoc """
  Raised when a network-level error occurs (connection refused, timeout, etc.).
  """

  defexception [:message, :reason]

  @type t :: %__MODULE__{
          message: String.t(),
          reason: any()
        }

  @impl true
  def message(%__MODULE__{message: msg}), do: msg
end

defmodule Controlvar.ConfigError do
  @moduledoc """
  Raised when configuration is invalid.
  """

  defexception [:message]

  @type t :: %__MODULE__{
          message: String.t()
        }

  @impl true
  def message(%__MODULE__{message: msg}), do: msg
end
