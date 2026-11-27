from collections import deque

from services.cowork_agent.engine.sessions_io import find_session_file

from .history import read_page


class Observations:
    def __init__(self, history, *, chunks=False):
        self.history = history
        self.chunks = chunks

    def resolve_native_file(self, meta, session_id):
        native = meta.get("nativeSessionId") or session_id
        return next((path for key, _, path in self.history.files() if key == native and path.is_file()), None)

    def owns_session(self, session_id):
        return self.resolve_native_file({}, session_id) is not None

    def _items(self, session_id, path):
        if path.stat().st_size > 256 * 1024 * 1024:
            raise ValueError("Transcript exceeds maximum scan size")
        after = None
        while True:
            page = read_page(session_id, path, self.history.decode, after=after, limit=256)
            yield from page.items
            after = page.next
            if after is None:
                return

    def get_messages(self, session_id):
        path = find_session_file(session_id)
        if path is None:
            return []
        messages = []
        calls = {}
        previous = None
        for item in self._items(session_id, path):
            role = "user" if item.type == "input" else "assistant"
            if item.type == "tool_result" and item.call_id in calls:
                state = calls.pop(item.call_id)
                state.update(status="error" if item.status == "error" else "completed", output=item.text or "",
                             time_end=item.ts)
                previous = item.type
                continue
            if not messages or role != messages[-1]["data"]["role"] or (role == "user" and not self.chunks):
                messages.append(dict(id=item.id, session_id=session_id, time_created=item.ts,
                                     data={"role": role}, parts=[]))
            message = messages[-1]
            if self.chunks and previous == item.type and item.type in ("input", "text", "thinking"):
                message["parts"][-1]["data"]["text"] += item.text or ""
                continue
            if item.type == "tool_call":
                state = dict(status="running", title=item.title, time_start=item.ts, time_end=None,
                             input={"target": item.target} if item.target else {}, output=None)
                calls[item.call_id] = state
                data = dict(type="tool", tool=item.tool, call_id=item.call_id, state=state)
            else:
                data = dict(type="reasoning" if item.type == "thinking" else "text", text=item.text or "")
            message["parts"].append(dict(id=item.id + "_part", message_id=message["id"], session_id=session_id,
                                         time_created=item.ts, data=data))
            previous = item.type
        return messages

    def prompts(self, session_id, source, label):
        path = self.resolve_native_file({}, session_id)
        if path is None:
            raise FileNotFoundError("Native transcript unavailable")
        prompts = deque(maxlen=200)
        total = 0
        previous = None
        for item in self._items(session_id, path):
            if item.type == "input":
                if previous != "input" or not self.chunks:
                    total += 1
                    prompts.append(dict(turn=total, timestamp=item.ts, text="", truncated=False,
                                        responses=0, tool_uses=0))
                prompt = prompts[-1]
                text = item.text or ""
                remaining = 4000 - len(prompt["text"])
                prompt["text"] += text[:remaining]
                prompt["truncated"] |= len(text) > remaining
            elif prompts and item.type == "text" and (previous != "text" or not self.chunks):
                prompts[-1]["responses"] += 1
            elif prompts and item.type == "tool_call":
                prompts[-1]["tool_uses"] += 1
            previous = item.type
        return dict(source={"id": source, "label": label}, session_id=session_id, supported=True,
                    total_prompts=total, capped=total > len(prompts), prompts=list(prompts))
