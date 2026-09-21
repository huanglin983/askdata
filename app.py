"""AskData entrypoint: run the Flask demo app."""
from __future__ import annotations

from askdata.web.app import app

__all__ = ["app"]


if __name__ == "__main__":
    import os

    from askdata.infra import db

    db.init_db()
    host = os.environ.get("ASKDATA_HOST", "0.0.0.0")
    port = int(os.environ.get("ASKDATA_PORT", "5050"))
    debug = os.environ.get("ASKDATA_DEBUG", "1") == "1"
    # Scripts disable reloader so stop/restart can track a single PID.
    use_reloader = os.environ.get("ASKDATA_RELOAD", "0") == "1"
    app.run(host=host, port=port, debug=debug, use_reloader=use_reloader)
