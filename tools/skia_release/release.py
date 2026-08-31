#! /usr/bin/env python3

import hashlib
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

import common


PUBLIC_RELEASE_APPROVAL = 'PUBLISH_PUBLIC_SKIA_RELEASE'
API_TIMEOUT_SECONDS = 30
UPLOAD_TIMEOUT_SECONDS = 300
FULL_COMMIT_PATTERN = re.compile(r'[0-9a-f]{40}\Z')
RELEASE_VERSION_PATTERN = re.compile(r'm[1-9][0-9]*-[0-9a-f]{10}\Z')


def require_public_release_approval():
  if os.environ.get('SKIA_PUBLIC_RELEASE_APPROVAL') != PUBLIC_RELEASE_APPROVAL:
    raise RuntimeError(
        'Durable public Skia release is frozen. Set SKIA_PUBLIC_RELEASE_APPROVAL '
        + 'to the exact approval phrase only after explicit release approval.'
    )


def _repo_api_url():
  return 'https://api.github.com/repos/' + common.github_repo()


def _request_json(url, headers, method='GET', payload=None):
  request_headers = dict(headers)
  data = None
  if payload is not None:
    data = json.dumps(payload).encode('utf-8')
    request_headers['Content-Type'] = 'application/json'
  with urllib.request.urlopen(
      urllib.request.Request(url, data=data, headers=request_headers, method=method),
      timeout=API_TIMEOUT_SECONDS,
  ) as response:
    body = response.read()
  return json.loads(body.decode('utf-8')) if body else None


def _find_release(version, headers):
  releases_url = _repo_api_url() + '/releases'
  tag_url = releases_url + '/tags/' + urllib.parse.quote(version, safe='')

  try:
    return _request_json(tag_url, headers)
  except urllib.error.HTTPError as error:
    if error.code != 404:
      raise

  # Draft releases are not guaranteed to resolve through the tag endpoint.
  # Search the authenticated release list so an interrupted draft is resumed
  # rather than replaced by another release.
  releases = _request_json(releases_url + '?per_page=100', headers)
  return next((record for record in releases if record.get('tag_name') == version), None)


def _create_draft_release(version, headers):
  return _request_json(
      _repo_api_url() + '/releases',
      headers,
      method='POST',
      payload={
        'tag_name': version,
        'name': version,
        'target_commitish': common.current_revision(),
        'draft': True,
      },
  )


def _get_or_create_draft_release(version, headers):
  release_record = _find_release(version, headers)
  if release_record is not None:
    return release_record
  try:
    return _create_draft_release(version, headers)
  except urllib.error.HTTPError as error:
    # A concurrent retry may have created the draft after our lookup.
    if error.code != 422:
      raise
    release_record = _find_release(version, headers)
    if release_record is None:
      raise
    return release_record


def _list_release_assets(release_id, headers):
  return _request_json(
      _repo_api_url() + f'/releases/{release_id}/assets?per_page=100',
      headers,
  )


def _delete_release_asset(asset_id, headers):
  _request_json(
      _repo_api_url() + f'/releases/assets/{asset_id}',
      headers,
      method='DELETE',
  )


def _upload_release_asset(upload_url, artifact, headers):
  print('Staging', artifact.name, 'in draft release')
  upload_headers = dict(headers)
  upload_headers['Content-Type'] = 'application/zip'
  upload_headers['Content-Length'] = str(artifact.stat().st_size)
  query = urllib.parse.urlencode({'name': artifact.name})
  with artifact.open('rb') as data:
    with urllib.request.urlopen(
        urllib.request.Request(upload_url + '?' + query, data=data, headers=upload_headers),
        timeout=UPLOAD_TIMEOUT_SECONDS,
    ) as response:
      body = response.read()
  return json.loads(body.decode('utf-8')) if body else None


def _publish_draft_release(release_id, headers):
  return _request_json(
      _repo_api_url() + f'/releases/{release_id}',
      headers,
      method='PATCH',
      payload={'draft': False},
  )


def _sha256(path):
  digest = hashlib.sha256()
  with path.open('rb') as stream:
    while chunk := stream.read(1024 * 1024):
      digest.update(chunk)
  return 'sha256:' + digest.hexdigest()


def validate_zip_artifact(artifact):
  artifact = Path(artifact)
  if artifact.suffix != '.zip':
    raise RuntimeError(f'Skia release artifact is not a ZIP: {artifact}')
  before = artifact.stat()
  try:
    with zipfile.ZipFile(artifact) as archive:
      entries = archive.infolist()
      if not entries:
        raise RuntimeError(f'Skia release ZIP is empty: {artifact}')
      names = [entry.filename for entry in entries]
      if len(names) != len(set(names)):
        raise RuntimeError(f'Skia release ZIP repeats an entry name: {artifact}')
      for entry in entries:
        name = entry.filename
        path = PurePosixPath(name)
        normalized_name = name[:-1] if name.endswith('/') else name
        if (
            not name
            or '\\' in name
            or path.is_absolute()
            or normalized_name != path.as_posix()
            or any(part in ('', '.', '..') or ':' in part for part in path.parts)
        ):
          raise RuntimeError(f'Skia release ZIP has an unsafe entry {name!r}: {artifact}')
        mode = (entry.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
          raise RuntimeError(f'Skia release ZIP contains a symlink {name!r}: {artifact}')
        if entry.flag_bits & 0x1:
          raise RuntimeError(f'Skia release ZIP contains encrypted data {name!r}: {artifact}')
      corrupt = archive.testzip()
      if corrupt is not None:
        raise RuntimeError(
            f'Skia release ZIP failed CRC verification at {corrupt!r}: {artifact}'
        )
  except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
    if isinstance(error, RuntimeError) and str(error).startswith('Skia release ZIP'):
      raise
    raise RuntimeError(f'Invalid Skia release ZIP {artifact}: {error}') from error
  digest = _sha256(artifact)
  after = artifact.stat()
  if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
    raise RuntimeError(f'Skia release ZIP changed while it was validated: {artifact}')
  return {'path': artifact, 'size': after.st_size, 'digest': digest}


def _index_remote_assets(remote_assets):
  issues = []
  remote_by_name = {}
  for index, asset in enumerate(remote_assets):
    if not isinstance(asset, dict) or not isinstance(asset.get('name'), str):
      issues.append(f'remote release asset {index} has no valid name')
      continue
    name = asset['name']
    if name in remote_by_name:
      issues.append(f'remote release repeats asset name {name}')
      continue
    remote_by_name[name] = asset
  if issues:
    raise RuntimeError('Invalid GitHub release asset response:\n  ' + '\n  '.join(issues))
  return remote_by_name


def _validate_remote_assets(artifacts_by_name, remote_assets):
  remote_by_name = _index_remote_assets(remote_assets)
  issues = []
  missing = sorted(set(artifacts_by_name) - set(remote_by_name))
  extra = sorted(set(remote_by_name) - set(artifacts_by_name))
  issues.extend(f'missing {name}' for name in missing)
  issues.extend(f'unexpected {name}' for name in extra)
  for name in sorted(set(artifacts_by_name) & set(remote_by_name)):
    local = artifacts_by_name[name]
    remote = remote_by_name[name]
    if remote.get('state') != 'uploaded':
      issues.append(f'{name}: state={remote.get("state")!r}')
    if remote.get('size') != local['size']:
      issues.append(f'{name}: size={remote.get("size")!r}, expected={local["size"]}')
    if remote.get('digest') != local['digest']:
      issues.append(
          f'{name}: digest={remote.get("digest")!r}, expected={local["digest"]}'
      )
  if issues:
    raise RuntimeError('Draft Skia release asset verification failed:\n  ' + '\n  '.join(issues))


def stage_and_publish_release_artifacts(artifacts, version):
  """Stage a complete set in a draft, verify it, then make it public."""
  require_public_release_approval()
  if not isinstance(version, str) or RELEASE_VERSION_PATTERN.fullmatch(version) is None:
    raise RuntimeError(f'Invalid Skia release version: {version!r}')
  artifacts = [Path(artifact) for artifact in artifacts]
  if not artifacts:
    raise RuntimeError('No Skia release artifacts were provided.')
  missing = [str(artifact) for artifact in artifacts if not artifact.is_file()]
  if missing:
    raise RuntimeError('Missing Skia release artifacts:\n  ' + '\n  '.join(missing))
  symlinks = [str(artifact) for artifact in artifacts if artifact.is_symlink()]
  if symlinks:
    raise RuntimeError('Symlinked Skia release artifacts:\n  ' + '\n  '.join(symlinks))
  empty = [str(artifact) for artifact in artifacts if artifact.stat().st_size == 0]
  if empty:
    raise RuntimeError('Empty Skia release artifacts:\n  ' + '\n  '.join(empty))

  artifact_paths_by_name = {artifact.name: artifact for artifact in artifacts}
  if len(artifact_paths_by_name) != len(artifacts):
    raise RuntimeError('Skia release artifact names must be unique.')
  artifacts_by_name = {
      name: validate_zip_artifact(artifact)
      for name, artifact in sorted(artifact_paths_by_name.items())
  }

  revision = common.current_revision()
  if not isinstance(revision, str) or FULL_COMMIT_PATTERN.fullmatch(revision) is None:
    raise RuntimeError(f'Skia release revision is not a full lowercase commit SHA: {revision!r}')
  if not version.endswith('-' + revision[:10]):
    raise RuntimeError(
        f'Skia release version {version!r} does not identify commit {revision}'
    )

  headers = common.github_headers()
  release_record = _get_or_create_draft_release(version, headers)
  if not isinstance(release_record, dict):
    raise RuntimeError('GitHub release lookup did not return an object.')
  if release_record.get('tag_name') != version:
    raise RuntimeError('GitHub release tag does not match the requested Skia version.')
  if release_record.get('target_commitish') != revision:
    raise RuntimeError(
        'GitHub release target commit does not match the exact Skia source revision.'
    )
  release_id = release_record.get('id')
  if type(release_id) is not int or release_id <= 0:
    raise RuntimeError(f'GitHub release has an invalid id: {release_id!r}')
  remote_assets = _list_release_assets(release_id, headers)

  if not release_record.get('draft', False):
    _validate_remote_assets(artifacts_by_name, remote_assets)
    print('Skia release is already public with the complete verified asset set.')
    return

  remote_by_name = _index_remote_assets(remote_assets)
  unexpected = sorted(set(remote_by_name) - set(artifacts_by_name))
  if unexpected:
    raise RuntimeError(
        'Draft Skia release contains unexpected assets; refusing to delete them:\n  '
        + '\n  '.join(unexpected)
    )

  raw_upload_url = release_record.get('upload_url')
  if not isinstance(raw_upload_url, str):
    raise RuntimeError('Draft GitHub release has no upload URL.')
  upload_url = raw_upload_url.split('{', 1)[0]
  parsed_upload_url = urllib.parse.urlparse(upload_url)
  if parsed_upload_url.scheme != 'https' or parsed_upload_url.hostname != 'uploads.github.com':
    raise RuntimeError(f'Draft GitHub release has an unsafe upload URL: {upload_url!r}')
  for name, artifact in sorted(artifacts_by_name.items()):
    existing = remote_by_name.get(name)
    if existing is not None:
      if (
          existing.get('state') == 'uploaded'
          and existing.get('size') == artifact['size']
          and existing.get('digest') == artifact['digest']
      ):
        print('Keeping verified draft asset', name)
        continue
      existing_id = existing.get('id')
      if type(existing_id) is not int or existing_id <= 0:
        raise RuntimeError(f'Draft asset {name} has an invalid id: {existing_id!r}')
      _delete_release_asset(existing_id, headers)
    _upload_release_asset(upload_url, artifact['path'], headers)

  staged_assets = _list_release_assets(release_id, headers)
  _validate_remote_assets(artifacts_by_name, staged_assets)
  published = _publish_draft_release(release_id, headers)
  if (
      not isinstance(published, dict)
      or published.get('draft', True)
      or published.get('tag_name') != version
      or published.get('target_commitish') != revision
  ):
    raise RuntimeError('GitHub did not publish the fully staged Skia release.')


def main():
  require_public_release_approval()
  raise RuntimeError(
      'Single-asset release is disabled. Use release_artifacts.py so the full '
      'matrix is staged and verified before publication.'
  )


if __name__ == '__main__':
  sys.exit(main())
