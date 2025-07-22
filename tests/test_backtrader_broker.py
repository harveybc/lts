"""
Unit tests for Backtrader Broker Plugin
"""

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import tempfile
import os
import sys
from unittest.mock import Mock, patch, MagicMock

# Add the lts path for imports
sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))

# Mock backtrader imports to avoid dependency issues in tests
sys.modules['backtrader'] = Mock()
sys.modules['backtrader.brokers'] = Mock()
sys.modules['backtrader.brokers.BackBroker'] = Mock()

class TestBacktraderBroker:
    
    @pytest.fixture
    def sample_csv_data(self):
        """Create sample CSV data for testing."""
        dates = pd.date_range(start='2023-01-01', periods=100, freq='H')
        closes = 1.1000 + np.random.normal(0, 0.001, 100).cumsum() * 0.01
        
        data = {
            'DATE_TIME': dates.strftime('%Y-%m-%d %H:%M:%S'),
            'OPEN': closes + np.random.uniform(-0.0001, 0.0001, 100),
            'HIGH': closes + np.random.uniform(0.0001, 0.0003, 100),
            'LOW': closes - np.random.uniform(0.0001, 0.0003, 100),
            'CLOSE': closes
        }
        
        return pd.DataFrame(data)
    
    @pytest.fixture
    def temp_csv_file(self, sample_csv_data):
        """Create temporary CSV file for testing."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            sample_csv_data.to_csv(f.name, index=False)
            yield f.name
        os.unlink(f.name)
    
    def test_initialization_with_csv_source(self, temp_csv_file):
        """Test broker initialization with CSV prediction source."""
        # Import here to avoid circular import issues
        from plugins_broker.backtrader_broker import BacktraderBroker
        
        config = {
            "prediction_source": "csv",
            "csv_file": temp_csv_file,
            "initial_cash": 10000.0,
            "prediction_horizons": ["1h", "1d"]
        }
        
        with patch('plugins_broker.backtrader_broker.BacktraderBroker._init_csv_predictor'):
            broker = BacktraderBroker(config)
            
            assert broker.prediction_source == "csv"
            assert broker.config["initial_cash"] == 10000.0
            assert broker.config["prediction_horizons"] == ["1h", "1d"]
    
    def test_initialization_with_api_source(self):
        """Test broker initialization with API prediction source."""
        from plugins_broker.backtrader_broker import BacktraderBroker
        
        config = {
            "prediction_source": "api",
            "api_url": "http://localhost:8001",
            "initial_cash": 5000.0
        }
        
        with patch('plugins_broker.backtrader_broker.BacktraderBroker._init_csv_predictor'):
            broker = BacktraderBroker(config)
            
            assert broker.prediction_source == "api"
            assert broker.config["api_url"] == "http://localhost:8001"
            assert broker.csv_predictor is None
    
    @patch('plugins_broker.backtrader_broker.CSVPredictor')
    def test_csv_predictor_initialization(self, mock_csv_predictor, temp_csv_file):
        """Test CSV predictor initialization."""
        from plugins_broker.backtrader_broker import BacktraderBroker
        
        config = {
            "prediction_source": "csv",
            "csv_file": temp_csv_file,
            "prediction_horizons": ["1h", "1d"]
        }
        
        broker = BacktraderBroker(config)
        
        # Verify CSV predictor was created with correct config
        mock_csv_predictor.assert_called_once()
        call_args = mock_csv_predictor.call_args[0][0]
        assert call_args["csv_file"] == temp_csv_file
        assert call_args["prediction_horizons"] == ["1h", "1d"]
    
    @patch('requests.post')
    def test_api_predictions(self, mock_post):
        """Test getting predictions from API."""
        from plugins_broker.backtrader_broker import BacktraderBroker
        
        # Mock API response
        mock_response = Mock()
        mock_response.json.return_value = {
            "predictions": [
                {"horizon": "1h", "prediction": 0.0025},
                {"horizon": "1d", "prediction": 0.0150}
            ]
        }
        mock_post.return_value = mock_response
        
        config = {
            "prediction_source": "api",
            "api_url": "http://localhost:8001",
            "api_timeout": 30
        }
        
        with patch('plugins_broker.backtrader_broker.BacktraderBroker._init_csv_predictor'):
            broker = BacktraderBroker(config)
            
            test_timestamp = datetime(2023, 1, 1, 12, 0, 0)
            result = broker._get_api_predictions(test_timestamp, "EURUSD")
            
            assert "predictions" in result
            assert len(result["predictions"]) == 2
            
            # Verify API call
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert "http://localhost:8001/predict" in call_args[0]
    
    def test_csv_predictions(self, temp_csv_file):
        """Test getting predictions from CSV predictor."""
        from plugins_broker.backtrader_broker import BacktraderBroker
        
        # Mock CSV predictor
        mock_predictor = Mock()
        mock_predictor.predict.return_value = {
            "predictions": [
                {"horizon": "1h", "prediction": 0.0025},
                {"horizon": "1d", "prediction": 0.0150}
            ]
        }
        
        config = {
            "prediction_source": "csv",
            "csv_file": temp_csv_file
        }
        
        with patch('plugins_broker.backtrader_broker.BacktraderBroker._init_csv_predictor'):
            broker = BacktraderBroker(config)
            broker.csv_predictor = mock_predictor
            
            test_timestamp = datetime(2023, 1, 1, 12, 0, 0)
            result = broker._get_csv_predictions(test_timestamp, "EURUSD")
            
            assert "predictions" in result
            assert len(result["predictions"]) == 2
            
            # Verify predictor was called
            mock_predictor.predict.assert_called_once_with(test_timestamp, "EURUSD")
    
    def test_get_predictions_routing(self, temp_csv_file):
        """Test prediction routing based on source configuration."""
        from plugins_broker.backtrader_broker import BacktraderBroker
        
        # Test CSV routing
        with patch('plugins_broker.backtrader_broker.BacktraderBroker._init_csv_predictor'):
            with patch.object(BacktraderBroker, '_get_csv_predictions') as mock_csv:
                mock_csv.return_value = {"predictions": []}
                
                broker = BacktraderBroker({"prediction_source": "csv", "csv_file": temp_csv_file})
                
                test_timestamp = datetime(2023, 1, 1, 12, 0, 0)
                broker.get_predictions(test_timestamp)
                
                mock_csv.assert_called_once()
    
        # Test API routing
        with patch('plugins_broker.backtrader_broker.BacktraderBroker._init_csv_predictor'):
            with patch.object(BacktraderBroker, '_get_api_predictions') as mock_api:
                mock_api.return_value = {"predictions": []}
                
                broker = BacktraderBroker({"prediction_source": "api"})
                
                test_timestamp = datetime(2023, 1, 1, 12, 0, 0)
                broker.get_predictions(test_timestamp)
                
                mock_api.assert_called_once()
    
    def test_buy_order_with_predictions(self, temp_csv_file):
        """Test buy order execution with prediction integration."""
        from plugins_broker.backtrader_broker import BacktraderBroker
        
        # Mock data object
        mock_data = Mock()
        mock_data.datetime = [123456789.0]  # Mock datetime value
        
        # Mock bt.num2date
        with patch('plugins_broker.backtrader_broker.bt.num2date') as mock_num2date:
            mock_num2date.return_value = datetime(2023, 1, 1, 12, 0, 0)
            
            with patch('plugins_broker.backtrader_broker.BacktraderBroker._init_csv_predictor'):
                with patch.object(BacktraderBroker, 'get_predictions') as mock_get_pred:
                    mock_get_pred.return_value = {
                        "predictions": [{"horizon": "1h", "prediction": 0.0025}]
                    }
                    
                    # Mock parent buy method
                    with patch('backtrader.brokers.BackBroker.buy') as mock_parent_buy:
                        mock_parent_buy.return_value = Mock()
                        
                        broker = BacktraderBroker({"prediction_source": "csv", "csv_file": temp_csv_file})
                        
                        # Execute buy order
                        broker.buy(Mock(), mock_data, 100, price=1.1050)
                        
                        # Verify predictions were retrieved
                        mock_get_pred.assert_called_once()
                        
                        # Verify parent buy was called
                        mock_parent_buy.assert_called_once()
                        
                        # Verify prediction usage was recorded
                        assert len(broker.predictions_used) == 1
                        assert broker.predictions_used[0]["action"] == "buy"
    
    def test_performance_metrics_calculation(self):
        """Test performance metrics calculation."""
        from plugins_broker.backtrader_broker import BacktraderBroker
        
        with patch('plugins_broker.backtrader_broker.BacktraderBroker._init_csv_predictor'):
            broker = BacktraderBroker({"prediction_source": "api", "initial_cash": 10000.0})
            
            # Mock some trades
            broker.trades = [
                {"pnl": 100.0, "size": 1000, "commission": 1.0},
                {"pnl": -50.0, "size": 500, "commission": 0.5},
                {"pnl": 200.0, "size": 1500, "commission": 1.5}
            ]
            
            # Mock portfolio values
            broker.portfolio_values = [
                {"value": 10000.0, "cash": 9000.0},
                {"value": 10100.0, "cash": 9100.0},
                {"value": 10250.0, "cash": 9250.0}
            ]
            
            metrics = broker.get_performance_metrics()
            
            assert "performance" in metrics
            assert "trades" in metrics
            assert "predictions" in metrics
            
            # Check performance calculations
            assert metrics["performance"]["initial_value"] == 10000.0
            assert metrics["performance"]["final_value"] == 10250.0
            assert metrics["performance"]["total_return"] == 0.025
            
            # Check trade statistics
            assert metrics["trades"]["total_trades"] == 3
            assert metrics["trades"]["winning_trades"] == 2
            assert metrics["trades"]["losing_trades"] == 1
            assert metrics["trades"]["total_pnl"] == 250.0
    
    def test_prediction_source_switching(self, temp_csv_file):
        """Test switching between prediction sources."""
        from plugins_broker.backtrader_broker import BacktraderBroker
        
        with patch('plugins_broker.backtrader_broker.BacktraderBroker._init_csv_predictor'):
            broker = BacktraderBroker({"prediction_source": "api"})
            
            assert broker.prediction_source == "api"
            
            # Switch to CSV
            broker.switch_prediction_source("csv", {"csv_file": temp_csv_file})
            
            assert broker.prediction_source == "csv"
            assert broker.config["csv_file"] == temp_csv_file
    
    def test_get_broker_info(self):
        """Test getting broker information."""
        from plugins_broker.backtrader_broker import BacktraderBroker
        
        config = {
            "prediction_source": "csv",
            "initial_cash": 10000.0,
            "commission": 0.001
        }
        
        with patch('plugins_broker.backtrader_broker.BacktraderBroker._init_csv_predictor'):
            with patch.object(BacktraderBroker, 'getcash', return_value=9500.0):
                with patch.object(BacktraderBroker, 'getvalue', return_value=10200.0):
                    with patch.object(BacktraderBroker, 'positions', [Mock(size=100), Mock(size=0)]):
                        broker = BacktraderBroker(config)
                        
                        info = broker.get_broker_info()
                        
                        assert info["broker_type"] == "backtrader_broker"
                        assert info["prediction_source"] == "csv"
                        assert info["current_cash"] == 9500.0
                        assert info["current_value"] == 10200.0
                        assert "config" in info
    
    def test_error_handling(self, temp_csv_file):
        """Test error handling in broker operations."""
        from plugins_broker.backtrader_broker import BacktraderBroker
        
        # Test invalid prediction source
        with pytest.raises(ValueError, match="Invalid prediction source"):
            broker = BacktraderBroker({"prediction_source": "invalid"})
            broker.switch_prediction_source("invalid")
        
        # Test prediction error handling
        with patch('plugins_broker.backtrader_broker.BacktraderBroker._init_csv_predictor'):
            broker = BacktraderBroker({"prediction_source": "csv", "csv_file": temp_csv_file})
            
            with patch.object(broker, '_get_csv_predictions', side_effect=Exception("Test error")):
                test_timestamp = datetime(2023, 1, 1, 12, 0, 0)
                result = broker.get_predictions(test_timestamp)
                
                assert "error" in result
                assert result["predictions"] == []
    
    def test_notify_trade_tracking(self):
        """Test trade notification and tracking."""
        from plugins_broker.backtrader_broker import BacktraderBroker
        
        with patch('plugins_broker.backtrader_broker.BacktraderBroker._init_csv_predictor'):
            broker = BacktraderBroker({"prediction_source": "api"})
            
            # Mock trade object
            mock_trade = Mock()
            mock_trade.isclosed = True
            mock_trade.ref = 1
            mock_trade.size = 1000
            mock_trade.price = 1.1050
            mock_trade.pnl = 100.0
            mock_trade.pnlcomm = 99.0
            mock_trade.dtopen = 123456789
            mock_trade.dtclose = 123456800
            mock_trade.commission = 1.0
            
            # Call notify_trade
            broker.notify_trade(mock_trade)
            
            # Verify trade was recorded
            assert len(broker.trades) == 1
            assert broker.trades[0]["pnl"] == 100.0
            assert broker.trades[0]["size"] == 1000
    
    def test_next_method_portfolio_tracking(self):
        """Test next method for portfolio value tracking."""
        from plugins_broker.backtrader_broker import BacktraderBroker
        
        with patch('plugins_broker.backtrader_broker.BacktraderBroker._init_csv_predictor'):
            with patch.object(BacktraderBroker, 'getvalue', return_value=10200.0):
                with patch.object(BacktraderBroker, 'getcash', return_value=9500.0):
                    with patch.object(BacktraderBroker, 'positions', [Mock(size=100), Mock(size=0)]):
                        broker = BacktraderBroker({"prediction_source": "api"})
                        
                        # Call next method
                        broker.next()
                        
                        # Verify portfolio value was recorded
                        assert len(broker.portfolio_values) == 1
                        assert broker.portfolio_values[0]["value"] == 10200.0
                        assert broker.portfolio_values[0]["cash"] == 9500.0
                        assert broker.portfolio_values[0]["positions"] == 1

if __name__ == "__main__":
    pytest.main([__file__])
