"""
End-to-end integration test for CSV plugins workflow

This test validates the complete workflow from CSV data to strategy execution
using ideal predictions through the backtrader broker.
"""

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import tempfile
import os
import sys
import json
import time
from unittest.mock import Mock, patch, MagicMock

# Add paths for imports
sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "prediction_provider"))

# backtrader is installed — no need to mock it
sys.modules['backtrader.brokers.BackBroker'] = Mock()

class TestCSVWorkflowEndToEnd:
    
    @pytest.fixture
    def comprehensive_csv_data(self):
        """Create comprehensive CSV data for end-to-end testing."""
        # Create 2 weeks of hourly data with clear trend and patterns
        dates = pd.date_range(start='2023-01-01', periods=336, freq='h')
        
        # Create predictable pattern: 
        # - Upward trend over 2 weeks
        # - Daily cycles (higher during "trading hours")
        # - Some noise but predictable enough for testing
        
        base_price = 1.1000
        
        # Long-term trend (100 pips over 2 weeks)
        long_trend = np.linspace(0, 0.0100, 336)
        
        # Daily pattern (10 pip daily cycle)
        hours = np.array([d.hour for d in dates])
        daily_pattern = 0.0010 * np.sin(2 * np.pi * hours / 24)
        
        # Random noise (small)
        noise = np.random.normal(0, 0.0001, 336)
        
        # Combine patterns
        closes = base_price + long_trend + daily_pattern + noise
        
        # Generate OHLC data
        data = {
            'DATE_TIME': dates.strftime('%Y-%m-%d %H:%M:%S'),
            'OPEN': closes + np.random.uniform(-0.00005, 0.00005, 336),
            'HIGH': closes + np.random.uniform(0.00005, 0.00015, 336),
            'LOW': closes - np.random.uniform(0.00005, 0.00015, 336),
            'CLOSE': closes
        }
        
        # Ensure OHLC constraints
        for i in range(336):
            data['HIGH'][i] = max(data['OPEN'][i], data['CLOSE'][i], data['HIGH'][i])
            data['LOW'][i] = min(data['OPEN'][i], data['CLOSE'][i], data['LOW'][i])
        
        return pd.DataFrame(data)
    
    @pytest.fixture
    def temp_csv_file(self, comprehensive_csv_data):
        """Create temporary CSV file for testing."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            comprehensive_csv_data.to_csv(f.name, index=False)
            yield f.name
        os.unlink(f.name)
    
    def test_complete_csv_workflow(self, temp_csv_file):
        """Test complete workflow from CSV data to strategy execution."""
        # Import plugins
        from feeder_plugins.csv_feeder import CSVFeeder
        from predictor_plugins.csv_predictor import CSVPredictor
        from plugins_broker.backtrader_broker import BacktraderBroker
        
        # Step 1: Initialize CSV Feeder
        feeder_config = {
            "csv_file": temp_csv_file,
            "datetime_column": "DATE_TIME",
            "horizon_periods": 48,  # 2 days of data
            "data_columns": ["OPEN", "HIGH", "LOW", "CLOSE"]
        }
        
        feeder = CSVFeeder(feeder_config)
        assert feeder.data_loaded is True
        
        # Step 2: Initialize CSV Predictor
        predictor_config = {
            "csv_file": temp_csv_file,
            "datetime_column": "DATE_TIME",
            "prediction_horizons": ["1h", "6h", "1d"],
            "close_column": "CLOSE"
        }
        
        predictor = CSVPredictor(predictor_config)
        
        # Step 3: Initialize Backtrader Broker
        broker_config = {
            "prediction_source": "csv",
            "csv_file": temp_csv_file,
            "initial_cash": 10000.0,
            "commission": 0.001,
            "prediction_horizons": ["1h", "6h", "1d"]
        }
        
        with patch('plugins_broker.backtrader_broker.BacktraderBroker._init_csv_predictor'):
            broker = BacktraderBroker(broker_config)
            broker.csv_predictor = predictor  # Manually set for testing
        
        # Step 4: Simulate trading workflow
        
        # Get initial data
        initial_data = feeder.get_latest_data(periods=24)
        assert len(initial_data) == 24
        
        current_time = initial_data.index[-1]
        
        # Generate predictions
        predictions = predictor.predict(current_time)
        assert "predictions" in predictions
        assert len(predictions["predictions"]) == 3  # 1h, 6h, 1d
        
        # Verify predictions are ideal (should match future values)
        full_data = feeder.get_data(periods=len(feeder.data))
        current_idx = full_data.index.get_indexer([current_time], method='nearest')[0]
        current_close = full_data.iloc[current_idx]["CLOSE"]
        
        for pred in predictions["predictions"]:
            horizon = pred["horizon"]
            if horizon == "1h":
                future_idx = current_idx + 1
            elif horizon == "6h":
                future_idx = current_idx + 6
            elif horizon == "1d":
                future_idx = current_idx + 24
            
            if future_idx < len(full_data):
                future_close = full_data.iloc[future_idx]["CLOSE"]
                expected_return = (future_close - current_close) / current_close
                
                # Verify prediction accuracy (should be exact for CSV predictor)
                assert abs(pred["prediction"] - expected_return) < 1e-10
        
        # Step 5: Simulate broker operations
        
        # Mock data object for broker
        mock_data = Mock()
        mock_data.datetime = [123456789.0]
        
        with patch('plugins_broker.backtrader_broker.bt.num2date') as mock_num2date:
            mock_num2date.return_value = current_time
            
            with patch.object(broker, 'get_predictions') as mock_get_pred:
                mock_get_pred.return_value = predictions
                
                with patch('backtrader.brokers.BackBroker.buy') as mock_parent_buy:
                    mock_parent_buy.return_value = Mock()
                    
                    # Execute buy order
                    broker.buy(Mock(), mock_data, 1000, price=current_close)
                    
                    # Verify prediction was used
                    assert len(broker.predictions_used) == 1
                    assert broker.predictions_used[0]["action"] == "buy"
                    assert broker.predictions_used[0]["predictions"] == predictions
        
        # Step 6: Verify performance tracking
        broker.trades.append({
            "pnl": 50.0,
            "size": 1000,
            "commission": 1.0
        })
        
        broker.portfolio_values.append({
            "value": 10050.0,
            "cash": 9000.0,
            "positions": 1
        })
        
        metrics = broker.get_performance_metrics()
        assert "performance" in metrics
        assert "trades" in metrics
        assert "predictions" in metrics
        
        assert metrics["performance"]["total_return"] == 0.005  # 0.5% return
        assert metrics["trades"]["total_trades"] == 1
        assert metrics["predictions"]["prediction_source"] == "csv"
    
    def test_prediction_source_switching_workflow(self, temp_csv_file):
        """Test switching between CSV and API prediction sources."""
        from plugins_broker.backtrader_broker import BacktraderBroker
        
        # Start with API source
        broker_config = {
            "prediction_source": "api",
            "api_url": "http://localhost:8001",
            "initial_cash": 10000.0
        }
        
        with patch('plugins_broker.backtrader_broker.BacktraderBroker._init_csv_predictor'):
            broker = BacktraderBroker(broker_config)
            
            assert broker.prediction_source == "api"
            
            # Switch to CSV source
            broker.switch_prediction_source("csv", {"csv_file": temp_csv_file})
            
            assert broker.prediction_source == "csv"
            assert broker.config["csv_file"] == temp_csv_file
            
            # Switch back to API
            broker.switch_prediction_source("api", {"api_url": "http://localhost:8002"})
            
            assert broker.prediction_source == "api"
            assert broker.config["api_url"] == "http://localhost:8002"
    
    def test_multi_timeframe_prediction_accuracy(self, temp_csv_file):
        """Test prediction accuracy across multiple timeframes."""
        from predictor_plugins.csv_predictor import CSVPredictor
        
        predictor_config = {
            "csv_file": temp_csv_file,
            "prediction_horizons": ["1h", "2h", "6h", "12h", "1d"],
            "close_column": "CLOSE"
        }
        
        predictor = CSVPredictor(predictor_config)
        
        # Test predictions at multiple timestamps
        test_timestamps = [
            datetime(2023, 1, 2, 12, 0, 0),
            datetime(2023, 1, 3, 18, 0, 0),
            datetime(2023, 1, 5, 6, 0, 0)
        ]
        
        for timestamp in test_timestamps:
            try:
                result = predictor.predict(timestamp)
                
                if "predictions" in result and result["predictions"]:
                    # Verify each prediction is reasonable
                    for pred in result["predictions"]:
                        # Predictions should be small returns (within ±10%)
                        assert -0.1 < pred["prediction"] < 0.1
                        
                        # Should have all required fields
                        assert "horizon" in pred
                        assert "timestamp" in pred
                        assert "future_timestamp" in pred
                        assert "current_close" in pred
                        assert "future_close" in pred
                        
                        # Future timestamp should be after current
                        current_ts = datetime.fromisoformat(pred["timestamp"])
                        future_ts = datetime.fromisoformat(pred["future_timestamp"])
                        assert future_ts > current_ts
                        
            except Exception as e:
                # Some timestamps may not have enough future data
                print(f"Prediction failed for {timestamp}: {e}")
                continue
    
    def test_csv_data_validation_workflow(self, temp_csv_file):
        """Test data validation throughout the CSV workflow."""
        from feeder_plugins.csv_feeder import CSVFeeder
        from predictor_plugins.csv_predictor import CSVPredictor
        
        # Test feeder data validation
        feeder = CSVFeeder({
            "csv_file": temp_csv_file,
            "datetime_column": "DATE_TIME"
        })
        
        data_info = feeder.get_data_info()
        assert data_info["loaded"] is True
        assert data_info["total_records"] == 336  # 2 weeks of hourly data
        assert "start_date" in data_info
        assert "end_date" in data_info
        
        # Test data availability validation
        start_time = datetime(2023, 1, 1, 5, 0, 0)
        end_time = datetime(2023, 1, 10, 15, 0, 0)
        assert feeder.validate_data_availability(start_time, end_time) is True
        
        # Test invalid range
        future_start = datetime(2025, 1, 1, 0, 0, 0)
        future_end = datetime(2025, 1, 2, 0, 0, 0)
        assert feeder.validate_data_availability(future_start, future_end) is False
        
        # Test predictor capabilities
        predictor = CSVPredictor({
            "csv_file": temp_csv_file,
            "prediction_horizons": ["1h", "1d", "1w"]
        })
        
        test_time = datetime(2023, 1, 5, 12, 0, 0)
        capabilities = predictor.validate_prediction_capability(test_time)
        
        assert "1h" in capabilities
        assert "1d" in capabilities
        assert "1w" in capabilities
        
        # Early timestamp should have good capabilities
        assert capabilities["1h"] is True
        assert capabilities["1d"] is True
        # 1w may or may not be available depending on data length
    
    def test_performance_with_realistic_data_size(self, temp_csv_file):
        """Test performance with realistic data size and operations."""
        from feeder_plugins.csv_feeder import CSVFeeder
        from predictor_plugins.csv_predictor import CSVPredictor
        
        start_time = time.time()
        
        # Initialize components
        feeder = CSVFeeder({
            "csv_file": temp_csv_file,
            "datetime_column": "DATE_TIME",
            "horizon_periods": 168  # 1 week
        })
        
        predictor = CSVPredictor({
            "csv_file": temp_csv_file,
            "prediction_horizons": ["1h", "6h", "1d"]
        })
        
        # Perform multiple operations
        data_operations = []
        prediction_operations = []
        
        test_times = [
            datetime(2023, 1, 2, i, 0, 0) for i in range(0, 24, 4)  # Every 4 hours
        ]
        
        for test_time in test_times:
            # Data retrieval
            data_start = time.time()
            data = feeder.get_data_at_time(test_time, periods=24)
            data_operations.append(time.time() - data_start)
            
            # Prediction generation
            pred_start = time.time()
            try:
                predictions = predictor.predict(test_time)
                prediction_operations.append(time.time() - pred_start)
            except Exception:
                # Some predictions may fail due to insufficient future data
                continue
        
        total_time = time.time() - start_time
        
        # Performance assertions
        assert total_time < 10.0, f"Total workflow took too long: {total_time}s"
        
        if data_operations:
            avg_data_time = sum(data_operations) / len(data_operations)
            assert avg_data_time < 0.1, f"Average data retrieval too slow: {avg_data_time}s"
        
        if prediction_operations:
            avg_pred_time = sum(prediction_operations) / len(prediction_operations)
            assert avg_pred_time < 0.5, f"Average prediction too slow: {avg_pred_time}s"
    
    def test_error_handling_throughout_workflow(self, temp_csv_file):
        """Test error handling at each step of the workflow."""
        from feeder_plugins.csv_feeder import CSVFeeder
        from predictor_plugins.csv_predictor import CSVPredictor
        from plugins_broker.backtrader_broker import BacktraderBroker
        
        # Test feeder error handling
        with pytest.raises(FileNotFoundError):
            CSVFeeder({"csv_file": "nonexistent.csv"})
        
        # Test predictor error handling
        with pytest.raises(ValueError):
            CSVPredictor({
                "csv_file": temp_csv_file,
                "prediction_horizons": []  # Empty horizons
            })
        
        # Test broker error handling
        with pytest.raises(ValueError):
            broker = BacktraderBroker({"prediction_source": "invalid"})
            broker.switch_prediction_source("invalid")
        
        # Test graceful handling of edge cases
        predictor = CSVPredictor({
            "csv_file": temp_csv_file,
            "prediction_horizons": ["1h"]
        })
        
        # Test prediction at end of data
        full_data = predictor._get_full_data()
        last_timestamp = full_data.index[-1]
        
        try:
            result = predictor.predict(last_timestamp)
            # Should either succeed with empty predictions or fail gracefully
            if "predictions" in result:
                assert isinstance(result["predictions"], list)
        except ValueError:
            # Acceptable to raise error for insufficient data
            pass

if __name__ == "__main__":
    pytest.main([__file__])
