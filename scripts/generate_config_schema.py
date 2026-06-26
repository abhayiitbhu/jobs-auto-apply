from __future__ import annotations

import json
from pathlib import Path

from jobs_auto_apply.config import AppConfig


def main() -> None:
    # Generate schema, but fix any non-serializable default values (like Path objects)
    schema = AppConfig.json_schema()
    if "properties" in schema and "base_dir" in schema["properties"]:
        del schema["properties"]["base_dir"]
    if "required" in schema and "base_dir" in schema["required"]:
        schema["required"].remove("base_dir")
    # Write schema
    schema_path = Path(__file__).parent.parent / "config.schema.json"
    schema_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    print(f"Generated config schema at {schema_path}")


if __name__ == "__main__":
    main()
