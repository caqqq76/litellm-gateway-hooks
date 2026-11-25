"""Import target for LiteLLM: `litellm_settings.callbacks: gateway_hooks.callback.proxy_handler_instance`.

Reads the config file named by the GATEWAY_HOOKS_CONFIG environment variable.
"""

import os

from .core.config import load_config
from .gateway import GatewayHooks

_path = os.environ.get("GATEWAY_HOOKS_CONFIG")
if not _path:
    raise RuntimeError("Set GATEWAY_HOOKS_CONFIG to the path of your gateway-hooks.yaml")

proxy_handler_instance = GatewayHooks(load_config(_path))
