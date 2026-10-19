"""Serve the signer: `python -m signer` (127.0.0.1:8300 unless SIGNER_HOST / SIGNER_PORT).

One process only: the gateway is the only client, and one worker keeps the logs and the
SQLite ledger simple (the ledger's leases are still safe with more).
"""

import sys

import uvicorn
from pydantic import ValidationError

from signer.config import get_settings
from signer.keys import AgentKeyError
from signer.main import create_app

if __name__ == "__main__":
    try:
        settings = get_settings()
        app = create_app(settings)
    except ValidationError as exc:
        # Pydantic would echo the offending values; print only which settings are wrong.
        fields = ", ".join(".".join(map(str, e["loc"])) or e["msg"] for e in exc.errors())
        sys.exit(f"signer refused to start: invalid settings: {fields}")
    except AgentKeyError as exc:
        sys.exit(f"signer refused to start: {exc}")
    uvicorn.run(app, host=settings.signer_host, port=settings.signer_port, workers=1)
