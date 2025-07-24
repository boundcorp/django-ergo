import json
from typing import Any, Dict, List, OrderedDict, Union, Protocol, Optional
from abc import ABC, abstractmethod
from papa.apps.ergo.models import Knowledgebase

from agents import Agent, Runner, function_tool
from functools import wraps
from pydantic import BaseModel
import inspect
from asgiref.sync import sync_to_async


# Protocols for better testability
class AgentRunner(Protocol):
    """Protocol for agent runner to allow dependency injection and testing."""

    async def run(
        self,
        agent: Agent,
        input_data: Union[str, List[Dict[str, Any]]],
        max_turns: int = 25,
    ) -> Any: ...

    def run_sync(
        self,
        agent: Agent,
        input_data: Union[str, List[Dict[str, Any]]],
        max_turns: int = 25,
    ) -> Any: ...


class ContentProcessor(Protocol):
    """Protocol for content processing to allow different implementations."""

    async def process_content(self, content: str) -> List[Dict[str, Any]]: ...


class KnowledgebaseRepository(Protocol):
    """Protocol for knowledgebase operations to allow testing."""

    async def get_knowledgebase_info(self, kb: Knowledgebase) -> str: ...

    async def get_table_of_contents(self, kb: Knowledgebase) -> str: ...

    async def create_articles(
        self, kb: Knowledgebase, articles: List["AddKnowledgebaseArticle"]
    ) -> List[Any]: ...


# Data models
class AddKnowledgebaseArticle(BaseModel):
    title: str
    content: str
    hierarchy_code: str


class IngestResult(BaseModel):
    """Result of an ingestion operation."""

    success: bool
    articles_created: int
    errors: List[str] = []
    details: Dict[str, Any] = {}


# Concrete implementations
class DefaultAgentRunner:
    """Default implementation of AgentRunner using the agents library."""

    async def run(
        self,
        agent: Agent,
        input_data: Union[str, List[Dict[str, Any]]],
        max_turns: int = 25,
    ) -> Any:
        return await Runner.run(agent, input_data, max_turns=max_turns)  # type: ignore

    def run_sync(
        self,
        agent: Agent,
        input_data: Union[str, List[Dict[str, Any]]],
        max_turns: int = 25,
    ) -> Any:
        return Runner.run_sync(agent, input_data, max_turns=max_turns)  # type: ignore


class DefaultKnowledgebaseRepository:
    """Default implementation of KnowledgebaseRepository."""

    async def get_knowledgebase_info(self, kb: Knowledgebase) -> str:
        return await sync_to_async(kb.to_markdown)()

    async def get_table_of_contents(self, kb: Knowledgebase) -> str:
        return await sync_to_async(kb.get_table_of_contents)()

    async def create_articles(
        self, kb: Knowledgebase, articles: List[AddKnowledgebaseArticle]
    ) -> List[Any]:
        created_articles = []
        for article in articles:
            created = await kb.articles.acreate(
                title=article.title,
                content=article.content,
                hierarchy_code=article.hierarchy_code,
            )
            created_articles.append(created)
        return created_articles


class DefaultContentProcessor:
    """Default implementation of ContentProcessor."""

    def __init__(self, agent: Agent):
        self.agent = agent

    async def process_content(self, content: str) -> List[Dict[str, Any]]:
        # This would contain the logic for processing content with the agent
        # For now, return a simple structure
        return [{"content": content, "role": "user"}]


# Core business logic classes
class AgentWorkflow:
    """Base class for agent workflows with dependency injection."""

    def __init__(
        self,
        agent: Agent,
        runner: Optional[AgentRunner] = None,
        content_processor: Optional[ContentProcessor] = None,
    ):
        self.agent = agent
        self.runner = runner or DefaultAgentRunner()
        self.content_processor = content_processor or DefaultContentProcessor(agent)
        self.prefetch_content: OrderedDict[str, Any] = OrderedDict()

    def run(self, input_data: Union[str, List[Dict[str, Any]]]) -> Any:
        """Run the workflow synchronously."""
        processed_input = self._prepare_input(input_data)
        return self.runner.run_sync(self.agent, processed_input, max_turns=25)

    async def run_async(self, input_data: Union[str, List[Dict[str, Any]]]) -> Any:
        """Run the workflow asynchronously."""
        processed_input = self._prepare_input(input_data)
        return await self.runner.run(self.agent, processed_input, max_turns=25)

    def _prepare_input(
        self, input_data: Union[str, List[Dict[str, Any]]]
    ) -> List[Dict[str, Any]]:
        """Prepare input data for the agent."""
        if isinstance(input_data, str):
            input_data = [{"content": input_data, "role": "user"}]

        if self.prefetch_content:
            for tool_name, content in self.prefetch_content.items():
                input_data = [
                    {
                        "content": json.dumps(
                            {"preseed_tool_name": tool_name, "output": content}
                        ),
                        "role": "system",
                    }
                ] + input_data

        return input_data


class IngestKnowledgeBase(AgentWorkflow):
    """Knowledgebase ingestion workflow with improved testability."""

    def __init__(
        self,
        target_kb: Knowledgebase,
        other_kbs: List[Knowledgebase],
        runner: Optional[AgentRunner] = None,
        repository: Optional[KnowledgebaseRepository] = None,
    ):
        self.target_kb = target_kb
        self.other_kbs = other_kbs
        self.repository = repository or DefaultKnowledgebaseRepository()

        # Create agent with tools
        agent = self._create_agent()
        super().__init__(agent, runner=runner)

    def _create_agent(self) -> Agent:
        """Create the agent with tools. Extracted for better testability."""

        @function_tool
        async def about_kbs():
            return await self.about_kbs()

        @function_tool
        async def add_knowledgebase_articles(articles: List[AddKnowledgebaseArticle]):
            return await self.add_knowledgebase_articles(articles)

        return Agent(
            model="gpt-4o-mini",
            name="IngestKnowledgeBase",
            instructions=self._get_agent_instructions(),
            tools=[about_kbs, add_knowledgebase_articles],
        )

    def _get_agent_instructions(self) -> str:
        """Get agent instructions. Extracted for better testability."""
        return """
<role>You are a knowledgebase curation agent.</role>
<task>
Your job is to extract and preserve the original factual content from source materials and organize it into a structured knowledgebase.
</task>

<critical note="You must preserve the original facts, data, techniques, and specific information from the source material.">
    <prohibition>Do NOT create summaries, interpretations, or commentary.</prohibition>
    <requirement>
        The content you add should contain the actual facts, numbers, procedures, and specific details from the original text.
    </requirement>
</critical>

<forbidden note="ABSOLUTELY FORBIDDEN: Do not start any content with phrases like:" instead="start directly with the factual content from the source material.">
    <example>"This study delves into..."</example>
    <example>"The document discusses..."</example>
    <example>"This article focuses on..."</example>
    <example>"The content explores..."</example>
    <example>"This section covers..."</example>
    <example>"The material addresses..."</example>
    <example>"This chapter examines..."</example>
</forbidden>

<additional requirement>
    In addition to creating structured articles, you MUST extract and output a separate list of atomic, verifiable facts from the source material.
    Each fact should be a single, self-contained statement (e.g., "Ideal temperature during vegetative growth is between 78°F to 80°F during the day.").
    Facts should be phrased so they can be checked as true/false later.
    Do NOT include opinions, interpretations, or generalizations—only direct, factual claims from the source.
    Output the facts as a bullet-point list or as a JSON array of strings, in addition to the main article content.
</additional requirement>
"""

    async def about_kbs(self) -> str:
        """Get information about knowledgebases. Extracted for better testability."""
        target_kb_markdown = await self.repository.get_knowledgebase_info(
            self.target_kb
        )
        other_kbs_markdown = "\n".join(
            [await self.repository.get_knowledgebase_info(kb) for kb in self.other_kbs]
        )
        table_of_contents = await self.repository.get_table_of_contents(self.target_kb)

        return f"""
        <target_knowledgebase description="This is the knowledgebase you are curating. It is the main knowledgebase that you will be adding articles to.">
            {target_kb_markdown}
        </target_knowledgebase>
        <other_knowledgebases description="These are other KBs that you can search; it might be helpful to know what other KBs exist. Information might belong in another KB.">
            {other_kbs_markdown}
        </other_knowledgebases>
        <table_of_contents description="This is the table of contents of the target knowledgebase. It is a list of articles in the knowledgebase, with their hierarchy codes.">
            {table_of_contents}
        </table_of_contents>
        """

    async def add_knowledgebase_articles(
        self, articles: List[AddKnowledgebaseArticle]
    ) -> List[Any]:
        """Add articles to the knowledgebase. Extracted for better testability."""
        print(
            f"Adding {len(articles)} articles to knowledgebase: {[article.hierarchy_code for article in articles]}"
        )

        try:
            created_articles = await self.repository.create_articles(
                self.target_kb, articles
            )

            for i, article in enumerate(articles):
                print(f"| Adding article to knowledgebase: {article.title}")
                print(f"| Content: {article.content}")
                print(f"| Hierarchy code: {article.hierarchy_code}")
                print(f"| Created article: {created_articles[i].id}")

            return created_articles
        except Exception as e:
            print(f"| ERROR creating articles: {e}")
            raise

    async def ingest_content(self, content: str) -> IngestResult:
        """Main ingestion method that returns a structured result."""
        try:
            # Set up prefetch content
            self.prefetch_content["about_kbs"] = await self.about_kbs()

            # Run the ingestion
            result = await self.run_async(content)

            # Count created articles
            article_count = await self.target_kb.articles.acount()

            return IngestResult(
                success=True, articles_created=article_count, details={"result": result}
            )
        except Exception as e:
            return IngestResult(
                success=False,
                articles_created=0,
                errors=[str(e)],
                details={"exception": e},
            )


class FactExtractionAgent(AgentWorkflow):
    """Dedicated agent for extracting atomic, verifiable facts from content."""

    def __init__(self, runner: Optional[AgentRunner] = None):
        # Create agent with fact extraction tools
        agent = self._create_agent()
        super().__init__(agent, runner=runner)

    def _create_agent(self) -> Agent:
        """Create the fact extraction agent with tools."""

        @function_tool
        async def extract_facts(content: str) -> List[str]:
            """Extract atomic, verifiable facts from the given content."""
            return await self._extract_facts_from_content(content)

        return Agent(
            model="gpt-4o-mini",
            name="FactExtractionAgent",
            instructions=self._get_agent_instructions(),
            tools=[extract_facts],
        )

    def _get_agent_instructions(self) -> str:
        """Get agent instructions for fact extraction."""
        return """
<role>You are a fact extraction specialist.</role>
<task>
Your job is to extract atomic, verifiable facts from source materials. Each fact should be a single, self-contained statement that can be checked as true/false.
</task>

<critical requirements>
    <requirement>Extract ONLY factual claims, not opinions or interpretations</requirement>
    <requirement>Each fact should be specific and measurable when possible</requirement>
    <requirement>Include numbers, ranges, procedures, and specific details from the source</requirement>
    <requirement>Phrase facts so they can be verified independently</requirement>
    <prohibition>Do NOT include generalizations, opinions, or subjective statements</prohibition>
    <prohibition>Do NOT include meta-commentary about the source material</prohibition>
</critical>

<output format>
    Return facts as a list of strings, where each string is one atomic fact.
    Example format:
    [
        "Optimal temperature range for vegetative growth is 70-85°F",
        "Humidity should be maintained between 40-80% RH",
        "CO2 levels should be 600-1200 ppm for optimal growth",
        "Plant spacing requires 2.3 square feet per plant"
    ]
</output>

<fact quality guidelines>
    - Prefer specific numbers and ranges over vague terms
    - Include units of measurement when available
    - Focus on actionable, verifiable information
    - Avoid facts that are too obvious or universal (e.g., "water is wet")
    - Avoid facts that are too specific to be useful (e.g., exact page numbers)
</fact quality guidelines>
"""

    async def _extract_facts_from_content(self, content: str) -> List[str]:
        """Extract facts from content using the agent."""
        try:
            result = await self.run_async(content)

            # The agent should return a list of facts
            if hasattr(result, "final_output") and hasattr(
                result.final_output, "extract_facts"
            ):
                return result.final_output.extract_facts
            elif isinstance(result, list):
                return result
            else:
                # Fallback: try to parse the result as a list
                return [str(result)]
        except Exception as e:
            print(f"Error extracting facts: {e}")
            return []

    async def extract_facts(self, content: str) -> List[str]:
        """Public method to extract facts from content."""
        return await self._extract_facts_from_content(content)
