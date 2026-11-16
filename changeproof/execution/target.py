import hashlib
import importlib.util
from pathlib import Path
from uuid import uuid4


class TargetError(ValueError):
    pass


def load_target(repo):
    repo = Path(repo).resolve()
    adapter = repo / "changeproof_target.py"
    if not adapter.is_file():
        raise TargetError("Target needs a changeproof_target.py adapter; see the payment examples")
    spec = importlib.util.spec_from_file_location(f"changeproof_target_{uuid4().hex}", adapter)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "VARIANT", None) not in {"vulnerable", "corrected"}:
        raise TargetError("Adapter VARIANT must be vulnerable or corrected")
    if not callable(getattr(module, "create_app", None)):
        raise TargetError("Adapter must export create_app(store, gateway_transport, hook)")
    return module, {"path": str(repo), "adapter_sha256": hashlib.sha256(adapter.read_bytes()).hexdigest(),
                    "variant": module.VARIANT}
