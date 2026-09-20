from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.parse
import urllib.request
import venv
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import Requirement


@dataclass(frozen=True)
class Resource:
    name: str
    url: str
    sha256: str
    archive: Path


def runtime_packages(root: Path) -> list[dict]:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    lock = tomllib.loads((root / "uv.lock").read_text())
    by_name: dict[str, list[dict]] = {}
    for package in lock["package"]:
        by_name.setdefault(package["name"], []).append(package)
    app = by_name[project["name"]][0]
    if app["version"] != project["version"]:
        raise ValueError("project version disagrees with uv.lock; run uv lock")
    requirements = [Requirement(value) for value in project["dependencies"]]
    if {requirement.name for requirement in requirements} != {
        dependency["name"] for dependency in app["dependencies"]
    }:
        raise ValueError("project runtime dependencies disagree with uv.lock")
    ordered: dict[str, dict] = {}
    active: set[str] = set()

    def visit(dependency: dict) -> None:
        if set(dependency) != {"name"}:
            raise ValueError(
                "conditional or multiple-version runtime dependencies need an explicit Homebrew policy"
            )
        name = dependency["name"]
        if name in ordered:
            return
        if name in active or len(by_name.get(name, [])) != 1:
            raise ValueError(f"ambiguous runtime dependency: {name}")
        active.add(name)
        package = by_name[name][0]
        if set(package["source"]) != {"registry"}:
            raise ValueError(f"runtime dependency requires a registry source: {name}")
        for child in package.get("dependencies", []):
            visit(child)
        active.remove(name)
        ordered[name] = package

    for dependency in app["dependencies"]:
        visit(dependency)
    for requirement in requirements:
        package = ordered[requirement.name]
        if requirement.marker or requirement.extras:
            raise ValueError("conditional project dependencies need an explicit Homebrew policy")
        if requirement.url:
            raise ValueError("direct project dependencies need an explicit Homebrew policy")
        if package["version"] not in requirement.specifier:
            raise ValueError(f"locked version does not satisfy {requirement}")
    return list(ordered.values())


def _download(url: str, destination: Path) -> None:
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("release downloads require HTTPS")
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
        destination.write_bytes(response.read())


def prepare_resources(
    root: Path, cache: Path, overrides: dict | None = None
) -> tuple[Resource, ...]:
    cache.mkdir(parents=True, exist_ok=True)
    resources: list[Resource] = []
    for package in runtime_packages(root):
        sdist = package["sdist"]
        url = sdist["url"]
        expected = sdist["hash"].removeprefix("sha256:")
        archive = cache / f"{package['name']}.tar.gz"
        if overrides is not None:
            override = overrides[url]
            payload = Path(override["path"]).read_bytes()
            if hashlib.sha256(payload).hexdigest() != override["sha256"]:
                raise ValueError(f"local transport checksum mismatch: {package['name']}")
            archive.write_bytes(payload)
        else:
            _download(url, archive)
        checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        if checksum != expected:
            raise ValueError(f"locked source checksum mismatch: {package['name']}")
        resources.append(Resource(package["name"], url, checksum, archive))
    return tuple(resources)


def stage_resource(resource: Resource, destination: Path) -> Path:
    if hashlib.sha256(resource.archive.read_bytes()).hexdigest() != resource.sha256:
        raise ValueError(f"source checksum mismatch: {resource.name}")
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(resource.archive) as archive:
        archive.extractall(destination, filter="data")
    roots = list(destination.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise ValueError(f"source archive needs one project root: {resource.name}")
    project = roots[0]
    if not (project / "pyproject.toml").is_file():
        raise ValueError(f"source project is missing pyproject.toml: {resource.name}")
    return project


def verify_install(resources: tuple[Resource, ...], environment: Path, project: Path) -> None:
    if environment.exists():
        raise ValueError("verification environment must be new")
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / "bin" / "python"
    arguments = [
        str(python),
        "-m",
        "pip",
        "install",
        "--no-deps",
        "--no-binary=:all:",
        "--ignore-installed",
        "--no-compile",
    ]
    with tempfile.TemporaryDirectory() as temporary:
        for resource in resources:
            staged = stage_resource(resource, Path(temporary) / resource.name)
            subprocess.run([*arguments, str(staged)], check=True)
        subprocess.run([*arguments, str(project)], check=True)
    subprocess.run([str(python), "-m", "pip", "check"], check=True)
    subprocess.run([str(environment / "bin" / "splash"), "ai", "--help"], check=True)


def render_formula(version: str, source_sha: str, resources: tuple[Resource, ...]) -> str:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) or not re.fullmatch(
        r"[0-9a-f]{64}", source_sha
    ):
        raise ValueError("invalid release version or source checksum")
    blocks = "\n".join(
        f'  resource "{item.name}" do\n    url "{item.url}"\n    sha256 "{item.sha256}"\n  end\n'
        for item in resources
    )
    installs = "\n".join(
        f'    resource("{item.name}").stage {{ venv.pip_install Pathname.pwd }}'
        for item in resources
    )
    return f'''class Splashdown < Formula
  include Language::Python::Virtualenv

  desc "Per-checkout resource provisioner: sims, ports, env templates for git worktrees"
  homepage "https://github.com/nielsmadan/splashdown"
  url "https://github.com/nielsmadan/splashdown/archive/refs/tags/v{version}.tar.gz"
  sha256 "{source_sha}"
  license "MIT"
  head "https://github.com/nielsmadan/splashdown.git", branch: "main"

  depends_on "python@3.13"

{blocks}
  def install
    venv = virtualenv_create(libexec, "python3.13")
{installs}
    venv.pip_install_and_link buildpath
    generate_completions_from_executable(
      libexec/"bin/register-python-argcomplete", "splash",
      shell_parameter_format: :arg, base_name: "splash", shells: [:bash, :zsh]
    )
  end

  test do
    assert_match "Per-checkout resource provisioner", shell_output("#{{bin}}/splash --help")
    Dir.chdir(testpath) do
      ENV["XDG_STATE_HOME"] = (testpath/"state").to_s
      system "git", "init", "-q"
      system bin/"splash", "init", "minimal"
      assert_predicate testpath/"splashdown.toml", :exist?
      assert_predicate testpath/"splashdown.env", :exist?
      system bin/"splash", "ai", "status"
    end
  end
end
'''


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--verify-install", type=Path)
    parser.add_argument("--artifact-map", type=Path)
    args = parser.parse_args()
    overrides = json.loads(args.artifact_map.read_text()) if args.artifact_map else None
    resources = prepare_resources(args.root, args.cache, overrides)
    if args.verify_install:
        verify_install(resources, args.verify_install, args.root)
    version = tomllib.loads((args.root / "pyproject.toml").read_text())["project"]["version"]
    args.output.write_text(render_formula(version, args.source_sha, resources))


if __name__ == "__main__":
    main()
