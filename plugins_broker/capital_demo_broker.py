"""Mutation-disabled Capital.com Demo broker plugin."""


class CapitalDemoBroker:
    """Expose explicit denials until protected paper execution is approved."""

    def __init__(self, config=None):
        self.config = config or {}

    @staticmethod
    def _disabled():
        return {
            "success": False,
            "error": "Capital.com Demo order mutations are disabled",
        }

    def open_order(self, *args, **kwargs):
        return self._disabled()

    def modify_order(self, *args, **kwargs):
        return self._disabled()

    def close_order(self, *args, **kwargs):
        return self._disabled()

    def execute_order(self, *args, **kwargs):
        return self._disabled()
