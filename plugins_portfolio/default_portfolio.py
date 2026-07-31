"""
Default Portfolio plugin for LTS.
Manages cell weights, vol-scaling, and portfolio-level position aggregation.
"""
import numpy as np
from app.plugin_base import PluginBase


class DefaultPortfolio(PluginBase):
    """Portfolio manager: aggregates strategy cell signals into portfolio positions.

    Responsibilities:
      - Maintain per-cell weight allocation (P3 fixed weights)
      - Vol-scale each cell to target volatility
      - Aggregate weighted cell returns into portfolio return
      - Track cell-level and portfolio-level state
    """

    plugin_params = {
        "target_vol": 0.10,
        "ppy_daily": 252,
        "max_vol_scalar": 5.0,
        "min_vol_floor": 0.01,
        "vol_lookback": 63,  # Trailing days for realized vol estimate
    }
    plugin_debug_vars = ["target_vol", "max_vol_scalar"]

    def __init__(self, config=None):
        super().__init__(config)
        self._cell_weights = {}    # cell_name -> weight (must sum to 1)
        self._cell_positions = {}  # cell_name -> current position (scalar)
        self._cell_returns = {}    # cell_name -> list of daily net returns
        self._cell_vol_scalar = {} # cell_name -> current vol scalar

    def set_weights(self, weights):
        """Set portfolio cell weights. Must sum to ~1.0.

        Args:
            weights: dict mapping cell_name -> weight (e.g. {"eurusd_mr": 0.2055})
        """
        self._cell_weights = dict(weights)
        for name in weights:
            if name not in self._cell_positions:
                self._cell_positions[name] = 0.0
                self._cell_returns[name] = []
                self._cell_vol_scalar[name] = 1.0

    def get_allocations(self):
        """Return current cell weights."""
        return dict(self._cell_weights)

    def update_cell(self, cell_name, position, daily_net_return):
        """Update a cell's position and record its daily return.

        Args:
            cell_name: strategy cell identifier
            position: current position after today's signal (-1, 0, 1, or continuous)
            daily_net_return: today's net return for this cell (after costs)
        """
        self._cell_positions[cell_name] = position
        if cell_name not in self._cell_returns:
            self._cell_returns[cell_name] = []
        self._cell_returns[cell_name].append(daily_net_return)

        # Recompute vol scalar for this cell
        lookback = self.params["vol_lookback"]
        rets = self._cell_returns[cell_name]
        if len(rets) >= lookback:
            recent = np.array(rets[-lookback:])
            realized = np.std(recent) * np.sqrt(self.params["ppy_daily"])
            scalar = self.params["target_vol"] / max(realized, self.params["min_vol_floor"])
            scalar = min(scalar, self.params["max_vol_scalar"])
            self._cell_vol_scalar[cell_name] = scalar

    def get_vol_scalar(self, cell_name):
        """Get current vol scalar for a cell."""
        return self._cell_vol_scalar.get(cell_name, 1.0)

    @staticmethod
    def net_instrument_targets(cell_targets):
        """Aggregate virtual-cell targets into exact broker instrument targets."""
        net_targets = {}
        attribution = {}
        for row in cell_targets:
            cell_id = str(row["cell_id"])
            instrument = str(row["instrument"])
            target_units = float(row["target_units"])
            if not np.isfinite(target_units):
                raise ValueError("target_units must be finite")
            if cell_id in attribution:
                raise ValueError(f"duplicate cell_id: {cell_id}")
            attribution[cell_id] = {
                "instrument": instrument,
                "target_units": target_units,
            }
            net_targets[instrument] = (
                net_targets.get(instrument, 0.0) + target_units
            )
        return {
            "net_targets": net_targets,
            "cell_attribution": attribution,
        }

    def allocate(self, cell_returns_today):
        """Compute portfolio daily return from cell returns.

        Args:
            cell_returns_today: dict mapping cell_name -> today's gross daily return

        Returns:
            dict with portfolio_return, cell_contributions, vol_scalars
        """
        portfolio_return = 0.0
        contributions = {}
        scalars = {}

        for cell_name, cell_ret in cell_returns_today.items():
            w = self._cell_weights.get(cell_name, 0.0)
            vs = self._cell_vol_scalar.get(cell_name, 1.0)
            scaled_ret = cell_ret * vs
            contribution = w * scaled_ret
            portfolio_return += contribution
            contributions[cell_name] = contribution
            scalars[cell_name] = vs

        return {
            "portfolio_return": portfolio_return,
            "cell_contributions": contributions,
            "vol_scalars": scalars,
        }

    def rebalance(self):
        """Placeholder for periodic rebalancing logic.
        In current P3 implementation, weights are fixed. This method exists
        for future dynamic weight allocation strategies.
        """
        pass
