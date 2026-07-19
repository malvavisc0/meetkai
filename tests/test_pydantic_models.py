"""Guard tests for every pydantic model in the ``kai`` package.

These exist to catch two recurring classes of bug at once:

1. **Schema-rebuild / forward-reference failures** — a model whose JSON
   schema can't be generated because a referenced name (e.g. ``Literal``)
   isn't resolvable in the model's namespace. This is the
   ``is not fully defined`` family of errors that broke the
   ``schedule_task`` tool at chat time.

2. **Widened-value assignments to closed-set fields** — code that loads a
   ``str`` (or other widened value) into a ``Literal`` / ``Enum`` field,
   which is a static type error *and* a latent runtime risk. The fix is to
   validate/coerce at the deserialization boundary; these tests pin that
   every ``Literal`` / ``Enum`` field actually rejects non-members and
   accepts each declared member.

The model set is discovered automatically by walking the ``kai`` package,
so new pydantic models are covered without touching this file.
"""

import importlib
import inspect
import pkgutil
import types
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

import pytest
from pydantic import BaseModel, ValidationError

import kai

try:
    from pydantic_settings import BaseSettings
except Exception:  # pragma: no cover - pydantic_settings is a hard dep
    BaseSettings = None  # type: ignore[assignment,misc]


def _qid(model: type[BaseModel]) -> str:
    return f"{model.__module__}.{model.__qualname__}"


def _discover_models() -> list[type[BaseModel]]:
    found: dict[str, type[BaseModel]] = {}
    for _finder, name, _ispkg in pkgutil.walk_packages(kai.__path__, prefix="kai."):
        try:
            module = importlib.import_module(name)
        except Exception:
            # A submodule that can't be imported is its own problem (surfaced
            # by test_discovery_covers_key_models for the important ones); skip
            # it here so one bad module can't blank out the whole sweep.
            continue
        for obj in vars(module).values():
            if not inspect.isclass(obj) or obj is BaseModel:
                continue
            if not issubclass(obj, BaseModel):
                continue
            if not getattr(obj, "__module__", "").startswith("kai."):
                continue
            found.setdefault(_qid(obj), obj)
    return list(found.values())


MODELS: list[type[BaseModel]] = _discover_models()
MODEL_IDS = [_qid(m) for m in MODELS]

# Models the suite must cover. If discovery drops one of these (because its
# module failed to import), the guard test fails loudly instead of silently
# losing coverage.
_REQUIRED_MODELS = {
    "kai.agent.scheduler.Task",
    "kai.agent.core.ActionResult",
    "kai.agent.core.ChatResult",
    "kai.agent.core.ToolCallRecord",
    "kai.bots.base.TaskAction",
    "kai.bots.base.TellResult",
    "kai.bots.waha.actions.WahaAction",
    "kai.bots.waha.actions.WahaNoSilentAction",
    "kai.bots.waha.media.MediaAttachment",
    "kai.bots.waha.payload.MessageMetadata",
    "kai.bots.waha.mentions.ResolvedReply",
    "kai.agent.context.MessageContext",
    "kai.agent.context.ChatContext",
    "kai.agent.goal.Goal",
    "kai.runs.RunRecord",
}


class _UnsampleableError(Exception):
    """Raised when a valid sample value can't be synthesized for an annotation."""


def _unwrap_optional(ann: Any) -> Any:
    origin = get_origin(ann)
    if origin is Union or origin is types.UnionType:
        args = [a for a in get_args(ann) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return ann


def _sample(ann: Any) -> Any:
    """Synthesize one valid value for a field annotation.

    Only the annotation shapes that appear across the ``kai`` models are
    handled; anything else raises ``_Unsampleable`` so the caller can skip
    that model's per-field sweep (its schema is still validated by
    ``test_model_builds_json_schema``).
    """
    ann = _unwrap_optional(ann)
    origin = get_origin(ann)

    if origin is Literal:
        return get_args(ann)[0]
    if origin is list:
        return []
    if origin is tuple:
        return ()
    if origin is dict or ann is dict:
        return {}
    if ann is list:
        return []
    if ann is tuple:
        return ()

    if isinstance(ann, type) and issubclass(ann, Enum):
        return next(iter(ann))
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return _sample_dict(ann)

    if ann is str:
        return "x"
    if ann is bool:
        return True
    if ann is int:
        return 1
    if ann is float:
        return 1.0
    if ann is bytes:
        return b""
    if ann is datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)
    if ann is Path:
        return Path("/tmp")
    if ann is Any or ann is None or ann is inspect.Parameter.empty:
        return "x"

    raise _UnsampleableError(ann)


def _sample_dict(model: type[BaseModel]) -> dict[str, Any]:
    """Build a dict of valid values for a model's required fields."""
    try:
        hints = _hints(model)
    except Exception as exc:
        raise _UnsampleableError(model) from exc
    sample: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        if not field.is_required():
            continue
        sample[name] = _sample(hints.get(name, field.annotation))
    return sample


def _hints(model: type[BaseModel]) -> dict[str, Any]:
    return get_type_hints(model, include_extras=True)


def _is_settings(model: type[BaseModel]) -> bool:
    return BaseSettings is not None and issubclass(model, BaseSettings)


def _enum_fields(model: type[BaseModel]) -> dict[str, list[Any]]:
    """Map field name -> allowed members for every Literal/Enum field.

    pydantic emits ``Literal`` fields with an inline ``enum``, but ``Enum``
    class fields as a ``$ref`` into ``$defs`` (and Optional enums as
    ``anyOf`` of a ``$ref`` + null). All three shapes are resolved here.
    """
    schema = model.model_json_schema()
    props = schema.get("properties", {})
    fields: dict[str, list[Any]] = {}
    for name, prop in props.items():
        if not isinstance(prop, dict):
            continue
        members = _enum_of_prop(schema, prop)
        if members:
            fields[name] = members
    return fields


def _resolve_ref(schema: dict[str, Any], ref: str | None) -> dict[str, Any] | None:
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        return None
    node: Any = schema.get("$defs") or {}
    for part in ref[len("#/$defs/") :].split("/"):
        node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return None
    return node


def _enum_of_prop(schema: dict[str, Any], prop: dict[str, Any]) -> list[Any] | None:
    if isinstance(prop.get("enum"), list):
        return list(prop["enum"])
    target = _resolve_ref(schema, prop.get("$ref"))
    if target and isinstance(target.get("enum"), list):
        return list(target["enum"])
    for key in ("anyOf", "oneOf"):
        options = prop.get(key)
        if not isinstance(options, list):
            continue
        # A union is only a closed enum if every option is itself an enum (or
        # null, for Optional[Literal]). A union like ``Literal[...] | str`` —
        # emitted as ``anyOf: [{enum, type:string}, {type:string}]`` — is an
        # *open* field (any string is accepted), so it must not be reported
        # as a closed set the sweep would then try to enforce.
        members: list[Any] = []
        all_enum = True
        for opt in options:
            if not isinstance(opt, dict):
                all_enum = False
                break
            if opt.get("type") == "null":
                continue
            if isinstance(opt.get("enum"), list):
                members = list(opt["enum"])
                continue
            ref_target = _resolve_ref(schema, opt.get("$ref"))
            if ref_target and isinstance(ref_target.get("enum"), list):
                members = list(ref_target["enum"])
                continue
            all_enum = False
            break
        if all_enum and members:
            return members
    return None


def test_discovery_covers_key_models() -> None:
    names = {m: _qid(m) for m in MODELS}
    present = set(names.values())
    missing = _REQUIRED_MODELS - present
    assert not missing, (
        f"model discovery missed: {sorted(missing)}; "
        "an import likely failed and was silently skipped"
    )


def test_discovery_found_models() -> None:
    # A sanity floor so a broken walk (e.g. empty result) can't pass silently.
    assert len(MODELS) >= len(_REQUIRED_MODELS), MODELS


@pytest.mark.parametrize("model", MODELS, ids=MODEL_IDS)
def test_model_builds_json_schema(model: type[BaseModel]) -> None:
    """Every model must generate its JSON schema (forces forward-ref rebuild).

    Catches the ``is not fully defined`` class of error for every pydantic
    model in the package, not just the one that broke at chat time.
    """
    if _is_settings(model):
        pytest.skip("BaseSettings is env-backed; validated via its own path")
    model.model_rebuild()
    schema = model.model_json_schema()
    assert isinstance(schema, dict)


@pytest.mark.parametrize("model", MODELS, ids=MODEL_IDS)
def test_enum_fields_validate(model: type[BaseModel]) -> None:
    """Each Literal/Enum field accepts every member and rejects non-members.

    Guards against widened-value assignments (e.g. feeding a raw ``str`` into a
    ``Literal`` field) by pinning that the field's closed set is enforced by
    pydantic for every model.
    """
    if _is_settings(model):
        pytest.skip("BaseSettings is env-backed; validated via its own path")
    fields = _enum_fields(model)
    if not fields:
        pytest.skip("no Literal/Enum fields")

    base = _try_build_base(model)
    if base is None:
        pytest.skip("cannot build a valid base instance")
    assert isinstance(base, dict)

    for field, members in fields.items():
        for member in members:
            model.model_validate({**base, field: member})
        with pytest.raises(ValidationError):
            model.model_validate({**base, field: "__no_such_value__"})


def _try_build_base(model: type[BaseModel]) -> dict[str, Any] | None:
    """Synthesize a valid field dict for ``model``'s required fields, or None."""
    try:
        base = _sample_dict(model)
        model.model_validate(base)
        return base
    except _UnsampleableError:
        return None
    except Exception:
        return None


# --- Task-specific: the actual deserialization boundary that regressed ------


def _task_dict(repeat: Any = "none") -> dict[str, Any]:
    from kai.agent.scheduler import Task

    task = Task(
        id="x",
        chat_id="c",
        goal="a clear goal text",
        due_at=datetime(2026, 6, 18, 12, 0, tzinfo=UTC),
    )
    data = task.to_dict()
    data["repeat"] = repeat
    return data


def test_from_dict_roundtrips_every_repeat_kind() -> None:
    from kai.agent.scheduler import REPEAT_KINDS, Task

    for kind in REPEAT_KINDS:
        assert Task.from_dict(_task_dict(repeat=kind)).repeat == kind


def test_from_dict_invalid_repeat_falls_back_to_none() -> None:
    from kai.agent.scheduler import Task

    for bad in ("weklky", "", "DAILY", "monthly-ish", None, 5, True):
        data = _task_dict(repeat="daily")
        data["repeat"] = bad
        assert Task.from_dict(data).repeat == "none", bad


def test_from_dict_missing_repeat_defaults_to_none() -> None:
    from kai.agent.scheduler import Task

    data = _task_dict()
    data.pop("repeat", None)
    assert Task.from_dict(data).repeat == "none"


# --- Tool schema: the chat-time path that originally blew up -----------------


def test_task_tools_build_openai_schema() -> None:
    from kai.agent.context import ToolContext
    from kai.agent.scheduler import TaskScheduler, TaskStore, build_task_tools

    async def _execute(task: object) -> None: ...

    scheduler = TaskScheduler(TaskStore(None), execute=_execute)
    context = ToolContext(chat_id="c", owner_id="o", tz_hint="UTC")
    tools = build_task_tools(scheduler, context=context)
    assert {t.metadata.name for t in tools} == {
        "schedule_task",
        "list_tasks",
        "cancel_task",
    }
    # This is the exact call that raised PydanticUserError at chat time.
    for tool in tools:
        tool.metadata.to_openai_tool(skip_length_check=True)
