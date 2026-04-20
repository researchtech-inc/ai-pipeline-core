"""Tests for deployment/deploy.py — bundled wheels deployment."""

# pyright: reportPrivateUsage=false, reportUnusedClass=false

import shlex
import shutil
import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from pydantic import Field
from prefect.deployments.runner import RunnerDeployment

from ai_pipeline_core import DeploymentResult, Document, FlowOptions, PipelineDeployment
from ai_pipeline_core.deployment.deploy import _Deployer as Deployer
from ai_pipeline_core.pipeline import PipelineFlow


@pytest.fixture(autouse=True)
def _suppress_registration():
    return


class TestDeployer:
    """Test the Deployer class uses settings instead of environment variables."""

    @patch("ai_pipeline_core.deployment.deploy.settings")
    def test_init_validates_prefect_api_url(self, mock_settings):
        """Test that Deployer validates PREFECT_API_URL from settings."""
        mock_settings.prefect_api_url = ""
        mock_settings.prefect_gcs_bucket = "test-bucket"
        mock_settings.prefect_work_pool_name = "default"
        mock_settings.prefect_work_queue_name = "default"

        with patch("ai_pipeline_core.deployment.deploy.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            with patch("builtins.open", create=True), patch("ai_pipeline_core.deployment.deploy.tomllib.load") as mock_toml:
                mock_toml.return_value = {"project": {"name": "test-project", "version": "1.0.0"}}

                with pytest.raises(SystemExit) as exc_info:
                    Deployer()
                assert exc_info.value.code == 1

    @patch("ai_pipeline_core.deployment.deploy.settings")
    def test_init_validates_prefect_gcs_bucket(self, mock_settings):
        """Test that Deployer validates PREFECT_GCS_BUCKET from settings."""
        mock_settings.prefect_api_url = "http://test.api"
        mock_settings.prefect_gcs_bucket = ""
        mock_settings.prefect_work_pool_name = "default"
        mock_settings.prefect_work_queue_name = "default"

        with patch("ai_pipeline_core.deployment.deploy.Path") as mock_path:
            mock_path.return_value.exists.return_value = True

            with pytest.raises(SystemExit) as exc_info:
                Deployer()
            assert exc_info.value.code == 1

    @patch("ai_pipeline_core.deployment.deploy.settings")
    def test_init_loads_config_from_settings(self, mock_settings):
        """Test that Deployer loads configuration from settings."""
        mock_settings.prefect_api_url = "http://test.api"
        mock_settings.prefect_gcs_bucket = "test-bucket"
        mock_settings.prefect_work_pool_name = "test-pool"
        mock_settings.prefect_work_queue_name = "test-queue"

        with patch("ai_pipeline_core.deployment.deploy.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            with patch("builtins.open", create=True), patch("ai_pipeline_core.deployment.deploy.tomllib.load") as mock_toml:
                mock_toml.return_value = {"project": {"name": "test-project", "version": "1.0.0"}}

                deployer = Deployer()

                assert deployer.config["bucket"] == "test-bucket"
                assert deployer.config["work_pool"] == "test-pool"
                assert deployer.config["work_queue"] == "test-queue"
                assert deployer.api_url == "http://test.api"

    @patch("ai_pipeline_core.deployment.deploy.settings")
    def test_no_os_environ_usage(self, mock_settings):
        """Test that Deployer does not use os.environ directly."""
        mock_settings.prefect_api_url = "http://test.api"
        mock_settings.prefect_gcs_bucket = "test-bucket"
        mock_settings.prefect_work_pool_name = "default"
        mock_settings.prefect_work_queue_name = "default"

        with patch("ai_pipeline_core.deployment.deploy.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            with patch("builtins.open", create=True), patch("ai_pipeline_core.deployment.deploy.tomllib.load") as mock_toml:
                mock_toml.return_value = {"project": {"name": "test-project", "version": "1.0.0"}}

                with patch.dict("os.environ", {}, clear=True) as mock_environ:
                    mock_environ.__setitem__ = Mock(side_effect=AssertionError("os.environ should not be modified"))

                    deployer = Deployer()
                    assert deployer.api_url == "http://test.api"

    @patch("ai_pipeline_core.deployment.deploy.settings")
    def test_deploy_uses_settings_for_client(self, mock_settings):
        """Test that deployment uses settings for Prefect client configuration."""
        mock_settings.prefect_api_url = "http://test.api"
        mock_settings.prefect_gcs_bucket = "test-bucket"
        mock_settings.prefect_work_pool_name = "test-pool"
        mock_settings.prefect_work_queue_name = "test-queue"
        mock_settings.prefect_api_key = "test-key"

        with patch("ai_pipeline_core.deployment.deploy.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            with patch("builtins.open", create=True), patch("ai_pipeline_core.deployment.deploy.tomllib.load") as mock_toml:
                mock_toml.return_value = {"project": {"name": "test-project", "version": "1.0.0"}}

                deployer = Deployer()

                assert deployer.api_url == "http://test.api"
                assert deployer.config["bucket"] == "test-bucket"

    @patch("ai_pipeline_core.deployment.deploy.settings")
    def test_config_normalization(self, mock_settings):
        """Test that project names are normalized correctly."""
        mock_settings.prefect_api_url = "http://test.api"
        mock_settings.prefect_gcs_bucket = "test-bucket"
        mock_settings.prefect_work_pool_name = "default"
        mock_settings.prefect_work_queue_name = "default"

        with patch("ai_pipeline_core.deployment.deploy.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            with patch("builtins.open", create=True), patch("ai_pipeline_core.deployment.deploy.tomllib.load") as mock_toml:
                mock_toml.return_value = {"project": {"name": "my-test-project", "version": "1.0.0"}}

                deployer = Deployer()

                assert deployer.config["name"] == "my-test-project"
                assert deployer.config["package"] == "my_test_project"
                assert deployer.config["folder"] == "flows/my-test-project"


# --- Test types for deploy schema tests ---


class _DeployInputDoc(Document):
    """Input document for deploy schema tests."""


class _DeployOutputDoc(Document):
    """Output document for deploy schema tests."""


class _DeployTestOptions(FlowOptions):
    """Options with concrete fields for deploy schema testing."""

    search_query: str = Field(default="", description="Search query")
    max_results: int = Field(default=10, description="Max results")


class _DeployTestResult(DeploymentResult):
    """Result for deploy schema testing."""

    report: str = ""


class _DeployTestFlow(PipelineFlow):
    """Class-based flow for deploy schema tests."""

    async def run(self, input_docs: tuple[_DeployInputDoc, ...], options: _DeployTestOptions) -> tuple[_DeployOutputDoc, ...]:
        _ = (input_docs, options)
        return (_DeployOutputDoc.create_root(name="out.txt", content="ok", reason="test"),)


class _DeploySchemaTestDeployment(PipelineDeployment[_DeployTestOptions, _DeployTestResult]):
    flow_retries = 0

    def build_flows(self, options: _DeployTestOptions) -> list[PipelineFlow]:
        return [_DeployTestFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: _DeployTestOptions) -> _DeployTestResult:
        return _DeployTestResult(success=True, report="done")


def _resolve_options_props(schema: Any) -> dict[str, Any]:
    """Resolve options properties from a ParameterSchema object."""
    options_entry = schema.properties.get("options") or schema.properties.get("flow_options") or {}
    if "$ref" in options_entry:
        ref_name = options_entry["$ref"].split("/")[-1]
        return schema.definitions.get(ref_name, {}).get("properties", {})
    if "allOf" in options_entry:
        for item in options_entry["allOf"]:
            if "$ref" in item:
                ref_name = item["$ref"].split("/")[-1]
                return schema.definitions.get(ref_name, {}).get("properties", {})
    return options_entry.get("properties", {})


async def _capture_deployed_runner() -> RunnerDeployment:
    """Run _deploy_via_api with mocked dependencies and capture the RunnerDeployment before apply()."""
    prefect_flow = _DeploySchemaTestDeployment().as_prefect_flow()
    captured: dict[str, Any] = {}

    async def capture_apply(deployment_self: Any) -> str:
        captured["deployment"] = deployment_self
        return "test-deployment-id"

    deployer = Deployer.__new__(Deployer)
    deployer.config = {
        "package": "test_pkg",
        "name": "test-pkg",
        "version": "1.0.0",
        "bucket": "test-bucket",
        "folder": "flows/test",
        "bundle": "test_pkg-1.0.0-bundle.tar.gz",
        "work_pool": "test-pool",
        "work_queue": "default",
    }
    deployer.api_url = "http://test.api"
    deployer._project_wheel_name = ""
    deployer._info = deployer._success = lambda *a, **k: None  # type: ignore[assignment]
    deployer._die = lambda msg: (_ for _ in ()).throw(RuntimeError(msg))  # type: ignore[assignment]

    mock_client = AsyncMock()
    mock_client.read_work_pool.return_value = MagicMock(type="process")

    with (
        patch("ai_pipeline_core.deployment.deploy.load_flow_from_entrypoint", return_value=prefect_flow),
        patch("ai_pipeline_core.deployment.deploy.get_client") as mock_gc,
        patch.object(RunnerDeployment, "apply", capture_apply),
    ):
        mock_gc.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_gc.return_value.__aexit__ = AsyncMock(return_value=None)
        await deployer._deploy_via_api()

    return captured["deployment"]


class TestDeployParameterSchemaPopulation:
    """Test that _deploy_via_api populates RunnerDeployment's parameter_openapi_schema."""

    async def test_parameter_schema_is_populated(self):
        """_parameter_openapi_schema must have properties after deploy (was empty before fix)."""
        deployment = await _capture_deployed_runner()
        schema = deployment._parameter_openapi_schema
        assert schema.properties, "parameter schema properties must be populated"
        assert "options" in schema.properties

    async def test_options_schema_has_concrete_fields(self):
        """Options schema must include fields from the concrete FlowOptions subclass."""
        deployment = await _capture_deployed_runner()
        schema = deployment._parameter_openapi_schema
        options_props = _resolve_options_props(schema)
        assert "search_query" in options_props, f"search_query missing: {options_props}"
        assert "max_results" in options_props, f"max_results missing: {options_props}"

    async def test_result_schema_injected(self):
        """_ResultSchema must be present in definitions with concrete result fields."""
        deployment = await _capture_deployed_runner()
        schema = deployment._parameter_openapi_schema
        assert "_ResultSchema" in schema.definitions, f"_ResultSchema missing: {list(schema.definitions.keys())}"
        result_props = schema.definitions["_ResultSchema"].get("properties", {})
        assert "success" in result_props
        assert "error" in result_props
        assert "report" in result_props

    async def test_input_document_types_injected(self):
        """_InputDocumentTypes must be a dict wrapping the document type list."""
        deployment = await _capture_deployed_runner()
        schema = deployment._parameter_openapi_schema
        assert "_InputDocumentTypes" in schema.definitions, f"_InputDocumentTypes missing: {list(schema.definitions.keys())}"
        wrapper = schema.definitions["_InputDocumentTypes"]
        assert isinstance(wrapper, dict), f"_InputDocumentTypes must be a dict, got {type(wrapper).__name__}"
        items = wrapper["document_types"]
        class_names = [item["class_name"] for item in items]
        assert "_DeployInputDoc" in class_names

    async def test_input_document_types_have_descriptions(self):
        """Input document type entries must include docstring descriptions."""
        deployment = await _capture_deployed_runner()
        items = deployment._parameter_openapi_schema.definitions["_InputDocumentTypes"]["document_types"]
        for item in items:
            assert "description" in item, f"Missing description for {item.get('class_name')}"
            assert item["description"], f"Empty description for {item['class_name']}"

    async def test_deployment_meta_injected(self):
        """_DeploymentMeta must contain flow_chain and all_document_types."""
        deployment = await _capture_deployed_runner()
        schema = deployment._parameter_openapi_schema
        assert "_DeploymentMeta" in schema.definitions, f"_DeploymentMeta missing: {list(schema.definitions.keys())}"
        meta = schema.definitions["_DeploymentMeta"]
        assert "all_document_types" in meta
        assert "flow_chain" in meta
        chain = meta["flow_chain"]
        assert len(chain) == 1
        assert chain[0]["input_types"] == ["_DeployInputDoc"]
        assert chain[0]["output_types"] == ["_DeployOutputDoc"]


class TestSchemaDefinitionsAreValidJsonSchema:
    """All entries in parameter_openapi_schema.definitions must be valid JSON Schema."""

    async def test_all_definitions_are_valid_json_schema_types(self):
        """Every value in schema.definitions must be a dict or bool (JSON Schema requirement)."""
        deployment = await _capture_deployed_runner()
        definitions = deployment._parameter_openapi_schema.definitions

        for key, value in definitions.items():
            assert isinstance(value, (dict, bool)), (
                f"definitions['{key}'] is {type(value).__name__} ({value!r}), "
                f"but JSON Schema requires 'object' or 'boolean'. "
                f"Prefect server will reject this with a 500 error."
            )


class TestDocumentInputFieldDescriptions:
    """_DocumentInput and AttachmentInput fields must have descriptions in JSON schema."""

    def test_document_input_fields_have_descriptions(self):
        """All _DocumentInput fields must have a description in JSON schema output."""
        from ai_pipeline_core.deployment._resolve import _DocumentInput

        schema = _DocumentInput.model_json_schema()
        props = schema["properties"]
        for field_name in ("content", "url", "name", "description", "class_name", "derived_from", "triggered_by", "attachments"):
            assert "description" in props[field_name], f"_DocumentInput.{field_name} missing description"

    def test_attachment_input_fields_have_descriptions(self):
        """All AttachmentInput fields must have a description in JSON schema output."""
        from ai_pipeline_core.deployment._resolve import AttachmentInput

        schema = AttachmentInput.model_json_schema()
        props = schema["properties"]
        for field_name in ("content", "url", "name", "description"):
            assert "description" in props[field_name], f"AttachmentInput.{field_name} missing description"


def _make_deployer(**config_overrides: Any) -> Deployer:
    """Create a Deployer with mocked config (no filesystem/settings dependency)."""
    deployer = Deployer.__new__(Deployer)
    deployer.config = {
        "name": "test-project",
        "package": "test_project",
        "version": "1.0.0",
        "bucket": "test-bucket",
        "folder": "flows/test-project",
        "bundle": "test_project-1.0.0-bundle.tar.gz",
        "work_pool": "default",
        "work_queue": "default",
        **config_overrides,
    }
    deployer.api_url = "http://test.api"
    deployer._project_wheel_name = ""
    return deployer


def _decode_install_body_from_script(script: str) -> str:
    """Recover the bash install body from the emitted two-line Prefect script.

    The script's first line is a shlex-quoted python3 -c invocation. Parse it
    the same way Prefect does (shlex.split) so the outer quoting peels off,
    then extract the base64 payload from the clean python source.
    """
    import base64

    first_line = script.splitlines()[0]
    tokens = shlex.split(first_line, posix=True)
    assert tokens[:2] == ["python3", "-c"], f"unexpected first line: {first_line!r}"
    py_source = tokens[2]
    marker = "b64decode('"
    start = py_source.index(marker) + len(marker)
    end = py_source.index("'", start)
    return base64.b64decode(py_source[start:end]).decode("utf-8")


class TestBundleNaming:
    """Bundle tarball uses '-bundle' suffix to distinguish from legacy sdist."""

    @patch("ai_pipeline_core.deployment.deploy.settings")
    def test_bundle_name_format(self, mock_settings: Any) -> None:
        mock_settings.prefect_api_url = "http://test.api"
        mock_settings.prefect_gcs_bucket = "bucket"
        mock_settings.prefect_work_pool_name = "default"
        mock_settings.prefect_work_queue_name = "default"

        with (
            patch("ai_pipeline_core.deployment.deploy.Path") as mock_path,
            patch("builtins.open", create=True),
            patch("ai_pipeline_core.deployment.deploy.tomllib.load") as mock_toml,
        ):
            mock_path.return_value.exists.return_value = True
            mock_toml.return_value = {"project": {"name": "my-app", "version": "2.1.0"}}
            deployer = Deployer()

        assert deployer.config["bundle"] == "my_app-2.1.0-bundle.tar.gz"
        assert "-bundle" in deployer.config["bundle"]


class TestInstallScript:
    """Install script must install deps into a per-deployment immutable prefix."""

    def test_install_body_offline_mode(self) -> None:
        deployer = _make_deployer()
        body = deployer._build_install_body()

        assert "--no-index" in body, "Install must not contact PyPI"
        assert "--target" in body, "Install must use a per-deployment prefix (--target), not system Python"
        assert "--system" not in body, "Install must not mutate shared system site-packages"
        assert "--find-links" not in body, "Direct wheel args should be used, not --find-links"
        assert "/opt/ai-pipeline-deps/" in body, "Per-deployment prefix must live under /opt/ai-pipeline-deps/"
        assert "flock" in body, "Install must be guarded by a global lock to serialize concurrent first-installs"
        assert "tar xzf" in body, "Install must extract the bundle first"
        assert ".ai_pipeline_deps_prefix" in body, "Pull dir must carry the deps prefix hint for the bootstrap"
        assert ".ai_pipeline_bootstrap.py" in body, "Bootstrap module must be generated for the runtime entrypoint"
        assert "unzip" in body, "Project wheel must be unzipped into the pull directory"

    def test_install_body_references_bundle_name(self) -> None:
        deployer = _make_deployer(bundle="custom_pkg-3.0.0-bundle.tar.gz")
        body = deployer._build_install_body()

        assert "custom_pkg-3.0.0-bundle.tar.gz" in body

    def test_install_body_uses_explicit_wheel_name(self) -> None:
        deployer = _make_deployer()
        deployer._project_wheel_name = "test_project-1.0.0-py3-none-any.whl"
        body = deployer._build_install_body()

        assert "test_project-1.0.0-py3-none-any.whl" in body

    def test_install_body_falls_back_to_glob_without_wheel_name(self) -> None:
        deployer = _make_deployer()
        deployer._project_wheel_name = ""
        body = deployer._build_install_body()

        assert "./*.whl" in body

    def test_install_script_each_line_starts_with_real_binary(self) -> None:
        """Prefect run_shell_script execs each line via open_process with no shell.

        If any line starts with a shell builtin (set, cd, source, ...) or a shell
        construct (variable assignment, subshell, heredoc), the worker crashes
        with FileNotFoundError when it tries to exec that token as a binary.
        This regression guards against v0.22.4's bug.
        """
        deployer = _make_deployer()
        script = deployer._build_install_script()
        for i, line in enumerate(script.splitlines()):
            if not line.strip():
                continue
            tokens = shlex.split(line, posix=True)
            assert tokens, f"Line {i} empty after shlex.split: {line!r}"
            binary = tokens[0]
            assert shutil.which(binary) is not None, (
                f"Line {i} first token {binary!r} is not an exec'able binary. "
                f"Prefect run_shell_script calls each line via open_process "
                f"with no shell; shell builtins like 'set' or bash constructs "
                f"like 'VAR=value' fail at exec time. Line was: {line!r}"
            )


class TestUvValidation:
    """Deployer must fail early if uv is not available."""

    def test_build_bundle_fails_without_uv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        deployer = _make_deployer()

        with patch("ai_pipeline_core.deployment.deploy.shutil.which", return_value=None):
            with pytest.raises(SystemExit) as exc_info:
                deployer._build_bundle()
            assert exc_info.value.code == 1


class TestBuildBundle:
    """Test _build_bundle executes correct commands and creates correct tarball structure."""

    def test_build_bundle_commands(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify the correct sequence of shell commands is executed."""
        monkeypatch.chdir(tmp_path)
        deployer = _make_deployer()
        commands_run: list[str] = []

        def mock_run(cmd: str, *, check: bool = True) -> str:
            commands_run.append(cmd)
            if "build --wheel" in cmd:
                outdir = cmd.rsplit("--out-dir ", maxsplit=1)[-1].strip().strip("'\"")
                (Path(outdir) / "test_project-1.0.0-py3-none-any.whl").write_bytes(b"wheel")
            elif "pip download" in cmd:
                dest = cmd.rsplit("-d ", maxsplit=1)[-1].split(maxsplit=1)[0].strip("'\"")
                (Path(dest) / "dep-1.0.0-py3-none-any.whl").write_bytes(b"dep")
            return ""

        deployer._run = mock_run  # type: ignore[assignment]
        deployer._info = deployer._success = lambda *a, **k: None  # type: ignore[assignment]

        with patch("ai_pipeline_core.deployment.deploy.shutil.which", return_value="/usr/bin/uv"):
            deployer._build_bundle()

        assert len(commands_run) == 3, f"Expected 3 commands, got {len(commands_run)}: {commands_run}"

        # Command 1: build wheel
        assert "uv build --wheel" in commands_run[0]

        # Command 2: compile lock (must target worker platform)
        assert "uv pip compile" in commands_run[1]
        assert "pyproject.toml" in commands_run[1]
        assert "--python-platform" in commands_run[1], "Compile must target worker platform"
        assert "--python-version" in commands_run[1], "Compile must target worker Python version"

        # Command 3: download wheels via pip (not uv — uv lacks download subcommand)
        assert "pip download" in commands_run[2]
        assert "--platform" in commands_run[2]
        assert "--python-version" in commands_run[2]
        assert "--only-binary" in commands_run[2]
        assert "--no-deps" in commands_run[2]

    def test_build_bundle_tarball_structure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify the bundle tarball has project wheel at root and deps in wheels/."""
        monkeypatch.chdir(tmp_path)
        deployer = _make_deployer()

        def mock_run(cmd: str, *, check: bool = True) -> str:
            if "build --wheel" in cmd:
                outdir = cmd.rsplit("--out-dir ", maxsplit=1)[-1].strip().strip("'\"")
                (Path(outdir) / "test_project-1.0.0-py3-none-any.whl").write_bytes(b"project wheel")
            elif "pip download" in cmd:
                dest = cmd.rsplit("-d ", maxsplit=1)[-1].split(maxsplit=1)[0].strip("'\"")
                (Path(dest) / "httpx-0.28.0-py3-none-any.whl").write_bytes(b"httpx")
                (Path(dest) / "pydantic-2.11.0-py3-none-any.whl").write_bytes(b"pydantic")
            return ""

        deployer._run = mock_run  # type: ignore[assignment]
        deployer._info = deployer._success = lambda *a, **k: None  # type: ignore[assignment]

        with patch("ai_pipeline_core.deployment.deploy.shutil.which", return_value="/usr/bin/uv"):
            bundle_path = deployer._build_bundle()

        assert bundle_path.exists()
        with tarfile.open(bundle_path, "r:gz") as tar:
            names = tar.getnames()

        assert "test_project-1.0.0-py3-none-any.whl" in names

        dep_wheels = [n for n in names if n.startswith("wheels/")]
        assert len(dep_wheels) == 2
        assert "wheels/httpx-0.28.0-py3-none-any.whl" in names
        assert "wheels/pydantic-2.11.0-py3-none-any.whl" in names

        bundle_path.unlink(missing_ok=True)


class TestPullStepConfiguration:
    """Verify pull steps use the correct install script."""

    async def test_pull_step_uses_offline_install(self) -> None:
        """The pull step install script must install into a per-deployment prefix."""
        deployment = await _capture_deployed_runner()
        storage = deployment.storage

        pull_steps = storage.pull_steps  # type: ignore[union-attr]
        shell_step = pull_steps[1]
        script = shell_step["prefect.deployments.steps.run_shell_script"]["script"]
        body = _decode_install_body_from_script(script)

        assert "--no-index" in body
        assert "--target" in body
        assert "/opt/ai-pipeline-deps/" in body
        assert "tar xzf" in body


class TestShellQuoting:
    """Install body must properly quote filenames for shell safety."""

    def test_bundle_name_is_quoted(self) -> None:
        deployer = _make_deployer(bundle="my app-1.0-bundle.tar.gz")
        body = deployer._build_install_body()

        assert "my app-1.0-bundle.tar.gz" in body
        assert "'" in body

    def test_wheel_name_is_quoted(self) -> None:
        deployer = _make_deployer()
        deployer._project_wheel_name = "my_pkg-1.0.0-py3-none-any.whl"
        body = deployer._build_install_body()

        assert "my_pkg-1.0.0-py3-none-any.whl" in body


class TestPlatformTargeting:
    """Dependency resolution must target the worker platform, not the deployer's."""

    def test_compile_targets_worker_platform(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """uv pip compile must use --python-platform to target linux worker."""
        monkeypatch.chdir(tmp_path)
        from ai_pipeline_core.deployment.deploy import (
            _PIP_TARGET_PLATFORMS as PIP_TARGET_PLATFORMS,
            _TARGET_PYTHON_VERSION as TARGET_PYTHON_VERSION,
            _UV_TARGET_PLATFORM as UV_TARGET_PLATFORM,
        )

        deployer = _make_deployer()
        commands_run: list[str] = []

        def mock_run(cmd: str, *, check: bool = True) -> str:
            commands_run.append(cmd)
            if "build --wheel" in cmd:
                outdir = cmd.rsplit("--out-dir ", maxsplit=1)[-1].strip().strip("'\"")
                (Path(outdir) / "test_project-1.0.0-py3-none-any.whl").write_bytes(b"wheel")
            elif "pip download" in cmd:
                dest = cmd.rsplit("-d ", maxsplit=1)[-1].split(maxsplit=1)[0].strip("'\"")
                (Path(dest) / "dep-1.0.0-py3-none-any.whl").write_bytes(b"dep")
            return ""

        deployer._run = mock_run  # type: ignore[assignment]
        deployer._info = deployer._success = lambda *a, **k: None  # type: ignore[assignment]

        with patch("ai_pipeline_core.deployment.deploy.shutil.which", return_value="/usr/bin/uv"):
            deployer._build_bundle()

        compile_cmd = commands_run[1]
        assert UV_TARGET_PLATFORM in compile_cmd, f"Compile must target {UV_TARGET_PLATFORM}"
        assert TARGET_PYTHON_VERSION in compile_cmd

        download_cmd = commands_run[2]
        for plat in PIP_TARGET_PLATFORMS:
            assert plat in download_cmd, f"Download must include platform {plat}"
        assert TARGET_PYTHON_VERSION in download_cmd

    def test_build_bundle_stores_project_wheel_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """_build_bundle must store the project wheel name for use in install script."""
        monkeypatch.chdir(tmp_path)
        deployer = _make_deployer()

        def mock_run(cmd: str, *, check: bool = True) -> str:
            if "build --wheel" in cmd:
                outdir = cmd.rsplit("--out-dir ", maxsplit=1)[-1].strip().strip("'\"")
                (Path(outdir) / "test_project-1.0.0-py3-none-any.whl").write_bytes(b"wheel")
            elif "pip download" in cmd:
                dest = cmd.rsplit("-d ", maxsplit=1)[-1].split(maxsplit=1)[0].strip("'\"")
                (Path(dest) / "dep-1.0.0-py3-none-any.whl").write_bytes(b"dep")
            return ""

        deployer._run = mock_run  # type: ignore[assignment]
        deployer._info = deployer._success = lambda *a, **k: None  # type: ignore[assignment]

        with patch("ai_pipeline_core.deployment.deploy.shutil.which", return_value="/usr/bin/uv"):
            deployer._build_bundle()

        assert deployer._project_wheel_name == "test_project-1.0.0-py3-none-any.whl"

        body = deployer._build_install_body()
        assert "test_project-1.0.0-py3-none-any.whl" in body
        assert "./*.whl" not in body


class TestIsExcludedPackage:
    """Test lock file line filtering for vendor packages."""

    def test_pinned_package_match(self) -> None:
        from ai_pipeline_core.deployment.deploy import _is_excluded_package

        assert _is_excluded_package("lib-common==0.2.0", {"lib-common"})

    def test_pinned_package_no_match(self) -> None:
        from ai_pipeline_core.deployment.deploy import _is_excluded_package

        assert not _is_excluded_package("httpx==0.28.0", {"lib-common"})

    def test_comment_line_skipped(self) -> None:
        from ai_pipeline_core.deployment.deploy import _is_excluded_package

        assert not _is_excluded_package("# via lib-common", {"lib-common"})

    def test_blank_line_skipped(self) -> None:
        from ai_pipeline_core.deployment.deploy import _is_excluded_package

        assert not _is_excluded_package("", {"lib-common"})

    def test_flag_line_skipped(self) -> None:
        from ai_pipeline_core.deployment.deploy import _is_excluded_package

        assert not _is_excluded_package("-e .", {"lib-common"})

    def test_case_insensitive(self) -> None:
        from ai_pipeline_core.deployment.deploy import _is_excluded_package

        assert _is_excluded_package("Lib-Common==0.2.0", {"lib-common"})

    def test_underscore_normalized(self) -> None:
        from ai_pipeline_core.deployment.deploy import _is_excluded_package

        assert _is_excluded_package("lib_common==0.2.0", {"lib-common"})

    def test_extras_stripped(self) -> None:
        """Packages with extras like lib-common[grpc]==1.0 must still match."""
        from ai_pipeline_core.deployment.deploy import _is_excluded_package

        assert _is_excluded_package("lib-common[grpc]==0.2.0", {"lib-common"})

    def test_url_reference(self) -> None:
        """Direct URL references like pkg @ file:///... must match."""
        from ai_pipeline_core.deployment.deploy import _is_excluded_package

        assert _is_excluded_package("lib-common @ file:///tmp/lib_common-0.2.0.whl", {"lib-common"})


class TestFilterLockFile:
    """Test end-to-end lock file filtering."""

    def test_removes_vendor_keeps_public(self, tmp_path: Path) -> None:
        from ai_pipeline_core.deployment.deploy import _filter_lock_file

        lock = tmp_path / "requirements.lock"
        lock.write_text("# generated by uv\nlib-common==0.2.0\nlib-drive==0.2.0\nhttpx==0.28.0\npydantic==2.11.0\n")

        _filter_lock_file(lock, {"lib-common", "lib-drive"})

        lines = [line for line in lock.read_text().splitlines() if line.strip()]
        assert "lib-common==0.2.0" not in lines
        assert "lib-drive==0.2.0" not in lines
        assert "httpx==0.28.0" in lines
        assert "pydantic==2.11.0" in lines
        assert "# generated by uv" in lock.read_text()


class TestVendorPackages:
    """Test vendor package wheel building and bundle integration."""

    def test_build_bundle_with_vendor_includes_find_links(self, tmp_path: Path) -> None:
        """When vendor_packages is set, uv pip compile must receive --find-links."""
        vendor_dir = tmp_path / "libs" / "common"
        vendor_dir.mkdir(parents=True)
        deployer = _make_deployer()
        deployer.config["vendor_packages"] = [str(vendor_dir)]
        commands_run: list[str] = []

        def mock_run(cmd: str, *, check: bool = True) -> str:
            commands_run.append(cmd)
            if "build --wheel" in cmd:
                outdir = cmd.rsplit("--out-dir ", maxsplit=1)[-1].split(maxsplit=1)[0].strip("'\"")
                Path(outdir).mkdir(parents=True, exist_ok=True)
                if "common" in cmd:
                    (Path(outdir) / "lib_common-0.2.0-py3-none-any.whl").write_bytes(b"vendor")
                else:
                    (Path(outdir) / "test_project-1.0.0-py3-none-any.whl").write_bytes(b"wheel")
            elif "pip download" in cmd:
                dest = cmd.rsplit("-d ", maxsplit=1)[-1].split(maxsplit=1)[0].strip("'\"")
                (Path(dest) / "httpx-0.28.0-py3-none-any.whl").write_bytes(b"dep")
            elif "uv pip compile" in cmd:
                out_flag = cmd.rsplit("-o ", maxsplit=1)[-1].split(maxsplit=1)[0].strip("'\"")
                Path(out_flag).write_text("lib-common==0.2.0\nhttpx==0.28.0\n")
            return ""

        deployer._run = mock_run  # type: ignore[assignment]
        deployer._info = deployer._success = lambda *a, **k: None  # type: ignore[assignment]

        with patch("ai_pipeline_core.deployment.deploy.shutil.which", return_value="/usr/bin/uv"):
            deployer._build_bundle()

        compile_cmds = [c for c in commands_run if "uv pip compile" in c]
        assert compile_cmds, "uv pip compile was not called"
        assert "--find-links" in compile_cmds[0]

    def test_build_bundle_without_vendor_no_find_links(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When no vendor_packages, uv pip compile must NOT have --find-links."""
        monkeypatch.chdir(tmp_path)
        deployer = _make_deployer()
        commands_run: list[str] = []

        def mock_run(cmd: str, *, check: bool = True) -> str:
            commands_run.append(cmd)
            if "build --wheel" in cmd:
                outdir = cmd.rsplit("--out-dir ", maxsplit=1)[-1].strip().strip("'\"")
                (Path(outdir) / "test_project-1.0.0-py3-none-any.whl").write_bytes(b"wheel")
            elif "pip download" in cmd:
                dest = cmd.rsplit("-d ", maxsplit=1)[-1].split(maxsplit=1)[0].strip("'\"")
                (Path(dest) / "dep-1.0.0-py3-none-any.whl").write_bytes(b"dep")
            return ""

        deployer._run = mock_run  # type: ignore[assignment]
        deployer._info = deployer._success = lambda *a, **k: None  # type: ignore[assignment]

        with patch("ai_pipeline_core.deployment.deploy.shutil.which", return_value="/usr/bin/uv"):
            deployer._build_bundle()

        compile_cmds = [c for c in commands_run if "uv pip compile" in c]
        assert "--find-links" not in compile_cmds[0]

    def test_vendor_wheel_names_normalized(self, tmp_path: Path) -> None:
        """Wheel filenames with underscores must normalize to hyphenated names for lock file filtering."""
        vendor_dir = tmp_path / "libs" / "common"
        vendor_dir.mkdir(parents=True)
        deployer = _make_deployer()
        deployer.config["vendor_packages"] = [str(vendor_dir)]

        def mock_run(cmd: str, *, check: bool = True) -> str:
            if "build --wheel" in cmd:
                outdir = cmd.rsplit("--out-dir ", maxsplit=1)[-1].split(maxsplit=1)[0].strip("'\"")
                Path(outdir).mkdir(parents=True, exist_ok=True)
                (Path(outdir) / "lib_common-0.2.0-py3-none-any.whl").write_bytes(b"vendor")
            return ""

        deployer._run = mock_run  # type: ignore[assignment]
        deployer._info = deployer._success = lambda *a, **k: None  # type: ignore[assignment]

        wheels_dir = tmp_path / "wheels"
        wheels_dir.mkdir()
        vendor_names = deployer._build_vendor_wheels(wheels_dir)

        assert "lib-common" in vendor_names
