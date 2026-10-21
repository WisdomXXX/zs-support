#!/usr/bin/env python3
"""Verify deb payload/hooks/completion in temporary directories; no system install."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, check=True, **kwargs).stdout


def main():
    version = (ROOT / "VERSION").read_text().strip()
    artifact = ROOT / "dist" / f"zephyr-zs-support_{version}_all.deb"
    assert run(["dpkg-deb", "-f", str(artifact), "Package"]).strip() == "zephyr-zs-support"
    assert run(["dpkg-deb", "-f", str(artifact), "Version"]).strip() == version
    with tempfile.TemporaryDirectory(prefix="zs-support-package-") as temp:
        root = Path(temp)
        payload, metadata = root / "payload", root / "metadata"
        run(["dpkg-deb", "-x", str(artifact), str(payload)])
        run(["dpkg-deb", "-e", str(artifact), str(metadata)])
        binary = payload / "usr/bin/zs-support"
        assert not (payload / "usr/bin/zs").exists()
        env = {**os.environ, "PYTHONPATH": str(payload / "usr/lib/python3/dist-packages"),
               "PATH": str(binary.parent) + os.pathsep + os.environ["PATH"],
               "XDG_CONFIG_HOME": str(root / "config"), "XDG_STATE_HOME": str(root / "state")}
        assert run([str(binary), "--version"], cwd=root, env=env).strip() == "zs-support " + version
        assert json.loads(run([str(binary), "doctor", "--json"], cwd=root, env=env))["ok"]
        assert "localization" in run([str(binary), "__complete", "scenes", "--product", "zsm1"], cwd=root, env=env)
        # Package hooks: install/upgrade with zt absent, present and failing.
        absent = {**env, "PATH": "/nonexistent"}
        run(["/bin/sh", str(metadata / "postinst"), "configure"], env=absent)
        run(["/bin/sh", str(metadata / "prerm"), "remove"], env=absent)
        stub = root / "stub"
        stub.mkdir()
        (stub / "zt").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$ZT_TEST_LOG"\nexit "${ZT_TEST_EXIT:-0}"\n')
        (stub / "zt").chmod(0o755)
        hooks = {**env, "PATH": str(stub), "ZT_TEST_LOG": str(root / "zt.log")}
        run(["/bin/sh", str(metadata / "postinst"), "configure"], env=hooks)
        run(["/bin/sh", str(metadata / "postinst"), "configure", "0.0.9"], env=hooks)
        run(["/bin/sh", str(metadata / "prerm"), "upgrade"], env=hooks)
        run(["/bin/sh", str(metadata / "prerm"), "remove"], env=hooks)
        log = (root / "zt.log").read_text()
        assert log.count("register --name zs-support") == 2
        assert log.count("unregister zs-support") == 1
        assert "--version " + version in log and "--category robotics" in log
        hooks["ZT_TEST_EXIT"] = "1"
        run(["/bin/sh", str(metadata / "postinst"), "configure"], env=hooks)
        run(["/bin/sh", str(metadata / "prerm"), "remove"], env=hooks)
        run(["/bin/sh", str(metadata / "postrm"), "purge"], env=hooks)
        # Exercise Bash's fresh-shell lazy loader using an isolated completion root.
        completion_root = root / "bash-completion"
        (completion_root / "completions").mkdir(parents=True)
        shutil.copyfile(payload / "usr/share/bash-completion/completions/zs-support", completion_root / "completions/zs-support")
        env["BASH_COMPLETION_USER_DIR"] = str(completion_root)
        bash_test = r'''
source /usr/share/bash-completion/bash_completion
_completion_loader zs-support
[[ $? == 124 ]] && declare -F _zs_support >/dev/null || exit 1
COMP_WORDS=(zs-support ''); COMP_CWORD=1; _zs_support
[[ " ${COMPREPLY[*]} " == *' connect '* ]] || exit 2
[[ " ${COMPREPLY[*]} " != *' rec '* ]] || exit 8
[[ " ${COMPREPLY[*]} " != *' collect '* ]] || exit 9
COMP_WORDS=(zs-support rec --); COMP_CWORD=2; _zs_support
[[ " ${COMPREPLY[*]} " == *' --scene '* ]] || exit 3
COMP_WORDS=(zs-support rec --product zs); COMP_CWORD=3; _zs_support
[[ " ${COMPREPLY[*]} " == *' zsm1 '* ]] || exit 4
COMP_WORDS=(zs-support rec --product zsm1 --scene loc); COMP_CWORD=5; _zs_support
[[ " ${COMPREPLY[*]} " == *' localization '* ]] || exit 5
COMP_WORDS=(zs-support bag --); COMP_CWORD=2; _zs_support
[[ " ${COMPREPLY[*]} " == *' --duration '* ]] || exit 6
COMP_WORDS=(zs-support pull ''); COMP_CWORD=2; _zs_support
[[ " ${COMPREPLY[*]} " == *' log '* ]] || exit 7
'''
        run(["bash", "--noprofile", "--norc", "-ic", bash_test], cwd=root, env=env)
        if shutil.which("zsh"):
            env["TEST_ZSH_COMPLETIONS"] = str(payload / "usr/share/zsh/vendor-completions")
            env["TEST_ZSH_DUMP"] = str(root / "zcompdump")
            run(["zsh", "-f", "-c", r'''
fpath=("$TEST_ZSH_COMPLETIONS" $fpath)
autoload -Uz compinit
compinit -d "$TEST_ZSH_DUMP"
[[ ${_comps[zs-support]} == _zs-support ]] || exit 1
source <(zs-support completion zsh)
[[ ${_comps[zs-support]} == _zs_support ]] || exit 2
'''], cwd=root, env=env)
        print("PASS: deb metadata, isolated arbitrary-directory CLI, install/upgrade/remove hooks with/without zt, Bash completion values and Zsh registration")
        print("NOTE: This is payload/hook verification, not a clean Ubuntu apt installation or hardware acceptance.")


if __name__ == "__main__":
    main()
