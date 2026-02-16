# config_handler.py

"""
Config handler: loads/merges config from CLI, file, remote, with precedence and integrity checks.
"""

import os as _os
_QUIET = _os.environ.get('LTS_QUIET', '0') == '1'

import json
import hashlib
import sys
import requests
import os
from typing import Dict, Any
from app.config import DEFAULT_VALUES
from app.plugin_loader import load_plugin

class ConfigHandler:
    """
    Handles configuration loading and merging from CLI, file, and remote sources.
    Maintains config integrity and provides checksum calculation.
    """
    def __init__(self, default_file_path: str = None, override_file_path: str = None, cli_args: Dict[str, Any] = None, remote_url: str = None):
        self.config = {}

        # 1. Load defaults from file
        if default_file_path and os.path.exists(default_file_path):
            with open(default_file_path) as f:
                self.config.update(json.load(f))

        # 2. Load overrides from file
        if override_file_path and os.path.exists(override_file_path):
            with open(override_file_path) as f:
                self.config.update(json.load(f))
        
        # 3. Load from remote
        if remote_url:
            # In a real scenario, you would fetch and merge the remote config
            remote_conf = remote_load_config(remote_url)
            if remote_conf:
                self.config.update(remote_conf)

        # 4. Load environment variables
        self.config['DATABASE_PATH'] = os.environ.get('LTS_DATABASE_PATH', self.config.get('DATABASE_PATH'))

        # 5. Load CLI arguments (highest precedence)
        if cli_args:
            # Filter out None values from cli_args to not override existing settings with None
            self.config.update({k: v for k, v in cli_args.items() if v is not None})

        self.checksum = self._calc_checksum()

    def get_config(self):
        return self.config

    def _calc_checksum(self):
        """
        Calculate a SHA256 checksum of the current config for integrity verification.
        :return: Hex digest string of the checksum.
        """
        return hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()

    def get(self, key, default=None):
        """
        Get a config value by key, with optional default.
        :param key: Config key.
        :param default: Default value if key not found.
        :return: Value from config or default.
        """
        return self.config.get(key, default)

    def merge(self, other: Dict[str, Any]):
        """
        Merge another config dictionary into the current config and update checksum.
        :param other: Dictionary to merge.
        """
        self.config.update(other)
        self.checksum = self._calc_checksum()

def load_config(file_path):
    """
    Load configuration from a JSON file.
    :param file_path: Path to config file.
    :return: Loaded config as dict.
    """
    with open(file_path, 'r') as f:
        config = json.load(f)
    return config

def get_plugin_default_params(plugin_name, config=None):
    """
    Get default parameters for a plugin by name.
    :param plugin_name: Name of the plugin.
    :param config: Optional config dict.
    :return: Plugin default parameters dict.
    """
    plugin_class, _ = load_plugin('predictor.plugins', plugin_name)
    plugin_instance = plugin_class(config)
    return plugin_instance.plugin_params

def compose_config(config):
    """
    Compose a config dict, removing defaults and plugin defaults for saving.
    :param config: Input config dict.
    :return: Config dict to save.
    """
    plugin_name = config.get('plugin', DEFAULT_VALUES.get('plugin'))
    
    plugin_default_params = get_plugin_default_params(plugin_name, config)

    config_to_save = {}
    for k, v in config.items():
        if k not in DEFAULT_VALUES or v != DEFAULT_VALUES[k]:
            if k not in plugin_default_params or v != plugin_default_params[k]:
                config_to_save[k] = v
    
    # prints config_to_save
    if not _QUIET: print(f"Actual config_to_save: {config_to_save}")
    return config_to_save

def save_config(config, path='config_out.json'):
    config_to_save = compose_config(config)
    
    with open(path, 'w') as f:
        json.dump(config_to_save, f, indent=4)
    return config, path

def save_debug_info(debug_info, path='debug_out.json'):
    with open(path, 'w') as f:
        json.dump(debug_info, f, indent=4)

def remote_save_config(config, url, username, password):
    config_to_save = compose_config(config)
    try:
        response = requests.post(
            url,
            auth=(username, password),
            data={'json_config': json.dumps(config_to_save)}
        )
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        if not _QUIET: print(f"Failed to save remote configuration: {e}", file=sys.stderr)
        return False
    
def remote_load_config(url, username=None, password=None):
    try:
        if username and password:
            response = requests.get(url, auth=(username, password))
        else:
            response = requests.get(url)
        response.raise_for_status()
        config = response.json()
        return config
    except requests.RequestException as e:
        if not _QUIET: print(f"Failed to load remote configuration: {e}", file=sys.stderr)
        return None

def remote_log(config, debug_info, url, username, password):
    config_to_save = compose_config(config)
    try:
        data = {
            'json_config': json.dumps(config_to_save),
            'json_result': json.dumps(debug_info)
        }
        response = requests.post(
            url,
            auth=(username, password),
            data=data
        )
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        if not _QUIET: print(f"Failed to log remote information: {e}", file=sys.stderr)
        return False
