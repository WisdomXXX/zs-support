#!/usr/bin/env python3
"""Build a pure-Python all-architecture deb without modifying the host system."""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    version = (ROOT / "VERSION").read_text().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit("VERSION must be MAJOR.MINOR.PATCH")
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="zs-support-build-") as temp:
        stage = Path(temp)
        package = stage / "usr/lib/python3/dist-packages/zs_support"
        shutil.copytree(ROOT / "src/zs_support", package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (package / "resources/VERSION").write_text(version + "\n")
        binary = stage / "usr/bin/zs-support"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/usr/bin/python3\nfrom zs_support.cli import main\nraise SystemExit(main())\n")
        binary.chmod(0o755)
        for name, target in [("bash", "usr/share/bash-completion/completions/zs-support"),
                             ("zsh", "usr/share/zsh/vendor-completions/_zs-support")]:
            destination = stage / target
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(package / "resources/completions" / name, destination)
        metadata = stage / "DEBIAN"
        metadata.mkdir()
        for template in (ROOT / "packaging/debian").iterdir():
            destination = metadata / template.name
            destination.write_text(template.read_text().replace("@VERSION@", version))
            destination.chmod(0o644 if template.name == "control" else 0o755)
        doc = stage / "usr/share/doc/zephyr-zs-support"
        doc.mkdir(parents=True)
        for name in ("README.md",):
            if (ROOT / name).exists():
                shutil.copyfile(ROOT / name, doc / name)
        (doc / "docs").mkdir()
        shutil.copyfile(ROOT / "docs/配置说明.md", doc / "docs/配置说明.md")
        artifact = dist / f"zephyr-zs-support_{version}_all.deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(stage), str(artifact)], check=True)
        print(artifact)


if __name__ == "__main__":
    main()
