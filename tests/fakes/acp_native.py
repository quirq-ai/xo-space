import json

from tests.fakes import answer, expect, scenario
from tests.fakes.agy import acp


def catalog(provider):
    if provider == "cursor":
        return [answer(["--version"], "2026.11.22-e9042b2"), answer(["--list-models"], "auto - Auto"),
                answer(["status", "--format", "json"], '{"isAuthenticated":true}')]
    return [answer(["--version"], "grok 1.0.92"), answer(["models"], "Available models:\n* grok-4.6")]


def cases(provider):
    def build(case):
        auth = "cursor_login" if provider == "cursor" else "grok.com"
        steps = acp.call("initialize", {"protocolVersion": 1, "agentCapabilities": {"loadSession": True},
                                        "authMethods": [{"id": auth}]})
        if provider == "cursor":
            steps += acp.call("authenticate", {}, methodId="cursor_login")
        steps += acp.call("session/new", {"sessionId": acp.SID})
        flat = provider == "cursor"
        steps.append({"plugin_hook": "sessionStart" if flat else "SessionStart"})
        if case == "coordinator_write":
            if flat:
                steps += acp.call("session/set_mode", {}, modeId="ask")
        if case == "close":
            return scenario(steps, argv=catalog(provider))
        steps.append(acp.prompt())
        if case == "unknown_wire":
            steps.append(acp.update("brand_new_update"))
        else:
            steps.append(acp.tool_call("c1", "Shell", {"command": "ls"}, kind="execute"))
        if case == "coordinator_write":
            steps.append({"plugin_hook": "preToolUse" if flat else "PreToolUse",
                          "payload": {"tool_name": "Write", "tool_use_id": "c1",
                                      "tool_input": {"file_path": "/work/x.txt", "content": "hi"}}, "exit": 2})
        if case == "held_permission":
            steps.append({"emit": {"jsonrpc": "2.0", "id": 99, "method": "session/request_permission",
                                   "params": {"sessionId": acp.SID, "toolCall": {"toolCallId": "c1", "title": "Shell"},
                                              "options": [dict(kind=k, optionId=k) for k in ("allow_once", "reject_once")]}}})
            wanted = "allow_once" if case == "held_permission" else "reject_once"
            steps.append(expect({"id": 99, "result": {"outcome": {"optionId": wanted}}}))
        if case == "interrupt":
            steps.append(expect({"method": "session/cancel"}))
        if case != "unknown_wire":
            steps.append(acp.tool_done("c1", "Shell", {}, status="completed"))
        if case == "interrupt":
            steps += [acp.done("cancelled"), acp.prompt()]
        steps.extend([*acp.say("done"), acp.done()])
        return scenario(steps, argv=catalog(provider))
    return {name: (lambda case=name: build(case)) for name in
            ("turn", "close", "coordinator_write", "held_permission", "interrupt", "unknown_wire")}


def transcript(home, provider):
    root, child = "parent", "child"
    if provider == "cursor":
        folder = home / ".cursor/projects/work/agent-transcripts" / root
        (folder / "subagents").mkdir(parents=True)
        (folder / f"{root}.jsonl").touch()
        path = folder / "subagents" / f"{child}.jsonl"
        rows = [{"role": role, "message": {"content": [block]}} for role, block in (
            ("user", {"type": "text", "text": "question"}),
            ("assistant", {"type": "tool_use", "id": "c1", "name": "Read"}),
            ("user", {"type": "tool_result", "tool_use_id": "c1", "content": "result"}),
            ("assistant", {"type": "text", "text": "answer"}))]
    else:
        folder = home / ".grok/sessions/work"
        for key in (root, child):
            (folder / key).mkdir(parents=True)
            (folder / key / "summary.json").write_text(json.dumps({"info": {"id": key, "cwd": "/work"}}))
        child_meta = folder / root / "subagents" / child
        child_meta.mkdir(parents=True)
        (child_meta / "meta.json").write_text("{}")
        path = folder / child / "updates.jsonl"
        rows = [step["emit"] for step in (
            acp.update("user_message_chunk", content={"text": "question"}),
            acp.tool_call("c1", "Read", {}, kind="read"), acp.tool_done("c1", "Read", {}),
            *acp.say("answer"))]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return root, child
