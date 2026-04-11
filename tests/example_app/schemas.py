from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID


# Response schemas
class WorkflowSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str
    instructions: str
    tools_config: dict[str, Any]
    is_active: bool
    created_at: datetime
    updated_at: datetime
    knowledgebases_list: list[str] = Field(alias="get_knowledgebases_list")


class KnowledgebaseSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str
    owner_id: str | None = None
    created_at: datetime
    updated_at: datetime
    article_count: int = Field(default=0)


class ArticleSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    knowledgebase: UUID
    knowledgebase_name: str = Field(alias="knowledgebase.name")
    hierarchy_code: str
    title: str
    content: str
    summary: str | None = None
    created_at: datetime
    updated_at: datetime


class UserChatSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user: int
    user_username: str = Field(alias="user.username")
    workflow: UUID
    workflow_name: str = Field(alias="workflow.name")
    title: str
    is_active: bool
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    message_count: int = Field(default=0)
    last_message_at: datetime | None = None


class ChatMessageSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    chat: UUID
    chat_title: str = Field(alias="chat.title")
    message_type: str
    role: str
    content: str
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class UserSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str
    first_name: str
    last_name: str
    date_joined: datetime


# Input schemas for creating/updating resources
class WorkflowCreateSchema(BaseModel):
    name: str = Field(max_length=255)
    description: str
    instructions: str
    tools_config: dict[str, Any] | None = Field(default_factory=dict)
    is_active: bool = True
    knowledgebases: list[UUID] | None = Field(default_factory=list)


class WorkflowUpdateSchema(BaseModel):
    name: str | None = Field(None, max_length=255)
    description: str | None = None
    instructions: str | None = None
    tools_config: dict[str, Any] | None = None
    is_active: bool | None = None
    knowledgebases: list[UUID] | None = None


class KnowledgebaseCreateSchema(BaseModel):
    name: str = Field(max_length=255)
    description: str
    workflows: list[UUID] | None = Field(default_factory=list)


class KnowledgebaseUpdateSchema(BaseModel):
    name: str | None = Field(None, max_length=255)
    description: str | None = None
    workflows: list[UUID] | None = None


class ArticleCreateSchema(BaseModel):
    knowledgebase: UUID
    hierarchy_code: str = Field(max_length=16)
    title: str = Field(max_length=512)
    content: str
    summary: str | None = None


class ArticleUpdateSchema(BaseModel):
    hierarchy_code: str | None = Field(None, max_length=16)
    title: str | None = Field(None, max_length=512)
    content: str | None = None
    summary: str | None = None


class ArticleSearchSchema(BaseModel):
    query: str = Field(description="Search query text for semantic search")
    top_k: int = Field(
        default=10, ge=1, le=50, description="Number of results to return (1-50)"
    )
    search_type: str = Field(
        default="multi_field",
        description="Type of semantic search to perform",
        pattern="^(content|summary|multi_field)$",
    )


class UserChatCreateSchema(BaseModel):
    workflow: UUID
    title: str = Field(default="New Chat", max_length=255)
    metadata: dict[str, Any] | None = Field(default_factory=dict)


class UserChatUpdateSchema(BaseModel):
    title: str | None = Field(None, max_length=255)
    is_active: bool | None = None
    metadata: dict[str, Any] | None = None


class ChatMessageCreateSchema(BaseModel):
    message_type: str
    role: str
    content: str
    metadata: dict[str, Any] | None = Field(default_factory=dict)


# Authentication schemas
class LoginSchema(BaseModel):
    username: str
    password: str


class TokenSchema(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


# Response wrapper schemas
class ArticleSearchResponseSchema(BaseModel):
    query: str
    search_type: str
    count: int
    results: list[ArticleSchema]


class TableOfContentsResponseSchema(BaseModel):
    table_of_contents: str


class PaginatedResponseSchema(BaseModel):
    count: int
    next: str | None = None
    previous: str | None = None
    results: list[Any]


class ErrorSchema(BaseModel):
    detail: str
    code: str | None = None
