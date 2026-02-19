"""
CSV Predictor Plugin

Provides ideal (perfect) predictions by looking ahead in CSV data.
Returns predictions as returns (future_close - current_close) / current_close.
"""

import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional


# Map horizon strings to number of periods (assuming hourly data)
_HORIZON_MAP = {
    "1h": 1,
    "2h": 2,
    "6h": 6,
    "12h": 12,
    "1d": 24,
    "1w": 168,
}


class CSVPredictor:
    """
    Ideal predictor that looks ahead in CSV data to produce perfect predictions.

    Config:
        csv_file: Path to CSV file with OHLC data
        datetime_column: Datetime column name (default: 'DATE_TIME')
        close_column: Close price column name (default: 'CLOSE')
        prediction_horizons: List of horizon strings, e.g. ['1h', '6h', '1d']
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config

        csv_file = config.get("csv_file")
        if not csv_file:
            raise ValueError("csv_file is required")

        horizons = config.get("prediction_horizons", [])
        if not horizons:
            raise ValueError("prediction_horizons must be a non-empty list")

        self.datetime_column = config.get("datetime_column", "DATE_TIME")
        self.close_column = config.get("close_column", "CLOSE")
        self.prediction_horizons = horizons

        self.data: Optional[pd.DataFrame] = None
        self._load_data(csv_file)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_data(self, csv_file: str):
        import os
        if not os.path.exists(csv_file):
            raise FileNotFoundError(f"CSV file not found: {csv_file}")

        df = pd.read_csv(csv_file)
        df[self.datetime_column] = pd.to_datetime(df[self.datetime_column])
        df.set_index(self.datetime_column, inplace=True)
        df.sort_index(inplace=True)
        self.data = df

    def _get_full_data(self) -> pd.DataFrame:
        """Return full loaded data (used by tests)."""
        return self.data

    def _horizon_to_periods(self, horizon: str) -> int:
        if horizon in _HORIZON_MAP:
            return _HORIZON_MAP[horizon]
        raise ValueError(f"Unknown horizon: {horizon}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def predict(self, timestamp: datetime, symbol: str = None) -> Dict[str, Any]:
        """
        Generate ideal predictions at *timestamp* for each configured horizon.

        Returns a dict with key ``predictions`` containing a list of dicts,
        each with: horizon, prediction (return), timestamp, future_timestamp,
        current_close, future_close.
        """
        ts = pd.Timestamp(timestamp)
        idx = self.data.index.get_indexer([ts], method="nearest")[0]
        current_close = self.data.iloc[idx][self.close_column]
        current_ts = self.data.index[idx]

        predictions: List[Dict[str, Any]] = []
        for h in self.prediction_horizons:
            periods = self._horizon_to_periods(h)
            future_idx = idx + periods
            if future_idx < len(self.data):
                future_close = self.data.iloc[future_idx][self.close_column]
                future_ts = self.data.index[future_idx]
                predicted_return = (future_close - current_close) / current_close
                predictions.append({
                    "horizon": h,
                    "prediction": predicted_return,
                    "timestamp": current_ts.isoformat(),
                    "future_timestamp": future_ts.isoformat(),
                    "current_close": float(current_close),
                    "future_close": float(future_close),
                })

        return {"predictions": predictions, "status": "success"}

    def validate_prediction_capability(self, timestamp: datetime) -> Dict[str, bool]:
        """
        Check which horizons can be predicted at *timestamp*.
        """
        ts = pd.Timestamp(timestamp)
        idx = self.data.index.get_indexer([ts], method="nearest")[0]
        result: Dict[str, bool] = {}
        for h in self.prediction_horizons:
            periods = self._horizon_to_periods(h)
            result[h] = bool((idx + periods) < len(self.data))
        return result
