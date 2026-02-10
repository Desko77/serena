"""Generate docker-compose.yml from a Serena Docker configuration file.

The configuration file (``~/.serena/docker.yml``) lists host project paths.
This module resolves those paths into Docker bind-mount volumes and produces
a ready-to-use ``docker-compose.yml``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from serena.constants import DOCKER_CONFIG_TEMPLATE_FILE, SERENA_FILE_ENCODING
from serena.util.general import load_yaml, save_yaml

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults applied when keys are missing from docker.yml
# ---------------------------------------------------------------------------
_DOCKER_DEFAULTS: dict[str, Any] = {
    "build_context": ".",
    "build_target": "production",
    "admin_port": 9000,
    "base_port": 9200,
    "port_headroom": 5,
    "transport": "streamable-http",
    "host": "0.0.0.0",
    "serena_data_volume": "serena-data",
}

CONTAINER_PROJECTS_DIR = "/projects"
"""Mount-point inside the container where project directories appear."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_docker_config(config_path: str) -> dict[str, Any]:
    """Load and validate a ``docker.yml`` file, applying defaults.

    :param config_path: Absolute path to the YAML file.
    :return: Normalised configuration dict with ``projects`` and ``docker`` keys.
    :raises FileNotFoundError: If *config_path* does not exist.
    :raises ValueError: If the file is invalid or has no projects.
    """
    if not os.path.isfile(config_path):
        raise FileNotFoundError(f"Docker config not found: {config_path}")

    data: dict[str, Any] = load_yaml(config_path, preserve_comments=False)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid docker config (expected YAML mapping): {config_path}")

    projects = data.get("projects")
    if not projects or not isinstance(projects, list):
        raise ValueError(
            f"No projects listed in {config_path}.\n"
            "Add project paths under the 'projects:' key, for example:\n"
            "  projects:\n"
            "    - C:\\Projects\\my-erp-project\n"
            "    - /home/user/my_project"
        )

    docker_section: dict[str, Any] = data.get("docker", {})
    if not isinstance(docker_section, dict):
        docker_section = {}

    # Apply defaults for missing keys
    for key, default in _DOCKER_DEFAULTS.items():
        docker_section.setdefault(key, default)

    data["docker"] = docker_section
    return data


def resolve_project_mounts(project_paths: list[str]) -> list[tuple[str, str]]:
    """Map host paths to unique container directory names.

    :param project_paths: List of absolute host paths.
    :return: List of ``(host_path, container_name)`` tuples.
             Container name is the basename, with ``_2``, ``_3`` … suffixes
             to resolve collisions.
    """
    used_names: dict[str, int] = {}
    result: list[tuple[str, str]] = []
    skipped: list[str] = []

    for raw_path in project_paths:
        host_path = os.path.normpath(str(raw_path))

        if not os.path.isdir(host_path):
            log.warning("Skipping (directory not found): %s", host_path)
            skipped.append(host_path)
            continue

        base = os.path.basename(host_path)
        if not base:
            # Edge case: path like "C:\" → basename is empty
            base = "project"

        if base in used_names:
            used_names[base] += 1
            name = f"{base}_{used_names[base]}"
        else:
            used_names[base] = 1
            name = base

        serena_yml = os.path.join(host_path, ".serena", "project.yml")
        if not os.path.isfile(serena_yml):
            log.warning("Project not initialised (no .serena/project.yml): %s", host_path)

        result.append((host_path, name))

    if skipped:
        log.warning("Skipped %d path(s) that do not exist on disk.", len(skipped))

    return result


def generate_compose_dict(
    config: dict[str, Any],
    mounts: list[tuple[str, str]],
) -> dict[str, Any]:
    """Build a docker-compose structure as a Python dict.

    :param config: Normalised config from :func:`load_docker_config`.
    :param mounts: Resolved mounts from :func:`resolve_project_mounts`.
    :return: Dict ready for YAML serialisation.
    """
    docker = config["docker"]
    admin_port: int = int(docker["admin_port"])
    base_port: int = int(docker["base_port"])
    headroom: int = int(docker["port_headroom"])
    transport: str = str(docker["transport"])
    host: str = str(docker["host"])
    volume_name: str = str(docker["serena_data_volume"])

    max_port = base_port + len(mounts) - 1 + headroom
    if max_port > 65535:
        max_port = 65535
        log.warning("Port range capped at 65535.")

    # -- volumes --
    volumes: list[str] = [f"{volume_name}:/root/.serena"]
    for host_path, container_name in mounts:
        # Docker Desktop for Windows accepts native paths in compose YAML.
        volumes.append(f"{host_path}:{CONTAINER_PROJECTS_DIR}/{container_name}")

    # -- environment --
    environment: dict[str, str] = {
        "SERENA_MULTI_SERVER": "1",
        "SERENA_PROJECTS_DIR": CONTAINER_PROJECTS_DIR,
        "SERENA_TRANSPORT": transport,
        "SERENA_BASE_PORT": str(base_port),
        "SERENA_HOST": host,
        "SERENA_ADMIN_PORT": str(admin_port),
    }

    # -- ports --
    ports: list[str] = []
    if admin_port > 0:
        ports.append(f"{admin_port}:{admin_port}")
    ports.append(f"{base_port}-{max_port}:{base_port}-{max_port}")

    # -- service definition --
    service: dict[str, Any] = {}

    image = docker.get("image")
    if image:
        service["image"] = str(image)
    else:
        service["build"] = {
            "context": str(docker["build_context"]),
            "target": str(docker["build_target"]),
        }

    service["volumes"] = volumes
    service["environment"] = environment
    service["ports"] = ports

    compose: dict[str, Any] = {
        "services": {"serena": service},
        "volumes": {volume_name: None},
    }
    return compose


def compose_dict_to_yaml(compose: dict[str, Any]) -> str:
    """Serialise a compose dict to a YAML string with a header comment.

    :param compose: Dict from :func:`generate_compose_dict`.
    :return: YAML text ready to write to a file.
    """
    import io

    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.default_flow_style = False

    buf = io.StringIO()
    yaml.dump(compose, buf)
    body = buf.getvalue()

    header = (
        "# Auto-generated by: serena docker generate-compose\n"
        "# Edit ~/.serena/docker.yml and re-run the command to regenerate.\n"
        "#\n"
        "# Usage:\n"
        "#   docker compose up -d\n"
        "#   docker compose down\n\n"
    )
    return header + body


def write_compose_file(compose: dict[str, Any], output_path: str) -> None:
    """Write a docker-compose.yml file.

    :param compose: Dict from :func:`generate_compose_dict`.
    :param output_path: Destination file path.
    """
    content = compose_dict_to_yaml(compose)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding=SERENA_FILE_ENCODING) as f:
        f.write(content)


def init_docker_config(dest_path: str) -> None:
    """Copy the bundled docker.template.yml to *dest_path*.

    :param dest_path: Target file (usually ``~/.serena/docker.yml``).
    :raises FileExistsError: If *dest_path* already exists.
    """
    if os.path.exists(dest_path):
        raise FileExistsError(f"Config already exists: {dest_path}")
    import shutil

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    shutil.copy2(DOCKER_CONFIG_TEMPLATE_FILE, dest_path)
