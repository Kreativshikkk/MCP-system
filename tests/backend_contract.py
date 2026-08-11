"""Backend-neutral lifecycle assertions shared by storage implementations."""

from __future__ import annotations

from typing import Any, Callable
import unittest

from mcp_system import MCPSystem, TemplateSpec, TemplateStatus


def assert_template_clone_contract(
    case: unittest.TestCase,
    *,
    system: MCPSystem,
    restart: Callable[[], MCPSystem],
    template_spec: TemplateSpec,
    mutate_first: Callable[[MCPSystem, str], None],
    read_second: Callable[[MCPSystem, str], Any],
    expected_second_state: Any,
) -> tuple[MCPSystem, str]:
    template = system.create_template(template_spec)
    case.assertEqual(template.status, TemplateStatus.READY)

    first = system.create_environment_from_template(
        template.id, name="first clone"
    )
    mutate_first(system, first.id)

    restarted = restart()
    second = restarted.create_environment_from_template(
        template.id, name="second clone"
    )

    case.assertEqual(second.template_id, template.id)
    case.assertEqual(
        read_second(restarted, second.id), expected_second_state
    )
    case.assertEqual(restarted.require_template(template.id).template, template)
    return restarted, second.id
