#!/usr/bin/env python3
"""
Integration Test Script for LTS Prediction Provider Integration

This script tests the integration between LTS and the prediction provider,
including CSV test mode and strategy signal generation.
"""

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add the LTS app to the Python path
sys.path.insert(0, '/home/harveybc/Documents/GitHub/lts')

from app.config import DEFAULT_VALUES
from app.prediction_client import PredictionProviderClient


async def test_csv_mode():
    """Test CSV-based prediction mode"""
    print("🧪 Testing CSV Prediction Mode...")
    
    # Configure for CSV test mode
    config = DEFAULT_VALUES.copy()
    config.update({
        'csv_test_mode': True,
        'csv_test_data_path': '/home/harveybc/Documents/GitHub/prediction_provider/examples/data/phase_3/base_d1.csv',
        'csv_test_lookahead': True
    })
    
    # Create prediction client
    client = PredictionProviderClient(config)
    
    # Test prediction for a specific time
    test_datetime = "2005-05-10T12:00:00"
    
    try:
        predictions = await client.get_predictions(
            symbol="EURUSD",
            datetime_str=test_datetime,
            prediction_types=['short_term', 'long_term']
        )
        
        print(f"✅ CSV Predictions successful for {test_datetime}")
        print(f"   Status: {predictions['status']}")
        print(f"   Source: {predictions['source']}")
        print(f"   Short-term predictions: {len(predictions['predictions'].get('short_term', []))} values")
        print(f"   Long-term predictions: {len(predictions['predictions'].get('long_term', []))} values")
        
        # Show first few predictions
        if predictions['predictions'].get('short_term'):
            short_preds = predictions['predictions']['short_term'][:3]
            print(f"   First 3 short-term: {[f'{p:.5f}' for p in short_preds]}")
        
        if predictions['predictions'].get('long_term'):
            long_preds = predictions['predictions']['long_term'][:3]
            print(f"   First 3 long-term: {[f'{p:.5f}' for p in long_preds]}")
        
        return True
        
    except Exception as e:
        print(f"❌ CSV Predictions failed: {e}")
        return False


async def test_strategy_integration():
    """Test strategy integration with predictions"""
    print("\n🧪 Testing Strategy Integration...")
    
    try:
        from plugins_strategy.prediction_strategy import PredictionBasedStrategy
        
        # Configure for CSV test mode
        config = DEFAULT_VALUES.copy()
        config.update({
            'csv_test_mode': True,
            'csv_test_data_path': '/home/harveybc/Documents/GitHub/prediction_provider/examples/data/phase_3/base_d1.csv',
            'csv_test_lookahead': True,
            'confidence_threshold': 0.5,
            'uncertainty_threshold': 0.1
        })
        
        # Initialize strategy
        strategy = PredictionBasedStrategy(config)
        strategy.set_prediction_client_config(config)
        
        # Test signal generation
        current_price = 1.2845
        historical_data = [
            {'timestamp': '2005-05-10T12:00:00', 'close': 1.2845},
            {'timestamp': '2005-05-10T11:00:00', 'close': 1.2840},
            {'timestamp': '2005-05-10T10:00:00', 'close': 1.2838}
        ]
        portfolio_context = {
            'positions': {},
            'max_position_size': 0.1,
            'available_capital': 10000
        }
        
        signal = await strategy.generate_signal(
            symbol="EURUSD",
            current_price=current_price,
            historical_data=historical_data,
            portfolio_context=portfolio_context
        )
        
        print(f"✅ Strategy signal generated successfully")
        print(f"   Action: {signal.action}")
        print(f"   Confidence: {signal.confidence:.3f}")
        print(f"   Quantity: {signal.quantity:.4f}")
        print(f"   Reasoning: {signal.reasoning}")
        print(f"   Short-term predictions: {len(signal.short_term_predictions)} values")
        print(f"   Long-term predictions: {len(signal.long_term_predictions)} values")
        
        return True
        
    except Exception as e:
        print(f"❌ Strategy integration failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_model_configurations():
    """Test model configuration setup"""
    print("\n🧪 Testing Model Configurations...")
    
    config = DEFAULT_VALUES.copy()
    
    # Check short-term model config
    short_config = config.get('short_term_model', {})
    print(f"✅ Short-term model config:")
    print(f"   Predictor: {short_config.get('predictor_plugin', 'N/A')}")
    print(f"   Window size: {short_config.get('window_size', 'N/A')}")
    print(f"   Prediction horizon: {short_config.get('prediction_horizon', 'N/A')}")
    print(f"   Interval: {short_config.get('interval', 'N/A')}")
    
    # Check long-term model config
    long_config = config.get('long_term_model', {})
    print(f"✅ Long-term model config:")
    print(f"   Predictor: {long_config.get('predictor_plugin', 'N/A')}")
    print(f"   Window size: {long_config.get('window_size', 'N/A')}")
    print(f"   Prediction horizon: {long_config.get('prediction_horizon', 'N/A')}")
    print(f"   Interval: {long_config.get('interval', 'N/A')}")
    
    return True


async def test_csv_data_availability():
    """Test if CSV test data is available"""
    print("\n🧪 Testing CSV Data Availability...")
    
    csv_path = Path('/home/harveybc/Documents/GitHub/prediction_provider/examples/data/phase_3/base_d1.csv')
    
    if csv_path.exists():
        import pandas as pd
        try:
            df = pd.read_csv(csv_path)
            print(f"✅ CSV data loaded successfully")
            print(f"   File: {csv_path}")
            print(f"   Rows: {len(df)}")
            print(f"   Columns: {list(df.columns)}")
            print(f"   Date range: {df['DATE_TIME'].iloc[0]} to {df['DATE_TIME'].iloc[-1]}")
            return True
        except Exception as e:
            print(f"❌ Failed to load CSV data: {e}")
            return False
    else:
        print(f"❌ CSV file not found: {csv_path}")
        return False


async def main():
    """Run all integration tests"""
    print("🚀 Starting LTS Prediction Provider Integration Tests")
    print("=" * 60)
    
    tests = [
        ("CSV Data Availability", test_csv_data_availability()),
        ("Model Configurations", test_model_configurations()),
        ("CSV Prediction Mode", test_csv_mode()),
        ("Strategy Integration", test_strategy_integration())
    ]
    
    results = []
    for test_name, test_coro in tests:
        try:
            result = await test_coro
            results.append((test_name, result))
        except Exception as e:
            print(f"❌ Test '{test_name}' crashed: {e}")
            results.append((test_name, False))
    
    # Summary
    print("\n" + "=" * 60)
    print("📊 Test Results Summary:")
    passed = 0
    for test_name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"   {status}: {test_name}")
        if result:
            passed += 1
    
    print(f"\n🎯 Overall: {passed}/{len(results)} tests passed")
    
    if passed == len(results):
        print("🎉 All tests passed! Integration is ready.")
    else:
        print("⚠️  Some tests failed. Please check the issues above.")
    
    return passed == len(results)


if __name__ == "__main__":
    try:
        success = asyncio.run(main())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n⏹️  Tests interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 Test suite crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
