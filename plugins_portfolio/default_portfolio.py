from app.plugin_base import PluginBase

class DefaultPortfolio(PluginBase):
    plugin_params = {}
    plugin_debug_vars = []

    def __init__(self, config=None):
        super().__init__(config)

    def set_params(self, **kwargs):
        super().set_params(**kwargs)

    def rebalance(self):
        pass

    def get_allocations(self):
        return {}
