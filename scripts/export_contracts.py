"""Export the local contracts; --check detects drift without writing files."""

import argparse
import json
from pathlib import Path

from frameseek.fixtures import examples
from frameseek.schema import Answer, QueryControl


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    folder = Path(__file__).resolve().parents[1] / "docs" / "schema"
    documents = {}
    for model, filename in ((Answer, "answer-0.1.json"), (QueryControl, "query-control-0.1.json")):
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        documents[filename] = schema
    documents["fixtures-0.1.json"] = {key: value.model_dump() for key, value in examples().items()}
    stale = []
    for filename, document in documents.items():
        path = folder / filename
        text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not path.exists() or path.read_text() != text:
                stale.append(filename)
        else:
            folder.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    if stale:
        raise SystemExit("Contract exports are stale; run make schema: " + ", ".join(stale))
    print("Contract exports match source" if args.check else "Exported 2 schemas and 4 fixtures")


if __name__ == "__main__":
    main()
