---
title: Structured output
description: Enforce JSON Schema contracts on agent output.
---

## Output contracts

Declare a JSON Schema that the agent's final output must satisfy:

```json
{
  "prompt": {"text": "Extract emails from the text"},
  "agent": {"provider": "codex"},
  "output_contract": {
    "schema": {
      "type": "object",
      "properties": {
        "emails": {
          "type": "array",
          "items": {"type": "string", "format": "email"}
        }
      },
      "required": ["emails"]
    },
    "enforcement": "strict"
  }
}
```

When the run completes, the agent's final message is validated against the schema.

## Enforcement modes

| Mode | Behavior |
| --- | --- |
| `strict` (default) | Invalid output → run `ERROR` with `contract_violation` code |
| `warn` | Invalid output → run `FINISHED` with `output_contract.status: invalid` |

## Verdict structure

Once a run is terminal, `structured_output` holds the extracted JSON value
and `output_contract` holds the verdict:

```json
{
  "status": "FINISHED",
  "structured_output": {"emails": ["ada@example.com"]},
  "output_contract": {
    "schema": {"type": "object", "...": "..."},
    "enforcement": "strict",
    "schema_digest": "sha256:...",
    "status": "valid",
    "extraction": "fence",
    "violations": []
  }
}
```

`structured_output` is the bare JSON value recovered from the agent's final
message — `null` when the run carried no contract or the message held no
parseable JSON. `output_contract` echoes the declared `schema` and reports
the verdict. Each `violations` entry is machine-diagnosable:

```json
{"path": "$", "code": "required", "message": "missing required property 'emails'"}
```

### Verdict status

- **valid** — output satisfied the schema
- **invalid** — output failed validation
- **pending** — contract not yet evaluated (shouldn't happen at terminal)
- **skipped** — the run ended without evaluable output

### Extraction types

- **embedded** — JSON found inline in the message
- **fence** — JSON in a code block (`` ```json ... ``` ``)
- **raw** — entire final message is JSON

## Schema constraints

Only a **safe subset** of JSON Schema is allowed (to prevent denial-of-service and unbounded evaluation):

**Allowed assertion keywords:**
- `type`, `enum`, `const`, `properties`, `required`, `additionalProperties`, `propertyNames`
- `minProperties`, `maxProperties`, `dependentRequired`
- `items`, `prefixItems`, `minItems`, `maxItems`, `uniqueItems`, `contains`, `minContains`, `maxContains`
- `minLength`, `maxLength`, `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`
- `allOf`, `anyOf`, `oneOf`, `not`

**Annotation keywords (accepted, ignored):**
- `$schema`, `$id`, `title`, `description`, `default`, `format`, `examples`, `deprecated`, `readOnly`, `writeOnly`

**Forbidden:**
- `$ref` (external schema references)
- `$defs` (recursive schemas)
- `pattern` (unbounded regex engine; use `minLength`/`maxLength`/`enum` instead)
- `if`/`then`/`else` (conditional schemas)
- `patternProperties` (pattern-based object keys)

**Example:** Email extraction contract:

```json
{
  "type": "object",
  "properties": {
    "emails": {
      "type": "array",
      "items": {
        "type": "string",
        "format": "email"
      },
      "minItems": 1
    },
    "invalid_entries": {
      "type": "array",
      "items": {"type": "string"}
    }
  },
  "required": ["emails"]
}
```

## Per-run overrides

Override the contract on a follow-up run:

```bash
POST /v1/agents/{id}/runs
-H "Authorization: Bearer $SBX_API_KEY"

{
  "prompt": {"text": "Extract phone numbers"},
  "output_contract": {
    "schema": {
      "type": "object",
      "properties": {
        "phone_numbers": {
          "type": "array",
          "items": {"type": "string"}
        }
      },
      "required": ["phone_numbers"]
    }
  }
}
```

## Errors

| Error | Cause |
| --- | --- |
| `invalid_output_contract` | Schema is malformed or uses forbidden keywords |
| `contract_violation` | Output failed validation in strict mode |

Example (strict mode, invalid output):

```json
{
  "status": "ERROR",
  "error": {
    "code": "contract_violation",
    "source": "control",
    "message": "…"
  }
}
```

Example (warn mode, same failure):

```json
{
  "status": "FINISHED",
  "structured_output": {"emails": ["ada@example.com"]},
  "output_contract": {
    "status": "invalid",
    "extraction": "fence",
    "violations": [
      {"path": "$", "code": "minItems", "message": "array has too few items"}
    ]
  }
}
```

## Parsing output

The control plane attempts to extract JSON from the agent's final message in this order:

1. **Raw** message (if the entire message is valid JSON)
2. **Code fence** (`` ```json ... ``` `` or bare `` ``` ... ``` ``)
3. **Embedded** JSON anywhere in the text

If no JSON is found:
- **strict mode** → run ERROR with `contract_violation`
- **warn mode** → `output_contract.status = "invalid"` (extraction never ran)
