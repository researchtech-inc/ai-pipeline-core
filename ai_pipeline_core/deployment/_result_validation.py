"""Private validation helpers for DeploymentResult field annotations."""

from types import UnionType
from typing import Annotated, Any, get_args, get_origin

from pydantic import BaseModel

from ai_pipeline_core.documents import Document


def validate_deployment_result_annotation(
    *,
    cls_name: str,
    field_path: str,
    annotation: Any,
    required: bool,
    seen_models: set[type[BaseModel]],
) -> bool:
    """Validate one DeploymentResult field annotation and return whether it contains a Document type."""
    annotation = _unwrap_annotated(annotation)
    if annotation is Document:
        _raise_bare_document_error(cls_name=cls_name, field_path=field_path)
    if annotation is type(None):
        return False

    origin = get_origin(annotation)
    if origin is dict:
        _validate_dict_annotation(cls_name=cls_name, field_path=field_path, annotation=annotation, seen_models=seen_models)
        return False
    if origin is not None or isinstance(annotation, UnionType):
        return _validate_union_annotation(
            cls_name=cls_name,
            field_path=field_path,
            annotation=annotation,
            required=required,
            seen_models=seen_models,
        )
    if isinstance(annotation, type) and issubclass(annotation, Document):
        return _validate_document_annotation(cls_name=cls_name, field_path=field_path, required=required)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _validate_model_annotation(
            cls_name=cls_name,
            field_path=field_path,
            annotation=annotation,
            required=required,
            seen_models=seen_models,
        )
    return False


def _raise_bare_document_error(*, cls_name: str, field_path: str) -> None:
    raise TypeError(
        f"DeploymentResult '{cls_name}' field '{field_path}' uses bare 'Document'. "
        "Use a concrete Document subclass such as FinalReportDocument so the result contract stays explicit and serializable."
    )


def _validate_dict_annotation(
    *,
    cls_name: str,
    field_path: str,
    annotation: Any,
    seen_models: set[type[BaseModel]],
) -> None:
    if _contains_document_type(annotation, seen_models=seen_models.copy()):
        raise TypeError(
            f"DeploymentResult '{cls_name}' field '{field_path}' uses dict[...] containing a Document type. "
            "Dicts containing Documents are not supported in DeploymentResult. "
            "Use a typed BaseModel or a list[...] of typed items instead."
        )


def _validate_union_annotation(
    *,
    cls_name: str,
    field_path: str,
    annotation: Any,
    required: bool,
    seen_models: set[type[BaseModel]],
) -> bool:
    members = get_args(annotation)
    if any(_unwrap_annotated(member) is type(None) for member in members):
        non_none_members = tuple(member for member in members if _unwrap_annotated(member) is not type(None))
        if any(_contains_document_type(member, seen_models=seen_models.copy()) for member in non_none_members):
            raise TypeError(
                f"DeploymentResult '{cls_name}' field '{field_path}' is optional but contains a Document type. "
                "Deployment results are terminal-only: Document-bearing fields must be required and non-nullable. "
                "Remove '| None' / Optional[...] and inspect incomplete runs with ai-trace instead of returning a partial result."
            )
    has_document = False
    for member in members:
        if member is Ellipsis or _unwrap_annotated(member) is type(None):
            continue
        has_document = (
            validate_deployment_result_annotation(
                cls_name=cls_name,
                field_path=field_path,
                annotation=member,
                required=required,
                seen_models=seen_models,
            )
            or has_document
        )
    return has_document


def _validate_document_annotation(*, cls_name: str, field_path: str, required: bool) -> bool:
    if not required:
        raise TypeError(
            f"DeploymentResult '{cls_name}' field '{field_path}' contains a Document type but is not required. "
            "Deployment results are only produced after the full plan completes, so Document-bearing fields must be required. "
            "Remove the default value and return no result for incomplete runs."
        )
    return True


def _validate_model_annotation(
    *,
    cls_name: str,
    field_path: str,
    annotation: type[BaseModel],
    required: bool,
    seen_models: set[type[BaseModel]],
) -> bool:
    if annotation in seen_models or issubclass(annotation, Document):
        return False
    seen_models.add(annotation)
    try:
        has_document = False
        for nested_name, nested_field in annotation.model_fields.items():
            has_document = (
                validate_deployment_result_annotation(
                    cls_name=cls_name,
                    field_path=f"{field_path}.{nested_name}",
                    annotation=nested_field.annotation,
                    required=nested_field.is_required(),
                    seen_models=seen_models,
                )
                or has_document
            )
    finally:
        seen_models.remove(annotation)
    if has_document and not required:
        raise TypeError(
            f"DeploymentResult '{cls_name}' field '{field_path}' contains nested Document fields but is not required. "
            "Document-bearing result fields must be required all the way to the top-level result contract. "
            "Remove the default value and inspect incomplete runs with ai-trace instead of returning a partial result."
        )
    return has_document


def _contains_document_type(annotation: Any, *, seen_models: set[type[BaseModel]]) -> bool:
    annotation = _unwrap_annotated(annotation)
    contains_document = False
    if annotation is not type(None):
        if annotation is Document or (isinstance(annotation, type) and issubclass(annotation, Document)):
            contains_document = True
        else:
            origin = get_origin(annotation)
            if origin is not None:
                contains_document = any(arg is not Ellipsis and _contains_document_type(arg, seen_models=seen_models) for arg in get_args(annotation))
            elif isinstance(annotation, type) and issubclass(annotation, BaseModel) and annotation not in seen_models and not issubclass(annotation, Document):
                seen_models.add(annotation)
                try:
                    contains_document = any(_contains_document_type(field.annotation, seen_models=seen_models) for field in annotation.model_fields.values())
                finally:
                    seen_models.remove(annotation)
    return contains_document


def _unwrap_annotated(annotation: Any) -> Any:
    while get_origin(annotation) is Annotated:
        args = get_args(annotation)
        if not args:
            return annotation
        annotation = args[0]
    return annotation
