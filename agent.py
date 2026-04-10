"""
NEON Authentication Agent
Connects to wss://neonhealth.software/agent-puzzle/challenge
and autonomously completes the authentication sequence.
"""

import asyncio
import json
import math
import re
import urllib.parse
import urllib.request
from pprint import pprint

import anthropic
import websockets

# ── Config ────────────────────────────────────────────────────────────────────
NEON_WS = "wss://neonhealth.software/agent-puzzle/challenge"
NEON_CODE = "7dd13cc73ae0f532"

def load_resume(path: str = "resume.txt") -> str:
    with open(path, "r") as f:
        return f.read()

RESUME = load_resume()

# ── Anthropic client ──────────────────────────────────────────────────────────
client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

# Track all speak_text responses so the verification checkpoint can recall them
response_history: list[str] = []


# ── Helpers ───────────────────────────────────────────────────────────────────

def reconstruct(fragments: list[dict]) -> str:
    """Sort fragments by timestamp and join into a message."""
    sorted_frags = sorted(fragments, key=lambda f: f["timestamp"])
    return " ".join(f["word"] for f in sorted_frags)


def fetch_wikipedia_summary(title: str) -> str:
    """Fetch the Wikipedia plain-text extract for a given title."""
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}"
    req = urllib.request.Request(url, headers={"User-Agent": "neon-agent/1.0"})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    return data.get("extract", "")


def eval_math(expr: str) -> int:
    """Safely evaluate a JavaScript-style Math.floor expression.
    Uses JS-style % (remainder, sign follows dividend) via math.fmod.
    """
    expr_py = re.sub(r"Math\.floor", "math.floor", expr.strip())
    if re.search(r"[^0-9+\-*/().\s%a-z_]", expr_py):
        raise ValueError(f"Unsafe expression: {expr_py}")

    # Split on the outermost % (last one) to apply JS-style remainder
    pct = expr_py.rfind("%")
    if pct != -1:
        left = expr_py[:pct].strip()
        right = expr_py[pct+1:].strip()
        left_val = eval(left, {"__builtins__": {}, "math": math})
        right_val = int(right)
        return int(math.fmod(left_val, right_val))

    return int(eval(expr_py, {"__builtins__": {}, "math": math}))


# ── Claude decides the response ───────────────────────────────────────────────

def ask_claude(message: str) -> dict:
    """
    Given the reconstructed NEON message, use Claude to determine the correct
    response JSON. Returns a dict ready to send over the WebSocket.
    """
    system = f"""You are an AI co-pilot completing a NEON authentication sequence.

Your Vessel Authorization Code (Neon Code): {NEON_CODE}

Crew manifest (resume):
{RESUME}

Prior speak_text responses this session (for recall checkpoints):
{json.dumps(response_history, indent=2)}

Rules:
- Respond ONLY with a single JSON object — no markdown, no commentary.
- Two response types:
  * enter_digits: {{ "type": "enter_digits", "digits": "<string>" }}
  * speak_text:   {{ "type": "speak_text", "text": "<string>" }}

Checkpoint guide:
a) Vessel Identification — NEON asks you to respond on a channel with your code.
   → enter_digits with your Neon Code (add # if prompt says "fog key" or "pound key").

b) Math Evaluation — evaluate JavaScript-style Math.floor(...) arithmetic.
   → enter_digits with the integer result (add # if prompt says "fog key"/"pound key").

c) Knowledge Archive (Wikipedia) — "Nth word in the knowledge archive entry for <Title>"
   → speak_text with that single word. Use Wikipedia REST API /page/summary/<Title>.

d) Manifest / Resume — NEON asks about crew background (education, experience, skills).
   → speak_text. Respect any character limits ("between X and Y characters").

e) Recall — NEON asks you to recall a word from a previous response.
   → speak_text with that word from the prior response history above.

Output ONLY the JSON object, nothing else."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": message}],
    )

    raw = response.content[0].text.strip()
    # Strip markdown code fences if Claude wraps the JSON
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw).strip()
    print(f"  Claude → {raw}")
    return json.loads(raw)


# ── Recall handler ───────────────────────────────────────────────────────────

def handle_recall(text: str) -> dict:
    """
    NEON asks to recall the Nth word from a prior transmission.
    e.g. 'Speak the 3rd word of that transmission.'
    """
    n_match = re.search(r"(\d+)(?:st|nd|rd|th) word", text, re.IGNORECASE)
    if not n_match or not response_history:
        return None

    # Figure out which prior response they mean by keyword
    target = None
    if re.search(r"skill", text, re.IGNORECASE):
        for r in reversed(response_history):
            if any(w in r.lower() for w in ["python", "terraform", "docker", "kubernetes", "devops", "aws"]):
                target = r
                break
    elif re.search(r"educat|university|degree", text, re.IGNORECASE):
        for r in reversed(response_history):
            if any(w in r.lower() for w in ["university", "engineering", "bachelor", "degree"]):
                target = r
                break
    elif re.search(r"work|experience|career", text, re.IGNORECASE):
        for r in reversed(response_history):
            if any(w in r.lower() for w in ["years", "engineer", "meta", "apple", "alation"]):
                target = r
                break
    elif re.search(r"project", text, re.IGNORECASE):
        for r in reversed(response_history):
            if any(w in r.lower() for w in ["terraform", "ci/cd", "pipeline", "monitoring"]):
                target = r
                break

    # Fall back to most recent response
    if target is None:
        target = response_history[-1]

    n = int(n_match.group(1))
    words = target.split()
    if 1 <= n <= len(words):
        return {"type": "speak_text", "text": words[n - 1]}
    return {"type": "speak_text", "text": words[-1]}


# ── Deterministic checkpoint handlers ────────────────────────────────────────

def handle_vessel_id(text: str) -> dict:
    # Authorization code request
    if re.search(r"authorization code|vessel code|neon code", text, re.IGNORECASE):
        digits = NEON_CODE + ("#" if re.search(r"fog key|pound key", text, re.IGNORECASE) else "")
        return {"type": "enter_digits", "digits": digits}
    # Frequency selection
    match = re.search(r"(?:AI co-pilot|pilot).{0,80}respond on frequency (\d+)", text, re.IGNORECASE)
    if match:
        digits = match.group(1) + ("#" if re.search(r"fog key|pound key", text, re.IGNORECASE) else "")
        return {"type": "enter_digits", "digits": digits}
    return None


# ── Wikipedia tool ────────────────────────────────────────────────────────────

def handle_wikipedia_checkpoint(message: str) -> dict:
    """
    If the message asks for an Nth word from a Wikipedia article,
    fetch it directly without relying on Claude's knowledge.
    """
    # e.g. "Speak the 5th word in the knowledge archive entry for Apollo 11."
    match = re.search(
        r"(\d+)(?:st|nd|rd|th) word in the (?:knowledge archive|entry)(?: summary)? for ['\"]?([A-Za-z0-9_()\- ]+?)['\"]?(?:,|\.|\s|$)",
        message,
        re.IGNORECASE,
    )
    if not match:
        return None
    n = int(match.group(1))
    title = match.group(2).strip().rstrip(".")
    summary = fetch_wikipedia_summary(title)
    words = summary.split()
    if n < 1 or n > len(words):
        return {"type": "speak_text", "text": words[-1] if words else ""}
    return {"type": "speak_text", "text": words[n - 1]}


def handle_math_checkpoint(message: str) -> dict:
    """
    If the message contains a Math.floor expression, evaluate it directly.
    """
    if "Math.floor" not in message:
        return None
    # Grab everything after the last colon — that's always where NEON puts the expression
    colon_split = message.rsplit(":", 1)
    expr = colon_split[-1].strip().rstrip(".")
    try:
        result = eval_math(expr)
        digits = str(result)
        if re.search(r"fog key|pound key", message, re.IGNORECASE):
            digits += "#"
        return {"type": "enter_digits", "digits": digits}
    except Exception as e:
        print(f"  Math eval error: {e} | expr: {expr}")
        return None


# ── Main loop ─────────────────────────────────────────────────────────────────

def divider(char="─", width=60):
    print(char * width)

async def run():
    print(f"\n{'═' * 60}")
    print(f"  NEON AGENT STARTING")
    print(f"{'═' * 60}")
    print(f"  Endpoint : {NEON_WS}")
    print(f"  Neon Code: {NEON_CODE}")
    print(f"{'═' * 60}\n")

    checkpoint = 0
    async with websockets.connect(NEON_WS) as ws:
        print("  Connected.\n")
        async for raw in ws:
            msg = json.loads(raw)

            if msg.get("type") == "success":
                print(f"\n{'═' * 60}")
                print("  ACCESS GRANTED — First contact achieved!")
                print(f"{'═' * 60}\n")
                break

            if msg.get("type") == "error":
                print(f"\n{'═' * 60}")
                print(f"  REJECTED: {msg.get('message')}")
                print(f"{'═' * 60}\n")
                break

            if msg.get("type") != "challenge":
                print(f"  [unhandled type: {msg.get('type')}]")
                continue

            checkpoint += 1
            divider()
            print(f"  CHECKPOINT #{checkpoint}")
            divider()

            # Reconstruct the fragmented transmission
            fragments = msg.get("message", [])
            text = reconstruct(fragments)
            print(f"  NEON   : {text}")

            # Try deterministic handlers first
            response = handle_vessel_id(text) or handle_wikipedia_checkpoint(text) or handle_math_checkpoint(text) or handle_recall(text)

            # Fall back to Claude for everything else
            if response is None:
                response = ask_claude(text)

            # Enforce character limits for speak_text responses
            if response.get("type") == "speak_text":
                t = response["text"]
                limit_match = re.search(r"between (\d+) and (\d+) characters", text, re.IGNORECASE)
                less_match = re.search(r"in less than (\d+)", text, re.IGNORECASE)
                exact_match = re.search(r"exactly (\d+) characters", text, re.IGNORECASE)
                if exact_match:
                    max_c = int(exact_match.group(1))
                elif limit_match:
                    raw_max = int(limit_match.group(2))
                    # Round down to nearest 100 for safety buffer
                    max_c = (raw_max // 100) * 100
                elif less_match:
                    raw_max = int(less_match.group(1)) - 1
                    max_c = (raw_max // 100) * 100
                else:
                    max_c = 200  # default safe cap
                response["text"] = t[:max_c]
                print(f"  [chars: {len(response['text'])} / cap {max_c}]")

            # Track speak_text answers for the recall checkpoint
            if response.get("type") == "speak_text":
                response_history.append(response["text"])

            payload = json.dumps(response)
            print(f"  SEND   :")
            pprint(response)
            print()
            await ws.send(payload)


if __name__ == "__main__":
    asyncio.run(run())
