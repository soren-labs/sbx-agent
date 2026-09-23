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
| `warn` | Invalid output → run `FINISHED` with `structured_output.verdict.status: invalid` |

## Verdict structure

Once a run is terminal, `structured_output` is populated:

```json
{
  "status": "FINISHED",
  "structured_output": {
    "raw": "{ \"emails\": [\"...\"]}",
    "extraction": "fence",
    "output_contract": {
      "enforcement": "strict",
      "schema_digest": "sha256:..."
    },
    "verdict": {
      "status": "valid|invalid|pending|skipped",
      "violations": [
        "emails: is required"
      ]
    }
  }
}
```

### Verdict status

- **valid** — output satisfied the schema
- **invalid** — output failed validation
- **pending** — contract not yet evaluated (shouldn't happen at terminal)
- **skipped** — no contract declared

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
  "structured_output": {
    "verdict": {
      "status": "invalid",
      "violations": ["emails is required"]
    }
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
- **warn mode** → `verdict.status = "skipped"`
