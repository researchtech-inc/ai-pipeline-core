"""Vulture whitelist — methods called by frameworks, not direct code."""

# Pydantic validators/serializers — called by Pydantic, not our code
from ai_pipeline_core.documents.document import Document

Document._validate_content
Document._validate_name
Document._validate_total_size
Document._serialize_content

from ai_pipeline_core.documents.attachment import Attachment

Attachment._validate_name
Attachment._serialize_content

# __init_subclass__ — called by Python
Document.__init_subclass__

# Tool base class — execute(input) is an interface method, overridden by subclasses
from ai_pipeline_core.llm.tools import Tool

Tool.execute
input  # parameter of Tool.execute base method — used by subclass implementations

# PromptContract.execute stub — implementation lands in a follow-up PR
from ai_pipeline_core.prompt_contract import PromptContract

PromptContract.execute
tool_bindings  # parameter of PromptContract.execute stub — wired by the upcoming engine integration

# Prefect/deployment hooks — parameters in Prefect deployment function signatures
input_document_sha256s  # Prefect deployment parameter — passed by RemoteDeployment caller
created_by_task  # keyword-only parameter for _persist_documents — reserved for future use
exc_type  # __exit__ parameter — required by context manager protocol
tb  # __exit__ parameter — required by context manager protocol
