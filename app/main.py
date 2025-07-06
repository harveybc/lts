#!/usr/bin/env python3
"""
main.py

Entry point for the Prediction Provider application. This script orchestrates:
- Loading and merging configurations (CLI, files).
- Initializing all plugins: Core, Endpoints, Feeder, Pipeline, and Predictor.
- Starting the core plugin to launch the FastAPI application.

:author: Your Name
:copyright: (c) 2025 Your Organization
:license: MIT
"""

import sys
from typing import Any, Dict
import logging
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.config_handler import load_config, remote_load_config
from app.cli import parse_args
from app.config import DEFAULT_VALUES
from app.plugin_loader import load_plugin
from app.config_merger import merge_config, process_unknown_args
# Import the FastAPI app for tests
try:
    from plugins_core.default_core import app
except ImportError:
    app = None  # Allows tests to run if plugins_core is not present

def setup_logging(level=logging.INFO):
    """
    Setup logging configuration.

    :param level: Logging level (default: logging.INFO)
    :type level: int
    :return: Configured logger
    :rtype: logging.Logger
    """
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('app.log')
        ]
    )
    return logging.getLogger(__name__)

class ErrorHandler:
    """
    Handles errors by sanitizing sensitive information from error messages.
    """
    @staticmethod
    def handle_error(error: Exception) -> str:
        """
        Sanitize error messages to remove sensitive information.

        :param error: The exception to sanitize.
        :type error: Exception
        :return: Sanitized error message string.
        :rtype: str
        """
        import re
        # Remove common sensitive patterns (e.g., password, secret, token)
        msg = str(error)
        # Replace password-like patterns
        msg = re.sub(r'(password|secret|token)\s*=\s*[^,;\s]+', r'\1=***', msg, flags=re.IGNORECASE)
        # Remove any obvious sensitive info
        msg = re.sub(r'\b\d{4,}\b', '****', msg)  # Mask long numbers
        return msg

def main():
    """
    Orchestrates the execution of the Prediction Provider system.
    """
    print("--- Initializing Prediction Provider ---")

    # 1. Configuration Loading
    args, unknown_args = parse_args()
    cli_args: Dict[str, Any] = vars(args)
    config: Dict[str, Any] = DEFAULT_VALUES.copy()
    file_config: Dict[str, Any] = {}

    if args.load_config:
        try:
            file_config = load_config(args.load_config)
            print(f"Loaded local config from: {args.load_config}")
        except Exception as e:
            print(f"Failed to load local configuration: {e}")
            sys.exit(1)

    # First merge pass (without plugin-specific parameters)
    print("Merging configuration (first pass)...")
    unknown_args_dict = process_unknown_args(unknown_args)
    config = merge_config(config, {}, {}, file_config, cli_args, unknown_args_dict)

    # 2. Plugin Loading
    plugin_types = ['core', 'endpoints', 'feeder', 'pipeline', 'predictor']
    plugins = {}

    for plugin_type in plugin_types:
        plugin_name = config.get(f'{plugin_type}_plugin', f'default_{plugin_type}')
        print(f"Loading {plugin_type.capitalize()} Plugin: {plugin_name}")
        try:
            plugin_class, _ = load_plugin(f'{plugin_type}.plugins', plugin_name)
            plugin_instance = plugin_class(config)
            plugin_instance.set_params(**config)
            plugins[plugin_type] = plugin_instance
        except Exception as e:
            print(f"Failed to load or initialize {plugin_type.capitalize()} Plugin '{plugin_name}': {e}")
            sys.exit(1)

    # Second merge pass (with all plugin parameters)
    print("Merging configuration (second pass, with plugin params)...")
    for plugin_type, plugin_instance in plugins.items():
        config = merge_config(config, plugin_instance.plugin_params, {}, file_config, cli_args, unknown_args_dict)

    # 3. Start Application using the Core Plugin
    core_plugin = plugins.get('core')
    if not core_plugin:
        print("Fatal: Core plugin not found. Cannot start application.")
        sys.exit(1)
    # Pass all loaded plugins to the core system
    core_plugin.set_plugins(plugins)

    try:
        print("Starting Core Plugin...")
        core_plugin.start()
    except Exception as e:
        print(f"An unexpected error occurred while starting the core plugin: {e}")
        core_plugin.stop()
        sys.exit(1)

if __name__ == "__main__":
    main()
