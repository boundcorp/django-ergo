from typing import List, Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict
from uuid import UUID


# Response schemas
class WorkflowSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    id: UUID
    name: str
    description: str
    instructions: str
    tools_config: Dict[str, Any]
    is_active: bool
    created_at: datetime
    updated_at: datetime
    knowledgebases_list: List[str] = Field(alias="get_knowledgebases_list")


class KnowledgebaseSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    id: UUID
    name: str
    description: str
    owner_id: Optional[str] = None
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
    summary: Optional[str] = None
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
    metadata: Dict[str, Any]
    created_at: datetime
    updated_at: datetime
    message_count: int = Field(default=0)
    last_message_at: Optional[datetime] = None


class ChatMessageSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    id: UUID
    chat: UUID
    chat_title: str = Field(alias="chat.title")
    message_type: str
    role: str
    content: str
    metadata: Dict[str, Any]
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
    tools_config: Optional[Dict[str, Any]] = Field(default_factory=dict)
    is_active: bool = True
    knowledgebases: Optional[List[UUID]] = Field(default_factory=list)


class WorkflowUpdateSchema(BaseModel):
    name: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    instructions: Optional[str] = None
    tools_config: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None
    knowledgebases: Optional[List[UUID]] = None


class KnowledgebaseCreateSchema(BaseModel):
    name: str = Field(max_length=255)
    description: str
    workflows: Optional[List[UUID]] = Field(default_factory=list)


class KnowledgebaseUpdateSchema(BaseModel):
    name: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    workflows: Optional[List[UUID]] = None


class ArticleCreateSchema(BaseModel):
    knowledgebase: UUID
    hierarchy_code: str = Field(max_length=16)
    title: str = Field(max_length=512)
    content: str
    summary: Optional[str] = None


class ArticleUpdateSchema(BaseModel):
    hierarchy_code: Optional[str] = Field(None, max_length=16)
    title: Optional[str] = Field(None, max_length=512)
    content: Optional[str] = None
    summary: Optional[str] = None


class ArticleSearchSchema(BaseModel):
    query: str = Field(description="Search query text for semantic search")
    top_k: int = Field(default=10, ge=1, le=50, description="Number of results to return (1-50)")
    search_type: str = Field(
        default="multi_field",
        description="Type of semantic search to perform",
        pattern="^(content|summary|multi_field)$"
    )


class UserChatCreateSchema(BaseModel):
    workflow: UUID
    title: str = Field(default="New Chat", max_length=255)
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)


class UserChatUpdateSchema(BaseModel):
    title: Optional[str] = Field(None, max_length=255)
    is_active: Optional[bool] = None
    metadata: Optional[Dict[str, Any]] = None


class ChatMessageCreateSchema(BaseModel):
    message_type: str
    role: str
    content: str
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)


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
    results: List[ArticleSchema]


class TableOfContentsResponseSchema(BaseModel):
    table_of_contents: str


class PaginatedResponseSchema(BaseModel):
    count: int
    next: Optional[str] = None
    previous: Optional[str] = None
    results: List[Any]


class ErrorSchema(BaseModel):
    detail: str
    code: Optional[str] = None