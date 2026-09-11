#!/usr/bin/env python3
import importlib.util
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("export_source", ROOT / "scripts/export_source.py")
export_source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export_source)


def test_rejects_credential_assignment():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "README.md").write_text("API_TOKEN=" + "private" + "value12345\n")
        old_root, old_files, old_trees = export_source.ROOT, export_source.FILES, export_source.TREES
        export_source.ROOT, export_source.FILES, export_source.TREES = root, ["README.md"], []
        try:
            try:
                export_source.export(root / "release.tar.gz")
            except ValueError as exc:
                assert "Credential assignment" in str(exc)
            else:
                raise AssertionError("credential assignment was exported")
        finally:
            export_source.ROOT, export_source.FILES, export_source.TREES = old_root, old_files, old_trees


def test_excludes_macos_metadata():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        tree = root / "plugins"
        tree.mkdir()
        (tree / "manifest.yaml").write_text("name: fixture\n")
        (tree / ".DS_Store").write_bytes(b"metadata")
        old_root, old_files, old_trees = export_source.ROOT, export_source.FILES, export_source.TREES
        export_source.ROOT, export_source.FILES, export_source.TREES = root, [], ["plugins"]
        try:
            destination = root / "release.tar.gz"
            export_source.export(destination)
            with tarfile.open(destination) as archive:
                assert "inference-dock/plugins/.DS_Store" not in archive.getnames()
        finally:
            export_source.ROOT, export_source.FILES, export_source.TREES = old_root, old_files, old_trees


if __name__ == "__main__":
    test_rejects_credential_assignment()
    test_excludes_macos_metadata()
    print("ok")
