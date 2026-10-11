"""The tools this voice lane offers the model.

Deliberately a small set of verbs over one delegate rather than Hermes' real tool list: the
api_server runs the whole toolset server-side, so shipping 30 JSON schemas into the setup
frame would spend tokens on every call to describe capabilities the model is not good at
using from audio. One "do this work" call plus the two that make multi-tasking honest —
``hermes_tasks`` to read the board, ``hermes_task_update`` to answer or redirect a task that
is waiting. Without those two the model can start work but never finish it: a run parked on
an approval is invisible to it, and a second request forces a duplicate run.
"""

from __future__ import annotations

DELEGATE_NAME = "hermes_task"
BOARD_NAME = "hermes_tasks"
UPDATE_NAME = "hermes_task_update"

TASK_TOOLS = (DELEGATE_NAME, BOARD_NAME, UPDATE_NAME)

#: What the model may do to a task already on the board. One list on purpose: the enum the
#: declaration offers and the verbs the relay acts on must never drift apart.
ACTIONS = ("approve", "deny", "steer", "stop")

DESCRIPTION = (
    "Hand real work to Hermes: files, terminals, web, code, memory, anything the user's "
    "install can do. The call returns immediately with a short task id — Hermes keeps working "
    "in the background and the result is delivered to you later as a system note. After "
    "calling it, say in one short sentence that you are on it, then stay quiet until the "
    "result arrives. Never invent the result. Several tasks can run at once, and the user may "
    "ask about one while another is still going: check hermes_tasks before starting anything "
    "they might already have asked for."
)

#: Gemini's functionDeclarations schema uses upper-case type names; a lowercase "object"
#: is rejected by the endpoint.
PARAMETERS = {
    "type": "OBJECT",
    "properties": {
        "task": {
            "type": "STRING",
            "description": "What to do, self-contained: name files, commands, URLs and "
                           "the exact question the user asked, since Hermes does not see "
                           "this conversation.",
        },
        "context": {
            "type": "STRING",
            "description": "Optional background the worker needs: prior findings, tone, "
                           "constraints, what the user already tried.",
        },
    },
    "required": ["task"],
}


def declarations() -> list[dict]:
    """The functionDeclarations list for the setup frame.

    ``behavior: NON_BLOCKING`` goes on the declaration — not on the Tool entry, which the
    endpoint rejects ("Unknown name \"behavior\" at 'setup.tools[0]'"). Google's page for
    the extended-thinking Live model says function calling there is async-only; measured on
    this key it is also what makes it happen at all: 2/5 trials handed the work over with the
    flag, 0/5 without it. ``gemini-3.8-live`` calls either way (8/8), so the flag costs the
    non-thinking model nothing.
    """
    return [{**entry, "behavior": "NON_BLOCKING"} for entry in _DECLARATIONS]


_DECLARATIONS = [
    {"name": DELEGATE_NAME, "description": DESCRIPTION, "parameters": PARAMETERS},
    {
        "name": BOARD_NAME,
        "description": (
            "Read the task board: every task this call has started, its short id, whether it "
            "is working, waiting on the user, done or failed. Call it before starting work that "
            "might already be running, and whenever the user asks what happened to something "
            "they already requested. It answers from the call's own memory — no new work runs."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": UPDATE_NAME,
        "description": (
            "Act on a task already on the board, by its short id. 'approve' and 'deny' answer a "
            "task that stopped to ask permission — say what it asked first and use the user's own "
            "answer, never your own judgement. 'steer' sends new information to a task that is "
            "still working. 'stop' abandons one the user no longer wants."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task": {"type": "STRING", "description": "The task id from the board, without the #."},
                "action": {"type": "STRING", "enum": list(ACTIONS),
                           "description": "What to do with it."},
                "answer": {"type": "STRING", "description": "For 'steer': what to tell it. "
                           "For approve/deny: the user's words, if any."},
            },
            "required": ["task", "action"],
        },
    },
]


#: Shaped for the reader, not for a chat window: a delegated run's final text is handed to a
#: voice model to speak, so the conclusion has to be at the front and in prose. Without this
#: the run answers the way it answers a person typing — tables, paths, code — and everything
#: before the tail is cut by the speakable limit.
OUTPUT_SHAPE = (
    "\n\nFinish with an answer a voice assistant can read aloud: the conclusion in your first "
    "sentence, then at most four short sentences of what actually matters. No markdown, tables, "
    "bullets, code blocks or bare file paths — the user will ask you in this same session for "
    "anything more detailed, so keep the answer, not the report."
)


def compose_prompt(args: dict) -> str:
    """The run's prompt text: task, plus any context the model chose to pass."""
    task = str((args or {}).get("task") or "").strip()
    context = str((args or {}).get("context") or "").strip()
    if not task:
        return ""
    prompt = f"{task}\n\nContext from the voice call: {context}" if context else task
    return prompt + OUTPUT_SHAPE
