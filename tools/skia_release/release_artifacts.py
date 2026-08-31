#! /usr/bin/env python3

import argparse
import sys
from pathlib import Path

import release


BUILD_TYPES = ('Debug', 'Release')
APPLE_TARGETS = ('macos', 'ios', 'iosSim', 'tvos', 'tvosSim')


def expected_release_artifact_names(version):
  coordinates = set()
  for build_type in BUILD_TYPES:
    for target in APPLE_TARGETS:
      for machine in ('x64', 'arm64'):
        if target == 'tvos' and machine == 'x64':
          continue
        coordinates.add((target, build_type, machine))
    coordinates.add(('linux', build_type, 'x64'))
    coordinates.add(('linux', build_type, 'arm64'))
    coordinates.add(('wasm', build_type, 'wasm'))
    for machine in ('x64', 'arm64'):
      coordinates.add(('android', build_type, machine))
      coordinates.add(('windows', build_type, machine))

  return {
      f'Skia-{version}-{target}-{build_type}-{machine}.zip'
      for target, build_type, machine in coordinates
  }


def collect_release_artifacts(artifact_dir, version):
  artifact_dir = Path(artifact_dir)
  if artifact_dir.is_symlink() or not artifact_dir.is_dir():
    raise RuntimeError(f'Skia release artifact path is not a directory: {artifact_dir}')
  entries = list(artifact_dir.iterdir())
  unsafe_entries = [
      entry.name
      for entry in entries
      if entry.is_symlink() or not entry.is_file() or entry.suffix != '.zip'
  ]
  artifacts = sorted(
      entry
      for entry in entries
      if entry.is_file() and not entry.is_symlink() and entry.suffix == '.zip'
  )
  actual_names = {artifact.name for artifact in artifacts}
  expected_names = expected_release_artifact_names(version)
  missing = sorted(expected_names - actual_names)
  unexpected = sorted(actual_names - expected_names)
  issues = [f'missing {name}' for name in missing]
  issues.extend(f'unexpected {name}' for name in unexpected)
  issues.extend(f'unexpected repository entry {name}' for name in sorted(unsafe_entries))
  issues.extend(f'empty {artifact.name}' for artifact in artifacts if artifact.stat().st_size == 0)
  if not issues:
    for artifact in artifacts:
      try:
        release.validate_zip_artifact(artifact)
      except RuntimeError as error:
        issues.append(str(error))
  if issues:
    raise RuntimeError(
        f'Skia release artifact matrix is incomplete in {artifact_dir}:\n  '
        + '\n  '.join(issues)
    )
  return artifacts


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('--artifact-dir', required=True)
  parser.add_argument('--version', required=True)
  args = parser.parse_args()

  release.require_public_release_approval()
  artifacts = collect_release_artifacts(args.artifact_dir, args.version)
  release.stage_and_publish_release_artifacts(artifacts, args.version)
  return 0


if __name__ == '__main__':
  sys.exit(main())
