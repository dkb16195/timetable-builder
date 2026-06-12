"""YAML → Config with plain-English error reporting."""
from __future__ import annotations

import yaml
from pydantic import ValidationError

from .schema import Config


class ConfigLoadError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


def load_config(path: str) -> Config:
    try:
        with open(path) as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigLoadError([f"Config file not found: {path}"])
    except yaml.YAMLError as e:
        raise ConfigLoadError(
            [f"The config file is not valid YAML. Details: {e}"])
    if not isinstance(raw, dict):
        raise ConfigLoadError(["The config file must be a YAML mapping "
                               "(key: value pairs at the top level)."])
    try:
        return Config(**raw)
    except ValidationError as e:
        problems = []
        for err in e.errors():
            where = " → ".join(str(x) for x in err["loc"])
            problems.append(f"{where}: {err['msg']}")
        raise ConfigLoadError(problems)
