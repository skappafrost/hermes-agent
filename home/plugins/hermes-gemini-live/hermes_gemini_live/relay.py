"""Pump one browser WebSocket against one Live socket, and hand real work to Hermes.

Two tasks own the sockets, and either side dying closes both: a call with no browser is
a metered Live session with no listener.

The agent lane obeys one rule above all others: nothing about a run may be awaited on the
loop that carries audio. A tool call therefore answers the model with a receipt
immediately and the result arrives later, from a worker thread, as a spoken-note — the
same shape the voice domain learned a waiting tool is a dead microphone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any

from . import agent_lane, config, wire, tools as tools_module
from .config import ConfigError
from .live_client import LiveError, LiveSession

logger = logging.getLogger(__name__)

#: Audio frames arrive ~10/s; anything bigger than this is a bug or an attack.
MAX_BROWSER_FRAME_BYTES = 1 << 20
#: Forwarded, then the call ends — waiting for the socket to die on its own would only
#: turn an announced goodbye into a silent one.
TERMINAL_FRAME_TYPES = frozenset({"go_away"})
RESULT_NOTE = (
    "Hermes finished task #{task_id} ({state}). Result: {text}\n"
    "Tell the user what you found: the answer in your first sentence, then at most four more "
    "short spoken sentences. The full report stays in the Hermes session, so offer to answer "
    "questions about it instead of reading everything. Do not invent details, do not add "
    "commentary, and do not start another task about it."
)
NEEDS_NOTE = (
    "Task #{task_id} has stopped and needs the user: {question}\n"
    "Ask the user about it now in one short spoken question, wait for their answer, then call "
    "hermes_task_update on task {task_id} with action approve or deny using what they said. "
    "Never decide the approval yourself, and never start a second task about this one."
)
#: How long the model must have been silent before a note reaches it. Injecting a finished
#: result into the middle of its own speech makes it stop the sentence it was saying and read
#: the report instead, which is what the user heard as being cut off.
QUIET_BEFORE_NOTE_S = 1.0


class NoteQueue:
    """Model-facing notes, released only in the gaps between the model's utterances.

    One at a time and first-in-first-out: several tasks can finish during a single long
    answer, and dumping all of them at once turns the report into noise. A note waiting on
    an approval is urgent, because the run behind it is standing still.
    """

    def __init__(self, live: LiveSession, clock=None) -> None:
        self._live = live
        # Injectable rather than patching ``time.monotonic``: that module is the event
        # loop's own clock, so a test that freezes it freezes asyncio with it.
        self._clock = clock or time.monotonic
        self._notes: deque[str] = deque()
        self._spoken_at = 0.0

    def speaking(self) -> None:
        """Stamp the model as busy; called for every audio frame forwarded."""
        self._spoken_at = self._clock()

    def push(self, text: str, *, urgent: bool = False) -> None:
        (self._notes.appendleft if urgent else self._notes.append)(text)

    def waiting(self) -> int:
        return len(self._notes)

    async def release_quietly(self) -> bool:
        """Hand over one note if the model has stopped speaking. True when it did.

        Popped only after the send succeeds: a socket that dies mid-delivery must not eat
        the result, because the pump tries again on its next wake-up.
        """
        if not self._notes or self._clock() - self._spoken_at < QUIET_BEFORE_NOTE_S:
            return False
        await self._live.send_text(self._notes[0])
        self._notes.popleft()
        return True


async def _browser_to_live(browser: Any, live: LiveSession, note: dict) -> None:
    while True:
        message = await browser.receive_text()
        if len(message) > MAX_BROWSER_FRAME_BYTES:
            continue
        kind = _frame_type(message)
        if kind == "audio":
            await live.send_audio(_b64(message))
        elif kind == "text":
            await live.send_text(_text(message))
        elif kind == "close":
            note["reason"] = "browser closed the call"
            return


async def _handle_tool_call(call: dict, live: LiveSession, browser: Any,
                            book: agent_lane.RunBook, session_key: str | None,
                            profile: str | None, notes: NoteQueue) -> None:
    """Receipt first, work in the pool. The only await here is a small local socket send.

    The rule is about not waiting on a *run* (seconds to minutes), not about never
    awaiting: a receipt frame is what lets the model keep talking, so it goes out now.
    """
    call_id = str(call.get("id") or "")
    name = call.get("name")
    args = call.get("args") or {}
    if not call_id:
        logger.warning("hermes-gemini-live: tool call with no id was dropped")
        return
    if name == tools_module.BOARD_NAME:
        await live.send_function_response(call_id, book.board(), name)
        return
    if name == tools_module.UPDATE_NAME:
        await _update_task(call_id, args, live, browser, book, session_key, profile)
        return

    task = tools_module.compose_prompt(args)
    if not task:
        await live.send_function_response(call_id, "No task text was supplied.", name)
        return

    loop = asyncio.get_running_loop()
    await live.send_function_response(
        call_id,
        f"WORK_STARTED {call_id}: Hermes is running it. Do not describe any result yet.",
        name)
    # The panel shows what the user asked for, not the shaping instruction appended for the
    # worker — otherwise every task row ends with a paragraph about voice output.
    await browser.send_text(_dump({"type": "task_started", "id": call_id,
                                   "prompt": str(args.get("task") or "").strip()[:200]}))
    agent_lane.start_task(
        task,
        on_finished=_make_finisher(notes, browser, loop),
        session_key=session_key,
        book=book,
        profile=profile,
        # One name per task: the id in this receipt is the id the model addresses later.
        task_id=call_id,
        on_needs_input=_make_parker(notes, browser, loop),
    )


async def _update_task(call_id: str, args: dict, live: LiveSession, browser: Any,
                       book: agent_lane.RunBook, session_key: str | None,
                       profile: str | None) -> None:
    """Answer, redirect or abandon a task the model already started.

    These hit the api_server, so they go off-loop with ``to_thread``: the audio loop keeps
    running, only this one tool call waits — which is what the model asked for.
    """
    task = str(args.get("task") or "").strip().lstrip("#")
    action = str(args.get("action") or "").strip().lower()
    answer = str(args.get("answer") or "").strip()
    if action not in tools_module.ACTIONS:
        await live.send_function_response(
            call_id, f"BAD_REQUEST unknown action '{action}'; "
                     f"use {', '.join(tools_module.ACTIONS)}.")
        return
    run_id = book.run_id(task)
    if not run_id:
        await live.send_function_response(
            call_id, f"NO_SUCH_TASK {task}: it is not on this call's board. "
                     f"Call hermes_tasks to see what is.")
        return

    try:
        if action in ("approve", "deny"):
            agent_lane.approve(run_id, "once" if action == "approve" else "deny",
                               book.request_id(task), session_key=session_key, profile=profile)
        elif action == "steer":
            agent_lane.steer(run_id, answer or "The user has no further instruction.",
                             session_key=session_key, profile=profile)
        else:
            agent_lane.stop(run_id, session_key=session_key, profile=profile)
    except agent_lane.LaneUnavailable as exc:
        # A refused control is news the model must say out loud — silently retrying would
        # leave the user believing their answer landed.
        book.set_state(task, "needs_input", question=str(exc))
        await live.send_function_response(call_id, f"UPDATE_FAILED {task}: {exc}")
        await browser.send_text(_dump({"type": "task_failed", "id": task,
                                       "detail": str(exc)[:200]}))
        return

    book.set_state(task, "running" if action in ("approve", "steer") else "cancelled")
    await live.send_function_response(call_id, f"UPDATE_DONE {task} {action}.")
    await browser.send_text(_dump({"type": "task_update", "id": task, "action": action}))


def _hand_off(coro, loop: asyncio.AbstractEventLoop, task_id: str, why: str) -> None:
    try:
        asyncio.run_coroutine_threadsafe(coro, loop)
    except RuntimeError:
        # The loop is gone: the call ended while Hermes was still working. The run keeps
        # going and its news simply goes unspoken, which is said here rather than dying
        # quietly on a worker thread.
        logger.info("hermes-gemini-live: task %s %s after its call closed", task_id, why)


def _make_parker(notes: NoteQueue, browser: Any, loop: asyncio.AbstractEventLoop):
    """A worker-thread callback that queues the question a parked run is waiting on."""
    def on_needs_input(task_id: str, question: str, request_id: str) -> None:
        async def deliver() -> None:
            notes.push(NEEDS_NOTE.format(task_id=task_id, question=question), urgent=True)
            await browser.send_text(_dump({"type": "task_needs_input", "id": task_id,
                                           "question": question[:200]}))
            await _release(notes, task_id, "is waiting")

        _hand_off(deliver(), loop, task_id, "was waiting")

    return on_needs_input


def _make_finisher(notes: NoteQueue, browser: Any,
                   loop: asyncio.AbstractEventLoop):
    """A worker-thread callback that queues a finished result for the next gap in speech."""
    def on_finished(task_id: str, state: str, text: str) -> None:
        async def deliver() -> None:
            notes.push(RESULT_NOTE.format(task_id=task_id, state=state, text=text or "no output"))
            await browser.send_text(_dump({"type": "task_done", "id": task_id,
                                           "state": state}))
            await _release(notes, task_id, "finished")

        _hand_off(deliver(), loop, task_id, "finished")

    return on_finished


async def _release(notes: NoteQueue, task_id: str, why: str) -> None:
    """Try to speak a note straight away if the model happens to be quiet.

    A dead socket here is not a lost result: the note stays queued and the pump retries it
    on its next wake-up, so the only thing to report is that the early attempt failed.
    """
    try:
        await notes.release_quietly()
    except LiveError as exc:
        logger.info("hermes-gemini-live: task %s %s but the call is closing (%s)",
                    task_id, why, exc)


async def _live_to_browser(browser: Any, live: LiveSession, note: dict,
                           book: agent_lane.RunBook, session_key: str | None,
                           profile: str | None, notes: NoteQueue) -> None:
    while True:
        try:
            # The timeout is not a liveness check, it is the other half of the queue: a
            # result that landed mid-sentence has to be released once the model stops, and
            # frames only arrive while it is talking.
            raw = await asyncio.wait_for(live.recv(), timeout=QUIET_BEFORE_NOTE_S)
        except asyncio.TimeoutError:
            await notes.release_quietly()
            continue
        for frame in wire.browser_frames(raw):
            if frame["type"] == "audio":
                notes.speaking()
            await browser.send_text(_dump(frame))
            if frame["type"] == "error":
                note["reason"] = frame["detail"]
                return
            if frame["type"] in TERMINAL_FRAME_TYPES:
                note["reason"] = f"server ended the session ({frame.get('timeLeft') or 'now'})"
                return
            if frame["type"] == "tool_call":
                for call in frame["calls"]:
                    await _handle_tool_call(call, live, browser, book, session_key,
                                            profile, notes)
        await notes.release_quietly()


def _loads(message: str) -> dict:
    try:
        value = json.loads(message)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _frame_type(message: str) -> str:
    value = _loads(message)
    kind = value.get("type")
    if kind == "audio" and not str(value.get("data") or "").strip():
        return ""
    if kind == "text" and not str(value.get("text") or "").strip():
        return ""
    return kind if kind in ("audio", "text", "close") else ""


def _b64(message: str) -> str:
    return str(_loads(message).get("data") or "")


def _text(message: str) -> str:
    return str(_loads(message).get("text") or "")[:4000]


def _dump(frame: dict) -> str:
    return json.dumps(frame, separators=(",", ":"))


async def run_relay(browser: Any, session_key: str | None = None,
                     profile: str | None = None) -> None:
    """Own the whole call. The caller has already accepted the upgrade."""
    note: dict = {"reason": ""}
    # The serving home decides the profile, not a query parameter a client could send:
    # an unprefixed run resumes in the DEFAULT profile's store, so this is the one place
    # that must be right for delegated work to land in the right memory.
    profile = profile or config.serving_profile()
    # Off the audio loop: a probe is a network round trip, and a slow gateway must not
    # cost the user a dropped syllable.
    lane_status, lane_detail = await asyncio.to_thread(agent_lane.probe, session_key, profile)
    offer_tools = lane_status == agent_lane.STATUS_OK
    if not offer_tools:
        logger.warning("hermes-gemini-live: agent lane unavailable (%s: %s)",
                       lane_status, lane_detail)
    try:
        live = await LiveSession.open(tools_module.declarations() if offer_tools else None)
    except (LiveError, ConfigError) as exc:
        logger.warning("hermes-gemini-live: call refused: %s", exc)
        await browser.send_text(_dump({"type": "error", "detail": str(exc)}))
        return
    if not offer_tools:
        await browser.send_text(_dump({"type": "lane", "status": lane_status,
                                       "detail": lane_detail}))

    book = agent_lane.RunBook()
    notes = NoteQueue(live)
    up = asyncio.create_task(_browser_to_live(browser, live, note))
    down = asyncio.create_task(_live_to_browser(browser, live, note, book, session_key,
                                                profile, notes))
    try:
        done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            error = task.exception()
            if not error:
                continue
            note["reason"] = str(error)
            if task is up:
                # Which pump failed says who left. The browser pump only dies when the page
                # goes away — a navigation, not a fault — and there is nobody to tell, so
                # this is logged as an ordinary end. The panel used to receive an error
                # frame here, which read as "the system failed" every time the user clicked
                # somewhere else mid-call.
                logger.info("hermes-gemini-live: call ended (%s from the browser side)",
                            type(error).__name__)
                continue
            logger.warning("hermes-gemini-live: relay pump failed: %s: %s",
                           type(error).__name__, error)
            await browser.send_text(_dump({"type": "error", "detail": _safe(error),
                                           "ended": True}))
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        # A pump that lost the race can finish with an exception nobody asked for — the
        # cancel lands after it already raised. Retrieving it here keeps asyncio from
        # printing a bare traceback that looks like a crash in the log.
        for task in (up, down):
            if task.done() and not task.cancelled():
                task.exception()
        await live.close()
        if notes.waiting():
            # Queued and never spoken: the result is real, the user simply hung up first.
            logger.info("hermes-gemini-live: call ended with %d result(s) still queued",
                        notes.waiting())
        if book.pending():
            logger.info("hermes-gemini-live: call ended with %d run(s) still going: %s",
                        len(book.pending()), ",".join(book.pending()))
        logger.info("hermes-gemini-live: call ended (%s)", note["reason"] or "ended")


def _safe(error: BaseException) -> str:
    text = str(error) or type(error).__name__
    return text if "key=" not in text else "the Gemini Live socket failed"
