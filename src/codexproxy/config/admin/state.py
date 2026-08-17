"""Typed effective-value state shared by Admin configuration services."""

from dataclasses import dataclass

from codexproxy.core.json_types import JsonValue

type ConfigInputValue = JsonValue


@dataclass(frozen=True, slots=True)
class ConfigValueState:
    """One resolved Admin value and the configuration source that owns it."""

    value: str | None
    source: str


type ValueState = dict[str, ConfigValueState]
