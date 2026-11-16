from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import CampaignConfig


class ConfigurationError(ValueError):
    """The campaign cannot safely be interpreted."""


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject ambiguous duplicate mappings rather than silently overwriting."""


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            if key in result:
                raise ConfigurationError(f"Duplicate YAML key: {key!r}")
            result[key] = loader.construct_object(value_node, deep=deep)
        except TypeError as exc:
            raise ConfigurationError("YAML mapping keys must be scalar values") from exc
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_config(path: str | Path) -> CampaignConfig:
    try:
        data = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
        return CampaignConfig.model_validate(data)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigurationError(f"Invalid configuration: {exc}") from exc
