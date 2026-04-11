from uuid import UUID

from django.contrib.auth import get_user_model
from django.db.models import Count
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django_ergo.models import Article
from django_ergo.models import ChatMessage
from django_ergo.models import Knowledgebase
from django_ergo.models import UserChat
from django_ergo.models import Workflow
from ninja import NinjaAPI
from ninja import Query
from ninja.errors import HttpError
from ninja.pagination import PageNumberPagination
from ninja.pagination import paginate

from .auth import ACCESS_TOKEN_EXPIRE_MINUTES
from .auth import authenticate_user
from .auth import create_access_token
from .auth import jwt_auth
from .schemas import ArticleCreateSchema
from .schemas import ArticleSchema
from .schemas import ArticleSearchResponseSchema
from .schemas import ArticleSearchSchema
from .schemas import ArticleUpdateSchema
from .schemas import ChatMessageCreateSchema
from .schemas import ChatMessageSchema
from .schemas import KnowledgebaseCreateSchema
from .schemas import KnowledgebaseSchema
from .schemas import KnowledgebaseUpdateSchema
from .schemas import LoginSchema
from .schemas import TableOfContentsResponseSchema
from .schemas import TokenSchema
from .schemas import UserChatCreateSchema
from .schemas import UserChatSchema
from .schemas import UserChatUpdateSchema
from .schemas import UserSchema
from .schemas import WorkflowCreateSchema
from .schemas import WorkflowSchema
from .schemas import WorkflowUpdateSchema

User = get_user_model()

# Initialize API
api = NinjaAPI(
    title="Django Ergo API",
    description="AI Knowledgebase Toolkit API for Django applications",
    version="1.0.0",
    docs_url="/docs/",
)


# Custom pagination class
class StandardPagination(PageNumberPagination):
    page_size = 20
    max_page_size = 100


# Authentication endpoints
@api.post("/auth/token/", response=TokenSchema, tags=["Authentication"])
def login(request, credentials: LoginSchema):
    """Obtain JWT access token"""
    user = authenticate_user(credentials.username, credentials.password)
    if not user:
        raise HttpError(401, "Invalid credentials")

    access_token = create_access_token(data={"sub": str(user.id)})
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,  # Convert to seconds
    }


# User endpoints
@api.get("/users/me/", response=UserSchema, auth=jwt_auth, tags=["Users"])
def get_current_user(request):
    """Get current user information"""
    return request.auth


# Workflow endpoints
@api.get(
    "/workflows/", response=list[WorkflowSchema], auth=jwt_auth, tags=["Workflows"]
)
@paginate(StandardPagination)
def list_workflows(request, is_active: bool | None = Query(None)):
    """List all workflows with optional filtering"""
    queryset = Workflow.objects.all()

    if is_active is not None:
        queryset = queryset.filter(is_active=is_active)

    return queryset


@api.get(
    "/workflows/{workflow_id}/",
    response=WorkflowSchema,
    auth=jwt_auth,
    tags=["Workflows"],
)
def get_workflow(request, workflow_id: UUID):
    """Get a specific workflow"""
    return get_object_or_404(Workflow, id=workflow_id)


@api.post("/workflows/", response=WorkflowSchema, auth=jwt_auth, tags=["Workflows"])
def create_workflow(request, payload: WorkflowCreateSchema):
    """Create a new workflow"""
    # Extract knowledgebases from payload
    knowledgebases_data = payload.knowledgebases
    workflow_data = payload.model_dump(exclude={"knowledgebases"})

    workflow = Workflow.objects.create(**workflow_data)

    if knowledgebases_data:
        workflow.knowledgebases.set(knowledgebases_data)

    return workflow


@api.put(
    "/workflows/{workflow_id}/",
    response=WorkflowSchema,
    auth=jwt_auth,
    tags=["Workflows"],
)
def update_workflow(request, workflow_id: UUID, payload: WorkflowUpdateSchema):
    """Update a workflow"""
    workflow = get_object_or_404(Workflow, id=workflow_id)

    # Extract knowledgebases from payload
    knowledgebases_data = payload.knowledgebases
    workflow_data = payload.model_dump(exclude={"knowledgebases"}, exclude_unset=True)

    for attr, value in workflow_data.items():
        setattr(workflow, attr, value)
    workflow.save()

    if knowledgebases_data is not None:
        workflow.knowledgebases.set(knowledgebases_data)

    return workflow


@api.delete("/workflows/{workflow_id}/", auth=jwt_auth, tags=["Workflows"])
def delete_workflow(request, workflow_id: UUID):
    """Delete a workflow"""
    workflow = get_object_or_404(Workflow, id=workflow_id)
    workflow.delete()
    return {"success": True}


# Knowledgebase endpoints
@api.get(
    "/knowledgebases/",
    response=list[KnowledgebaseSchema],
    auth=jwt_auth,
    tags=["Knowledge Bases"],
)
@paginate(StandardPagination)
def list_knowledgebases(request):
    """List knowledge bases (user can see their own or public ones)"""
    user_id = str(request.auth.id)
    return Knowledgebase.objects.filter(
        Q(owner_id=user_id) | Q(owner_id__isnull=True) | Q(owner_id="")
    ).annotate(article_count=Count("articles"))


@api.get(
    "/knowledgebases/{kb_id}/",
    response=KnowledgebaseSchema,
    auth=jwt_auth,
    tags=["Knowledge Bases"],
)
def get_knowledgebase(request, kb_id: UUID):
    """Get a specific knowledge base"""
    user_id = str(request.auth.id)
    return get_object_or_404(
        Knowledgebase.objects.annotate(article_count=Count("articles")),
        id=kb_id,
        **({"owner_id": user_id} if True else {}),  # Add access control
    )


@api.post(
    "/knowledgebases/",
    response=KnowledgebaseSchema,
    auth=jwt_auth,
    tags=["Knowledge Bases"],
)
def create_knowledgebase(request, payload: KnowledgebaseCreateSchema):
    """Create a new knowledge base"""
    workflows_data = payload.workflows
    kb_data = payload.model_dump(exclude={"workflows"})
    kb_data["owner_id"] = str(request.auth.id)

    kb = Knowledgebase.objects.create(**kb_data)

    if workflows_data:
        kb.workflows.set(workflows_data)

    # Add article_count annotation
    kb.article_count = 0
    return kb


@api.put(
    "/knowledgebases/{kb_id}/",
    response=KnowledgebaseSchema,
    auth=jwt_auth,
    tags=["Knowledge Bases"],
)
def update_knowledgebase(request, kb_id: UUID, payload: KnowledgebaseUpdateSchema):
    """Update a knowledge base"""
    user_id = str(request.auth.id)
    kb = get_object_or_404(Knowledgebase, id=kb_id, owner_id=user_id)

    workflows_data = payload.workflows
    kb_data = payload.model_dump(exclude={"workflows"}, exclude_unset=True)

    for attr, value in kb_data.items():
        setattr(kb, attr, value)
    kb.save()

    if workflows_data is not None:
        kb.workflows.set(workflows_data)

    # Add article_count annotation
    kb.article_count = kb.articles.count()
    return kb


@api.delete("/knowledgebases/{kb_id}/", auth=jwt_auth, tags=["Knowledge Bases"])
def delete_knowledgebase(request, kb_id: UUID):
    """Delete a knowledge base"""
    user_id = str(request.auth.id)
    kb = get_object_or_404(Knowledgebase, id=kb_id, owner_id=user_id)
    kb.delete()
    return {"success": True}


@api.get(
    "/knowledgebases/{kb_id}/table_of_contents/",
    response=TableOfContentsResponseSchema,
    auth=jwt_auth,
    tags=["Knowledge Bases"],
)
def get_table_of_contents(request, kb_id: UUID):
    """Get table of contents for a knowledge base"""
    user_id = str(request.auth.id)
    kb = get_object_or_404(
        Knowledgebase,
        id=kb_id,
        **({"owner_id": user_id} if True else {}),  # Add access control
    )
    return {"table_of_contents": kb.get_table_of_contents()}


# Article endpoints
_KNOWLEDGEBASE_QUERY = Query(None)
_HIERARCHY_PREFIX_QUERY = Query(None)


@api.get("/articles/", response=list[ArticleSchema], auth=jwt_auth, tags=["Articles"])
@paginate(StandardPagination)
def list_articles(
    request,
    knowledgebase: UUID | None = _KNOWLEDGEBASE_QUERY,
    hierarchy_prefix: str | None = _HIERARCHY_PREFIX_QUERY,
):
    """List articles with optional filtering"""
    user_id = str(request.auth.id)

    # Base queryset with access control
    queryset = Article.objects.select_related("knowledgebase").filter(
        Q(knowledgebase__owner_id=user_id)
        | Q(knowledgebase__owner_id__isnull=True)
        | Q(knowledgebase__owner_id="")
    )

    if knowledgebase:
        queryset = queryset.filter(knowledgebase__id=knowledgebase)

    if hierarchy_prefix:
        queryset = queryset.filter(hierarchy_code__startswith=hierarchy_prefix)

    return queryset


@api.get(
    "/articles/{article_id}/", response=ArticleSchema, auth=jwt_auth, tags=["Articles"]
)
def get_article(request, article_id: UUID):
    """Get a specific article"""
    user_id = str(request.auth.id)
    return get_object_or_404(
        Article.objects.select_related("knowledgebase"),
        id=article_id,
        knowledgebase__owner_id__in=[user_id, None, ""],
    )


@api.post("/articles/", response=ArticleSchema, auth=jwt_auth, tags=["Articles"])
def create_article(request, payload: ArticleCreateSchema):
    """Create a new article"""
    user_id = str(request.auth.id)

    # Verify user can access the knowledgebase
    get_object_or_404(
        Knowledgebase,
        id=payload.knowledgebase,
        **({"owner_id": user_id} if True else {}),  # Add access control
    )

    return Article.objects.create(**payload.model_dump())


@api.put(
    "/articles/{article_id}/", response=ArticleSchema, auth=jwt_auth, tags=["Articles"]
)
def update_article(request, article_id: UUID, payload: ArticleUpdateSchema):
    """Update an article"""
    user_id = str(request.auth.id)
    article = get_object_or_404(
        Article, id=article_id, knowledgebase__owner_id__in=[user_id, None, ""]
    )

    article_data = payload.model_dump(exclude_unset=True)
    for attr, value in article_data.items():
        setattr(article, attr, value)
    article.save()
    return article


@api.delete("/articles/{article_id}/", auth=jwt_auth, tags=["Articles"])
def delete_article(request, article_id: UUID):
    """Delete an article"""
    user_id = str(request.auth.id)
    article = get_object_or_404(
        Article, id=article_id, knowledgebase__owner_id__in=[user_id, None, ""]
    )
    article.delete()
    return {"success": True}


@api.post(
    "/articles/search/",
    response=ArticleSearchResponseSchema,
    auth=jwt_auth,
    tags=["Articles"],
)
def search_articles(request, payload: ArticleSearchSchema):
    """Perform semantic search on articles"""
    user_id = str(request.auth.id)

    # Get base queryset with user permissions
    base_queryset = Article.objects.select_related("knowledgebase").filter(
        Q(knowledgebase__owner_id=user_id)
        | Q(knowledgebase__owner_id__isnull=True)
        | Q(knowledgebase__owner_id="")
    )

    # Perform semantic search based on type
    if payload.search_type == "content":
        results = base_queryset.semantic_search_content(payload.query, payload.top_k)
    elif payload.search_type == "summary":
        results = base_queryset.semantic_search_summary(payload.query, payload.top_k)
    else:  # multi_field
        results = base_queryset.multi_field_semantic_search(
            payload.query, payload.top_k
        )

    return {
        "query": payload.query,
        "search_type": payload.search_type,
        "count": len(results),
        "results": list(results),
    }


# User Chat endpoints
@api.get("/chats/", response=list[UserChatSchema], auth=jwt_auth, tags=["Chats"])
@paginate(StandardPagination)
def list_user_chats(request):
    """List user's chats"""
    return (
        UserChat.objects.select_related("user", "workflow")
        .filter(user=request.auth)
        .annotate(message_count=Count("messages"))
    )


@api.get("/chats/{chat_id}/", response=UserChatSchema, auth=jwt_auth, tags=["Chats"])
def get_user_chat(request, chat_id: UUID):
    """Get a specific chat"""
    return get_object_or_404(
        UserChat.objects.select_related("user", "workflow").annotate(
            message_count=Count("messages")
        ),
        id=chat_id,
        user=request.auth,
    )


@api.post("/chats/", response=UserChatSchema, auth=jwt_auth, tags=["Chats"])
def create_user_chat(request, payload: UserChatCreateSchema):
    """Create a new chat"""
    chat_data = payload.model_dump()
    chat_data["user"] = request.auth

    chat = UserChat.objects.create(**chat_data)
    chat.message_count = 0
    chat.last_message_at = None
    return chat


@api.put("/chats/{chat_id}/", response=UserChatSchema, auth=jwt_auth, tags=["Chats"])
def update_user_chat(request, chat_id: UUID, payload: UserChatUpdateSchema):
    """Update a chat"""
    chat = get_object_or_404(UserChat, id=chat_id, user=request.auth)

    chat_data = payload.model_dump(exclude_unset=True)
    for attr, value in chat_data.items():
        setattr(chat, attr, value)
    chat.save()

    chat.message_count = chat.messages.count()
    chat.last_message_at = chat.messages.order_by("-created_at").first()
    if chat.last_message_at:
        chat.last_message_at = chat.last_message_at.created_at

    return chat


@api.delete("/chats/{chat_id}/", auth=jwt_auth, tags=["Chats"])
def delete_user_chat(request, chat_id: UUID):
    """Delete a chat"""
    chat = get_object_or_404(UserChat, id=chat_id, user=request.auth)
    chat.delete()
    return {"success": True}


@api.get(
    "/chats/{chat_id}/messages/",
    response=list[ChatMessageSchema],
    auth=jwt_auth,
    tags=["Chats"],
)
@paginate(StandardPagination)
def get_chat_messages(request, chat_id: UUID):
    """Get messages for a chat"""
    chat = get_object_or_404(UserChat, id=chat_id, user=request.auth)
    return chat.messages.select_related("chat").order_by("created_at")


@api.post(
    "/chats/{chat_id}/add_message/",
    response=ChatMessageSchema,
    auth=jwt_auth,
    tags=["Chats"],
)
def add_message_to_chat(request, chat_id: UUID, payload: ChatMessageCreateSchema):
    """Add a message to a chat"""
    chat = get_object_or_404(UserChat, id=chat_id, user=request.auth)

    message_data = payload.model_dump()
    message_data["chat"] = chat

    return ChatMessage.objects.create(**message_data)


# Chat Message endpoints (direct)
@api.get(
    "/messages/", response=list[ChatMessageSchema], auth=jwt_auth, tags=["Messages"]
)
@paginate(StandardPagination)
def list_chat_messages(request):
    """List user's chat messages"""
    return (
        ChatMessage.objects.select_related("chat")
        .filter(chat__user=request.auth)
        .order_by("-created_at")
    )


@api.get(
    "/messages/{message_id}/",
    response=ChatMessageSchema,
    auth=jwt_auth,
    tags=["Messages"],
)
def get_chat_message(request, message_id: UUID):
    """Get a specific message"""
    return get_object_or_404(
        ChatMessage.objects.select_related("chat"),
        id=message_id,
        chat__user=request.auth,
    )


@api.post("/messages/", response=ChatMessageSchema, auth=jwt_auth, tags=["Messages"])
def create_chat_message(request, payload: ChatMessageCreateSchema):
    """Create a new message"""
    # Note: The chat field should be provided in the payload as UUID
    # We need to validate that the user owns the chat
    message_data = payload.model_dump()

    # This would need chat_id to be passed separately or included in schema
    # For now, this endpoint might be used differently than the chat-specific one
    return ChatMessage.objects.create(**message_data)


@api.put(
    "/messages/{message_id}/",
    response=ChatMessageSchema,
    auth=jwt_auth,
    tags=["Messages"],
)
def update_chat_message(request, message_id: UUID, payload: ChatMessageCreateSchema):
    """Update a message"""
    message = get_object_or_404(ChatMessage, id=message_id, chat__user=request.auth)

    message_data = payload.model_dump(exclude_unset=True)
    for attr, value in message_data.items():
        if attr != "chat":  # Don't allow changing chat
            setattr(message, attr, value)
    message.save()

    return message


@api.delete("/messages/{message_id}/", auth=jwt_auth, tags=["Messages"])
def delete_chat_message(request, message_id: UUID):
    """Delete a message"""
    message = get_object_or_404(ChatMessage, id=message_id, chat__user=request.auth)
    message.delete()
    return {"success": True}
