"""Export Core's schema without starting services or making model requests."""

import json
from pathlib import Path
from typing import cast

from eye.api.app import Graph, create_app
from eye.orchestrator import Orchestrator
from eye.warehouse.client import DataClient

data = DataClient("http://schema-only.invalid")
try:
    app = create_app(
        cast(Orchestrator, None),
        None,
        None,
        cast(Graph, None),
        None,
        data=data,
    )
    Path("packages/web/openapi.json").write_text(json.dumps(app.openapi()))
finally:
    data.close()
