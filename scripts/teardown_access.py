#!/usr/bin/env python3
"""Evaluator-friendly wrapper to run the audited uninstaller in extras/scripts.

Usage:
    ./scripts/teardown_access.py
    python3 scripts/teardown_access.py
"""
from __future__ import print_function
import os
import shutil
import subprocess
import sys


def main():
    here = os.path.abspath(os.path.dirname(__file__))
    repo_root = os.path.abspath(os.path.join(here, '..'))
    uninstaller = os.path.abspath(os.path.join(repo_root, 'extras', 'scripts', 'uninstall_simtemp_access.py'))
    if not os.path.exists(uninstaller):
        print(f"Uninstaller not found: {uninstaller}")
        sys.exit(2)

    python = sys.executable or 'python3'
    pkexec = shutil.which('pkexec')
    if pkexec:
        cmd = [pkexec, python, uninstaller]
        print('Running uninstaller via pkexec...')
        try:
            rc = subprocess.call(cmd)
            if rc == 0:
                print('Uninstaller completed successfully.')
            else:
                print(f'Uninstaller exited with code {rc}')
            sys.exit(rc)
        except Exception as e:
            print('Failed to invoke pkexec:', e)
            print('Falling back to sudo instruction below.')

    print('\npkexec not available on this system.')
    print('Run the following command in a terminal to restore recorded state:')
    print(f"\n    sudo {python} {uninstaller}\n")
    return 0


if __name__ == '__main__':
    sys.exit(main())
