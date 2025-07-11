#!/usr/bin/env python3
"""
LTS Prediction Provider Integration Demo

This demo showcases the complete integration between LTS and the prediction provider,
including CSV test mode for perfect predictions and strategy-based trading signals.

Features demonstrated:
1. CSV-based prediction provider for testing with perfect predictions
2. ML-based strategy using both short-term (1h transformer) and long-term (1d CNN) predictions
3. Trading signal generation with confidence levels and risk management
4. Backtesting capabilities for strategy evaluation
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
from plugins_strategy.prediction_strategy import PredictionBasedStrategy


class LTSPredictionDemo:
    """Demonstration class for LTS prediction provider integration."""
    
    def __init__(self):
        """Initialize the demo with configuration."""
        self.config = DEFAULT_VALUES.copy()
        
        # Configure for CSV test mode with perfect predictions
        self.config.update({
            'csv_test_mode': True,
            'csv_test_data_path': '/home/harveybc/Documents/GitHub/prediction_provider/examples/data/phase_3/base_d1.csv',
            'csv_test_lookahead': True,
            'confidence_threshold': 0.5,  # Lower threshold for demo
            'uncertainty_threshold': 0.1   # Higher tolerance for demo
        })
        
        self.prediction_client = PredictionProviderClient(self.config)
        self.strategy = PredictionBasedStrategy(self.config)
        self.strategy.set_prediction_client_config(self.config)
    
    async def demo_prediction_analysis(self):
        """Demonstrate prediction analysis for different market conditions."""
        print("🔮 Prediction Analysis Demo")
        print("=" * 50)
        
        # Test different time points to show various market conditions
        test_times = [
            ("2005-05-10T12:00:00", "Early morning - Low volatility"),
            ("2005-05-15T18:00:00", "Weekend approach - Moderate activity"),  
            ("2005-06-01T14:00:00", "Mid-month - High activity"),
            ("2005-07-20T09:00:00", "Summer trading - Moderate volatility")
        ]
        
        for datetime_str, description in test_times:
            print(f"\n📊 {description}")
            print(f"   Time: {datetime_str}")
            
            try:
                predictions = await self.prediction_client.get_predictions(
                    symbol="EURUSD",
                    datetime_str=datetime_str,
                    prediction_types=['short_term', 'long_term']
                )
                
                short_preds = predictions['predictions'].get('short_term', [])
                long_preds = predictions['predictions'].get('long_term', [])
                short_uncert = predictions['uncertainties'].get('short_term', [])
                long_uncert = predictions['uncertainties'].get('long_term', [])
                
                if short_preds and long_preds:
                    # Calculate trends
                    current_price = short_preds[0] if short_preds else 1.0
                    short_trend = ((short_preds[2] - current_price) / current_price * 100) if len(short_preds) > 2 else 0
                    long_trend = ((long_preds[2] - current_price) / current_price * 100) if len(long_preds) > 2 else 0
                    
                    print(f"   📈 Short-term trend (3h): {short_trend:+.3f}%")
                    print(f"   📈 Long-term trend (3d): {long_trend:+.3f}%")
                    print(f"   🎯 Avg uncertainty: {(sum(short_uncert[:3]) + sum(long_uncert[:3])) / 6:.4f}")
                    
                    # Determine market sentiment
                    if abs(short_trend) < 0.01 and abs(long_trend) < 0.01:
                        sentiment = "📊 Sideways/Consolidation"
                    elif short_trend > 0 and long_trend > 0:
                        sentiment = "🟢 Bullish (aligned trends)"
                    elif short_trend < 0 and long_trend < 0:
                        sentiment = "🔴 Bearish (aligned trends)"
                    else:
                        sentiment = "🟡 Mixed signals (divergent trends)"
                    
                    print(f"   {sentiment}")
                
            except Exception as e:
                print(f"   ❌ Error: {e}")
    
    async def demo_strategy_signals(self):
        """Demonstrate strategy signal generation."""
        print("\n\n🧠 Strategy Signal Generation Demo")
        print("=" * 50)
        
        # Test scenarios with different market conditions
        test_scenarios = [
            {
                "datetime": "2005-05-10T12:00:00",
                "description": "Quiet Market Conditions",
                "current_price": 1.2845,
                "portfolio": {"positions": {}, "max_position_size": 0.1, "available_capital": 10000}
            },
            {
                "datetime": "2005-06-15T16:00:00", 
                "description": "Active Trading Session",
                "current_price": 1.2234,
                "portfolio": {"positions": {"EURUSD": 0.02}, "max_position_size": 0.1, "available_capital": 8000}
            },
            {
                "datetime": "2005-08-20T10:00:00",
                "description": "High Volatility Period",
                "current_price": 1.2456,
                "portfolio": {"positions": {}, "max_position_size": 0.05, "available_capital": 15000}
            }
        ]
        
        for scenario in test_scenarios:
            print(f"\n🎯 {scenario['description']}")
            print(f"   Time: {scenario['datetime']}")
            print(f"   Price: {scenario['current_price']:.4f}")
            print(f"   Portfolio: {scenario['portfolio']['available_capital']} available")
            
            try:
                historical_data = [
                    {'timestamp': scenario['datetime'], 'close': scenario['current_price']},
                    {'timestamp': '2005-05-10T11:00:00', 'close': scenario['current_price'] * 0.999},
                    {'timestamp': '2005-05-10T10:00:00', 'close': scenario['current_price'] * 0.998}
                ]
                
                signal = await self.strategy.generate_signal(
                    symbol="EURUSD",
                    current_price=scenario['current_price'],
                    historical_data=historical_data,
                    portfolio_context=scenario['portfolio']
                )
                
                # Display signal details
                action_emoji = {"buy": "🟢", "sell": "🔴", "hold": "⏸️"}.get(signal.action, "❓")
                print(f"   {action_emoji} Action: {signal.action.upper()}")
                print(f"   🎯 Confidence: {signal.confidence:.3f}")
                print(f"   💰 Position Size: {signal.quantity:.4f}")
                print(f"   🧠 Reasoning: {signal.reasoning}")
                
                if signal.short_term_predictions:
                    short_change = ((signal.short_term_predictions[2] - scenario['current_price']) / scenario['current_price'] * 100) if len(signal.short_term_predictions) > 2 else 0
                    print(f"   📊 Expected 3h change: {short_change:+.3f}%")
                
                if signal.long_term_predictions:
                    long_change = ((signal.long_term_predictions[2] - scenario['current_price']) / scenario['current_price'] * 100) if len(signal.long_term_predictions) > 2 else 0
                    print(f"   📊 Expected 3d change: {long_change:+.3f}%")
                
            except Exception as e:
                print(f"   ❌ Signal generation failed: {e}")
    
    async def demo_backtest_capability(self):
        """Demonstrate backtesting capability."""
        print("\n\n📈 Backtesting Demo")
        print("=" * 50)
        
        # Simulate a series of historical signals
        backtest_times = [
            "2005-05-10T12:00:00",
            "2005-05-10T18:00:00", 
            "2005-05-11T12:00:00",
            "2005-05-11T18:00:00",
            "2005-05-12T12:00:00"
        ]
        
        portfolio_value = 10000
        position = 0
        trade_history = []
        
        print(f"🚀 Starting backtest with ${portfolio_value:,.2f}")
        print(f"📅 Period: {backtest_times[0]} to {backtest_times[-1]}")
        
        for i, test_time in enumerate(backtest_times):
            print(f"\n📊 Step {i+1}: {test_time}")
            
            try:
                # Get predictions for this time
                predictions = await self.prediction_client.get_predictions(
                    symbol="EURUSD",
                    datetime_str=test_time,
                    prediction_types=['short_term', 'long_term']
                )
                
                if predictions['status'] != 'success':
                    print(f"   ⚠️ No predictions available")
                    continue
                
                # Create mock historical data
                base_price = 1.2845 + (i * 0.001)  # Simulate price movement
                historical_data = [
                    {'timestamp': test_time, 'close': base_price}
                ]
                
                # Generate signal
                signal = await self.strategy.backtest_signal(
                    symbol="EURUSD",
                    datetime_str=test_time,
                    historical_data=historical_data
                )
                
                # Simulate trade execution
                if signal.action == "buy" and position <= 0:
                    position = signal.quantity
                    entry_price = base_price
                    trade_history.append({
                        'time': test_time,
                        'action': 'BUY',
                        'price': entry_price,
                        'quantity': position,
                        'confidence': signal.confidence
                    })
                    print(f"   🟢 BUY {position:.4f} at {entry_price:.4f} (confidence: {signal.confidence:.3f})")
                
                elif signal.action == "sell" and position >= 0:
                    if position > 0:
                        # Close existing position
                        exit_price = base_price
                        pnl = (exit_price - entry_price) * position * portfolio_value
                        portfolio_value += pnl
                        trade_history.append({
                            'time': test_time,
                            'action': 'SELL',
                            'price': exit_price,
                            'quantity': position,
                            'pnl': pnl,
                            'confidence': signal.confidence
                        })
                        print(f"   🔴 SELL {position:.4f} at {exit_price:.4f} (PnL: ${pnl:+.2f})")
                        position = 0
                    else:
                        # Open short position
                        position = -signal.quantity
                        entry_price = base_price
                        trade_history.append({
                            'time': test_time,
                            'action': 'SELL_SHORT',
                            'price': entry_price,
                            'quantity': abs(position),
                            'confidence': signal.confidence
                        })
                        print(f"   🔴 SELL SHORT {abs(position):.4f} at {entry_price:.4f}")
                
                else:
                    print(f"   ⏸️ HOLD (confidence: {signal.confidence:.3f})")
                
                print(f"   💼 Portfolio: ${portfolio_value:,.2f}, Position: {position:.4f}")
                
            except Exception as e:
                print(f"   ❌ Backtest step failed: {e}")
        
        # Backtest summary
        total_return = ((portfolio_value - 10000) / 10000) * 100
        print(f"\n🏁 Backtest Summary:")
        print(f"   💰 Final Portfolio Value: ${portfolio_value:,.2f}")
        print(f"   📊 Total Return: {total_return:+.2f}%")
        print(f"   📈 Total Trades: {len(trade_history)}")
        
        if trade_history:
            profitable_trades = sum(1 for trade in trade_history if trade.get('pnl', 0) > 0)
            print(f"   🎯 Win Rate: {(profitable_trades / len([t for t in trade_history if 'pnl' in t]) * 100):.1f}%" if len([t for t in trade_history if 'pnl' in t]) > 0 else "   🎯 Win Rate: N/A")
    
    async def demo_configuration_showcase(self):
        """Showcase configuration options and model information."""
        print("\n\n⚙️ Configuration & Model Information")
        print("=" * 50)
        
        # Show prediction provider configuration
        model_info = self.prediction_client.get_model_info()
        print(f"🔮 Prediction Provider Configuration:")
        print(f"   Mode: {'CSV Test Mode' if model_info['csv_test_mode'] else 'Live API Mode'}")
        print(f"   URL: {model_info['prediction_provider_url']}")
        
        print(f"\n📊 Short-term Model (1-6 hours):")
        short_model = model_info['short_term_model']
        print(f"   Predictor: {short_model.get('predictor_plugin', 'N/A')}")
        print(f"   Window Size: {short_model.get('window_size', 'N/A')}")
        print(f"   Interval: {short_model.get('interval', 'N/A')}")
        print(f"   Horizon: {short_model.get('prediction_horizon', 'N/A')} steps")
        
        print(f"\n📊 Long-term Model (1-6 days):")
        long_model = model_info['long_term_model']
        print(f"   Predictor: {long_model.get('predictor_plugin', 'N/A')}")
        print(f"   Window Size: {long_model.get('window_size', 'N/A')}")
        print(f"   Interval: {long_model.get('interval', 'N/A')}")
        print(f"   Horizon: {long_model.get('prediction_horizon', 'N/A')} steps")
        
        # Show strategy configuration
        strategy_params = self.strategy.get_strategy_parameters()
        print(f"\n🧠 Strategy Configuration:")
        print(f"   Short-term Weight: {strategy_params['short_term_weight']:.1f}")
        print(f"   Long-term Weight: {strategy_params['long_term_weight']:.1f}")
        print(f"   Confidence Threshold: {strategy_params['confidence_threshold']:.2f}")
        print(f"   Uncertainty Threshold: {strategy_params['uncertainty_threshold']:.3f}")
        print(f"   Base Position Size: {strategy_params['position_size_base']:.2f}")
        print(f"   Trend Alignment Required: {strategy_params['trend_alignment_required']}")
    
    async def run_complete_demo(self):
        """Run the complete demonstration."""
        print("🚀 LTS Prediction Provider Integration Demo")
        print("=" * 60)
        print("This demo showcases the integration between LTS and the prediction provider")
        print("using CSV test data for perfect predictions and ML-based trading strategies.")
        print("=" * 60)
        
        try:
            # Run all demo sections
            await self.demo_configuration_showcase()
            await self.demo_prediction_analysis()
            await self.demo_strategy_signals()
            await self.demo_backtest_capability()
            
            print("\n" + "=" * 60)
            print("🎉 Demo completed successfully!")
            print("\n📋 Next Steps:")
            print("   1. Replace CSV test mode with real prediction provider API")
            print("   2. Integrate with Backtrader for realistic broker simulation")
            print("   3. Connect to Oanda demo account for live testing")
            print("   4. Implement risk management and portfolio optimization")
            print("   5. Add performance monitoring and alerts")
            print("\n🎯 The foundation is ready for live trading!")
            
        except Exception as e:
            print(f"\n💥 Demo failed: {e}")
            import traceback
            traceback.print_exc()


async def main():
    """Main demo function."""
    demo = LTSPredictionDemo()
    await demo.run_complete_demo()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹️ Demo interrupted by user")
    except Exception as e:
        print(f"\n💥 Demo crashed: {e}")
        import traceback
        traceback.print_exc()
