"""Flag environments: production, staging and development in one install (#2011).

A feature flag has one configuration per environment: whether it is on, its
targeting rules, its rollout percentage and its split (#2012: no request sets
one yet). The set is fixed here, in code; there is no table of environments.

* ``production`` is the default, and its configuration is the flag row itself:
  ``feature_flags.status``, ``targeting_rules`` and ``rollout_percentage``,
  exactly where the previous release reads and writes them, and ``split``.
* ``staging`` and ``development`` are side rows in
  ``flag_environment_settings`` (``FlagEnvironmentSetting``), at most one per
  flag and environment, and only once someone configures the flag there. With
  no row the flag is off in that environment.

An environment is exactly one of these lower-case names. ``Production``,
``prod``, ``dev``, ``qa`` and ``""`` are not environments.

The flag environment is never read from the deployment setting
``ENVIRONMENT`` (development, test, staging or production): an install whose
``ENVIRONMENT`` is ``development`` still serves its flags from production by
default. This module imports no settings.

:func:`flag_in_environment` is the one place a reader turns a flag and its
side row into what one environment serves.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional, Tuple

from backend.app.models.feature_flag import FeatureFlagStatus, flag_status_name

PRODUCTION = "production"
STAGING = "staging"
DEVELOPMENT = "development"

#: Every flag environment, the default first.
FLAG_ENVIRONMENTS: Tuple[str, ...] = (PRODUCTION, STAGING, DEVELOPMENT)

#: The environment a flag row and an API key belong to unless told otherwise.
DEFAULT = PRODUCTION

#: The environments whose configuration is a side row. Production's is the
#: flag row, and ``check_flag_environment_name`` refuses a production side row.
NON_PRODUCTION: Tuple[str, ...] = (STAGING, DEVELOPMENT)

#: What a reader sees for each environment.
DISPLAY_NAMES: Dict[str, str] = {
    PRODUCTION: "Production",
    STAGING: "Staging",
    DEVELOPMENT: "Development",
}

#: The type of an environment name, for a request field or a path parameter.
FlagEnvironmentName = Literal["production", "staging", "development"]

#: The side row's columns that are not per-environment configuration. Every
#: other column of ``FlagEnvironmentSetting`` is served by
#: :class:`FlagInEnvironment`.
SETTING_IDENTITY_COLUMNS: Tuple[str, ...] = (
    "id",
    "created_at",
    "updated_at",
    "feature_flag_id",
    "environment",
)

#: The fields an environment's configuration decides; every other attribute of
#: a flag (key, id, name, value type, on value and off value) is the flag row's.
ENVIRONMENT_FIELDS: Tuple[str, ...] = (
    "status",
    "targeting_rules",
    "rollout_percentage",
    "split",
)

_ARCHIVED = FeatureFlagStatus.ARCHIVED.value


def is_flag_environment(name: Any) -> bool:
    """Whether *name* is exactly one of :data:`FLAG_ENVIRONMENTS`."""
    return isinstance(name, str) and name in FLAG_ENVIRONMENTS


def _refused(name: Any) -> ValueError:
    shown = repr(name)
    if len(shown) > 40:
        shown = shown[:37] + "..."
    return ValueError(
        f"not a flag environment: {shown}; expected one of {', '.join(FLAG_ENVIRONMENTS)}"
    )


class FlagInEnvironment:
    """A flag as one non-production environment serves it. Read-only.

    ``status``, ``targeting_rules``, ``rollout_percentage`` and ``split`` are
    the environment's (:data:`ENVIRONMENT_FIELDS`); ``split`` is ``None`` with
    no side row. Every other attribute -- ``key``, ``id``, ``name``,
    ``value_type``, ``on_value``, ``off_value``, ``updated_at`` and the rest --
    is read from the flag row, so a per-environment field this class left out
    would answer production's. ``configured`` says whether the environment has
    a side row; ``environment`` names it.
    """

    __slots__ = (
        "_flag",
        "environment",
        "configured",
        "status",
        "targeting_rules",
        "rollout_percentage",
        "split",
    )

    def __init__(self, flag: Any, setting: Any, environment: str) -> None:
        if flag_status_name(flag.status) == _ARCHIVED:
            status = FeatureFlagStatus.ARCHIVED
        elif setting is not None and setting.is_active:
            status = FeatureFlagStatus.ACTIVE
        else:
            status = FeatureFlagStatus.INACTIVE
        values = {
            "_flag": flag,
            "environment": environment,
            "configured": setting is not None,
            "status": status,
            "targeting_rules": None if setting is None else setting.targeting_rules,
            "rollout_percentage": 0 if setting is None else setting.rollout_percentage,
            "split": None if setting is None else setting.split,
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)

    def __getattr__(self, name: str) -> Any:
        # Called only for names that are not slots: the flag row's attributes.
        return getattr(object.__getattribute__(self, "_flag"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(
            f"a flag in an environment is read-only; {name!r} is written through "
            "the flag row (production) or its side row"
        )

    def __delattr__(self, name: str) -> None:
        raise AttributeError("a flag in an environment is read-only")

    def __repr__(self) -> str:
        return (
            f"<FlagInEnvironment {self._flag.key!r} in {self.environment}: "
            f"{flag_status_name(self.status)}, {self.rollout_percentage}%>"
        )


def flag_in_environment(flag: Any, setting: Optional[Any], environment: str) -> Any:
    """What *environment* serves for *flag*, given its side row there or ``None``.

    * ``production``: the flag row itself, unchanged. It has no side row, so
      *setting* must be ``None``.
    * ``staging`` or ``development``: a :class:`FlagInEnvironment`. With no
      side row the flag is off there (``INACTIVE``, no rules, 0%, no split). An
      archived flag row is ``ARCHIVED`` in every environment, whatever its side
      rows say. Otherwise the side row decides: ``ACTIVE`` when ``is_active``,
      with its rules, percentage and split.

    Raises ``ValueError`` for a name outside :data:`FLAG_ENVIRONMENTS`, for a
    side row given with production, and for a side row of another flag or
    another environment.
    """
    if not is_flag_environment(environment):
        raise _refused(environment)
    if environment == PRODUCTION:
        if setting is not None:
            raise ValueError(
                "production is the flag row and has no side row; got one for "
                f"{getattr(setting, 'environment', None)!r}"
            )
        return flag
    if setting is not None:
        if setting.environment != environment:
            raise ValueError(
                f"the side row is {setting.environment!r}'s, not {environment!r}'s"
            )
        if setting.feature_flag_id != flag.id:
            raise ValueError("the side row belongs to another flag")
    return FlagInEnvironment(flag, setting, environment)
