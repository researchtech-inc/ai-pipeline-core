"""Strict JSON codec for replayable span payloads."""

import asyncio
import dataclasses
import importlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path, PurePath
from typing import Any, Protocol, Self, cast, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ai_pipeline_core.database._hydrate import hydrate_document
from ai_pipeline_core.database._protocol import DatabaseReader
from ai_pipeline_core.database._types import HydratedDocument
from ai_pipeline_core.documents._hashing import compute_content_sha256
from ai_pipeline_core.documents.document import Document

__all__ = [
    "CodecCycleError",
    "CodecError",
    "CodecImportError",
    "CodecState",
    "EncodeResult",
    "EnumDecodeError",
    "SerializedError",
    "UniversalCodec",
]

TYPE_KEY = "$type"
DOCUMENT_REF_TYPE = "document_ref"
BLOB_REF_TYPE = "blob_ref"
PYDANTIC_TYPE = "pydantic"
TUPLE_TYPE = "tuple"
FROZENSET_TYPE = "frozenset"
TYPE_REF_TYPE = "type_ref"
UUID_TYPE = "uuid"
DATETIME_TYPE = "datetime"
ENUM_TYPE = "enum"
PATH_TYPE = "path"
DATACLASS_TYPE = "dataclass"


@dataclass(frozen=True, slots=True)
class EncodeResult:
    """JSON-safe encoded value plus referenced SHA sets."""

    value: Any
    document_shas: frozenset[str]
    blob_shas: frozenset[str]


class SerializedError(BaseModel):
    """Codec-friendly error payload for failed span execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    error_class_path: str
    type_name: str
    message: str
    traceback_text: str


class CodecError(ValueError):
    """Raised when a value cannot be encoded or decoded by UniversalCodec."""


class CodecCycleError(CodecError):
    """Raised when an object graph contains a reference cycle."""


class EnumDecodeError(CodecError):
    """Raised when an enum payload cannot be matched by name or value."""


class CodecImportError(CodecError, ImportError):
    """Raised when a stored import path cannot be resolved."""


# Protocol
@runtime_checkable
class CodecState(Protocol):
    """Explicit codec hooks for value objects with non-field state.

    Implementing types may define ``codec_state()`` and ``codec_load()`` when
    default Pydantic field serialization is not enough. These are normal
    framework extension methods discovered by ``getattr``; they are
    intentionally not Python data-model dunders.
    """

    def codec_state(self) -> dict[str, Any]:
        """Return replayable state for the instance."""
        ...

    @classmethod
    def codec_load(cls, state: dict[str, Any]) -> Self:
        """Reconstruct an instance from codec state."""
        ...


def import_by_path(path: str) -> Any:
    """Import an object from ``module:QualName``."""
    module_path, separator, qualname = path.partition(":")
    if not separator or not module_path or not qualname:
        raise CodecImportError(f"Invalid codec import path {path!r}. Use the form 'package.module:ClassName' or 'package.module:Outer.Inner'.")
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        raise CodecImportError(
            f"Codec could not import module '{module_path}' from path {path!r}. Import the module before decoding, or replay with --import {module_path}."
        ) from exc
    obj: Any = module
    try:
        for attribute in qualname.split("."):
            obj = getattr(obj, attribute.partition("[")[0])
    except AttributeError as exc:
        raise CodecImportError(
            f"Codec imported module '{module_path}' but could not resolve '{qualname}' from path {path!r}. "
            f"Import the defining module before decoding, or replay with --import {module_path}."
        ) from exc
    return obj


@dataclass(slots=True)
class _EncodeContext:
    """Mutable state threaded through the encode pass."""

    path: str
    document_shas: set[str]
    blob_shas: set[str]
    active_paths: dict[int, str]

    def child(self, path: str) -> _EncodeContext:
        return _EncodeContext(path=path, document_shas=self.document_shas, blob_shas=self.blob_shas, active_paths=self.active_paths)


@dataclass(slots=True)
class _DecodeMemo:
    blobs: dict[str, bytes] = field(default_factory=dict)
    documents: dict[str, Document] = field(default_factory=dict)


class UniversalCodec:
    """Encode Python values into canonical JSON-safe payloads and decode them back."""

    def encode(self, value: Any) -> EncodeResult:
        """Encode a supported value into a JSON-safe payload."""
        ctx = _EncodeContext(path="$", document_shas=set(), blob_shas=set(), active_paths={})
        encoded = self._encode_value(value, ctx)
        return EncodeResult(value=encoded, document_shas=frozenset(ctx.document_shas), blob_shas=frozenset(ctx.blob_shas))

    def decode(self, encoded: Any, db: DatabaseReader | None = None) -> Any:
        """Decode a previously encoded payload (sync wrapper around async decode)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.decode_async(encoded, db=db))
        raise CodecError(
            "Codec.decode() was called from an active event loop. Use await codec.decode_async(...) in async code, or call decode() from synchronous code."
        )

    async def decode_async(self, encoded: Any, db: DatabaseReader | None = None) -> Any:
        """Decode a previously encoded payload."""
        return await self._decode_value(encoded, db=db, memo=_DecodeMemo())

    # ── Encode ──────────────────────────────────────────────────────────

    def _encode_value(self, value: Any, ctx: _EncodeContext) -> Any:
        simple = self._encode_simple(value, ctx)
        if simple is not _UNHANDLED:
            return simple
        if isinstance(value, BaseModel):
            return self._encode_pydantic(value, ctx)
        if isinstance(value, list):
            return self._encode_list(value, ctx)
        if isinstance(value, tuple):
            return self._encode_tuple(value, ctx)
        if isinstance(value, frozenset):
            return self._encode_frozenset(value, ctx)
        if isinstance(value, dict):
            return self._encode_dict(value, ctx)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return self._encode_dataclass(value, ctx)
        raise CodecError(
            f"Codec cannot encode value at {ctx.path} with type {type(value).__module__}:{type(value).__qualname__}. "
            "Use JSON primitives, bytes, Document, BaseModel, dataclass, UUID, datetime, Enum, type, list, tuple, frozenset, or dict."
        )

    def _encode_simple(self, value: Any, ctx: _EncodeContext) -> Any:
        if isinstance(value, Enum):
            return self._encode_enum(value, ctx)
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, UUID):
            return {TYPE_KEY: UUID_TYPE, "value": str(value)}
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise CodecError(f"Codec cannot encode naive datetime at {ctx.path}. Use an aware datetime with an explicit UTC offset like +00:00.")
            return {TYPE_KEY: DATETIME_TYPE, "value": value.isoformat()}
        if isinstance(value, PurePath):
            return {TYPE_KEY: PATH_TYPE, "value": str(value)}
        if isinstance(value, type):
            return {TYPE_KEY: TYPE_REF_TYPE, "path": _class_path(value)}
        if isinstance(value, bytes):
            sha = compute_content_sha256(value)
            ctx.blob_shas.add(sha)
            return {TYPE_KEY: BLOB_REF_TYPE, "sha256": sha}
        if isinstance(value, Document):
            ctx.document_shas.add(value.sha256)
            return {TYPE_KEY: DOCUMENT_REF_TYPE, "sha256": value.sha256, "class_path": _class_path(type(value))}
        return _UNHANDLED

    def _encode_list(self, value: list[Any], ctx: _EncodeContext) -> list[Any]:
        with _cycle_guard(value, ctx):
            return [self._encode_value(item, ctx.child(_index_path(ctx.path, i))) for i, item in enumerate(value)]

    def _encode_tuple(self, value: tuple[Any, ...], ctx: _EncodeContext) -> dict[str, Any]:
        with _cycle_guard(value, ctx):
            return {TYPE_KEY: TUPLE_TYPE, "items": [self._encode_value(item, ctx.child(_index_path(ctx.path, i))) for i, item in enumerate(value)]}

    def _encode_frozenset(self, value: frozenset[Any], ctx: _EncodeContext) -> dict[str, Any]:
        with _cycle_guard(value, ctx):
            items = sorted(value, key=repr)
            return {TYPE_KEY: FROZENSET_TYPE, "items": [self._encode_value(item, ctx.child(_index_path(ctx.path, i))) for i, item in enumerate(items)]}

    def _encode_dict(self, value: dict[Any, Any], ctx: _EncodeContext) -> dict[str, Any]:
        with _cycle_guard(value, ctx):
            return {_escape_user_key(k, path=ctx.path): self._encode_value(v, ctx.child(_dict_path(ctx.path, k))) for k, v in value.items()}

    def _encode_enum(self, value: Enum, ctx: _EncodeContext) -> dict[str, Any]:
        return {
            TYPE_KEY: ENUM_TYPE,
            "class_path": _class_path(type(value)),
            "name": value.name,
            "value": self._encode_value(value.value, ctx.child(f"{ctx.path}.value")),
        }

    def _encode_pydantic(self, value: BaseModel, ctx: _EncodeContext) -> dict[str, Any]:
        with _cycle_guard(value, ctx):
            codec_state = getattr(value, "codec_state", None)
            if callable(codec_state):
                state = codec_state()
                if not isinstance(state, dict):
                    raise CodecError(f"{_class_path(type(value))}.codec_state() must return dict[str, Any]. Got {type(state).__name__} instead.")
                data = self._encode_value(state, ctx.child(f"{ctx.path}.state"))
            else:
                model_data = {field_name: getattr(value, field_name) for field_name in type(value).model_fields}
                if value.model_extra:
                    model_data.update(value.model_extra)
                data = self._encode_value(model_data, ctx.child(f"{ctx.path}.data"))
            return {TYPE_KEY: PYDANTIC_TYPE, "class_path": _class_path(type(value)), "data": data}

    def _encode_dataclass(self, value: Any, ctx: _EncodeContext) -> dict[str, Any]:
        with _cycle_guard(value, ctx):
            data = {f.name: getattr(value, f.name) for f in dataclasses.fields(value)}
            encoded_data = self._encode_value(data, ctx.child(f"{ctx.path}.data"))
            return {TYPE_KEY: DATACLASS_TYPE, "class_path": _class_path(type(value)), "data": encoded_data}

    # ── Decode (async only) ─────────────────────────────────────────────

    async def _decode_value(self, encoded: Any, *, db: DatabaseReader | None, memo: _DecodeMemo) -> Any:
        if encoded is None or isinstance(encoded, (bool, int, float, str)):
            return encoded
        if isinstance(encoded, list):
            return [await self._decode_value(item, db=db, memo=memo) for item in encoded]
        if not isinstance(encoded, dict):
            raise CodecError(
                f"Codec expected a JSON-compatible value during decode, got {type(encoded).__name__}. "
                "Pass the exact payload produced by UniversalCodec.encode()."
            )
        type_name = encoded.get(TYPE_KEY)
        if isinstance(type_name, str):
            return await self._decode_envelope(type_name, encoded, db=db, memo=memo)
        return {_unescape_user_key(k): await self._decode_value(v, db=db, memo=memo) for k, v in encoded.items()}

    async def _decode_envelope(self, type_name: str, payload: dict[str, Any], *, db: DatabaseReader | None, memo: _DecodeMemo) -> Any:
        if type_name == DOCUMENT_REF_TYPE:
            return await self._decode_document_ref(payload, db=db, memo=memo)
        if type_name == BLOB_REF_TYPE:
            return await self._decode_blob_ref(payload, db=db, memo=memo)
        if type_name == PYDANTIC_TYPE:
            return await self._decode_pydantic(payload, db=db, memo=memo)
        if type_name == DATACLASS_TYPE:
            return await self._decode_dataclass(payload, db=db, memo=memo)
        if type_name == ENUM_TYPE:
            return await self._decode_enum(payload, db=db, memo=memo)
        if type_name == TUPLE_TYPE:
            return await self._decode_tuple(payload, db=db, memo=memo)
        if type_name == FROZENSET_TYPE:
            return await self._decode_frozenset(payload, db=db, memo=memo)
        if type_name == TYPE_REF_TYPE:
            return _decode_type_ref(payload)
        if type_name == UUID_TYPE:
            return UUID(_require_string(payload, "value", type_name=UUID_TYPE))
        if type_name == DATETIME_TYPE:
            return _decode_datetime(payload)
        if type_name == PATH_TYPE:
            return Path(_require_string(payload, "value", type_name=PATH_TYPE))
        raise CodecError(
            f"Codec encountered unsupported envelope type {type_name!r}. "
            "Only document_ref, blob_ref, pydantic, dataclass, tuple, frozenset, type_ref, uuid, datetime, path, and enum are supported."
        )

    @staticmethod
    async def _decode_document_ref(payload: dict[str, Any], *, db: DatabaseReader | None, memo: _DecodeMemo) -> Document:
        if db is None:
            raise CodecError("Codec cannot decode a document_ref without a DatabaseReader. Pass db=... when decoding payloads that reference stored documents.")
        sha256 = _require_string(payload, "sha256", type_name=DOCUMENT_REF_TYPE)
        if sha256 in memo.documents:
            return memo.documents[sha256]
        class_path = _require_string(payload, "class_path", type_name=DOCUMENT_REF_TYPE)
        hydrated = await db.get_document_with_content(sha256)
        if hydrated is None:
            raise CodecError(
                f"Codec could not load document {sha256[:12]}... from the database. Persist the document record and its blobs before decoding this payload."
            )
        document = _document_from_hydrated_record(hydrated, class_path)
        memo.documents[sha256] = document
        return document

    @staticmethod
    async def _decode_blob_ref(payload: dict[str, Any], *, db: DatabaseReader | None, memo: _DecodeMemo) -> bytes:
        if db is None:
            raise CodecError("Codec cannot decode a blob_ref without a DatabaseReader. Pass db=... when decoding payloads that reference stored blobs.")
        sha256 = _require_string(payload, "sha256", type_name=BLOB_REF_TYPE)
        if sha256 in memo.blobs:
            return memo.blobs[sha256]
        blob = await db.get_blob(sha256)
        if blob is None:
            raise CodecError(f"Codec could not load blob {sha256[:12]}... from the database. Persist the blob before decoding this payload.")
        memo.blobs[sha256] = blob.content
        return blob.content

    async def _decode_pydantic(self, payload: dict[str, Any], *, db: DatabaseReader | None, memo: _DecodeMemo) -> BaseModel:
        class_path = _require_string(payload, "class_path", type_name=PYDANTIC_TYPE)
        model_cls = import_by_path(class_path)
        if not isinstance(model_cls, type) or not issubclass(model_cls, BaseModel):
            raise CodecError(
                f"Codec pydantic path {class_path!r} resolved to {type(model_cls).__name__}, not a BaseModel subclass. "
                "Store BaseModel instances with the pydantic envelope."
            )
        data = await self._decode_value(payload.get("data"), db=db, memo=memo)
        codec_load = getattr(model_cls, "codec_load", None)
        if callable(codec_load):
            if not isinstance(data, dict):
                raise CodecError(f"Codec stateful model {class_path!r} requires 'data' to decode into a JSON object. Return dict[str, Any] from codec_state().")
            try:
                return cast(BaseModel, codec_load(data))
            except CodecError:
                raise
            except Exception as exc:
                raise CodecError(f"codec_load failed for {class_path!r}: {exc}") from exc
        try:
            return cast(BaseModel, model_cls.model_validate(data))
        except CodecError:
            raise
        except Exception as exc:
            raise CodecError(f"Pydantic validation failed for {class_path!r}: {exc}") from exc

    async def _decode_dataclass(self, payload: dict[str, Any], *, db: DatabaseReader | None, memo: _DecodeMemo) -> Any:
        class_path = _require_string(payload, "class_path", type_name=DATACLASS_TYPE)
        dc_cls = import_by_path(class_path)
        if not isinstance(dc_cls, type) or not dataclasses.is_dataclass(dc_cls):
            raise CodecError(
                f"Codec dataclass path {class_path!r} resolved to {type(dc_cls).__name__}, not a dataclass. "
                "Store dataclass instances with the dataclass envelope."
            )
        data = await self._decode_value(payload.get("data"), db=db, memo=memo)
        if not isinstance(data, dict):
            raise CodecError(f"Codec dataclass {class_path!r} requires 'data' to be a JSON object.")
        try:
            return dc_cls(**data)
        except (TypeError, ValueError) as exc:
            raise CodecError(f"Dataclass construction failed for {class_path!r}: {exc}") from exc

    async def _decode_enum(self, payload: dict[str, Any], *, db: DatabaseReader | None, memo: _DecodeMemo) -> Enum:
        class_path = _require_string(payload, "class_path", type_name=ENUM_TYPE)
        enum_cls = import_by_path(class_path)
        if not isinstance(enum_cls, type) or not issubclass(enum_cls, Enum):
            raise CodecError(
                f"Codec enum path {class_path!r} resolved to {type(enum_cls).__name__}, not an Enum subclass. Store enum values with the enum envelope."
            )
        name = _require_string(payload, "name", type_name=ENUM_TYPE)
        try:
            return cast(Enum, enum_cls[name])
        except KeyError:
            pass
        decoded_value = await self._decode_value(payload.get("value"), db=db, memo=memo)
        for member in enum_cls:
            if member.value == decoded_value:
                return cast(Enum, member)
        raise EnumDecodeError(
            f"Codec could not decode enum {class_path!r}: no member named {name!r} and no member with value {decoded_value!r}. "
            "Persist both enum name and value and keep at least one stable across refactors."
        )

    async def _decode_tuple(self, payload: dict[str, Any], *, db: DatabaseReader | None, memo: _DecodeMemo) -> tuple[Any, ...]:
        items = payload.get("items")
        if not isinstance(items, list):
            raise CodecError("Codec tuple payloads require a JSON array under 'items'. Use {'$type': 'tuple', 'items': [...]}.")
        decoded = [await self._decode_value(item, db=db, memo=memo) for item in items]
        return tuple(decoded)

    async def _decode_frozenset(self, payload: dict[str, Any], *, db: DatabaseReader | None, memo: _DecodeMemo) -> frozenset[Any]:
        items = payload.get("items")
        if not isinstance(items, list):
            raise CodecError("Codec frozenset payloads require a JSON array under 'items'. Use {'$type': 'frozenset', 'items': [...]}.")
        decoded = [await self._decode_value(item, db=db, memo=memo) for item in items]
        return frozenset(decoded)


# ── Helpers ─────────────────────────────────────────────────────────────

_UNHANDLED = object()


class _CycleGuard:
    def __init__(self, value: Any, ctx: _EncodeContext) -> None:
        self._active_paths = ctx.active_paths
        self._object_id = id(value)
        self._path = ctx.path

    def __enter__(self) -> None:
        prior_path = self._active_paths.get(self._object_id)
        if prior_path is not None:
            raise CodecCycleError(
                f"Codec detected a reference cycle while encoding {self._path}. "
                f"The same object is already active at {prior_path}. "
                "Cycles are not supported. Replace the cycle with a document or blob reference, or flatten the state."
            )
        self._active_paths[self._object_id] = self._path

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._active_paths.pop(self._object_id, None)


def _cycle_guard(value: Any, ctx: _EncodeContext) -> _CycleGuard:
    return _CycleGuard(value, ctx)


def _class_path(value_type: type[Any]) -> str:
    codec_type = _codec_origin_type(value_type)
    qualname = codec_type.__qualname__
    if "<locals>" in qualname:
        raise CodecError(
            f"Codec cannot encode class {codec_type.__module__}:{qualname} because it is defined inside a function. "
            "Move the class to module scope so it can be imported during replay."
        )
    return f"{codec_type.__module__}:{qualname}"


def _codec_origin_type(value_type: type[Any]) -> type[Any]:
    generic_metadata = getattr(value_type, "__pydantic_generic_metadata__", None)
    if isinstance(generic_metadata, dict):
        origin = generic_metadata.get("origin")
        if isinstance(origin, type):
            return origin
    return value_type


def _escape_user_key(key: Any, *, path: str) -> str:
    if not isinstance(key, str):
        raise CodecError(f"Codec cannot encode dict key at {path}: expected str, got {type(key).__name__}. Convert mapping keys to strings before encoding.")
    if _is_type_marker_key(key):
        return f"${key}"
    return key


def _unescape_user_key(key: str) -> str:
    if _is_escaped_type_marker_key(key):
        return key[1:]
    return key


def _dict_path(path: str, key: str) -> str:
    return f"{path}[{key!r}]"


def _index_path(path: str, index: int) -> str:
    return f"{path}[{index}]"


def _require_string(payload: dict[str, Any], key: str, *, type_name: str) -> str:
    value = payload.get(key)
    if isinstance(value, str):
        return value
    raise CodecError(f"Codec {type_name} payloads require a string field {key!r}. Got {type(value).__name__} instead.")


def _is_type_marker_key(key: str) -> bool:
    prefix = key.removesuffix("type")
    return bool(prefix) and key.endswith("type") and set(prefix) == {"$"}


def _is_escaped_type_marker_key(key: str) -> bool:
    return _is_type_marker_key(key) and key.startswith("$$")


def _decode_type_ref(payload: dict[str, Any]) -> type[Any]:
    path = _require_string(payload, "path", type_name=TYPE_REF_TYPE)
    loaded = import_by_path(path)
    if not isinstance(loaded, type):
        raise CodecError(
            f"Codec type_ref path {path!r} resolved to {type(loaded).__name__}, not a Python type. Encode only real type objects with the type_ref envelope."
        )
    return loaded


def _decode_datetime(payload: dict[str, Any]) -> datetime:
    value = datetime.fromisoformat(_require_string(payload, "value", type_name=DATETIME_TYPE))
    if value.tzinfo is None or value.utcoffset() is None:
        raise CodecError("Codec datetime payloads require an explicit UTC offset. Store datetimes like '2026-03-12T10:00:00+00:00'.")
    return value


def _document_from_hydrated_record(hydrated: HydratedDocument, class_path: str) -> Document:
    document_cls = import_by_path(class_path)
    if not isinstance(document_cls, type) or not issubclass(document_cls, Document):
        raise CodecError(
            f"Codec document path {class_path!r} resolved to {type(document_cls).__name__}, not a Document subclass. "
            "Store Document instances with the document_ref envelope."
        )
    try:
        return hydrate_document(document_cls, hydrated)
    except (TypeError, ValueError) as exc:
        raise CodecError(str(exc)) from exc
