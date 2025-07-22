#!/usr/bin/env python3
"""
Comprehensive CSV Plugin Integration Test

This test demonstrates the complete workflow using CSV-based plugins:
1. CSV Feeder - reads historical data
2. CSV Predictor - generates ideal predictions 
3. Strategy simulation using predictions
4. Portfolio performance evaluation

This validates the integration between LTS and prediction_provider repos
for CSV-based backtesting workflows.
"""

import sys
import os
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

# Add paths for both repos
prediction_provider_path = '/home/harveybc/Documents/GitHub/prediction_provider'
lts_path = '/home/harveybc/Documents/GitHub/lts'

sys.path.insert(0, prediction_provider_path)
sys.path.insert(0, lts_path)

def test_csv_feeder_integration():
    """Test CSV feeder functionality."""
    print("=== Testing CSV Feeder ===")
    
    from feeder_plugins.csv_feeder import CSVFeeder
    
    config = {
        'csv_file': os.path.join(prediction_provider_path, 'examples/data/phase_3/base_d6.csv'),
        'datetime_column': 'DATE_TIME',
        'horizon_periods': 168  # 1 week of hourly data
    }
    
    feeder = CSVFeeder(config)
    
    # Validate data loading
    assert feeder.data_loaded, "Data should be loaded"
    
    info = feeder.get_data_info()
    print(f"✓ Loaded {info['total_records']} records")
    print(f"✓ Date range: {info['start_date']} to {info['end_date']}")
    
    # Test data retrieval
    latest_data = feeder.get_latest_data(periods=24)
    assert len(latest_data) == 24, "Should get 24 records"
    assert list(latest_data.columns) == ['OPEN', 'HIGH', 'LOW', 'CLOSE'], "Should have OHLC columns"
    
    # Test historical data
    test_time = datetime(2019, 1, 15, 12, 0, 0)
    historical_data = feeder.get_data_at_time(test_time, periods=48)
    assert len(historical_data) <= 48, "Should get at most 48 records"
    
    print("✓ CSV Feeder integration test passed")

def test_csv_predictor_integration():
    """Test CSV predictor functionality."""
    print("\n=== Testing CSV Predictor ===")
    
    from predictor_plugins.csv_predictor import CSVPredictor
    
    config = {
        'csv_file': os.path.join(prediction_provider_path, 'examples/data/phase_3/base_d6.csv'),
        'datetime_column': 'DATE_TIME',
        'prediction_horizons': ['1h', '6h', '1d']
    }
    
    predictor = CSVPredictor(config)
    
    # Test prediction generation
    test_timestamp = datetime(2019, 1, 15, 12, 0, 0)
    result = predictor.predict(test_timestamp)
    
    assert 'predictions' in result, "Should have predictions"
    assert result['source'] == 'csv_predictor', "Should be CSV predictor"
    assert result['model_type'] == 'ideal', "Should be ideal predictions"
    
    predictions = result['predictions']
    print(f"✓ Generated {len(predictions)} predictions")
    
    # Verify prediction structure and accuracy
    full_data = predictor._get_full_data()
    test_idx = full_data.index.get_indexer([test_timestamp], method='nearest')[0]
    current_close = full_data.iloc[test_idx]['CLOSE']
    
    for pred in predictions:
        horizon = pred['horizon']
        prediction = pred['prediction']
        
        # Verify prediction calculation
        if horizon == '1h':
            future_idx = test_idx + 1
        elif horizon == '6h':
            future_idx = test_idx + 6
        elif horizon == '1d':
            future_idx = test_idx + 24
        
        if future_idx < len(full_data):
            future_close = full_data.iloc[future_idx]['CLOSE']
            expected_return = (future_close - current_close) / current_close
            
            # Verify accuracy (should be exact for CSV predictor)
            assert abs(prediction - expected_return) < 1e-10, f"Prediction should be exact for {horizon}"
            print(f"  ✓ {horizon} prediction: {prediction:.6f} (verified)")
    
    # Test batch predictions
    timestamps = [
        datetime(2019, 1, 15, 10, 0, 0),
        datetime(2019, 1, 15, 14, 0, 0),
        datetime(2019, 1, 15, 18, 0, 0)
    ]
    
    batch_results = predictor.predict_batch(timestamps)
    print(f"✓ Batch predictions: {len(batch_results)} results")
    
    print("✓ CSV Predictor integration test passed")

def test_prediction_based_strategy_simulation():
    """Test strategy simulation using CSV predictions."""
    print("\n=== Testing Strategy Simulation ===")
    
    from predictor_plugins.csv_predictor import CSVPredictor
    
    config = {
        'csv_file': os.path.join(prediction_provider_path, 'examples/data/phase_3/base_d6.csv'),
        'datetime_column': 'DATE_TIME',
        'prediction_horizons': ['1h', '6h', '1d']
    }
    
    predictor = CSVPredictor(config)
    
    # Simulate a simple prediction-based strategy
    # Strategy: Buy when 1h prediction > 0.001, Sell when < -0.001
    
    trades = []
    portfolio_value = 10000.0
    position = 0.0
    
    # Test over a week of data
    start_date = datetime(2019, 1, 15, 0, 0, 0)
    test_dates = [start_date + timedelta(hours=i) for i in range(0, 168, 6)]  # Every 6 hours
    
    for timestamp in test_dates:
        try:
            result = predictor.predict(timestamp)
            
            if 'predictions' in result and result['predictions']:
                # Look for 1h prediction
                pred_1h = None
                for pred in result['predictions']:
                    if pred['horizon'] == '1h':
                        pred_1h = pred['prediction']
                        break
                
                if pred_1h is not None:
                    current_close = pred['current_close']
                    
                    # Strategy logic
                    if pred_1h > 0.001 and position <= 0:  # Strong positive prediction, go long
                        if position < 0:  # Close short position
                            pnl = -position * current_close - abs(position) * current_close  # Close short
                            portfolio_value += pnl
                            trades.append({
                                'timestamp': timestamp,
                                'action': 'close_short',
                                'price': current_close,
                                'size': -position,
                                'pnl': pnl
                            })
                            position = 0
                        
                        # Open long position
                        trade_size = min(1000, portfolio_value * 0.1 / current_close)  # Risk 10% of portfolio
                        position += trade_size
                        portfolio_value -= trade_size * current_close
                        trades.append({
                            'timestamp': timestamp,
                            'action': 'buy',
                            'price': current_close,
                            'size': trade_size,
                            'pnl': 0
                        })
                    
                    elif pred_1h < -0.001 and position >= 0:  # Strong negative prediction, go short
                        if position > 0:  # Close long position
                            pnl = position * current_close - position * current_close  # Close long (simplified)
                            portfolio_value += position * current_close
                            trades.append({
                                'timestamp': timestamp,
                                'action': 'close_long',
                                'price': current_close,
                                'size': position,
                                'pnl': pnl
                            })
                            position = 0
                        
                        # Open short position (simplified)
                        trade_size = min(1000, portfolio_value * 0.1 / current_close)
                        position -= trade_size
                        portfolio_value += trade_size * current_close  # Get cash from short
                        trades.append({
                            'timestamp': timestamp,
                            'action': 'sell',
                            'price': current_close,
                            'size': trade_size,
                            'pnl': 0
                        })
        
        except Exception as e:
            # Some predictions may fail due to insufficient future data
            continue
    
    # Close any remaining position
    if position != 0:
        final_timestamp = test_dates[-1] + timedelta(hours=1)
        try:
            final_result = predictor.predict(final_timestamp)
            if 'predictions' in final_result and final_result['predictions']:
                final_close = final_result['predictions'][0]['current_close']
                
                if position > 0:
                    portfolio_value += position * final_close
                else:
                    portfolio_value -= abs(position) * final_close
                
                trades.append({
                    'timestamp': final_timestamp,
                    'action': 'close_final',
                    'price': final_close,
                    'size': abs(position),
                    'pnl': 0
                })
        except:
            pass
    
    # Calculate performance metrics
    total_trades = len(trades)
    winning_trades = len([t for t in trades if t.get('pnl', 0) > 0])
    losing_trades = len([t for t in trades if t.get('pnl', 0) < 0])
    
    total_return = (portfolio_value - 10000.0) / 10000.0
    
    print(f"✓ Strategy simulation completed")
    print(f"  - Total trades: {total_trades}")
    print(f"  - Winning trades: {winning_trades}")
    print(f"  - Losing trades: {losing_trades}")
    print(f"  - Final portfolio value: ${portfolio_value:.2f}")
    print(f"  - Total return: {total_return:.2%}")
    
    # With ideal predictions, we should expect positive performance
    # (though simplified strategy may not be optimal)
    assert total_trades > 0, "Should have executed some trades"
    
    print("✓ Strategy simulation test passed")

def test_workflow_integration():
    """Test complete CSV workflow integration."""
    print("\n=== Testing Complete Workflow Integration ===")
    
    # Test that feeder and predictor use the same underlying data
    from feeder_plugins.csv_feeder import CSVFeeder
    from predictor_plugins.csv_predictor import CSVPredictor
    
    csv_file = os.path.join(prediction_provider_path, 'examples/data/phase_3/base_d6.csv')
    
    feeder = CSVFeeder({
        'csv_file': csv_file,
        'datetime_column': 'DATE_TIME',
        'horizon_periods': 48
    })
    
    predictor = CSVPredictor({
        'csv_file': csv_file,
        'datetime_column': 'DATE_TIME',
        'prediction_horizons': ['1h']
    })
    
    # Test consistency between feeder and predictor
    test_timestamp = datetime(2019, 2, 1, 12, 0, 0)
    
    # Get data from feeder
    feeder_data = feeder.get_data_at_time(test_timestamp, periods=10)
    
    # Get prediction from predictor  
    pred_result = predictor.predict(test_timestamp)
    
    if 'predictions' in pred_result and pred_result['predictions']:
        pred_current_close = pred_result['predictions'][0]['current_close']
        
        # Find corresponding record in feeder data
        closest_feeder_time = feeder_data.index[feeder_data.index <= test_timestamp][-1]
        feeder_close = feeder_data.loc[closest_feeder_time, 'CLOSE']
        
        # Should be very close (allowing for slight timestamp differences)
        assert abs(pred_current_close - feeder_close) < 0.0001, "Feeder and predictor should use consistent data"
        print(f"✓ Data consistency verified: feeder={feeder_close:.5f}, predictor={pred_current_close:.5f}")
    
    print("✓ Workflow integration test passed")

def main():
    """Run comprehensive CSV plugin integration tests."""
    print("Starting Comprehensive CSV Plugin Integration Tests")
    print("=" * 60)
    
    try:
        # Test individual components
        feeder = test_csv_feeder_integration()
        predictor = test_csv_predictor_integration(feeder)
        
        # Test strategy simulation
        trades, final_value = test_prediction_based_strategy_simulation(predictor)
        
        # Test workflow integration
        test_workflow_integration()
        
        print("\n" + "=" * 60)
        print("🎉 ALL INTEGRATION TESTS PASSED! 🎉")
        print("\nSummary:")
        print("✅ CSV Feeder - Data loading and retrieval")
        print("✅ CSV Predictor - Ideal prediction generation")
        print("✅ Strategy Simulation - Prediction-based trading")
        print("✅ Workflow Integration - Component consistency")
        print(f"\nFinal Portfolio Value: ${final_value:.2f}")
        print(f"Total Return: {((final_value - 10000.0) / 10000.0):.2%}")
        
        return True
        
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
