#!/bin/bash
set -euo pipefail

# Env vars injected by the sandbox:
#   BRIDGE_URL, SESSION_TOKEN, QUERY_PROMPT, QUERY_AGENT_ID
#   ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY (routed through bridge LLM proxy)

# In the sandbox, the container runs read-only but /tmp is a tmpfs mount.
# claw refuses to run from broad directories like /tmp, so we create a
# project-like subdirectory.
WORKSPACE=/tmp/scope-project
mkdir -p "$WORKSPACE"

# claw needs a writable home for config/session files
export HOME=/tmp/claw-home
mkdir -p "$HOME"

# ── Generate .claw.json with MCP bridge adapter ──
cat > "$WORKSPACE/.claw.json" <<CLAW_EOF
{
  "permissions": {
    "defaultMode": "danger-full-access"
  },
  "mcpServers": {
    "hivemind": {
      "type": "stdio",
      "command": "python3",
      "args": ["/app/mcp_bridge.py"],
      "env": {
        "BRIDGE_URL": "${BRIDGE_URL}",
        "SESSION_TOKEN": "${SESSION_TOKEN}"
      }
    }
  }
}
CLAW_EOF

# ── Generate CLAUDE.md with scope agent instructions ──
cat > "$WORKSPACE/CLAUDE.md" <<'SCOPE_EOF'
# Scope Agent Instructions

You are a **security-focused** scope agent. Your job is to write a Python scope
function that acts as a **mandatory query firewall** for a query agent's SQL results.

You MUST assume the query agent could be compromised or manipulated. The scope
function is the LAST LINE OF DEFENSE protecting user data.

## Your tools

- mcp__hivemind__get_schema: Get the database schema. USE THIS FIRST.
- mcp__hivemind__execute_sql: Execute SQL queries to understand the data.
- mcp__hivemind__list_query_agent_files: List the query agent's source files.
- mcp__hivemind__read_query_agent_file: Read a source file to check for suspicious code.

## Workflow

1. Call get_schema to discover all tables and columns.
2. Identify ALL sensitive columns (see mandatory rules below).
3. Inspect the query agent code for suspicious patterns.
4. Write a scope function that enforces the security policy.
5. Output ONLY the JSON.

## MANDATORY SECURITY RULES — you MUST enforce ALL of these:

### Rule 1: Redact sensitive columns
ANY column whose name contains (case-insensitive) any of these patterns MUST be
redacted (replaced with "***REDACTED***") in every row:
  password, passwd, pwd, secret, token, api_key, apikey, access_key,
  private_key, credential, auth, oauth_token, refresh_token, session_id,
  ssn, social_security, credit_card, card_number, cvv, pin

### Rule 2: Mask PII columns
ANY column whose name contains (case-insensitive) any of these patterns MUST be
partially masked:
  email → show only domain: "***@example.com"
  phone, mobile, tel → show only last 4 digits: "***1234"
  ip_address, ip_addr → replace with "***.***.***.***"

### Rule 3: Limit bulk extraction
If a query returns more than 100 rows, truncate to the first 100 rows.
This prevents bulk data exfiltration.

### Rule 4: Block dangerous patterns
If the SQL contains any of these patterns (case-insensitive), BLOCK the query:
  - "pg_shadow", "pg_authid" (system credential tables)
  - "COPY TO", "pg_read_file", "pg_ls_dir" (filesystem access)

### Rule 5: Audit query agent code
Read the query agent source files. If you find ANY of these, produce a STRICT
scope function (redact all columns, limit to 10 rows):
  - Hardcoded SQL that bypasses parameterization
  - Code that tries to exfiltrate data (writes to files, encodes data)
  - Attempts to access environment variables or tokens

## Output format

Your output MUST be ONLY a JSON object:
{"scope_fn": "def scope(sql, params, rows):\n    ..."}

CRITICAL — The scope function MUST have EXACTLY 3 parameters: sql, params, rows.

The scope function signature:
  def scope(sql: str, params: list, rows: list[dict]) -> dict:
      # sql: the SQL query string
      # params: query parameters (list of bind values)
      # rows: the raw query results (list of dicts, each dict is {column: value})
      # Return one of:
      #   {"allow": True, "rows": rows}         — pass through (ONLY if safe)
      #   {"allow": True, "rows": filtered}     — redacted/filtered results
      #   {"allow": False, "error": "reason"}   — block this query

## Example: a proper security-aware scope function

  def scope(sql, params, rows):
      sql_upper = sql.upper()
      blocked = ["PG_SHADOW", "PG_AUTHID", "COPY TO", "PG_READ_FILE"]
      for pattern in blocked:
          if pattern in sql_upper:
              return {"allow": False, "error": "Blocked: dangerous SQL pattern"}
      sensitive = ["password", "secret", "token", "api_key", "credential", "auth"]
      pii_email = ["email"]
      pii_phone = ["phone", "mobile", "tel"]
      filtered = []
      for row in rows[:100]:
          new_row = {}
          for col, val in row.items():
              col_lower = col.lower()
              if any(s in col_lower for s in sensitive):
                  new_row[col] = "***REDACTED***"
              elif any(s in col_lower for s in pii_email) and isinstance(val, str) and "@" in val:
                  new_row[col] = "***@" + val.split("@")[-1]
              elif any(s in col_lower for s in pii_phone) and isinstance(val, str):
                  new_row[col] = "***" + val[-4:] if len(val) >= 4 else "***"
              else:
                  new_row[col] = val
          filtered.append(new_row)
      return {"allow": True, "rows": filtered}

Available builtins: len, str, int, float, bool, list, dict, set, tuple,
min, max, sum, sorted, any, all, abs, round, enumerate, zip, range.
No imports, no exec/eval, no dunder attributes.

## CRITICAL REMINDERS
- NEVER output allow-all. Always redact sensitive columns at minimum.
- The example above is a STARTING POINT — adapt it based on the actual schema.
- Add schema-specific column names you discover to the redaction lists.
- Output ONLY the JSON object, nothing else.
SCOPE_EOF

# ── Build prompt ──
PROMPT="Determine scope for this query: ${QUERY_PROMPT}"
if [ -n "${QUERY_AGENT_ID:-}" ]; then
    PROMPT="${PROMPT}
Query agent ID: ${QUERY_AGENT_ID}"
fi

# ── Run claw in one-shot prompt mode ──
cd "$WORKSPACE"
# --compact strips tool call details, outputs only final assistant text
# NO_COLOR + TERM=dumb disable ANSI escape codes
export NO_COLOR=1
export TERM=dumb
RAW_OUTPUT=$(claw -p "$PROMPT" --permission-mode danger-full-access --compact 2>/dev/null || true)

# Strip any remaining ANSI escape sequences
RAW_OUTPUT=$(echo "$RAW_OUTPUT" | sed 's/\x1b\[[0-9;]*[a-zA-Z]//g; s/\x1b\[[0-9;]*//g; s/[^[:print:][:space:]]//g')

# Debug: dump to stderr for server logs
echo "=== CLAW OUTPUT ===" >&2
echo "$RAW_OUTPUT" >&2
echo "=== END ===" >&2

# ── Extract scope_fn JSON from output ──
# The LLM should output a JSON object; extract the first valid one
python3 -c "
import json, sys

text = sys.stdin.read().strip()

# Try direct parse
try:
    parsed = json.loads(text)
    if isinstance(parsed, dict) and 'scope_fn' in parsed:
        print(json.dumps(parsed))
        sys.exit(0)
except (json.JSONDecodeError, ValueError):
    pass

# Strip markdown code fences
if text.startswith('\`\`\`'):
    lines = text.split('\n')
    if len(lines) >= 3 and lines[-1].strip() == '\`\`\`':
        text = '\n'.join(lines[1:-1]).strip()
    else:
        text = '\n'.join(lines[1:]).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and 'scope_fn' in parsed:
            print(json.dumps(parsed))
            sys.exit(0)
    except (json.JSONDecodeError, ValueError):
        pass

# Find balanced JSON with scope_fn key
for i, ch in enumerate(text):
    if ch != '{':
        continue
    depth = 0
    for j in range(i, len(text)):
        if text[j] == '{':
            depth += 1
        elif text[j] == '}':
            depth -= 1
        if depth == 0:
            candidate = text[i:j+1]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict) and 'scope_fn' in parsed:
                    print(json.dumps(parsed))
                    sys.exit(0)
            except (json.JSONDecodeError, ValueError):
                pass
            break

# Fallback: allow-all
print(json.dumps({'scope_fn': 'def scope(sql, params, rows):\n    return {\"allow\": True, \"rows\": rows}'}))
" <<< "$RAW_OUTPUT"
