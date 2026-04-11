import functools
import inspect
from collections.abc import Callable
from typing import Any

from django.http import HttpRequest
from papa.apps.users.models import User


class ToolRegistryBase:
    tools: list[Callable] = []
    resources: list[Callable] = []

    def __init__(self, tools: list[Callable] = [], resources: list[Callable] = []):
        self.tools = tools
        self.resources = resources

    def register_tool(self, tool: Callable):
        self.tools.append(tool)
        return tool

    def register_resource(self, resource: Callable):
        self.resources.append(resource)
        return resource

    def to_model_toolset(self, *args: Any, **kwargs: Any) -> "ModelToolset":
        return ModelToolset(tools=self.tools, resources=self.resources)


class ModelToolset(ToolRegistryBase):
    def get_readonly_tools(self):
        return self.resources

    def get_writeable_tools(self):
        return self.tools


class UserToolRegistry(ToolRegistryBase):
    def register_tool(self, tool: Callable):
        if not tool.__code__.co_varnames[0] == "user":
            raise ValueError("First argument to a user tool must be 'user'")
        self.tools.append(tool)
        return tool

    def register_resource(self, resource: Callable):
        if not resource.__code__.co_varnames[0] == "user":
            raise ValueError("First argument to a user resource must be 'user'")
        self.resources.append(resource)
        return resource

    def to_model_toolset(self, user: User) -> ModelToolset:
        def curry_user(func: Callable):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                return func(user, *args, **kwargs)

            # need to modify the signature of the function to remove the user argument
            modify_function_signature(
                wrapper, remove_annotations=["user"], add_annotations={}
            )

            return wrapper

        return ModelToolset(
            tools=[curry_user(tool) for tool in self.tools],
            resources=[curry_user(resource) for resource in self.resources],
        )


def modify_function_signature(
    func: Callable,
    add_annotations: dict[str, Any],
    remove_annotations: list[str] | None = None,
) -> Callable:
    remove_annotations = remove_annotations or []
    original_signature = inspect.signature(func)
    annotations = {
        k: v for k, v in func.__annotations__.items() if k not in remove_annotations
    }
    annotations.update(add_annotations)
    params = [
        inspect.Parameter(
            name=name,
            annotation=annotation,
            kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        for name, annotation in add_annotations.items()
    ] + [
        param
        for param in original_signature.parameters.values()
        if param.name not in remove_annotations
    ]
    func.__signature__ = original_signature.replace(parameters=params)
    func.__annotations__ = annotations
    return func


def tool_function_to_http_view(view: Callable) -> Callable:
    @functools.wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        return view(*args, **kwargs)

    modify_function_signature(wrapper, add_annotations={"request": HttpRequest})

    return wrapper


def user_tool_function_to_http_view(view: Callable) -> Callable:
    """Convert a function that takes a user and returns a response to a function that takes a request and returns a response."""

    @functools.wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        return view(user=request.user, *args, **kwargs)

    modify_function_signature(
        wrapper, add_annotations={"request": HttpRequest}, remove_annotations=["user"]
    )

    return wrapper


user_tools = UserToolRegistry()
