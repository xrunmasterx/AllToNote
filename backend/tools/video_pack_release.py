from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Callable, Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.adapters.video_packs.official_video_pack import (
    MEDIA_BASIC,
    TRANSCRIBE_CPU,
    OfficialVideoPackContract,
    official_video_pack,
)
from app.adapters.video_packs.official_video_pack_trust import (
    official_video_pack_trust_keys,
)
from app.adapters.video_packs.official_video_pack_verifier import (
    PackTrustKey,
    VerifiedOfficialVideoPack,
    canonical_manifest_bytes,
    verify_official_video_pack_source,
)
from tools.document_basic_pack_release import (
    ReleaseError,
    _cleanup_staging,
    _copy_file_snapshot,
    _copy_tree_snapshot,
    _output_path,
    _output_staging,
    _paths_overlap,
    _probe_python_identity,
    _public_bytes,
    _publish_staging,
    _source_root,
    _validate_key_id,
    load_signing_key,
)


_PLATFORM = "windows-x86_64"
_PYTHON_VERSION = "3.11.15"
_PUBLISHER = "alltonote-official"
_KEY_ID = "alltonote-video-packs-2026-01"
_EXPECTED_DEPENDENCIES = {
    MEDIA_BASIC.pack_id: {
        "requests": "2.32.3",
        "yt-dlp": "2026.7.4",
    },
    TRANSCRIBE_CPU.pack_id: {
        "av": "14.2.0",
        "ctranslate2": "4.6.0",
        "faster-whisper": "1.1.1",
        "setuptools": "80.10.2",
        "tokenizers": "0.21.1",
    },
}

PackProbe = Callable[
    [OfficialVideoPackContract, Path, Mapping[str, Path]],
    None,
]
PythonIdentityProbe = Callable[[Path], Mapping[str, object]]


def _normalize_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _package_inventory(
    python_root: Path,
    *,
    required: Mapping[str, str],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    site_packages = python_root / "Lib" / "site-packages"
    if not site_packages.is_dir():
        raise ReleaseError("prepared Python is missing Lib/site-packages")
    inventory: list[dict[str, object]] = []
    components: list[dict[str, object]] = []
    versions: dict[str, str] = {}
    for metadata_path in sorted(
        site_packages.glob("*.dist-info/METADATA"),
        key=lambda path: path.as_posix().casefold(),
    ):
        message = BytesParser(policy=policy.default).parsebytes(
            metadata_path.read_bytes()
        )
        raw_name = message.get("Name")
        version = message.get("Version")
        if not isinstance(raw_name, str) or not isinstance(version, str):
            raise ReleaseError("prepared Python contains invalid distribution metadata")
        name = _normalize_distribution_name(str(raw_name))
        version = str(version)
        if name in versions:
            raise ReleaseError(f"prepared Python contains duplicate distribution {name}")
        versions[name] = version
        dist_info = metadata_path.parent
        license_files = sorted(
            {
                "python/" + path.relative_to(python_root).as_posix()
                for path in dist_info.rglob("*")
                if path.is_file()
                and path.name.casefold().startswith(
                    ("license", "licence", "copying", "notice", "authors")
                )
            },
            key=str.casefold,
        )
        expression = str(
            message.get("License-Expression")
            or message.get("License")
            or "NOASSERTION"
        )
        inventory.append(
            {
                "name": name,
                "version": version,
                "license_expression": expression,
                "license_files": license_files,
            }
        )
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "licenses": [{"expression": expression}],
                "properties": [
                    {
                        "name": "alltonote:license-files",
                        "value": json.dumps(
                            license_files,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    }
                ],
            }
        )
    if not inventory:
        raise ReleaseError("prepared Python has no distribution metadata")
    for name, expected in required.items():
        if versions.get(name) != expected:
            raise ReleaseError(
                f"prepared Python distribution {name} must be {expected}"
            )
    return inventory, components


def _file_identity(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return {
        "path": path.name,
        "byte_length": size,
        "sha256": "sha256:" + digest.hexdigest(),
    }


def _write_controls(
    root: Path,
    *,
    contract: OfficialVideoPackContract,
    python_identity: Mapping[str, object],
    inventory: list[dict[str, object]],
    sbom_components: list[dict[str, object]],
) -> None:
    controls: dict[str, object] = {
        "schema_version": 1,
        "pack_id": contract.pack_id,
        "pack_version": contract.pack_version,
        "platform": _PLATFORM,
        "python": {
            **dict(python_identity),
            "required_distributions": _EXPECTED_DEPENDENCIES[contract.pack_id],
        },
    }
    if contract is MEDIA_BASIC:
        controls["tools"] = [
            _file_identity(root / "bin" / "ffmpeg.exe"),
            _file_identity(root / "bin" / "ffprobe.exe"),
        ]
    else:
        controls["model"] = {
            "name": "small",
            "revision": "536b0662742c02347bc0e980a01041f333bce120",
            "files": [
                _file_identity(root / "models" / "small" / name)
                for name in (
                "config.json",
                "model.bin",
                "tokenizer.json",
                "vocabulary.txt",
                )
            ],
        }
    license_index = {
        "schema_version": 1,
        "python_license": "python/LICENSE.txt",
        "distributions": inventory,
        "ffmpeg_license": (
            "FFmpeg is distributed under GPL-3.0-or-later in this Pack build"
            if contract is MEDIA_BASIC
            else None
        ),
    }
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": contract.pack_id,
                "version": contract.pack_version,
            }
        },
        "components": sbom_components,
    }
    lock_name = contract.required_payload_files[0]
    (root / lock_name).write_bytes(_canonical_bytes(controls))
    (root / "licenses").mkdir()
    (root / "licenses" / f"{contract.pack_id}.txt").write_bytes(
        _canonical_bytes(license_index)
    )
    (root / "sbom.cdx.json").write_bytes(_canonical_bytes(sbom))


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _default_probe(
    contract: OfficialVideoPackContract,
    root: Path,
    entrypoints: Mapping[str, Path],
) -> None:
    del root
    from app.cli.pack_commands import _DefaultPackService

    _DefaultPackService._probe_video_entrypoints(
        contract.pack_id,
        entrypoints,
    )


def assemble_video_pack(
    *,
    contract: OfficialVideoPackContract,
    python_root: Path,
    output: Path,
    ffmpeg: Path | None = None,
    ffprobe: Path | None = None,
    model_root: Path | None = None,
    probe: PackProbe = _default_probe,
    python_identity_probe: PythonIdentityProbe = _probe_python_identity,
) -> Path:
    prepared_python = _source_root(python_root)
    output_path, parent_metadata = _output_path(output)
    inputs = [prepared_python]
    if contract is MEDIA_BASIC:
        if ffmpeg is None or ffprobe is None or model_root is not None:
            raise ReleaseError("media-basic requires FFmpeg and ffprobe")
        prepared_ffmpeg = Path(ffmpeg).resolve(strict=True)
        prepared_ffprobe = Path(ffprobe).resolve(strict=True)
        inputs.extend((prepared_ffmpeg, prepared_ffprobe))
    elif contract is TRANSCRIBE_CPU:
        if model_root is None or ffmpeg is not None or ffprobe is not None:
            raise ReleaseError("transcribe-cpu requires the frozen model")
        prepared_model = _source_root(model_root)
        inputs.append(prepared_model)
    else:
        raise ReleaseError("unsupported Video Pack contract")
    if any(_paths_overlap(path, output_path) for path in inputs):
        raise ReleaseError("Pack inputs and output must be disjoint")

    staging = _output_staging(output_path, parent_metadata)
    try:
        _copy_tree_snapshot(prepared_python, staging / "python")
        python_executable = staging / "python" / "python.exe"
        identity = dict(python_identity_probe(python_executable))
        if identity != {
            "implementation": "cpython",
            "platform": "win32",
            "machine": "x86_64",
            "pointer_bits": 64,
            "version": _PYTHON_VERSION,
        }:
            raise ReleaseError(
                f"prepared Python must be Windows x86_64 CPython {_PYTHON_VERSION}"
            )
        if contract is MEDIA_BASIC:
            bin_root = staging / "bin"
            bin_root.mkdir()
            _copy_file_snapshot(
                prepared_ffmpeg,
                bin_root / "ffmpeg.exe",
                remaining_bytes=2 * 1024 * 1024 * 1024,
            )
            _copy_file_snapshot(
                prepared_ffprobe,
                bin_root / "ffprobe.exe",
                remaining_bytes=2 * 1024 * 1024 * 1024,
            )
        else:
            (staging / "models").mkdir()
            _copy_tree_snapshot(prepared_model, staging / "models" / "small")
        inventory, components = _package_inventory(
            staging / "python",
            required=_EXPECTED_DEPENDENCIES[contract.pack_id],
        )
        _write_controls(
            staging,
            contract=contract,
            python_identity=identity,
            inventory=inventory,
            sbom_components=components,
        )
        entrypoints = {
            name: staging.joinpath(*relative.split("/"))
            for name, relative in contract.entrypoints(_PLATFORM).items()
        }
        probe(contract, staging, entrypoints)
        _publish_staging(staging, output_path, parent_metadata)
        return output_path
    except BaseException:
        _cleanup_staging(staging, output_path)
        raise


def _unsigned_manifest(
    contract: OfficialVideoPackContract,
    files: list[dict[str, object]],
) -> dict[str, object]:
    sbom = next(item for item in files if item["path"] == "sbom.cdx.json")
    return {
        "manifest_version": 1,
        "pack_id": contract.pack_id,
        "version": contract.pack_version,
        "platform": _PLATFORM,
        "runtime_api": {"min": 1, "max": 1},
        "recipe_contracts": {
            recipe_id: list(versions)
            for recipe_id, versions in contract.recipe_contracts.items()
        },
        "capabilities": list(contract.capabilities),
        "entrypoints": [
            {
                "name": name,
                "type": "process",
                "relative_path": relative,
            }
            for name, relative in contract.entrypoints(_PLATFORM).items()
        ],
        "files": files,
        "licenses": [
            {
                "component": contract.pack_id,
                "spdx": "NOASSERTION",
                "file": f"licenses/{contract.pack_id}.txt",
            }
        ],
        "sbom": {
            "format": "cyclonedx-json",
            "file": "sbom.cdx.json",
            "sha256": sbom["sha256"],
        },
        "publisher": _PUBLISHER,
    }


def sign_video_pack(
    *,
    contract: OfficialVideoPackContract,
    assembled_root: Path,
    output: Path,
    key_id: str,
    private_key: Ed25519PrivateKey,
    trusted_keys: Mapping[str, PackTrustKey],
) -> VerifiedOfficialVideoPack:
    _validate_key_id(key_id)
    trust = trusted_keys.get(key_id)
    public_key = _public_bytes(private_key)
    if (
        trust is None
        or trust.publisher != _PUBLISHER
        or trust.public_key != public_key
    ):
        raise ReleaseError("signing key does not match the embedded trust root")
    assembled = _source_root(assembled_root)
    output_path, parent_metadata = _output_path(output)
    if _paths_overlap(assembled, output_path):
        raise ReleaseError("assembled Pack and signed output must be disjoint")
    staging = _output_staging(output_path, parent_metadata)
    try:
        files = _copy_tree_snapshot(assembled, staging)
        manifest = _unsigned_manifest(contract, files)
        manifest["signature"] = {
            "algorithm": "ed25519",
            "key_id": key_id,
            "value": base64.b64encode(
                private_key.sign(canonical_manifest_bytes(manifest))
            ).decode("ascii"),
        }
        (staging / "manifest.json").write_bytes(
            canonical_manifest_bytes(manifest)
        )
        verified = verify_official_video_pack_source(
            staging,
            contract=contract,
            trusted_keys=trusted_keys,
            platform_tag=_PLATFORM,
        )
        _publish_staging(staging, output_path, parent_metadata)
        return verified
    except BaseException:
        _cleanup_staging(staging, output_path)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Release official Video Packs")
    commands = parser.add_subparsers(dest="command", required=True)
    assemble = commands.add_parser("assemble")
    assemble.add_argument("pack_id", choices=("media-basic", "transcribe-cpu"))
    assemble.add_argument("--python-root", type=Path, required=True)
    assemble.add_argument("--ffmpeg", type=Path)
    assemble.add_argument("--ffprobe", type=Path)
    assemble.add_argument("--model-root", type=Path)
    assemble.add_argument("--output", type=Path, required=True)
    sign = commands.add_parser("sign")
    sign.add_argument("pack_id", choices=("media-basic", "transcribe-cpu"))
    sign.add_argument("--assembled-root", type=Path, required=True)
    sign.add_argument("--output", type=Path, required=True)
    sign.add_argument("--key-id", default=_KEY_ID)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    contract = official_video_pack(args.pack_id)
    try:
        if args.command == "assemble":
            output = assemble_video_pack(
                contract=contract,
                python_root=args.python_root,
                ffmpeg=args.ffmpeg,
                ffprobe=args.ffprobe,
                model_root=args.model_root,
                output=args.output,
            )
            result: dict[str, object] = {
                "pack_id": contract.pack_id,
                "pack_version": contract.pack_version,
                "assembled_root": str(output),
            }
        else:
            verified = sign_video_pack(
                contract=contract,
                assembled_root=args.assembled_root,
                output=args.output,
                key_id=args.key_id,
                private_key=load_signing_key(args.key_id),
                trusted_keys=official_video_pack_trust_keys(),
            )
            result = {
                "pack_id": verified.pack_id,
                "pack_version": verified.pack_version,
                "platform": verified.platform,
                "publisher": verified.publisher,
                "signature_key_id": verified.signature_key_id,
                "manifest_sha256": verified.manifest_sha256,
                "output": str(args.output.resolve()),
            }
    except (OSError, ReleaseError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    print(json.dumps({"ok": True, "data": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["assemble_video_pack", "main", "sign_video_pack"]
