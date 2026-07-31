from __future__ import annotations

from tai42_contract.template import ConditionMixin, ExprMixin


class CallbackSchema(ConditionMixin, ExprMixin):
    # Optional: with no ``tool`` the backend runs the rendered ``expr`` directly.
    tool: str = ""
