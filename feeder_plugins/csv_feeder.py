"""
CSV Feeder Plugin

Loads OHLC data from CSV files and provides data access methods
for strategies and predictors.
"""

import pandas as pd
from datetime import datetime
from typing import Dict, Any, Optional


class CSVFeeder:
    """
    Feeds OHLC data from CSV files.
    
    Config:
        csv_file: Path to CSV file
        datetime_column: Name of datetime column (default: 'DATE_TIME')
        data_columns: List of data columns (default: ['OPEN','HIGH','LOW','CLOSE'])
        horizon_periods: Number of periods for horizon (default: 48)
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        csv_file = config.get("csv_file")
        if not csv_file:
            raise ValueError("csv_file is required")

        self.datetime_column = config.get("datetime_column", "DATE_TIME")
        self.data_columns = config.get("data_columns", ["OPEN", "HIGH", "LOW", "CLOSE"])
        self.horizon_periods = config.get("horizon_periods", 48)
        self.data_loaded = False
        self.data: Optional[pd.DataFrame] = None

        self._load_data(csv_file)

    def _load_data(self, csv_file: str):
        """Load and parse CSV data."""
        import os
        if not os.path.exists(csv_file):
            raise FileNotFoundError(f"CSV file not found: {csv_file}")

        df = pd.read_csv(csv_file)
        df[self.datetime_column] = pd.to_datetime(df[self.datetime_column])
        df.set_index(self.datetime_column, inplace=True)
        df.sort_index(inplace=True)
        self.data = df
        self.data_loaded = True

    # ------------------------------------------------------------------
    # Public helpers expected by tests
    # ------------------------------------------------------------------

    def get_latest_data(self, periods: int = 24) -> pd.DataFrame:
        """Return the last *periods* rows that still have horizon data available."""
        # Reserve horizon_periods at the end for predictions
        usable_end = len(self.data) - self.horizon_periods
        if usable_end <= 0:
            usable_end = len(self.data)
        start = max(0, usable_end - periods)
        return self.data.iloc[start:usable_end]

    def get_data(self, periods: Optional[int] = None) -> pd.DataFrame:
        """Return data, optionally limited to *periods* rows from the end."""
        if periods is None or periods >= len(self.data):
            return self.data
        return self.data.iloc[-periods:]

    def get_data_at_time(self, timestamp: datetime, periods: int = 24) -> pd.DataFrame:
        """Return *periods* rows ending at (or closest to) *timestamp*."""
        idx = self.data.index.get_indexer([timestamp], method="nearest")[0]
        start = max(0, idx - periods + 1)
        return self.data.iloc[start: idx + 1]

    def get_data_info(self) -> Dict[str, Any]:
        """Return summary information about the loaded data."""
        return {
            "loaded": self.data_loaded,
            "total_records": len(self.data) if self.data is not None else 0,
            "start_date": str(self.data.index[0]) if self.data is not None and len(self.data) else None,
            "end_date": str(self.data.index[-1]) if self.data is not None and len(self.data) else None,
            "columns": list(self.data.columns) if self.data is not None else [],
        }

    def validate_data_availability(self, start: datetime, end: datetime) -> bool:
        """Check whether the loaded data covers the requested range."""
        if self.data is None or len(self.data) == 0:
            return False
        return self.data.index[0] <= pd.Timestamp(start) and self.data.index[-1] >= pd.Timestamp(end)
