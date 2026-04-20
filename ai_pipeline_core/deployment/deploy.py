#!/usr/bin/env python3
"""Prefect deployment with bundled wheels for offline installation.

Strategy: builds a project wheel, resolves and downloads ALL dependency wheels
at deploy time, bundles them into a single tarball, uploads to GCS. The worker
extracts and installs fully offline (no PyPI contact).

This eliminates stale pip cache issues, missing transitive dependencies, and
non-deterministic installs.

Requirements:
- `uv` installed locally (used for `uv build` and dependency resolution)
- `pip` installed locally (used for wheel download)
- Settings: PREFECT_API_URL, PREFECT_GCS_BUCKET
- pyproject.toml with project name and version
- Local package installed for flow metadata extraction

Usage:
    python -m ai_pipeline_core.deployment.deploy
"""

import annotationlib
import argparse
import asyncio
import base64
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import traceback
from pathlib import Path
from typing import Any

from prefect.cli.deploy._storage import _PullStepStorage
from prefect.client.orchestration import get_client
from prefect.deployments.runner import RunnerDeployment
from prefect.flows import load_flow_from_entrypoint
from prefect_gcp.cloud_storage import GcpCredentials, GcsBucket  # pyright: ignore[reportMissingTypeStubs]

from ai_pipeline_core.logger._logging_config import setup_logging
from ai_pipeline_core.settings import settings

__all__ = [
    "_Deployer",
    "_main",
]

_UV_TARGET_PLATFORM = "x86_64-unknown-linux-gnu"
_PIP_TARGET_PLATFORMS = ("manylinux_2_28_x86_64", "manylinux_2_17_x86_64", "manylinux2014_x86_64", "linux_x86_64")
_TARGET_PYTHON_VERSION = "3.14"
_TARGET_ABI = "cp314"


def _filter_lock_file(lock_file: Path, exclude_names: set[str]) -> None:
    """Remove vendor packages from lock file so pip download skips them."""
    lines = lock_file.read_text(encoding="utf-8").splitlines()
    filtered = [line for line in lines if not _is_excluded_package(line, exclude_names)]
    lock_file.write_text("\n".join(filtered) + "\n", encoding="utf-8")


def _is_excluded_package(line: str, exclude_names: set[str]) -> bool:
    """Check if a lock file line matches a vendor package name (e.g. 'lib-drive==0.2.0')."""
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", "-")):
        return False
    pkg_name = stripped.split("==")[0].split(">=")[0].split("<=")[0].split("~=")[0].split("@")[0].split("[")[0].strip().replace("_", "-").lower()
    return pkg_name in exclude_names


class _Deployer:
    """Deploy Prefect flows with fully bundled dependencies.

    Build pipeline:
        wheel build → dependency lock → download wheels → bundle tarball → GCS upload → Prefect deployment

    Worker install (pull step):
        extract tarball → uv pip install --target /opt/ai-pipeline-deps/<pkg>/<hash>/ → unzip project wheel → bootstrap entrypoint
    """

    def __init__(self) -> None:
        self.config = self._load_config()
        self._validate_prefect_settings()
        self._project_wheel_name: str = ""

    def _load_config(self) -> dict[str, Any]:
        """Load and normalize project configuration from pyproject.toml."""
        if not settings.prefect_gcs_bucket:
            self._die("PREFECT_GCS_BUCKET not configured in settings.\nConfigure via environment variable or .env file:\n  PREFECT_GCS_BUCKET=your-bucket-name")

        pyproject_path = Path("pyproject.toml")
        if not pyproject_path.exists():
            self._die("pyproject.toml not found. Run from project root.")

        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)

        project = data.get("project", {})
        name = project.get("name")
        version = project.get("version")

        if not name:
            self._die("Project name not found in pyproject.toml")
        if not version:
            self._die("Project version not found in pyproject.toml")

        package_name = name.replace("-", "_")
        flow_folder = name.replace("_", "-")

        deploy_config = data.get("tool", {}).get("deploy", {})
        vendor_packages: list[str] = deploy_config.get("vendor_packages", [])

        return {
            "name": name,
            "package": package_name,
            "version": version,
            "bucket": settings.prefect_gcs_bucket,
            "folder": f"flows/{flow_folder}",
            "bundle": f"{package_name}-{version}-bundle.tar.gz",
            "work_pool": settings.prefect_work_pool_name,
            "work_queue": settings.prefect_work_queue_name,
            "vendor_packages": vendor_packages,
        }

    def _check_stubs(self) -> None:
        """Block deployment if any imported classes are stubs (stub=True)."""
        from ai_pipeline_core.pipeline._file_rules import get_stubs

        stubs = get_stubs()
        if not stubs:
            return
        lines = [f"  - {kind} '{cls.__name__}' ({qualname})" for qualname, (cls, kind) in sorted(stubs.items())]
        self._die(
            f"Cannot deploy: {len(stubs)} stub class(es) found with stub=True.\n"
            f"Stub classes are placeholders awaiting implementation:\n"
            + "\n".join(lines)
            + "\n\nImplement these classes and remove 'stub=True' before deploying."
        )

    def _validate_prefect_settings(self) -> None:
        """Validate that required Prefect settings are configured."""
        self.api_url = settings.prefect_api_url
        if not self.api_url:
            self._die(
                "PREFECT_API_URL not configured in settings.\n"
                "Configure via environment variable or .env file:\n"
                "  PREFECT_API_URL=https://api.prefect.cloud/api/accounts/.../workspaces/..."
            )

    def _run(self, cmd: str, *, check: bool = True) -> str | None:
        """Execute shell command and return output."""
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False)
        if check and result.returncode != 0:
            output = (result.stdout + result.stderr).strip()
            self._die(f"Command failed: {cmd}\n{output}")
        return result.stdout.strip() if result.returncode == 0 else None

    @staticmethod
    def _info(msg: str) -> None:
        print(f"\u2192 {msg}")

    @staticmethod
    def _success(msg: str) -> None:
        print(f"\u2713 {msg}")

    @staticmethod
    def _die(msg: str) -> None:
        print(f"\u2717 {msg}", file=sys.stderr)
        sys.exit(1)

    def _build_vendor_wheels(self, wheels_dir: Path) -> set[str]:
        """Build wheels for local/private vendor packages and return their normalized names."""
        vendor_packages: list[str] = self.config.get("vendor_packages", [])
        if not vendor_packages:
            return set()

        self._info(f"Building {len(vendor_packages)} vendor package wheels...")
        for vendor_path_str in vendor_packages:
            vendor_path = Path(vendor_path_str)
            if not vendor_path.exists():
                self._die(f"Vendor package not found: {vendor_path_str}")
            self._run(f"uv build --wheel --out-dir {shlex.quote(str(wheels_dir))} {shlex.quote(str(vendor_path))}")

        vendor_names = {whl.name.split("-")[0].replace("_", "-").lower() for whl in wheels_dir.iterdir()}
        self._success(f"Built vendor wheels: {', '.join(sorted(vendor_names))}")
        return vendor_names

    def _build_bundle(self) -> Path:
        """Build deployment bundle: project wheel + all dependency wheels.

        Creates a tarball with the project wheel at the root and all dependency
        wheels in a wheels/ subdirectory. This enables fully offline installation
        on the worker with `uv pip install --no-index --find-links wheels/`.
        """
        if not shutil.which("uv"):
            self._die("`uv` is required but not found on PATH.\nInstall with: curl -LsSf https://astral.sh/uv/install.sh | sh")

        self._info(f"Building deployment bundle for {self.config['name']} v{self.config['version']}")

        dist_dir = Path("dist")
        dist_dir.mkdir(exist_ok=True)
        bundle_path = dist_dir / self.config["bundle"]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wheels_dir = tmp_path / "wheels"
            wheels_dir.mkdir()

            # 1. Build vendor package wheels (local/private packages from [tool.deploy].vendor_packages)
            vendor_names = self._build_vendor_wheels(wheels_dir)

            # 2. Build project wheel
            self._info("Building project wheel...")
            self._run(f"uv build --wheel --out-dir {shlex.quote(str(tmp_path))}")
            project_wheels = list(tmp_path.glob("*.whl"))
            if not project_wheels:
                self._die("Wheel build produced no output. Check pyproject.toml build configuration.")
            project_wheel = project_wheels[0]
            self._project_wheel_name = project_wheel.name
            self._success(f"Built {project_wheel.name}")

            # 3. Compile lock file (resolve all transitive dependencies, targeting worker platform)
            self._info("Resolving dependencies...")
            lock_file = tmp_path / "requirements.lock"
            find_links = f"--find-links {shlex.quote(str(wheels_dir))}" if vendor_names else ""
            self._run(
                f"uv pip compile pyproject.toml -o {shlex.quote(str(lock_file))} "
                f"--python-platform {_UV_TARGET_PLATFORM} --python-version {_TARGET_PYTHON_VERSION} "
                f"{find_links}"
            )

            # 4. Filter vendor packages from lock file (already have their wheels) and download public deps
            if vendor_names:
                _filter_lock_file(lock_file, vendor_names)
            platform_flags = " ".join(f"--platform {p}" for p in _PIP_TARGET_PLATFORMS)
            self._info(f"Downloading dependency wheels (linux_x86_64/python{_TARGET_PYTHON_VERSION})...")
            self._run(
                f"pip download -r {shlex.quote(str(lock_file))} -d {shlex.quote(str(wheels_dir))} "
                f"{platform_flags} --python-version {_TARGET_PYTHON_VERSION} "
                f"--implementation cp --abi {_TARGET_ABI} --abi abi3 --abi none "
                f"--only-binary :all: --no-deps"
            )

            # Warn if any sdists were downloaded (will need build tools on worker)
            sdists = [f for f in wheels_dir.iterdir() if f.suffix == ".gz" or not f.name.endswith(".whl")]
            if sdists:
                sdist_names = ", ".join(f.name for f in sdists)
                self._info(f"WARNING: {len(sdists)} source distributions downloaded (may require build tools on worker): {sdist_names}")

            dep_count = len(list(wheels_dir.iterdir()))
            self._success(f"Downloaded {dep_count} dependency packages")

            # 5. Create bundle tarball (project wheel at root, deps in wheels/)
            self._info("Creating bundle tarball...")
            with tarfile.open(bundle_path, "w:gz") as tar:
                tar.add(str(project_wheel), arcname=project_wheel.name)
                for whl in sorted(wheels_dir.iterdir()):
                    tar.add(str(whl), arcname=f"wheels/{whl.name}")

        size_mb = bundle_path.stat().st_size / (1024 * 1024)
        self._success(f"Bundle: {bundle_path.name} ({size_mb:.1f} MB, {dep_count + 1} packages)")
        return bundle_path

    def _create_gcs_bucket(self, bucket_folder: str) -> Any:
        """Create a GcsBucket instance for uploading files."""
        creds = GcpCredentials()
        if settings.gcs_service_account_file:
            creds = GcpCredentials(service_account_file=Path(settings.gcs_service_account_file))
        return GcsBucket(bucket=self.config["bucket"], bucket_folder=bucket_folder, gcp_credentials=creds)

    async def _upload_bundle(self, bundle: Path) -> None:
        """Upload deployment bundle to Google Cloud Storage."""
        flow_folder = self.config["folder"]
        bucket = self._create_gcs_bucket(flow_folder)

        dest_uri = f"gs://{self.config['bucket']}/{flow_folder}/{bundle.name}"
        self._info(f"Uploading to {dest_uri}")

        await bucket.write_path(bundle.name, bundle.read_bytes())
        self._success(f"Bundle uploaded to {flow_folder}/{bundle.name}")

    def _build_install_body(self) -> str:
        """Build the bash body of the worker-side install script.

        Installs dependencies into a per-deployment immutable prefix under
        ``/opt/ai-pipeline-deps/<pkg>/<hash>/`` via ``uv pip install --target``
        and unzips the project wheel into the per-run pull directory. Generates
        a bootstrap module that becomes the runtime entrypoint: it reads the
        hint file, prepends the deps prefix to ``sys.path`` via
        ``site.addsitedir``, then loads the original flow via
        ``AI_PIPELINE_TARGET_ENTRYPOINT``.

        Per-deployment content-addressed prefixes eliminate the shared-mutable
        ``site-packages`` race where one flow run's install would delete files
        another run was importing. Different deployments and different bundle
        hashes never collide. A single global flock serializes first-install of
        any new (pkg, hash) pair; steady-state is a marker stat plus an unzip.

        This body contains shell builtins and constructs (``set``, subshells,
        heredocs) and must be executed by bash. It cannot be the emitted Prefect
        pull-step script directly because Prefect execs each line with no shell.
        ``_build_install_script`` wraps this body for Prefect consumption.
        """
        pkg = shlex.quote(self.config["package"])
        bundle = shlex.quote(self.config["bundle"])
        wheel = shlex.quote(f"./{self._project_wheel_name}") if self._project_wheel_name else "./*.whl"
        return f"""set -eu
PKG={pkg}
BUNDLE={bundle}
WHEEL={wheel}
LOCK=/tmp/ai-pipeline-core.install.lock
tar xzf "$BUNDLE"
HASH=$(sha256sum wheels/*.whl | awk '{{print $1}}' | sort | sha256sum | cut -c1-16)
PREFIX="/opt/ai-pipeline-deps/$PKG/$HASH"
MARKER="$PREFIX/.complete"
(
  flock -x 9
  if [ ! -f "$MARKER" ]; then
    # Clear any partial install left by a previous crashed run
    rm -rf "$PREFIX"
    mkdir -p "$PREFIX"
    uv pip install --target "$PREFIX" --no-index --no-deps wheels/*.whl
    touch "$MARKER"
  fi
) 9>"$LOCK"
printf '%s\\n' "$PREFIX" > .ai_pipeline_deps_prefix
cat > .ai_pipeline_bootstrap.py <<'PY'
import os
import site
import sys
from prefect.flows import load_flow_from_entrypoint

hint = os.path.join(os.getcwd(), ".ai_pipeline_deps_prefix")
if os.path.isfile(hint):
    with open(hint, encoding="utf-8") as f:
        prefix = f.read().strip()
    if prefix:
        # Prepend so deps from the per-deployment prefix outrank any residual
        # packages in system site-packages. addsitedir still runs so .pth files
        # inside the prefix are processed.
        sys.path.insert(0, prefix)
        site.addsitedir(prefix)

target = os.environ.get("AI_PIPELINE_TARGET_ENTRYPOINT", "")
if not target:
    raise RuntimeError(
        "Missing AI_PIPELINE_TARGET_ENTRYPOINT env var; "
        "ai-pipeline-core deploy.py must set this in job_variables.env"
    )

runtime_flow = load_flow_from_entrypoint(target, use_placeholder_flow=False)
PY
unzip -q -o "$WHEEL" -d .
"""

    def _build_install_script(self) -> str:
        """Build the Prefect-safe wrapper around the install body.

        Prefect's ``run_shell_script`` execs each line directly via
        ``shlex.split`` + ``open_process`` with no shell interpreter, so shell
        builtins (``set``, variable assignment, subshells, heredocs) cannot be
        top-level lines. The emitted script is therefore two lines: a ``python3``
        one-liner that base64-decodes the body to a file in the pull dir,
        followed by ``bash <file>`` which runs the body with full shell
        semantics. First tokens are always exec'able binaries.
        """
        body = self._build_install_body()
        encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
        # Line 1 execs python3 directly (always present on the worker; it's the
        # interpreter running Prefect) to write the real script. Line 2 execs
        # bash with the written file. No nested shells, no pipes, no base64
        # binary dependency. Each line's first token is an exec'able binary.
        decoder = f"import base64,pathlib;pathlib.Path('.ai_install.sh').write_bytes(base64.b64decode('{encoded}'))"
        return f"python3 -c {shlex.quote(decoder)}\nbash .ai_install.sh\n"

    async def _deploy_via_api(self) -> None:
        """Create or update Prefect deployment using RunnerDeployment pattern."""
        project_entrypoint = f"{self.config['package']}:{self.config['package']}"
        # Runtime entrypoint is a bootstrap module written by the pull step
        # (see _build_install_script). It runs after the per-deployment deps
        # prefix is prepared, prepends it to sys.path, then loads the real flow
        # referenced by AI_PIPELINE_TARGET_ENTRYPOINT.
        runtime_entrypoint = "./.ai_pipeline_bootstrap.py:runtime_flow"

        self._info(f"Loading flow from entrypoint: {project_entrypoint}")
        try:
            flow = load_flow_from_entrypoint(project_entrypoint)
            self._success(f"Loaded flow: {flow.name}")
            self._check_stubs()
        except ImportError as e:
            self._die(
                f"Failed to import flow: {e}\n\n"
                f"The package must be installed locally to extract flow metadata.\n"
                f"Install it with: pip install -e .\n\n"
                f"Expected entrypoint: {project_entrypoint}\n"
                f"This means: Python package '{self.config['package']}' "
                f"with flow function '{self.config['package']}'"
            )
        except AttributeError as e:
            self._die(
                f"Flow function not found: {e}\n\n"
                f"Expected flow function named '{self.config['package']}' "
                f"in package '{self.config['package']}'.\n"
                f"Check that your flow is decorated with @flow and named correctly."
            )

        install_script = self._build_install_script()

        pull_steps = [
            {
                "prefect_gcp.deployments.steps.pull_from_gcs": {
                    "id": "pull_code",
                    "requires": "prefect-gcp>=0.6",
                    "bucket": self.config["bucket"],
                    "folder": self.config["folder"],
                }
            },
            {
                "prefect.deployments.steps.run_shell_script": {
                    "id": "install_project",
                    "stream_output": True,
                    "directory": "{{ pull_code.directory }}",
                    "script": install_script,
                }
            },
        ]

        self._info(f"Creating deployment for flow '{flow.name}'")  # pyright: ignore[reportPossiblyUnboundVariable]

        deployment = RunnerDeployment(
            name=self.config["package"],
            flow_name=flow.name,  # pyright: ignore[reportPossiblyUnboundVariable]
            entrypoint=runtime_entrypoint,
            work_pool_name=self.config["work_pool"],
            work_queue_name=self.config["work_queue"],
            tags=[self.config["name"]],
            version=self.config["version"],
            description=flow.description or f"Deployment for {self.config['package']} v{self.config['version']}",  # pyright: ignore[reportPossiblyUnboundVariable]
            storage=_PullStepStorage(pull_steps),
            parameters={},
            job_variables={"env": {"AI_PIPELINE_TARGET_ENTRYPOINT": project_entrypoint}},
            paused=False,
        )

        deployment._set_defaults_from_flow(flow)  # pyright: ignore[reportPossiblyUnboundVariable]

        return_type = annotationlib.get_annotations(flow.fn, format=annotationlib.Format.VALUE).get("return")  # pyright: ignore[reportPossiblyUnboundVariable]
        if return_type is not None and hasattr(return_type, "model_json_schema"):
            deployment._parameter_openapi_schema.definitions["_ResultSchema"] = return_type.model_json_schema()

        # Inject integration metadata for the integration-schema middleware endpoint
        integration_meta: dict[str, Any] | None = getattr(flow.fn, "_integration_meta", None)  # pyright: ignore[reportPossiblyUnboundVariable]
        if integration_meta is not None:
            deployment._parameter_openapi_schema.definitions["_InputDocumentTypes"] = {
                "document_types": integration_meta["input_document_types"],
            }
            deployment._parameter_openapi_schema.definitions["_DeploymentMeta"] = {
                "all_document_types": integration_meta["all_document_types"],
                "flow_chain": integration_meta["flow_chain"],
            }

        async with get_client() as client:
            try:
                work_pool = await client.read_work_pool(self.config["work_pool"])
                self._success(f"Work pool '{self.config['work_pool']}' verified (type: {work_pool.type})")
            except Exception as e:
                self._die(f"Work pool '{self.config['work_pool']}' not accessible: {e}\nCreate it in the Prefect UI or with: prefect work-pool create")

        self._info("Applying deployment (create or update)...")
        try:
            deployment_id = await deployment.apply()  # type: ignore[no-untyped-call]
            self._success(f"Deployment ID: {deployment_id}")

            if self.api_url:
                ui_url = self.api_url.replace("/api/", "/")
                print(f"\nView deployment: {ui_url}/deployments/deployment/{deployment_id}")
                print(f"Run now: prefect deployment run '{flow.name}/{self.config['package']}'")  # pyright: ignore[reportPossiblyUnboundVariable]
        except Exception as e:
            self._die(f"Failed to apply deployment: {e}")

    async def run(self) -> None:
        """Execute the complete deployment pipeline: build, upload, deploy."""
        print("=" * 70)
        print(f"Prefect Deployment: {self.config['name']} v{self.config['version']}")
        print(f"Target: gs://{self.config['bucket']}/{self.config['folder']}")
        print("Strategy: Bundled Wheels (Offline Install)")
        print("=" * 70)
        print()

        bundle = await asyncio.to_thread(self._build_bundle)
        await self._upload_bundle(bundle)
        await self._deploy_via_api()

        print()
        print("=" * 70)
        self._success("Deployment complete!")
        print("=" * 70)


def _main() -> None:
    """Command-line interface for deployment script."""
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Deploy Prefect flows with bundled wheels (offline install)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Prerequisites:
  - uv installed (for dependency resolution and wheel download)
  - Settings configured with PREFECT_API_URL (and optionally PREFECT_API_KEY)
  - Settings configured with PREFECT_GCS_BUCKET
  - pyproject.toml with project name and version
  - Package installed locally: pip install -e .
  - GCP authentication configured (via service account or default credentials)
  - Work pool created in Prefect UI or CLI
        """,
    )

    parser.parse_args()

    try:
        deployer = _Deployer()
        asyncio.run(deployer.run())
    except KeyboardInterrupt:
        print("\n\u2717 Deployment cancelled by user", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\n\u2717 Unexpected error: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    _main()
