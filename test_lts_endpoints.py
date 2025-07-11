#!/usr/bin/env python3
"""
Test LTS endpoints with running prediction provider
"""

import asyncio
import json
import httpx
import time
from datetime import datetime

async def test_prediction_endpoints():
    """Test the specific endpoints that LTS needs."""
    
    base_url = "http://127.0.0.1:8000"
    
    print("🔍 Testing LTS Required Endpoints")
    print("=" * 50)
    
    async with httpx.AsyncClient(timeout=30) as client:
        
        # Test 1: Health Check
        print("\n1. Testing Health Check...")
        try:
            response = await client.get(f"{base_url}/health")
            print(f"   Status: {response.status_code}")
            print(f"   Response: {response.text}")
        except Exception as e:
            print(f"   ❌ Health check failed: {e}")
        
        # Test 2: Short-term Prediction Request
        print("\n2. Testing Short-term Prediction Request...")
        short_term_data = {
            "symbol": "EURUSD",
            "prediction_type": "short_term",
            "predictor_plugin": "default_predictor",
            "feeder_plugin": "default_feeder", 
            "pipeline_plugin": "default_pipeline",
            "interval": "1h",
            "prediction_horizon": 6
        }
        
        try:
            response = await client.post(f"{base_url}/api/v1/predict", json=short_term_data)
            print(f"   Status: {response.status_code}")
            
            if response.status_code in [200, 201]:
                result = response.json()
                prediction_id = result.get('id')
                print(f"   ✅ Prediction created with ID: {prediction_id}")
                print(f"   Task ID: {result.get('task_id')}")
                print(f"   Status: {result.get('status')}")
                
                # Test 3: Check Prediction Status
                print(f"\n3. Testing Prediction Status Check (ID: {prediction_id})...")
                
                # Wait a moment and check status
                await asyncio.sleep(2)
                
                status_response = await client.get(f"{base_url}/api/v1/predictions/{prediction_id}")
                print(f"   Status: {status_response.status_code}")
                
                if status_response.status_code == 200:
                    status_result = status_response.json()
                    print(f"   Prediction Status: {status_result.get('status')}")
                    
                    if status_result.get('result'):
                        print(f"   ✅ Prediction Result Available!")
                        print(f"   Result: {json.dumps(status_result.get('result'), indent=2)}")
                    else:
                        print(f"   ⏳ Prediction still processing...")
                        
                        # Poll for completion (up to 30 seconds)
                        max_wait = 30
                        wait_time = 0
                        while wait_time < max_wait:
                            await asyncio.sleep(2)
                            wait_time += 2
                            
                            poll_response = await client.get(f"{base_url}/api/v1/predictions/{prediction_id}")
                            if poll_response.status_code == 200:
                                poll_result = poll_response.json()
                                if poll_result.get('status') == 'completed':
                                    print(f"   ✅ Prediction completed after {wait_time}s!")
                                    print(f"   Result: {json.dumps(poll_result.get('result'), indent=2)}")
                                    break
                                elif poll_result.get('status') == 'failed':
                                    print(f"   ❌ Prediction failed: {poll_result.get('error', 'Unknown error')}")
                                    break
                                else:
                                    print(f"   ⏳ Still processing... ({wait_time}s)")
                        else:
                            print(f"   ⚠️ Prediction timed out after {max_wait}s")
                
            else:
                print(f"   ❌ Failed to create prediction: {response.text}")
                
        except Exception as e:
            print(f"   ❌ Prediction request failed: {e}")
        
        # Test 4: Long-term Prediction Request
        print("\n4. Testing Long-term Prediction Request...")
        long_term_data = {
            "symbol": "EURUSD",
            "prediction_type": "long_term",
            "predictor_plugin": "default_predictor",
            "feeder_plugin": "default_feeder",
            "pipeline_plugin": "default_pipeline", 
            "interval": "1d",
            "prediction_horizon": 6
        }
        
        try:
            response = await client.post(f"{base_url}/api/v1/predict", json=long_term_data)
            print(f"   Status: {response.status_code}")
            
            if response.status_code in [200, 201]:
                result = response.json()
                print(f"   ✅ Long-term prediction created with ID: {result.get('id')}")
            else:
                print(f"   ❌ Failed: {response.text}")
                
        except Exception as e:
            print(f"   ❌ Long-term prediction failed: {e}")
        
        # Test 5: List Predictions
        print("\n5. Testing List Predictions...")
        try:
            response = await client.get(f"{base_url}/api/v1/predictions/")
            print(f"   Status: {response.status_code}")
            
            if response.status_code == 200:
                predictions = response.json()
                print(f"   ✅ Found {len(predictions)} predictions")
                for pred in predictions[-3:]:  # Show last 3
                    print(f"      ID: {pred.get('id')}, Status: {pred.get('status')}, Symbol: {pred.get('symbol')}")
            else:
                print(f"   ❌ Failed: {response.text}")
                
        except Exception as e:
            print(f"   ❌ List predictions failed: {e}")

    print("\n" + "=" * 50)
    print("✅ LTS Endpoint Testing Complete!")

if __name__ == "__main__":
    asyncio.run(test_prediction_endpoints())
