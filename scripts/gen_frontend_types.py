"""Генерация TypeScript-типов фронта из OpenAPI-схемы бэкенда.

Зачем скриптом, а не руками: типы ответов — это ~50 интерфейсов, которые
расходятся с реальностью при первой же правке Pydantic-модели. Здесь они
выводятся из того же источника, что и сам API, и перегенерируются одной
командой.

    python scripts/gen_frontend_types.py ../researcher-uz/src/lib/api/schema.ts
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from src.main import app

HEADER = """/**
 * Типы ответов API. Сгенерировано из OpenAPI-схемы бэкенда — не править руками.
 *
 * Обновить:
 *   cd researcher.uz-backend
 *   .venv/bin/python scripts/gen_frontend_types.py \\
 *     ../researcher-uz/src/lib/api/schema.ts
 */

/* eslint-disable */

"""


def ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def ts_type(schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        return ref_name(schema["$ref"])

    # anyOf с null — это Optional[...] из Pydantic.
    for key in ("anyOf", "oneOf"):
        if key in schema:
            parts = [ts_type(s) for s in schema[key]]
            parts = [p for p in parts if p != "null"] or ["any"]
            uniq: list[str] = []
            for p in parts:
                if p not in uniq:
                    uniq.append(p)
            has_null = any(s.get("type") == "null" for s in schema[key])
            out = " | ".join(uniq)
            return f"{out} | null" if has_null else out

    if "allOf" in schema and len(schema["allOf"]) == 1:
        return ts_type(schema["allOf"][0])

    if "enum" in schema:
        return " | ".join(
            "null" if v is None else f'"{v}"' if isinstance(v, str) else str(v).lower()
            for v in schema["enum"]
        )

    t = schema.get("type")
    if t == "null":
        return "null"
    if t == "string":
        return "string"
    if t in ("integer", "number"):
        return "number"
    if t == "boolean":
        return "boolean"
    if t == "array":
        return f"{ts_type(schema.get('items', {}))}[]"
    if t == "object":
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict) and extra:
            return f"Record<string, {ts_type(extra)}>"
        return "Record<string, any>"
    return "any";


def emit_interface(name: str, schema: dict[str, Any]) -> str:
    props: dict[str, Any] = schema.get("properties", {})
    required = set(schema.get("required", []))
    lines = [f"export interface {name} {{"]
    if not props:
        lines.append("  [key: string]: any;")
    for prop, prop_schema in props.items():
        # Поле, у которого есть default, можно не слать — на входе оно
        # необязательное; на выходе оно всегда придёт, но `?` тут безопаснее.
        optional = prop not in required
        desc = prop_schema.get("description")
        if desc:
            lines.append(f"  /** {desc.splitlines()[0]} */")
        key = prop if prop.isidentifier() else f'"{prop}"'
        lines.append(f"  {key}{'?' if optional else ''}: {ts_type(prop_schema)};")
    lines.append("}")
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    out_path = Path(sys.argv[1])

    schemas: dict[str, Any] = app.openapi()["components"]["schemas"]
    # Служебные схемы валидации FastAPI фронту не нужны.
    skip = {"HTTPValidationError", "ValidationError"}

    blocks = [
        emit_interface(name, schema)
        for name, schema in sorted(schemas.items())
        if name not in skip
    ]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(HEADER + "\n\n".join(blocks) + "\n", encoding="utf-8")
    print(f"{len(blocks)} интерфейсов → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
