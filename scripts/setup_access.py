#!/usr/bin/env python3
"""Evaluator-friendly wrapper to run the audited installer in extras/scripts.

This script locates extras/scripts/install_simtemp_access.py and attempts to run
it via a graphical privilege escalation helper (pkexec) if available. If pkexec
is not present it prints a one-line sudo command the evaluator can run.

Usage:
    ./scripts/setup_access.py        # attempts pkexec, otherwise prints sudo fallback
    python3 scripts/setup_access.py  # same
"""
from __future__ import print_function
import os
import shutil
import subprocess
import sys


def main():
    here = os.path.abspath(os.path.dirname(__file__))
    repo_root = os.path.abspath(os.path.join(here, '..'))
    installer = os.path.abspath(os.path.join(repo_root, 'extras', 'scripts', 'install_simtemp_access.py'))
    if not os.path.exists(installer):
        print(f"Installer not found: {installer}")
        sys.exit(2)

    python = sys.executable or 'python3'
    pkexec = shutil.which('pkexec')
    if pkexec:
        cmd = [pkexec, python, installer]
        print('Running installer via pkexec...')
        try:
            rc = subprocess.call(cmd)
            if rc == 0:
                print('Installer completed successfully.')
            else:
                print(f'Installer exited with code {rc}')
            sys.exit(rc)
        except Exception as e:
            print('Failed to invoke pkexec:', e)
            print('Falling back to sudo instruction below.')

    # fallback: print a sudo command for the evaluator to copy-paste
    print('\npkexec not available on this system.')
    print('Run the following command in a terminal to apply the access changes:')
    print(f"\n    sudo {python} {installer}\n")
    print('Tip: you may need to re-login for new group membership to take effect.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
