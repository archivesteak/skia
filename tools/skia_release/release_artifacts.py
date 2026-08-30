#! /usr/bin/env python3

import argparse
import re
import sys
from pathlib import Path

import release


TARGETS = ('macos', 'ios', 'iosSim', 'tvos', 'tvosSim', 'linux', 'wasm', 'android', 'windows')
BUILD_TYPES = ('Debug', 'Release')
MACHINES = ('x64', 'arm64', 'wasm')


def collect_release_artifacts(artifact_dir, version):
  artifact_dir = Path(artifact_dir)
  pattern = re.compile(
      r'^Skia-'
      + re.escape(version)
      + r'-('
      + '|'.join(TARGETS)
      + r')-('
      + '|'.join(BUILD_TYPES)
      + r')-('
      + '|'.join(MACHINES)
      + r')\.zip$'
  )
  artifacts = sorted(artifact_dir.glob('*.zip'))
  if not artifacts:
    raise RuntimeError(f'No Skia Actions artifacts found in {artifact_dir}.')
  unexpected = [artifact.name for artifact in artifacts if not pattern.fullmatch(artifact.name)]
  if unexpected:
    raise RuntimeError('Unexpected Skia artifact names:\n  ' + '\n  '.join(unexpected))
  return artifacts


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('--artifact-dir', required=True)
  parser.add_argument('--version', required=True)
  args = parser.parse_args()

  release.require_public_release_approval()
  artifacts = collect_release_artifacts(args.artifact_dir, args.version)
  release.upload_release_artifacts(artifacts, args.version)
  return 0


if __name__ == '__main__':
  sys.exit(main())
