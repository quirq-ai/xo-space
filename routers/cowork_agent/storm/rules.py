"""The person's rules (ch 17 §2.5; ch 15 §3, §3.1):

  GET    /api/chat/rules?after_seq=&limit=   the rules in force, and XO's default rules (revoked or not)
  POST   /api/chat/rules                     {scope, project?, thread?, kind, match, effect, sentence, client_id}
  DELETE /api/chat/rules/{id}                revoke a rule, or one of XO's default rules (``default:…``)

Only the person makes rules. A POST carries the sentence the server renders
for it; any other sentence is ``400 rule_sentence_mismatch``, whose
``sentence`` is the one to show the person. A rule keeps the widest tier its
scope allows (a workspace rule covers routine work only; a project rule at
most notable; destructive only as a thread's exact command). Rules live in
XO's store, never in a CLI's settings.
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Query
from pydantic import Field, model_validator

from routers.cowork_agent.bff.errors import ForbidExtra
from routers.cowork_agent.storm.errors import storm_error
from services.cowork_agent.storm.review import queue
from services.cowork_agent.storm.review.rules import Match
from services.errors import ServiceError

router = APIRouter()


class McpTool(ForbidExtra):
    server: str = Field(min_length=1)
    tool: str = Field(min_length=1)


class RuleMatch(ForbidExtra):
    command: Optional[list[str]] = Field(None, min_length=1, max_length=100)
    prefix: Optional[list[str]] = Field(None, min_length=1, max_length=100)
    path_glob: Optional[str] = Field(None, min_length=1, max_length=1000)
    host: Optional[str] = Field(None, min_length=1, max_length=255)
    mcp: Optional[McpTool] = None

    @model_validator(mode="after")
    def exactly_one(self) -> "RuleMatch":
        if sum(v is not None for v in (self.command, self.prefix, self.path_glob, self.host, self.mcp)) != 1:
            raise ValueError("a rule matches exactly one of command, prefix, path_glob, host, mcp")
        return self

    def to_match(self) -> Match:
        return Match(command=tuple(self.command) if self.command else None,
                     prefix=tuple(self.prefix) if self.prefix else None, path_glob=self.path_glob,
                     host=self.host.lower() if self.host else None,
                     mcp=(self.mcp.server, self.mcp.tool) if self.mcp else None)


class RuleBody(ForbidExtra):
    scope: Literal["thread", "project", "workspace"]
    project: Optional[str] = Field(None, min_length=1)
    thread: Optional[str] = Field(None, min_length=1)
    kind: Literal["create_file", "edit_file", "delete_file", "read", "search", "list", "command", "web_fetch", "mcp"]
    match: RuleMatch
    effect: Literal["allow", "deny"]
    sentence: str = Field(max_length=2000)
    client_id: str = Field(min_length=1, max_length=200)


@router.get("/api/chat/rules")
async def list_rules(after_seq: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)) -> dict:
    try:
        return await queue.current().rules(after_seq=after_seq, limit=limit)
    except ServiceError as exc:
        raise storm_error(exc)


@router.post("/api/chat/rules")
async def make_rule(body: RuleBody) -> dict:
    try:
        return await queue.current().make_rule(scope=body.scope, kind=body.kind, match=body.match.to_match(),
                                               effect=body.effect, sentence=body.sentence, project_pid=body.project,
                                               thread_ts=body.thread, client_id=body.client_id)
    except ServiceError as exc:
        raise storm_error(exc)


@router.delete("/api/chat/rules/{rule_id}")
async def revoke_rule(rule_id: str) -> dict:
    try:
        return await queue.current().revoke_rule(rule_id)
    except ServiceError as exc:
        raise storm_error(exc)
