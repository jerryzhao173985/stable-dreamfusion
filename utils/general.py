import configparser
import json
from json import JSONDecodeError
from pathlib import Path
import os

LOCAL_HPARAMS = "parameters.json"


def get_config():
    prefix = Path("/opt/program/")
    config = configparser.ConfigParser()
    config_path = prefix / os.getenv("CONFIG_PATH", default="config_dev.ini")
    config.read(config_path)
    return config


def load_params():
    param_path = get_params_path()

    with open(param_path, "r") as f:
        return parse_nested_json(json.load(f))


def get_params_path():
    prefix = Path("/opt/ml/")
    param_path = prefix / "input/config/hyperparameters.json"

    if not param_path.exists():
        param_path = prefix / LOCAL_HPARAMS

    return param_path


def parse_nested_json(obj):
    """Parse nested string json
    Required because sagemaker formats nested json as string values
    """
    if isinstance(obj, dict):
        return {k: parse_nested_json(v) for k, v in obj.items()}
    if isinstance(obj, str):
        try:
            return parse_nested_json(json.loads(obj.replace("'", '"')))
        except JSONDecodeError:
            return obj
    return obj
