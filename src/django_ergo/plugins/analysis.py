"""Opt-in isolated analysis tools. No workspace is created on chat creation."""

from django_ergo.analysis.service import AnalysisService
from django_ergo.analysis.service import safe_name
from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import bot_tool


class AnalysisPlugin(BotPlugin):
    name = "analysis"
    description = (
        "Isolated Python calculations, data analysis and attachment import/export"
    )

    @property
    def skill_instructions(self):
        return (
            "Use ergo_analysis_run for Python/pandas/matplotlib in a session workspace. "
            "Jobs are asynchronous: poll ergo_analysis_status until terminal. "
            "Import chat files with ergo_analysis_import, then open CSV/Parquet/XLSX "
            "with ergo_analysis_dataset. Select only needed columns and apply WHERE, "
            "group_by and aggregates before returning rows; use cursor for more. "
            "Use ergo_analysis_export to save results as independent chat attachments. "
            "No network or credentials are available. Files expire after idle retention. "
            "If disabled/unavailable, report it; host bash is not an isolation substitute."
        )

    def toolkits(self, ctx):
        if not self._applies(ctx):
            return []
        return [FunctionToolkit(self._tools(), ctx)]

    def _tools(self):
        @bot_tool(
            name="ergo_analysis_run",
            description="Submit Python in a credential-free isolated workspace. Return job id; poll status. Files in /workspace persist; Python/pandas/DuckDB/plotting available. No network.",
            takes_context=True,
        )
        def run(ctx, code: str) -> dict:
            return AnalysisService(ctx).submit({"op": "python", "code": code})

        @bot_tool(
            name="ergo_analysis_status",
            description="Poll a job from this session, or cancel it. Terminal means pod has been reaped.",
            takes_context=True,
        )
        def status(ctx, job: str, cancel: bool = False) -> dict:
            return AnalysisService(ctx).status(job, cancel=cancel)

        @bot_tool(
            name="ergo_analysis_files",
            description="List up to 100 regular files in this session workspace.",
            takes_context=True,
        )
        def files(ctx) -> dict:
            return AnalysisService(ctx).submit({"op": "list"})

        @bot_tool(
            name="ergo_analysis_import",
            description="Stream a permitted chat attachment into this workspace. Returns file handle; may take multiple jobs.",
            takes_context=True,
        )
        def import_file(ctx, attachment: str) -> dict:
            return AnalysisService(ctx).import_attachment(attachment)

        @bot_tool(
            name="ergo_analysis_export",
            description="Stream a regular workspace file to an independent chat attachment. Existing exports survive workspace cleanup.",
            takes_context=True,
        )
        def export_file(ctx, file: str, filename: str = "") -> dict:
            return AnalysisService(ctx).export_file(file, filename)

        @bot_tool(
            name="ergo_analysis_dataset",
            description="Parse a CSV/Parquet/XLSX workspace file in isolation; poll job for versioned dataset handle, schema and exact row count.",
            takes_context=True,
        )
        def dataset(ctx, file: str) -> dict:
            return AnalysisService(ctx).submit(
                {"op": "dataset", "file": safe_name(file)}
            )

        @bot_tool(
            name="ergo_analysis_select",
            description='Submit bounded full-data query; poll for rows, exact output count and cursor. query: {columns:[names],where:{op:"and",args:[{column:"amount",op:"ge",value:10}]},group_by:[names],aggregates:[{op:"sum",column:"amount",as:"total"}],sort:[{column:"total",direction:"desc"}],limit:100}. Predicates: eq/ne/lt/le/gt/ge/in/is_null/not_null/and/or/not. Keep the same query when supplying cursor.',
            takes_context=True,
        )
        def select(ctx, dataset: str, query: dict, cursor: str = "") -> dict:
            return AnalysisService(ctx).select(dataset, query, cursor)

        return [
            tool.__bot_tool__
            for tool in (run, status, files, import_file, export_file, dataset, select)
        ]
