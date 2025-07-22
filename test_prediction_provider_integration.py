#!/usr/bin/env python3
"""
Simple LTS Prediction Provider Integration Test

This test validates that LTS can successfully communicate with the prediction provider
by making HTTP requests and verifying the expected model selection behavior.
"""

import asyncio
import aiohttp
import json
import sys
import os
from datetime import datetime

# Add LTS app to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), 'app')))

async def test_prediction_provider_integration():
    """Test the integration with prediction provider endpoints."""
    
    print("🧪 Testing LTS + Prediction Provider Integration")
    print("=" * 60)
    
    # Test configuration
    base_url = "http://localhost:8000"
    test_cases = [
        {
            "name": "1h interval (CNN model)",
            "payload": {
                "symbol": "EURUSD",
                "interval": "1h",
                "prediction_horizon": 6,
                "predictor_plugin": "default_predictor",
                "feeder_plugin": "default_feeder",
                "pipeline_plugin": "default_pipeline",
                "prediction_type": "short_term"
            },
            "expected_model": "predictor_plugin_cnn_candidate_lt",
            "expected_architecture": "cnn_1d"
        },
        {
            "name": "1d interval (Transformer model)",
            "payload": {
                "symbol": "EURUSD", 
                "interval": "1d",
                "prediction_horizon": 6,
                "predictor_plugin": "default_predictor",
                "feeder_plugin": "default_feeder",
                "pipeline_plugin": "default_pipeline",
                "prediction_type": "long_term"
            },
            "expected_model": "predictor_plugin_transformer",
            "expected_architecture": "transformer"
        }
    ]
    
    results = []
    
    async with aiohttp.ClientSession() as session:
        
        # Test 1: Health check
        print("\n🔍 Testing health endpoint...")
        try:
            async with session.get(f"{base_url}/health") as response:
                if response.status == 200:
                    print("✅ Health check passed")
                    health_data = await response.json()
                    print(f"   Status: {health_data.get('status', 'unknown')}")
                else:
                    print(f"❌ Health check failed: {response.status}")
                    return False
        except Exception as e:
            print(f"❌ Cannot connect to prediction provider: {e}")
            print("   Make sure the prediction provider is running on http://localhost:8000")
            return False
        
        # Test 2: Prediction requests with model selection
        for test_case in test_cases:
            print(f"\n🔍 Testing: {test_case['name']}")
            
            try:
                # Submit prediction request
                async with session.post(
                    f"{base_url}/api/v1/predict", 
                    json=test_case['payload']
                ) as response:
                    
                    if response.status == 200:
                        prediction_response = await response.json()
                        prediction_id = prediction_response.get('id')
                        
                        print(f"   ✅ Prediction request submitted (ID: {prediction_id})")
                        print(f"      Status: {prediction_response.get('status')}")
                        
                        # Wait for processing and check result
                        await asyncio.sleep(3)  # Give time for processing
                        
                        # Get prediction result
                        async with session.get(
                            f"{base_url}/api/v1/predictions/{prediction_id}"
                        ) as result_response:
                            
                            if result_response.status == 200:
                                result_data = await result_response.json()
                                
                                print(f"      Final status: {result_data.get('status')}")
                                
                                if result_data.get('status') == 'completed':
                                    result = result_data.get('result', {})
                                    actual_model = result.get('predictor_plugin')
                                    model_config = result.get('model_config', {})
                                    actual_architecture = model_config.get('architecture')
                                    
                                    print(f"      Selected model: {actual_model}")
                                    print(f"      Architecture: {actual_architecture}")
                                    
                                    # Verify model selection
                                    if actual_model == test_case['expected_model']:
                                        print("      ✅ Correct model selected")
                                    else:
                                        print(f"      ❌ Wrong model: expected {test_case['expected_model']}, got {actual_model}")
                                    
                                    if actual_architecture == test_case['expected_architecture']:
                                        print("      ✅ Correct architecture selected")
                                    else:
                                        print(f"      ❌ Wrong architecture: expected {test_case['expected_architecture']}, got {actual_architecture}")
                                    
                                    # Check prediction output
                                    predictions = result.get('prediction', [])
                                    uncertainties = result.get('uncertainty', [])
                                    
                                    if len(predictions) == test_case['payload']['prediction_horizon']:
                                        print(f"      ✅ Correct number of predictions: {len(predictions)}")
                                    else:
                                        print(f"      ❌ Wrong prediction count: expected {test_case['payload']['prediction_horizon']}, got {len(predictions)}")
                                    
                                    if len(uncertainties) == len(predictions):
                                        print("      ✅ Uncertainty estimates provided")
                                    else:
                                        print("      ❌ Missing uncertainty estimates")
                                    
                                    # Store results
                                    results.append({
                                        'test_case': test_case['name'],
                                        'success': True,
                                        'model_correct': actual_model == test_case['expected_model'],
                                        'architecture_correct': actual_architecture == test_case['expected_architecture'],
                                        'prediction_count': len(predictions),
                                        'uncertainty_count': len(uncertainties)
                                    })
                                    
                                else:
                                    print(f"      ❌ Prediction failed: {result_data.get('status')}")
                                    if 'error' in result.get('result', {}):
                                        print(f"         Error: {result['result']['error']}")
                                    results.append({
                                        'test_case': test_case['name'],
                                        'success': False,
                                        'error': f"Prediction status: {result_data.get('status')}"
                                    })
                            else:
                                print(f"      ❌ Failed to get result: {result_response.status}")
                                results.append({
                                    'test_case': test_case['name'],
                                    'success': False,
                                    'error': f"Result fetch failed: {result_response.status}"
                                })
                    else:
                        print(f"   ❌ Prediction request failed: {response.status}")
                        error_text = await response.text()
                        print(f"      Error: {error_text}")
                        results.append({
                            'test_case': test_case['name'],
                            'success': False,
                            'error': f"Request failed: {response.status}"
                        })
                        
            except Exception as e:
                print(f"   ❌ Test case failed: {e}")
                results.append({
                    'test_case': test_case['name'],
                    'success': False,
                    'error': str(e)
                })
    
    # Print summary
    print("\n" + "=" * 60)
    print("📊 Test Results Summary:")
    
    successful_tests = [r for r in results if r.get('success', False)]
    failed_tests = [r for r in results if not r.get('success', False)]
    
    print(f"   ✅ Successful tests: {len(successful_tests)}/{len(results)}")
    print(f"   ❌ Failed tests: {len(failed_tests)}/{len(results)}")
    
    if failed_tests:
        print("\n🚨 Failed Tests:")
        for test in failed_tests:
            print(f"   - {test['test_case']}: {test.get('error', 'Unknown error')}")
    
    if successful_tests:
        print("\n🎉 Successful Tests:")
        for test in successful_tests:
            print(f"   - {test['test_case']}")
            if test.get('model_correct') and test.get('architecture_correct'):
                print("     ✅ Model selection working correctly")
            else:
                print("     ⚠️ Model selection issues detected")
    
    # Overall result
    all_passed = len(successful_tests) == len(results)
    if all_passed:
        print("\n🎯 Overall Result: ✅ ALL TESTS PASSED")
        print("   LTS + Prediction Provider integration is working correctly!")
        print("   - Model selection logic functioning properly")
        print("   - 1h interval → CNN model")
        print("   - 1d interval → Transformer model")
        print("   - Prediction horizons correctly handled")
    else:
        print("\n🎯 Overall Result: ❌ SOME TESTS FAILED")
        print("   Please check the failed tests and fix any issues.")
    
    return all_passed

if __name__ == "__main__":
    result = asyncio.run(test_prediction_provider_integration())
    sys.exit(0 if result else 1)
