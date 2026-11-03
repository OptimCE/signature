"""Export the FastAPI OpenAPI spec to disk for the KrakenD pipeline.

Usage: python scripts/export_openapi.py <output_path>
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import app


def export(output_path: Path) -> None:
    spec = app.openapi()
    spec["paths"] = {
        path: methods
        for path, methods in spec.get("paths", {}).items()
        if not path.startswith("/health")
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(spec, indent=2))
    print(f"Wrote OpenAPI spec ({len(spec['paths'])} paths) to {output_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/export_openapi.py <output_path>", file=sys.stderr)
        sys.exit(1)
    export(Path(sys.argv[1]))
