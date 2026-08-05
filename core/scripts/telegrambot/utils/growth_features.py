"""Emergency feature flags for the psychology-led growth rollout.

The three rollout features are enabled by default. Operators can disable one
without deploying code by setting its environment variable to a false value.
Keeping the parser here gives the main bot and hosted workers identical flag
semantics.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


BUYER_DISCOUNTS = "buyer_discounts"
RECRUITMENT_REWARDS = "recruitment_rewards"
REMINDERS = "reminders"

FEATURE_ENV_VARS = {
    BUYER_DISCOUNTS: "AJIB_GROWTH_BUYER_DISCOUNTS_ENABLED",
    RECRUITMENT_REWARDS: "AJIB_GROWTH_RECRUITMENT_REWARDS_ENABLED",
    REMINDERS: "AJIB_GROWTH_REMINDERS_ENABLED",
}

_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "disabled"})


def is_growth_feature_enabled(
    feature: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return the configured state for a known growth feature.

    Missing variables keep the rollout enabled. An invalid value raises rather
    than silently defeating an operator's emergency-disable request.
    """

    try:
        variable = FEATURE_ENV_VARS[str(feature)]
    except KeyError as error:
        raise ValueError(f"Unknown growth feature: {feature!r}") from error

    environment = os.environ if environ is None else environ
    raw_value = environment.get(variable)
    if raw_value is None or not str(raw_value).strip():
        return True
    normalized = str(raw_value).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(
        f"Invalid boolean value for {variable}: {raw_value!r}; "
        "use true/false, yes/no, on/off, or 1/0."
    )


def growth_feature_flags(
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, bool]:
    """Return a snapshot of every growth rollout flag."""

    return {
        feature: is_growth_feature_enabled(feature, environ=environ)
        for feature in FEATURE_ENV_VARS
    }
