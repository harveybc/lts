#!/usr/bin/env python3
"""
Live API Integration Test for LTS + Prediction Provider

This script demonstrates the complete integration between LTS and the prediction provider
using both CSV test mode and live API mode.
"""

import asyncio
import subprocess
import sys
import time
import os
import json
import logging
from pathlib import Path

# Add LTS app to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), 'app')))

from app.config import DEFAULT_VALUES
from app.prediction_client import PredictionProviderClient

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class LiveAPIIntegrationTest:
    """Test the complete LTS + Prediction Provider integration."""
    
    def __init__(self):
        self.prediction_provider_process = None
        self.test_results = {
            'csv_mode': {},
            'api_mode': {},
            'comparison': {}
        }
    
    def start_prediction_provider(self):
        """Start the prediction provider service in the background."""
        logger.info("Starting prediction provider service...")
        
        # Change to prediction provider directory
        pp_dir = Path(__file__).parent.parent / "prediction_provider"
        if not pp_dir.exists():
            raise FileNotFoundError(f"Prediction provider directory not found: {pp_dir}")
        
        try:
            # Start the prediction provider
            self.prediction_provider_process = subprocess.Popen(
                [sys.executable, "-m", "app.main"],
                cwd=str(pp_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            # Wait for service to start
            logger.info("Waiting for prediction provider to start...")
            time.sleep(10)
            
            # Check if process is still running
            if self.prediction_provider_process.poll() is None:
                logger.info("Prediction provider started successfully")
                return True
            else:
                stdout, stderr = self.prediction_provider_process.communicate()
                logger.error(f"Prediction provider failed to start. STDOUT: {stdout.decode()}, STDERR: {stderr.decode()}")
                return False
                
        except Exception as e:
            logger.error(f"Failed to start prediction provider: {e}")
            return False
    
    def stop_prediction_provider(self):
        """Stop the prediction provider service."""
        if self.prediction_provider_process:
            logger.info("Stopping prediction provider service...")
            self.prediction_provider_process.terminate()
            self.prediction_provider_process.wait()
            logger.info("Prediction provider stopped")
    
    async def test_csv_mode(self):
        """Test LTS with CSV test mode."""
        logger.info("=== Testing CSV Mode ===")
        
        try:
            # Configuration for CSV mode
            config = DEFAULT_VALUES.copy()
            config.update({
                'csv_test_mode': True,
                'csv_test_data_path': '../prediction_provider/examples/data/phase_3/base_d1.csv',
                'csv_test_lookahead': True,
                'prediction_provider_url': 'http://localhost:8000',
                'short_term_model': {
                    'predictor_plugin': 'transformer_predictor',
                    'interval': '1h',
                    'prediction_horizon': 6,
                    'lookback_ticks': 1000
                },
                'long_term_model': {
                    'predictor_plugin': 'cnn_predictor',
                    'interval': '1d',
                    'prediction_horizon': 6,
                    'lookback_ticks': 1000
                }
            })
            
            # Create prediction client
            client = PredictionProviderClient(config.config)
            
            # Test prediction request
            test_datetime = "2023-01-15T10:00:00"
            predictions = await client.get_predictions(
                symbol="EURUSD",
                datetime_str=test_datetime,
                prediction_types=['short_term', 'long_term']
            )
            
            self.test_results['csv_mode'] = {
                'status': 'success',
                'predictions': predictions,
                'test_datetime': test_datetime
            }
            
            logger.info(f"CSV mode test successful:")
            logger.info(f"  Short-term predictions: {len(predictions['predictions'].get('short_term', []))}")
            logger.info(f"  Long-term predictions: {len(predictions['predictions'].get('long_term', []))}")
            logger.info(f"  Historical context: {predictions.get('historical_context', {}).get('count', 0)} ticks")
            
        except Exception as e:
            logger.error(f"CSV mode test failed: {e}")
            self.test_results['csv_mode'] = {'status': 'failed', 'error': str(e)}
    
    async def test_api_mode(self):
        """Test LTS with live API mode."""
        logger.info("=== Testing Live API Mode ===")
        
        try:
            # Configuration for API mode
            config = Config()
            config.config.update({
                'csv_test_mode': False,
                'prediction_provider_url': 'http://localhost:8000',
                'prediction_provider_timeout': 120,
                'prediction_provider_retries': 3,
                'short_term_model': {
                    'predictor_plugin': 'default_predictor',
                    'feeder_plugin': 'default_feeder',
                    'pipeline_plugin': 'default_pipeline',
                    'interval': '1h',
                    'prediction_horizon': 6
                },
                'long_term_model': {
                    'predictor_plugin': 'default_predictor',
                    'feeder_plugin': 'default_feeder',
                    'pipeline_plugin': 'default_pipeline',
                    'interval': '1d',
                    'prediction_horizon': 6
                }
            })
            
            # Create prediction client
            client = PredictionProviderClient(config.config)
            
            # Test prediction request
            test_datetime = "2023-01-15T10:00:00"
            predictions = await client.get_predictions(
                symbol="EURUSD",
                datetime_str=test_datetime,
                prediction_types=['short_term', 'long_term']
            )
            
            self.test_results['api_mode'] = {
                'status': 'success',
                'predictions': predictions,
                'test_datetime': test_datetime
            }
            
            logger.info(f"API mode test successful:")
            logger.info(f"  Short-term predictions: {len(predictions['predictions'].get('short_term', []))}")
            logger.info(f"  Long-term predictions: {len(predictions['predictions'].get('long_term', []))}")
            logger.info(f"  Response source: {predictions.get('source', 'unknown')}")
            
        except Exception as e:
            logger.error(f"API mode test failed: {e}")
            self.test_results['api_mode'] = {'status': 'failed', 'error': str(e)}
    
    def compare_results(self):
        """Compare CSV and API mode results."""
        logger.info("=== Comparing Results ===")
        
        csv_success = self.test_results['csv_mode'].get('status') == 'success'
        api_success = self.test_results['api_mode'].get('status') == 'success'
        
        comparison = {
            'csv_mode_success': csv_success,
            'api_mode_success': api_success,
            'both_successful': csv_success and api_success
        }
        
        if csv_success and api_success:
            csv_preds = self.test_results['csv_mode']['predictions']
            api_preds = self.test_results['api_mode']['predictions']
            
            comparison.update({
                'csv_short_term_count': len(csv_preds['predictions'].get('short_term', [])),
                'api_short_term_count': len(api_preds['predictions'].get('short_term', [])),
                'csv_long_term_count': len(csv_preds['predictions'].get('long_term', [])),
                'api_long_term_count': len(api_preds['predictions'].get('long_term', [])),
                'csv_source': csv_preds.get('source'),
                'api_source': api_preds.get('source')
            })
        
        self.test_results['comparison'] = comparison
        
        logger.info(f"Comparison results:")
        logger.info(f"  CSV mode successful: {csv_success}")
        logger.info(f"  API mode successful: {api_success}")
        logger.info(f"  Both modes successful: {comparison['both_successful']}")
    
    def save_results(self):
        """Save test results to file."""
        results_file = "live_api_integration_results.json"
        with open(results_file, 'w') as f:
            json.dump(self.test_results, f, indent=2, default=str)
        logger.info(f"Test results saved to {results_file}")
    
    async def run_all_tests(self):
        """Run all integration tests."""
        logger.info("Starting Live API Integration Tests")
        
        try:
            # Test CSV mode first (doesn't require prediction provider)
            await self.test_csv_mode()
            
            # Start prediction provider for API tests
            if self.start_prediction_provider():
                # Test API mode
                await self.test_api_mode()
            else:
                logger.error("Could not start prediction provider - skipping API tests")
                self.test_results['api_mode'] = {
                    'status': 'failed', 
                    'error': 'Could not start prediction provider service'
                }
            
            # Compare results
            self.compare_results()
            
            # Save results
            self.save_results()
            
        except Exception as e:
            logger.error(f"Test execution failed: {e}")
        finally:
            # Clean up
            self.stop_prediction_provider()

async def main():
    """Main test execution."""
    test = LiveAPIIntegrationTest()
    await test.run_all_tests()
    
    # Print summary
    logger.info("=== Test Summary ===")
    if test.test_results['comparison'].get('both_successful'):
        logger.info("✅ All tests passed! LTS integration with prediction provider is working.")
    else:
        logger.warning("⚠️ Some tests failed. Check the results for details.")
        if test.test_results['csv_mode'].get('status') == 'success':
            logger.info("✅ CSV mode working correctly")
        else:
            logger.error("❌ CSV mode failed")
        
        if test.test_results['api_mode'].get('status') == 'success':
            logger.info("✅ API mode working correctly")
        else:
            logger.error("❌ API mode failed")

if __name__ == "__main__":
    asyncio.run(main())
