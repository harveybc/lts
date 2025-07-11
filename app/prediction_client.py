"""
Prediction Provider Client for LTS

This module provides a client interface to communicate with the prediction provider service,
handling both real prediction requests and CSV-based test mode for perfect predictions.
"""

import json
import logging
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
import asyncio
import httpx
from pathlib import Path


class PredictionProviderClient:
    """
    Client for communicating with the prediction provider service.
    
    Supports both real prediction API calls and CSV test mode for perfect predictions.
    """
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the prediction provider client.
        
        Args:
            config: Configuration dictionary with prediction provider settings
        """
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Prediction provider settings
        self.base_url = config.get("prediction_provider_url", "http://localhost:8000")
        self.api_key = config.get("prediction_provider_api_key")
        self.timeout = config.get("prediction_provider_timeout", 300)
        self.retries = config.get("prediction_provider_retries", 3)
        self.retry_delay = config.get("prediction_provider_retry_delay", 5)
        
        # Model configurations
        self.short_term_config = config.get("short_term_model", {})
        self.long_term_config = config.get("long_term_model", {})
        
        # CSV test mode settings
        self.csv_test_mode = config.get("csv_test_mode", False)
        self.csv_test_data_path = config.get("csv_test_data_path")
        self.csv_test_lookahead = config.get("csv_test_lookahead", True)
        self.csv_data = None
        
        # Load CSV data if in test mode
        if self.csv_test_mode and self.csv_test_data_path:
            self._load_csv_data()
    
    def _load_csv_data(self):
        """Load CSV data for test mode."""
        try:
            csv_path = Path(self.csv_test_data_path)
            if csv_path.exists():
                self.csv_data = pd.read_csv(csv_path)
                self.csv_data['DATE_TIME'] = pd.to_datetime(self.csv_data['DATE_TIME'])
                self.csv_data.set_index('DATE_TIME', inplace=True)
                self.logger.info(f"Loaded CSV test data from {csv_path} with {len(self.csv_data)} rows")
            else:
                self.logger.error(f"CSV test data file not found: {csv_path}")
                self.csv_test_mode = False
        except Exception as e:
            self.logger.error(f"Failed to load CSV test data: {e}")
            self.csv_test_mode = False
    
    async def get_predictions(self, symbol: str, datetime_str: str, 
                            prediction_types: List[str] = None) -> Dict[str, Any]:
        """
        Get predictions for a symbol at a specific datetime.
        
        Args:
            symbol: Trading symbol (e.g., 'EURUSD')
            datetime_str: ISO datetime string for prediction time
            prediction_types: List of prediction types ['short_term', 'long_term']
            
        Returns:
            Dictionary containing predictions and uncertainties for both short and long term
        """
        if prediction_types is None:
            prediction_types = ['short_term', 'long_term']
        
        if self.csv_test_mode:
            return await self._get_csv_predictions(symbol, datetime_str, prediction_types)
        else:
            return await self._get_api_predictions(symbol, datetime_str, prediction_types)
    
    async def _get_csv_predictions(self, symbol: str, datetime_str: str, 
                                 prediction_types: List[str]) -> Dict[str, Any]:
        """
        Generate perfect predictions from CSV data (test mode).
        
        Args:
            symbol: Trading symbol
            datetime_str: ISO datetime string
            prediction_types: List of prediction types
            
        Returns:
            Dictionary with perfect predictions based on future CSV data
        """
        if self.csv_data is None:
            raise ValueError("CSV data not loaded for test mode")
        
        try:
            query_time = pd.to_datetime(datetime_str)
            
            # Find the closest timestamp in the data
            closest_idx = self.csv_data.index.get_indexer([query_time], method='nearest')[0]
            
            result = {
                'symbol': symbol,
                'datetime': datetime_str,
                'predictions': {},
                'uncertainties': {},
                'status': 'success',
                'source': 'csv_test_mode'
            }
            
            # Generate short-term predictions (1-6 hours)
            if 'short_term' in prediction_types:
                short_predictions = []
                short_uncertainties = []
                
                for h in range(1, 7):  # 1 to 6 hours
                    future_idx = closest_idx + h
                    if future_idx < len(self.csv_data):
                        future_price = self.csv_data.iloc[future_idx]['CLOSE']
                        current_price = self.csv_data.iloc[closest_idx]['CLOSE']
                        
                        if self.csv_test_lookahead:
                            # Perfect prediction: actual future price
                            prediction = future_price
                            uncertainty = 0.001  # Very low uncertainty for perfect predictions
                        else:
                            # Imperfect prediction: add some noise
                            import random
                            noise_factor = random.uniform(-0.002, 0.002)
                            prediction = future_price * (1 + noise_factor)
                            uncertainty = abs(future_price - prediction) * 2
                        
                        short_predictions.append(prediction)
                        short_uncertainties.append(uncertainty)
                    else:
                        # No future data available, use trend extrapolation
                        if len(short_predictions) > 0:
                            last_pred = short_predictions[-1]
                            short_predictions.append(last_pred)
                            short_uncertainties.append(0.01)
                        else:
                            current_price = self.csv_data.iloc[closest_idx]['CLOSE']
                            short_predictions.append(current_price)
                            short_uncertainties.append(0.01)
                
                result['predictions']['short_term'] = short_predictions
                result['uncertainties']['short_term'] = short_uncertainties
            
            # Generate long-term predictions (1-6 days)
            if 'long_term' in prediction_types:
                long_predictions = []
                long_uncertainties = []
                
                for d in range(1, 7):  # 1 to 6 days
                    future_idx = closest_idx + (d * 24)  # Assuming hourly data
                    if future_idx < len(self.csv_data):
                        future_price = self.csv_data.iloc[future_idx]['CLOSE']
                        current_price = self.csv_data.iloc[closest_idx]['CLOSE']
                        
                        if self.csv_test_lookahead:
                            # Perfect prediction: actual future price
                            prediction = future_price
                            uncertainty = 0.002  # Low uncertainty for perfect predictions
                        else:
                            # Imperfect prediction: add some noise
                            import random
                            noise_factor = random.uniform(-0.005, 0.005)
                            prediction = future_price * (1 + noise_factor)
                            uncertainty = abs(future_price - prediction) * 3
                        
                        long_predictions.append(prediction)
                        long_uncertainties.append(uncertainty)
                    else:
                        # No future data available, use trend extrapolation
                        if len(long_predictions) > 0:
                            last_pred = long_predictions[-1]
                            long_predictions.append(last_pred)
                            long_uncertainties.append(0.02)
                        else:
                            current_price = self.csv_data.iloc[closest_idx]['CLOSE']
                            long_predictions.append(current_price)
                            long_uncertainties.append(0.02)
                
                result['predictions']['long_term'] = long_predictions
                result['uncertainties']['long_term'] = long_uncertainties
            
            # Add historical context (last 1k ticks)
            start_idx = max(0, closest_idx - 1000)
            end_idx = closest_idx + 1
            historical_data = self.csv_data.iloc[start_idx:end_idx]
            
            result['historical_context'] = {
                'data': historical_data[['OPEN', 'HIGH', 'LOW', 'CLOSE']].to_dict('records'),
                'timestamps': [t.isoformat() for t in historical_data.index],
                'count': len(historical_data)
            }
            
            self.logger.info(f"Generated CSV test predictions for {symbol} at {datetime_str}")
            return result
            
        except Exception as e:
            self.logger.error(f"Error generating CSV predictions: {e}")
            raise
    
    async def _get_api_predictions(self, symbol: str, datetime_str: str, 
                                 prediction_types: List[str]) -> Dict[str, Any]:
        """
        Get predictions from the prediction provider API.
        
        Args:
            symbol: Trading symbol
            datetime_str: ISO datetime string
            prediction_types: List of prediction types
            
        Returns:
            Dictionary with API predictions
        """
        results = {
            'symbol': symbol,
            'datetime': datetime_str,
            'predictions': {},
            'uncertainties': {},
            'status': 'success',
            'source': 'api'
        }
        
        # Make API requests for each prediction type
        for pred_type in prediction_types:
            try:
                prediction_data = await self._make_prediction_request(symbol, datetime_str, pred_type)
                results['predictions'][pred_type] = prediction_data.get('predictions', [])
                results['uncertainties'][pred_type] = prediction_data.get('uncertainties', [])
            except Exception as e:
                self.logger.error(f"Failed to get {pred_type} predictions: {e}")
                results['predictions'][pred_type] = []
                results['uncertainties'][pred_type] = []
                results['status'] = 'partial_failure'
        
        return results
    
    async def _make_prediction_request(self, symbol: str, datetime_str: str, 
                                     prediction_type: str) -> Dict[str, Any]:
        """
        Make a single prediction request to the API.
        
        Args:
            symbol: Trading symbol
            datetime_str: ISO datetime string
            prediction_type: Type of prediction ('short_term' or 'long_term')
            
        Returns:
            API response data
        """
        model_config = self.short_term_config if prediction_type == 'short_term' else self.long_term_config
        
        request_data = {
            'symbol': symbol,
            'prediction_type': prediction_type,
            'predictor_plugin': model_config.get('predictor_plugin', 'default_predictor'),
            'feeder_plugin': model_config.get('feeder_plugin', 'default_feeder'),
            'pipeline_plugin': model_config.get('pipeline_plugin', 'default_pipeline'),
            'interval': model_config.get('interval', '1h' if prediction_type == 'short_term' else '1d'),
            'prediction_horizon': model_config.get('prediction_horizon', 6)
        }
        
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['X-API-KEY'] = self.api_key
        
        prediction_response = None
        for attempt in range(self.retries):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    # Create prediction request
                    response = await client.post(
                        f"{self.base_url}/api/v1/predict",
                        json=request_data,
                        headers=headers
                    )
                    if response.status_code in [200, 201]:
                        prediction_response = response.json()
                        break
                    else:
                        error_text = response.text
                        raise Exception(f"API request failed with status {response.status_code}: {error_text}")
            
            except Exception as e:
                self.logger.warning(f"Prediction request attempt {attempt + 1} failed: {e}")
                if attempt < self.retries - 1:
                    await asyncio.sleep(self.retry_delay)
                else:
                    raise
        
        if not prediction_response:
            raise Exception("Failed to create prediction after all retries")
        
        # Get the prediction ID
        prediction_id = prediction_response.get('id')
        if not prediction_id:
            raise Exception("No prediction ID returned from API")
        
        # Poll for completion
        max_wait_time = self.timeout
        poll_interval = 2
        total_wait = 0
        
        while total_wait < max_wait_time:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    status_response = await client.get(
                        f"{self.base_url}/api/v1/predictions/{prediction_id}",
                        headers=headers
                    )
                    if status_response.status_code == 200:
                        status_data = status_response.json()
                        if status_data.get('status') == 'completed':
                            # Extract predictions from result
                            result = status_data.get('result', {})
                            predictions = result.get('prediction', [])
                            
                            # Convert single prediction to list if needed
                            if not isinstance(predictions, list):
                                predictions = [predictions] if predictions is not None else []
                            
                            # For now, create dummy uncertainties based on prediction variance
                            if predictions:
                                avg_pred = sum(predictions) / len(predictions)
                                uncertainties = [abs(p - avg_pred) * 0.1 for p in predictions]
                            else:
                                uncertainties = []
                            
                            return {
                                'predictions': predictions,
                                'uncertainties': uncertainties,
                                'status': 'completed',
                                'full_response': status_data
                            }
                        elif status_data.get('status') == 'failed':
                            raise Exception(f"Prediction failed: {status_data.get('error', 'Unknown error')}")
                        else:
                            # Still processing, continue waiting
                            await asyncio.sleep(poll_interval)
                            total_wait += poll_interval
                    else:
                        raise Exception(f"Status check failed with status {status_response.status_code}: {status_response.text}")
            except Exception as e:
                self.logger.warning(f"Status check attempt failed: {e}")
                await asyncio.sleep(poll_interval)
                total_wait += poll_interval
        
        raise Exception(f"Prediction timed out after {max_wait_time} seconds")
    
    def get_model_info(self) -> Dict[str, Any]:
        """Get information about configured models."""
        return {
            'short_term_model': self.short_term_config,
            'long_term_model': self.long_term_config,
            'csv_test_mode': self.csv_test_mode,
            'prediction_provider_url': self.base_url
        }
