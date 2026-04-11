from dataclasses import dataclass

from llama_index.core.agent.workflow import AgentWorkflow
from llama_index.core.agent.workflow import FunctionAgent
from llama_index.core.workflow import Context
from llama_index.llms.openai import OpenAI
from papa.apps.ergo.tools import ToolRegistryBase
from papa.apps.ergo.tools import user_tools
from papa.apps.users.models import User

llm = OpenAI(model="gpt-4o-mini")


def create_agent_notes_tool(agent_name: str):
    async def agent_notes_tool(ctx: Context, notes: str):
        current_state = await ctx.get("state")
        if current_state is None:
            current_state = {}
        if f"{agent_name}_notes" not in current_state:
            current_state[f"{agent_name}_notes"] = []
        current_state[f"{agent_name}_notes"].append(notes)
        await ctx.set("state", current_state)

    return agent_notes_tool


async def agent_create_question(ctx: Context, question: str):
    current_state = await ctx.get("state")
    if current_state is None:
        current_state = {}
    if "questions" not in current_state:
        current_state["questions"] = []
    current_state["questions"].append(question)
    await ctx.set("state", current_state)


async def agent_reply_to_user(ctx: Context, message: str):
    current_state = await ctx.get("state")
    if current_state is None:
        current_state = {}
    current_state["user_message"] = message
    await ctx.set("state", current_state)


@dataclass
class GeniusAgent:
    name: str
    description: str
    instructions: list[str]
    can_handoff_to: list[str]
    toolsets: list[ToolRegistryBase]
    readonly: bool = False

    def system_prompt(self):
        return """
        You are %s{name}.

        This system is a multi-agent workflow of the following agents:
        %{agents_str}

        %{instructions}
        """.format(**self.system_prompt_context())

    def system_prompt_context(self):
        return dict(
            name=self.name,
            instructions=self.instructions,
            agents_str="\n".join(
                [
                    f"- {agent.name}: {agent.description} (tools: {', '.join(agent.tool_names())})"
                    for agent in GENIUS_AGENTS
                ]
            ),
        )

    def tool_names(self):
        tool_names = [
            resource.__name__
            for toolset in self.toolsets
            for resource in toolset.resources
        ]
        if not self.readonly:
            tool_names.extend(
                [tool.__name__ for toolset in self.toolsets for tool in toolset.tools]
            )
        return tool_names

    def create_agent(self, user: User):
        tools = []
        for toolset in self.toolsets:
            tools.extend(toolset.to_model_toolset(user).resources)
            if not self.readonly:
                tools.extend(toolset.to_model_toolset(user).tools)
        print("creating agent with tools", self.name, self.toolsets, tools)
        return FunctionAgent(
            name=self.name,
            description=self.description,
            system_prompt=self.system_prompt(),
            llm=llm,
            tools=tools
            + [
                create_agent_notes_tool(self.name),
                agent_create_question,
                agent_reply_to_user,
            ],
            can_handoff_to=self.can_handoff_to,
        )


research_agent = GeniusAgent(
    name="ResearchAgent",
    description="Searches local knowledgebases and uses local API tools (readonly resources) to get information",
    instructions=[
        "You are the ResearchAgent that can search knowledgebases and use local API tools (readonly resources) for information on a given topic and use the notes tool to record notes on the topic for future agents.",
        "The knowledgebases are like wikis (and come with semantic search tools) while the other resources are mostly application APIs for our own services.",
        "Search the wikis for information, cultural context, business rules, and examples, but don't use the wikis for information that is available from the APIs.",
        "You'll maintain a list of outstanding questions or subqueries in your notes, continue until all questions are answered to the best of your ability. Based on your research, you might find more questions that need to be answered.",
        "For example, if a user asks about their hometown, you could try the get_profile tool, but when you don't find one, maybe you look for clues in the get_biography tool.",
        "Once you have gathered enough information, you should hand off control to the ActorAgent to perform actions with side effects (including replying to the user).",
        "Since your tools are all readonly, you can research without any side effects; ActorAgent needs to ask for enduser permission to use any tools with lasting impact.",
    ],
    can_handoff_to=["ActorAgent"],
    toolsets=[user_tools],
    readonly=True,
)

actor_agent = GeniusAgent(
    name="ActorAgent",
    description="Performs actions with side effects",
    instructions=[
        "You are the ActorAgent that can perform actions with side effects."
        "You can request tool calls, but they will be presented to the enduser for approval before being executed (except reply_to_user and notes tools, which are always allowed)."
        "If you need more research, hand control back to the ResearchAgent (does not require approval)."
    ],
    can_handoff_to=["ResearchAgent"],
    toolsets=[user_tools],
    readonly=False,
)

GENIUS_AGENTS = [
    research_agent,
    actor_agent,
]


def build_genius_workflow(user: User):
    agents = [agent.create_agent(user) for agent in GENIUS_AGENTS]
    return AgentWorkflow(
        agents=agents,
        root_agent=GENIUS_AGENTS[0].name,
        initial_state={},
    )


from llama_index.core.agent.workflow import AgentInput
from llama_index.core.agent.workflow import AgentOutput
from llama_index.core.agent.workflow import AgentStream
from llama_index.core.agent.workflow import ToolCall
from llama_index.core.agent.workflow import ToolCallResult


async def main():
    user = await User.objects.aget(username="leeward@boundcorp.net")
    agent_workflow = build_genius_workflow(user)
    handler = agent_workflow.run(
        user_msg=(
            "what timezone is my profile and what is my hometown and do i have any habits"
        )
    )

    current_agent = None
    current_tool_calls = ""
    async for event in handler.stream_events():
        if (
            hasattr(event, "current_agent_name")
            and event.current_agent_name != current_agent
        ):
            current_agent = event.current_agent_name
            print(f"\n{'=' * 50}")
            print(f"🤖 Agent: {current_agent}")
            print(f"{'=' * 50}\n")

        if isinstance(event, AgentStream):
            pass
        elif isinstance(event, AgentInput):
            print("📥 Input: sending", len(event.input), "messages")
            for message in event.input:
                message_text = message.blocks and message.blocks[0].text or ""
                if message.role == "system":
                    message_text = message_text.split("\n")[0][:100]
                if (
                    not message_text
                    and message.additional_kwargs
                    and message.additional_kwargs.get("tool_calls")
                ):
                    message_text = " ".join(
                        [
                            f"{call.function.name}: {call.function.arguments}"
                            for call in message.additional_kwargs["tool_calls"]
                        ]
                    )

                print(f"  {message.role}: {message_text}")
        elif isinstance(event, AgentOutput):
            if event.response.content:
                print("📤 Output:", event.response.content)
            if event.tool_calls:
                print(
                    "🛠️  Planning to use tools:",
                    [call.tool_name for call in event.tool_calls],
                )
        elif isinstance(event, ToolCallResult):
            print(f"🔧 Tool Result ({event.tool_name}):")
            print(f"  Arguments: {event.tool_kwargs}")
            print(f"  Output: {event.tool_output}")
        elif isinstance(event, ToolCall):
            print(f"🔨 Calling Tool: {event.tool_name}")
            print(f"  With arguments: {event.tool_kwargs}")
