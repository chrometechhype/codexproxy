"""Sanity check that the cdx_codex_cli smoke target is registered."""

from __future__ import annotations


def test_cdx_codex_cli_smoke_target_is_registered() -> None:
    from smoke.lib.config import (
        ALL_TARGETS,
        OPT_IN_TARGETS,
        TARGET_ALIASES,
        _parse_targets,
    )

    assert "cdx_codex_cli" in ALL_TARGETS
    assert "cdx_codex_cli" in OPT_IN_TARGETS
    assert TARGET_ALIASES.get("cdx_codex", "cdx_codex_cli") == "cdx_codex_cli"
    assert "cdx_codex_cli" in _parse_targets("cdx_codex_cli")
