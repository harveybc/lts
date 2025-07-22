#!/usr/bin/env python3

import sys
import os
from datetime import datetime
from unittest.mock import Mock, patch

# Add LTS directory to path
lts_path = '/home/harveybc/Documents/GitHub/lts'
sys.path.insert(0, lts_path)

# Add prediction_provider path for CSV predictor access
pp_path = '/home/harveybc/Documents/GitHub/prediction_provider'
sys.path.insert(0, pp_path)

try:
    print("Testing Backtrader Broker Plugin...")
    
    # Import the broker plugin
    from plugins_broker.backtrader_broker import BacktraderBroker
    print("✓ Import successful")
    
    # Test configuration for CSV mode
    config = {
        'prediction_source': 'csv',
        'csv_file': '/home/harveybc/Documents/GitHub/prediction_provider/examples/data/phase_3/base_d6.csv',
        'initial_cash': 10000.0,
        'commission': 0.001,
        'prediction_horizons': ['1h', '1d']
    }
    
    print("Creating backtrader broker...")
    with patch('plugins_broker.backtrader_broker.BacktraderBroker._init_csv_predictor'):
        broker = BacktraderBroker(config)
        print("✓ Backtrader Broker created successfully")
    
    print(f"✓ Prediction source: {broker.prediction_source}")
    print(f"✓ Initial cash: {broker.config['initial_cash']}")
    
    # Test broker info
    with patch.object(broker, 'getcash', return_value=9500.0):
        with patch.object(broker, 'getvalue', return_value=10200.0):
            with patch.object(broker, 'positions', [Mock(size=100), Mock(size=0)]):
                info = broker.get_broker_info()
                print("✓ Broker info retrieved")
                print(f"  - Broker type: {info.get('broker_type', 'N/A')}")
                print(f"  - Current cash: {info.get('current_cash', 'N/A')}")
                print(f"  - Current value: {info.get('current_value', 'N/A')}")
    
    # Test prediction source switching
    print("\nTesting prediction source switching...")
    broker.switch_prediction_source('api', {'api_url': 'http://localhost:8001'})
    print(f"✓ Switched to API source: {broker.prediction_source}")
    
    broker.switch_prediction_source('csv', {'csv_file': config['csv_file']})
    print(f"✓ Switched back to CSV source: {broker.prediction_source}")
    
    # Test performance metrics (empty state)
    metrics = broker.get_performance_metrics()
    print("✓ Performance metrics retrieved")
    print(f"  - Initial value: {metrics['performance']['initial_value']}")
    
    print("\nAll tests passed!")
    
except Exception as e:
    print(f"✗ Error: {e}")
    import traceback
    traceback.print_exc()
