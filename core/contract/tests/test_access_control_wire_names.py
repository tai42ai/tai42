"""The principal kinds and the api-key header the contract publishes for every reader."""

from __future__ import annotations

import inspect
from typing import get_args

from tai42_contract.access_control import API_KEY_HEADER, Principal, PrincipalKind
from tai42_contract.accounts.provider import AccountsAdminServices


def test_the_principal_kinds_are_human_and_service():
    assert get_args(PrincipalKind) == ("human", "service")


def test_the_principal_model_and_the_accounts_admin_use_the_published_kinds():
    assert Principal.model_fields["kind"].annotation == PrincipalKind
    hints = inspect.get_annotations(AccountsAdminServices.create_principal, eval_str=True)
    assert hints["kind"] == PrincipalKind


def test_the_principal_kind_schema_is_an_inline_enum():
    # A Literal alias keeps the exported schema an inline enum (no ``$defs`` reference).
    assert Principal.model_json_schema()["properties"]["kind"] == {
        "enum": ["human", "service"],
        "title": "Kind",
        "type": "string",
    }


def test_the_api_key_header_is_lower_case_x_api_key():
    assert API_KEY_HEADER == "x-api-key"
