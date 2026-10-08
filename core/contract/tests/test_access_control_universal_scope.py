"""The resource wildcard every access-control scope set may hold."""

from __future__ import annotations

from tai42_contract.access_control import UNIVERSAL_SCOPE, RoleDefinition
from tai42_contract.access_control.models import UNIVERSAL_SCOPE as MODELS_UNIVERSAL_SCOPE


def test_the_universal_scope_is_the_wildcard():
    assert UNIVERSAL_SCOPE == "*"
    assert UNIVERSAL_SCOPE is MODELS_UNIVERSAL_SCOPE


def test_a_role_defaults_to_the_universal_scope():
    role = RoleDefinition(name="r", description="", grants={})
    assert role.scopes == [UNIVERSAL_SCOPE]
