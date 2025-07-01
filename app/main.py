#!/usr/bin/env python3
"""
main.py

Punto de entrada de la aplicación LTS (Live Trading System). Este script orquesta:
    - La carga y fusión de configuraciones (CLI, archivos locales y remotos).
    - La inicialización de los plugins: Pipeline, Strategy, Broker y Portfolio.
    - La selección entre ejecutar la optimización de parámetros o ejecutar el trading directamente.
    - El guardado de la configuración resultante de forma local y/o remota.
"""

import sys
import json
import pandas as pd
from typing import Any, Dict

from app.config_handler import (
    load_config,
    save_config,
    remote_load_config,
    remote_save_config,
    remote_log
)
from app.cli import parse_args
from app.config import DEFAULT_VALUES
from app.plugin_loader import load_plugin
from config_merger import merge_config, process_unknown_args

# Se asume que los siguientes plugins se cargan desde sus respectivos namespaces:
# - pipeline.plugins
# - strategy.plugins
# - broker.plugins
# - portfolio.plugins

def main():
    """
    Orquesta la ejecución completa del sistema LTS, incluyendo la optimización (si se configura)
    y la ejecución del pipeline completo de trading (pipeline, estrategia, broker y portfolio management).
    """
    print("Parsing initial arguments...")
    args, unknown_args = parse_args()
    cli_args: Dict[str, Any] = vars(args)

    print("Loading default configuration...")
    config: Dict[str, Any] = DEFAULT_VALUES.copy()

    file_config: Dict[str, Any] = {}
    # Carga remota de configuración si se solicita
    if args.remote_load_config:
        try:
            file_config = remote_load_config(args.remote_load_config, args.username, args.password)
            print(f"Loaded remote config: {file_config}")
        except Exception as e:
            print(f"Failed to load remote configuration: {e}")
            sys.exit(1)

    # Carga local de configuración si se solicita
    if args.load_config:
        try:
            file_config = load_config(args.load_config)
            print(f"Loaded local config: {file_config}")
        except Exception as e:
            print(f"Failed to load local configuration: {e}")
            sys.exit(1)

    # Primera fusión de la configuración (sin parámetros específicos de plugins)
    print("Merging configuration with CLI arguments and unknown args (first pass, no plugin params)...")
    unknown_args_dict = process_unknown_args(unknown_args)
    config = merge_config(config, {}, {}, file_config, cli_args, unknown_args_dict)

    # Selección del plugins
    if not cli_args.get('pipeline_plugin'):
        cli_args['pipeline_plugin'] = config.get('pipeline_plugin', 'default_pipeline')
    plugin_name = config.get('pipeline_plugin', 'default_pipeline')
    
    
    # --- CARGA DE PLUGINS ---
    # Carga del Pipeline Plugin
    print(f"Loading Pipeline Plugin: {plugin_name}")
    try:
        pipeline_class, _ = load_plugin('pipeline.plugins', plugin_name)
        pipeline_plugin = pipeline_class(config)
        pipeline_plugin.set_params(**config)
    except Exception as e:
        print(f"Failed to load or initialize Pipeline Plugin '{plugin_name}': {e}")
        sys.exit(1)

    # Carga del Strategy Plugin
    # Selección del plugin si no se especifica
    plugin_name = config.get('strategy_plugin', 'default_strategy')
    print(f"Loading Plugin ..{plugin_name}")

    try:
        strategy_class, _ = load_plugin('strategy.plugins', plugin_name)
        strategy_plugin = strategy_class(config)
        strategy_plugin.set_params(**config)
    except Exception as e:
        print(f"Failed to load or initialize Strategy Plugin: {e}")
        sys.exit(1)

    # Carga del Broker Plugin
    plugin_name = config.get('broker_plugin', 'default_broker')
    print(f"Loading Plugin ..{plugin_name}")
    try:
        broker_class, _ = load_plugin('broker.plugins', plugin_name)
        broker_plugin = broker_class(config)
        broker_plugin.set_params(**config)
    except Exception as e:
        print(f"Failed to load or initialize Broker Plugin: {e}")
        sys.exit(1)

    # Carga del Portfolio Plugin
    plugin_name = config.get('portfolio_plugin', 'default_portfolio')
    print(f"Loading Plugin ..{plugin_name}")
    try:
        portfolio_class, _ = load_plugin('portfolio.plugins', plugin_name)
        portfolio_plugin = portfolio_class(config)
        portfolio_plugin.set_params(**config)
    except Exception as e:
        print(f"Failed to load or initialize Portfolio Plugin: {e}")
        sys.exit(1)

    # fusión de configuración, integrando parámetros específicos de plugin pipeline
    print("Merging configuration with CLI arguments and unknown args (second pass, with plugin params)...")
    config = merge_config(config, pipeline_plugin.plugin_params, {}, file_config, cli_args, unknown_args_dict)
    # fusión de configuración, integrando parámetros específicos de plugin strategy
    config = merge_config(config, strategy_plugin.plugin_params, {}, file_config, cli_args, unknown_args_dict)
    # fusión de configuración, integrando parámetros específicos de plugin broker
    config = merge_config(config, broker_plugin.plugin_params, {}, file_config, cli_args, unknown_args_dict)
    # fusión de configuración, integrando parámetros específicos de plugin portfolio
    config = merge_config(config, portfolio_plugin.plugin_params, {}, file_config, cli_args, unknown_args_dict)
    

    # --- DECISIÓN DE EJECUCIÓN ---
    if config.get('load_model', False):
        print("Loading and evaluating existing model...")
        try:
            # Usar el pipeline plugin para cargar y evaluar el modelo
            pipeline_plugin.load_and_evaluate_model(config)
        except Exception as e:
            print(f"Model evaluation failed: {e}")
            sys.exit(1)
    else:
        # Si se activa el optimizador, se ejecuta el proceso de optimización antes del pipeline
        if config.get('use_optimizer', False):
            print("Running hyperparameter optimization with Portfolio Plugin...")
            try:
                # El portfolio manager optimiza la asignación de capital
                optimal_params = portfolio_plugin.optimize(pipeline_plugin, strategy_plugin, config)
                # Se guardan los parámetros óptimos en un archivo JSON
                optimizer_output_file = config.get("optimizer_output_file", "optimizer_output.json")
                with open(optimizer_output_file, "w") as f:
                    json.dump(optimal_params, f, indent=4)
                print(f"Optimized parameters saved to {optimizer_output_file}.")
                # Actualizar la configuración con los parámetros optimizados
                config.update(optimal_params)
            except Exception as e:
                print(f"Hyperparameter optimization failed: {e}")
                sys.exit(1)
        else:
            print("Skipping hyperparameter optimization.")
            print("Running trading pipeline...")
            # El Pipeline Plugin orquesta:
            # 1. Procesamiento de predicciones y datos de mercado
            # 2. Ejecución de estrategia y manejo de broker
            pipeline_plugin.run_trading_pipeline(
                config,
                strategy_plugin,
                broker_plugin,
                portfolio_plugin
            )
        
    # Guardado de la configuración local y remota
    if config.get('save_config'):
        try:
            save_config(config, config['save_config'])
            print(f"Configuration saved to {config['save_config']}.")
        except Exception as e:
            print(f"Failed to save configuration locally: {e}")

    if config.get('remote_save_config'):
        print(f"Remote saving configuration to {config['remote_save_config']}")
        try:
            remote_save_config(config, config['remote_save_config'], config.get('username'), config.get('password'))
            print("Remote configuration saved.")
        except Exception as e:
            print(f"Failed to save configuration remotely: {e}")

if __name__ == "__main__":
    main()
