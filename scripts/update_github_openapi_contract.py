"""Extract the implemented GitHub REST subset from a pinned official document."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from mcp_system.http.github import ROUTES


def _resolve(document: dict, schema: dict) -> dict:
    while "$ref" in schema:
        value: object = document
        for part in schema["$ref"][2:].split("/"):
            value = value[part]  # type: ignore[index]
        schema = value  # type: ignore[assignment]
    return schema


def _properties(document: dict, schema: dict) -> set[str]:
    schema = _resolve(document, schema)
    result = set(schema.get("properties", {}))
    for keyword in ("allOf", "oneOf", "anyOf"):
        for child in schema.get(keyword, []):
            result.update(_properties(document, child))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("document", type=Path)
    parser.add_argument("--output", type=Path, default=Path("contracts/github/core-openapi.json"))
    parser.add_argument("--source", type=Path, default=Path("contracts/github/openapi-source.json"))
    args = parser.parse_args()

    document_bytes = args.document.read_bytes()
    document_sha256 = hashlib.sha256(document_bytes).hexdigest()
    document = json.loads(document_bytes)
    source = json.loads(args.source.read_text(encoding="utf-8"))
    expected_sha256 = source.get("source_document_sha256")
    if expected_sha256 and document_sha256 != expected_sha256:
        parser.error(
            f"source document SHA-256 mismatch: expected {expected_sha256}, "
            f"got {document_sha256}"
        )
    operations = []
    for route in ROUTES:
        operation = document["paths"][route.path][route.method.lower()]
        request = operation.get("requestBody", {}).get("content", {}).get("application/json", {})
        schema = _resolve(document, request.get("schema", {})) if request else {}
        parameters = [
            parameter
            for parameter in operation.get("parameters", [])
            if parameter.get("in") == "query"
        ]
        operations.append(
            {
                "method": route.method,
                "path": route.path,
                "operation_id": operation["operationId"],
                "responses": sorted(operation.get("responses", {})),
                "query": {
                    "properties": sorted(parameter["name"] for parameter in parameters),
                    "required": sorted(parameter["name"] for parameter in parameters if parameter.get("required")),
                },
                "request": {
                    "properties": sorted(_properties(document, schema)),
                    "required": sorted(schema.get("required", [])),
                    "any_of_required": sorted(
                        sorted(alternative.get("required", []))
                        for alternative in schema.get("anyOf", [])
                        if alternative.get("required")
                    ),
                },
            }
        )

    result = {
        "service": "github",
        "source_repository": source["source_repository"],
        "source_revision": source["source_revision"],
        "source_document": source["source_document"],
        "source_document_sha256": document_sha256,
        "api_version": source["api_version"],
        "openapi_info_version": document["info"]["version"],
        "operations": operations,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
