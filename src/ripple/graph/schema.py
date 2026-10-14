from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class NodeType(StrEnum):
    FILE = "File"
    FUNCTION = "Function"
    CLASS = "Class"
    API_ENDPOINT = "APIEndpoint"
    DB_TABLE = "DBTable"
    TEST = "Test"


class EdgeType(StrEnum):
    CALLS = "CALLS"
    DEFINED_IN = "DEFINED_IN"
    HANDLES = "HANDLES"
    QUERIES = "QUERIES"
    TESTS = "TESTS"
    IMPORTS = "IMPORTS"


def file_id(path: str) -> str:
    return path


def symbol_id(path: str, qualname: str) -> str:
    return f"{path}::{qualname}"


def endpoint_id(method: str, route: str) -> str:
    return f"endpoint::{method.upper()} {route}"


def table_id(name: str) -> str:
    return f"table: {name.lower()}"


class _Node(BaseModel):
    id: str
    name: str

    def to_properties(self) -> dict:
        return self.model_dump(mode="json", exclude_none=True, exclude={"type"})


class SymbolNode(_Node):
    qualname: str
    file_path: str
    line_start: int
    line_end: int


class FileNode(_Node):
    type: Literal[NodeType.FILE] = NodeType.FILE
    path: str
    content_hash: str
    is_test: bool = False


class FunctionNode(SymbolNode):
    type: Literal[NodeType.FUNCTION] = NodeType.FUNCTION
    is_async: bool = False
    class_name: str | None = None
    decorators: list[str] = Field(default_factory=list)


class ClassNode(SymbolNode):
    type: Literal[NodeType.CLASS] = NodeType.CLASS
    bases: list[str] = Field(default_factory=list)


class APIEndpointNode(_Node):
    type: Literal[NodeType.API_ENDPOINT] = NodeType.API_ENDPOINT
    method: str
    route: str


class DBTableNode(_Node):
    type: Literal[NodeType.DB_TABLE] = NodeType.DB_TABLE


class TestNode(SymbolNode):
    type: Literal[NodeType.TEST] = NodeType.TEST


Node = Annotated[
    FileNode | FunctionNode | ClassNode | APIEndpointNode | DBTableNode | TestNode,
    Field(discriminator="type"),
]


class Edge(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: EdgeType
    src: str
    dst: str


class CallRef(BaseModel):
    caller_id: str
    target: str
    line: int


class RouteRef(BaseModel):
    handler_id: str
    receiver: str
    method: str
    path: str
    line: int


class UnresolvedReason(StrEnum):
    EXTERNAL = "external"
    BUILTIN = "builtin"
    UNKNOWN_RECEIVER = "unknown_receiver"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


class UnresolvedCall(BaseModel):
    caller_id: str
    target: str
    line: int
    reason: UnresolvedReason


class ImportRef(BaseModel):
    module: str
    names: list[tuple[str, str | None]] = Field(default_factory=list)
    alias: str | None = None
    level: int = 0
    line: int


class ParsedFile(BaseModel):
    path: str
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    calls: list[CallRef] = Field(default_factory=list)
    imports: list[ImportRef] = Field(default_factory=list)
    error: str | None = None


class RouterKind(StrEnum):
    APP = "app"
    ROUTER = "router"


class RouterDecl(BaseModel):
    file_path: str
    var: str
    ctor: str
    kind: RouterKind
    prefix: str | None
    line: int


class MountRef(BaseModel):
    file_path: str
    parent: str
    child: str
    prefix: str | None
    line: int


class FastAPIFacts(BaseModel):
    path: str
    routes: list[RouteRef] = Field(default_factory=list)
    routers: list[RouterDecl] = Field(default_factory=list)
    mounts: list[MountRef] = Field(default_factory=list)


class RouteIssueReason(StrEnum):
    UNKNOWN_RECEIVER = "unknown_receiver"
    UNRESOLVED_MOUNT = "unresolved_mount"
    NON_LITERAL_PREFIX = "non_literal_prefix"
    MOUNT_CYCLE = "mount_cycle"


class RouteIssue(BaseModel):
    file_path: str
    line: int
    subject: str
    reason: RouteIssueReason


class ResolvedRoute(BaseModel):
    handler_id: str
    method: str
    path: str
    declared_path: str
    line: int
    exact: bool
    mounted: bool


class RouteResolution(BaseModel):
    routes: list[ResolvedRoute] = Field(default_factory=list)
    issues: list[RouteIssue] = Field(default_factory=list)
