"""Default mediator agent: audits query output for policy compliance.

Env vars:
  BRIDGE_URL, SESSION_TOKEN — bridge connection
  RAW_OUTPUT — the query agent's raw output to audit
  QUERY_PROMPT — the original question
  RECORDS_ACCESSED — JSON list of record IDs accessed
  MEDIATION_POLICY — optional policy instructions

Outputs filtered/audited text to stdout.
"""

import json
import os
import urllib.error
import urllib.request

BRIDGE_URL = os.environ["BRIDGE_URL"]
SESSION_TOKEN = os.environ["SESSION_TOKEN"]
RAW_OUTPUT = os.environ.get("RAW_OUTPUT", "")
QUERY_PROMPT = os.environ.get("QUERY_PROMPT", "")
RECORDS_ACCESSED = os.environ.get("RECORDS_ACCESSED", "[]")
MEDIATION_POLICY = os.environ.get("MEDIATION_POLICY", "")

MEDIATOR_SYSTEM = """\
PRIVACY GATE

You are a strict output mediator for a public response channel.
Treat all personal/sensitive information as high risk.

Task:
1. Review the candidate response for sensitive content.
2. If it is compliant, return it unchanged.
3. If not compliant, rewrite minimally so it is safe.
4. If the answer cannot be made safe without dropping core content,
   return a brief, generic safe answer.

Always strip or avoid:
- Names of people, companies, organizations, products, usernames, handles.
- Substance use (drugs, alcohol, smoking, intoxication).
- Mental health details (diagnoses, therapy, medications, breakdowns, internal struggles).
- Family conflict and relationship drama.
- Work conflict/failures (fired, toxic boss/coworker conflict, rage quitting).
- Financial stress (debt, money anxiety, hardship specifics).
- Medical issues (conditions, diagnoses, procedures, treatments).
- Credentials or secrets (passwords, tokens, API keys, private identifiers).
- Content from outside the provided response context.

Output rules:
- Return only final response text. No preamble, no policy explanation.
- Do not mention that redaction happened.
- Keep useful, non-sensitive actions/ideas/facts when possible.
- Prefer neutral, concise phrasing.
"""


def llm_call(messages, max_tokens=4096):
    data = json.dumps({
        "messages": messages,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        f"{BRIDGE_URL}/llm/chat",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SESSION_TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())["content"]
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return "Unable to provide a mediated response within budget."
        return f"Unable to provide a mediated response (HTTP {e.code})."
    except (urllib.error.URLError, TimeoutError):
        return "Unable to provide a mediated response."
    except Exception:
        return "Unable to provide a mediated response."


def main():
    if not RAW_OUTPUT.strip():
        print("")
        return

    user_msg = (
        f"QUERY_PROMPT:\n{QUERY_PROMPT}\n\n"
        f"RECORDS_ACCESSED:\n{RECORDS_ACCESSED}\n\n"
        f"RESPONSE TO AUDIT:\n{RAW_OUTPUT}"
    )
    if MEDIATION_POLICY:
        user_msg = f"POLICY:\n{MEDIATION_POLICY}\n\n{user_msg}"

    result = llm_call([
        {"role": "system", "content": MEDIATOR_SYSTEM},
        {"role": "user", "content": user_msg},
    ])

    print(result)


if __name__ == "__main__":
    main()
