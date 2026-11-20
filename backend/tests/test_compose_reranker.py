# SPEC-2026-11-20-fase-4-rerank-rewrite
"""The reranker of the Compose (T-04): AC-024 (REQ-013).

The configuration is read with `docker compose config`, never started: the real model is downloaded from Hugging Face.
The values are those of `.env.example`, with a database password (the only required value it leaves empty); the
variables of the caller's environment that the files read are removed, so that only these values count.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_env_example import compose_variables, env_example

ROOT = Path(__file__).resolve().parents[2]
DOCKER = shutil.which("docker")
TEI = "ghcr.io/huggingface/text-embeddings-inference"
DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"
# The services of the stack before phase 4 (phase 1 and the worker of phase 2): unchanged without the profile
BASE_SERVICES = {"db", "redis", "backend", "worker", "nginx"}

pytestmark = pytest.mark.skipif(DOCKER is None, reason="the docker CLI is not installed")


def compose_config(tmp_path: Path, *files: str, profiles: tuple[str, ...] = (), **values: str) -> dict[str, Any]:
    """The configuration of these Compose files, interpolated with .env.example plus `values`."""
    env_values = {**env_example(), "POSTGRES_PASSWORD": "compose-test-password", **values}
    env_file = tmp_path / "compose.env"
    env_file.write_text("".join(f"{name}={value}\n" for name, value in env_values.items()), encoding="utf-8")
    read_by_compose = compose_variables() | set(env_values)
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in read_by_compose and not name.startswith("COMPOSE_")
    }
    command = [str(DOCKER), "compose", "--project-directory", str(ROOT), "--env-file", str(env_file)]
    for file in files:
        command += ["-f", str(ROOT / file)]
    for profile in profiles:
        command += ["--profile", profile]
    result = subprocess.run(
        [*command, "config", "--format", "json"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    config: dict[str, Any] = json.loads(result.stdout)
    return config


def reranker_of(config: dict[str, Any]) -> dict[str, Any]:
    assert "reranker" in config["services"], sorted(config["services"])
    service: dict[str, Any] = config["services"]["reranker"]
    return service


def tei_services(config: dict[str, Any]) -> dict[str, Any]:
    return {name: service for name, service in config["services"].items() if TEI in str(service.get("image", ""))}


def test_AC_024_without_the_profile_the_stack_has_the_services_of_before(tmp_path: Path) -> None:
    for files in (("compose.yaml",), ("compose.yaml", "compose.tls.yaml"), ("compose.yaml", "compose.rerank-gpu.yaml")):
        config = compose_config(tmp_path, *files)
        assert set(config["services"]) == BASE_SERVICES, files
        assert tei_services(config) == {}, files


def test_AC_024_the_profile_rerank_adds_the_reranker_on_cpu(tmp_path: Path) -> None:
    config = compose_config(tmp_path, "compose.yaml", profiles=("rerank",))
    assert set(config["services"]) == BASE_SERVICES | {"reranker"}
    reranker = reranker_of(config)

    assert reranker["profiles"] == ["rerank"]
    assert re.fullmatch(re.escape(TEI) + r":cpu-\d+\.\d+\.\d+", reranker["image"]), reranker["image"]
    assert reranker["environment"]["MODEL_ID"] == DEFAULT_MODEL
    # The models are downloaded at the first start into a named volume, which the image reads at /data
    assert [(v["type"], v["source"], v["target"]) for v in reranker["volumes"]] == [
        ("volume", "reranker-models", "/data")
    ]
    assert "reranker-models" in config["volumes"]
    assert "ports" not in reranker
    assert "/health" in " ".join(reranker["healthcheck"]["test"])
    assert set(tei_services(config)) == {"reranker"}


def test_AC_024_rerank_compose_model_chooses_the_model(tmp_path: Path) -> None:
    config = compose_config(
        tmp_path, "compose.yaml", profiles=("rerank",), RERANK_COMPOSE_MODEL="BAAI/bge-reranker-base"
    )
    assert reranker_of(config)["environment"]["MODEL_ID"] == "BAAI/bge-reranker-base"


def test_AC_024_env_example_names_the_default_model() -> None:
    assert env_example().get("RERANK_COMPOSE_MODEL") == DEFAULT_MODEL


def test_AC_024_the_gpu_file_replaces_the_image_and_reserves_one_nvidia_gpu(tmp_path: Path) -> None:
    cpu = reranker_of(compose_config(tmp_path, "compose.yaml", profiles=("rerank",)))
    config = compose_config(tmp_path, "compose.yaml", "compose.rerank-gpu.yaml", profiles=("rerank",))
    assert set(config["services"]) == BASE_SERVICES | {"reranker"}
    assert set(tei_services(config)) == {"reranker"}
    reranker = config["services"]["reranker"]

    cpu_version = cpu["image"].rpartition(":cpu-")[2]
    assert reranker["image"] == f"{TEI}:{cpu_version}"
    assert reranker["deploy"]["resources"]["reservations"]["devices"] == [
        {"driver": "nvidia", "count": 1, "capabilities": ["gpu"]}
    ]
    # The rest of the service is the one of compose.yaml
    assert reranker["profiles"] == ["rerank"]
    assert reranker["environment"]["MODEL_ID"] == DEFAULT_MODEL
    assert reranker["volumes"] == cpu["volumes"]
    assert "ports" not in reranker
    assert "deploy" not in cpu


def test_AC_024_adr_004_records_the_two_images(tmp_path: Path) -> None:
    cpu = reranker_of(compose_config(tmp_path, "compose.yaml", profiles=("rerank",)))["image"]
    gpu = reranker_of(compose_config(tmp_path, "compose.yaml", "compose.rerank-gpu.yaml", profiles=("rerank",)))[
        "image"
    ]
    adr = (ROOT / "docs" / "adr" / "ADR-004-rerank-rewrite.md").read_text(encoding="utf-8")
    assert f"`{cpu}`" in adr
    assert f"`{gpu}`" in adr


# Owner's decisions on the review of PR #29 (second Test-Lock of T-04)
GPU_KEYS = ("runtime", "gpus", "device_requests", "devices")


def compose_config_with(
    tmp_path: Path, env_values: dict[str, str], *files: str, profiles: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Like `compose_config`, with exactly these values in the env file (a name left out is unset)."""
    env_file = tmp_path / "compose-exact.env"
    env_file.write_text("".join(f"{name}={value}\n" for name, value in env_values.items()), encoding="utf-8")
    read_by_compose = compose_variables() | set(env_example()) | set(env_values)
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in read_by_compose and not name.startswith("COMPOSE_")
    }
    command = [str(DOCKER), "compose", "--project-directory", str(ROOT), "--env-file", str(env_file)]
    for file in files:
        command += ["-f", str(ROOT / file)]
    for profile in profiles:
        command += ["--profile", profile]
    result = subprocess.run(
        [*command, "config", "--format", "json"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    config: dict[str, Any] = json.loads(result.stdout)
    return config


def env_without(*names: str) -> dict[str, str]:
    """The values of .env.example without these names (an older .env), with a database password."""
    values = {**env_example(), "POSTGRES_PASSWORD": "compose-test-password"}
    return {name: value for name, value in values.items() if name not in names}


def test_AC_024_the_reranker_accepts_as_many_texts_as_rerank_candidates(tmp_path: Path) -> None:
    def batch_size(config: dict[str, Any]) -> object:
        return reranker_of(config)["environment"].get("MAX_CLIENT_BATCH_SIZE")

    rerank = ("rerank",)
    gpu = ("compose.yaml", "compose.rerank-gpu.yaml")
    # .env.example has RERANK_CANDIDATES=20; empty or missing is the default of the backend, 20
    assert batch_size(compose_config(tmp_path, "compose.yaml", profiles=rerank)) == "20"
    assert batch_size(compose_config(tmp_path, "compose.yaml", profiles=rerank, RERANK_CANDIDATES="")) == "20"
    assert (
        batch_size(compose_config_with(tmp_path, env_without("RERANK_CANDIDATES"), "compose.yaml", profiles=rerank))
        == "20"
    )
    # Up to RETRIEVAL_CANDIDATES (50), above the 32 texts that TEI accepts by default
    assert batch_size(compose_config(tmp_path, "compose.yaml", profiles=rerank, RERANK_CANDIDATES="50")) == "50"
    assert batch_size(compose_config(tmp_path, *gpu, profiles=rerank, RERANK_CANDIDATES="50")) == "50"
    assert batch_size(compose_config(tmp_path, *gpu, profiles=rerank)) == "20"


def test_AC_024_on_cpu_no_service_uses_a_gpu_image_or_device(tmp_path: Path) -> None:
    gpu_image = reranker_of(compose_config(tmp_path, "compose.yaml", "compose.rerank-gpu.yaml", profiles=("rerank",)))[
        "image"
    ]
    config = compose_config(tmp_path, "compose.yaml", profiles=("rerank",))
    images = {name: str(service.get("image", "")) for name, service in config["services"].items()}

    assert gpu_image not in images.values()
    tei_images = {image for image in images.values() if image.startswith(f"{TEI}:")}
    assert len(tei_images) == 1
    assert next(iter(tei_images)).startswith(f"{TEI}:cpu-")
    for name, service in config["services"].items():
        assert [key for key in GPU_KEYS if key in service] == [], name
        assert service.get("deploy", {}).get("resources", {}).get("reservations", {}).get("devices") is None, name
        assert [key for key in service.get("environment") or {} if key.startswith("NVIDIA_")] == [], name


def test_AC_024_without_rerank_compose_model_the_default_model(tmp_path: Path) -> None:
    for config in (
        compose_config(tmp_path, "compose.yaml", profiles=("rerank",), RERANK_COMPOSE_MODEL=""),
        compose_config_with(tmp_path, env_without("RERANK_COMPOSE_MODEL"), "compose.yaml", profiles=("rerank",)),
    ):
        assert reranker_of(config)["environment"]["MODEL_ID"] == DEFAULT_MODEL
