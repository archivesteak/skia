#! /usr/bin/env python3

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import common


PUBLIC_RELEASE_APPROVAL = 'PUBLISH_PUBLIC_SKIA_RELEASE'


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
  response = urllib.request.urlopen(
      urllib.request.Request(url, data=data, headers=request_headers, method=method)
  )
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
    response = urllib.request.urlopen(
        urllib.request.Request(upload_url + '?' + query, data=data, headers=upload_headers)
    )
    body = response.read()
  return json.loads(body.decode('utf-8')) if body else None


def _publish_draft_release(release_id, headers):
  return _request_json(
      _repo_api_url() + f'/releases/{release_id}',
      headers,
      method='PATCH',
      payload={'draft': False},
  )


def _validate_remote_assets(artifacts_by_name, remote_assets):
  remote_by_name = {asset['name']: asset for asset in remote_assets}
  issues = []
  missing = sorted(set(artifacts_by_name) - set(remote_by_name))
  extra = sorted(set(remote_by_name) - set(artifacts_by_name))
  issues.extend(f'missing {name}' for name in missing)
  issues.extend(f'unexpected {name}' for name in extra)
  for name in sorted(set(artifacts_by_name) & set(remote_by_name)):
    local_size = artifacts_by_name[name].stat().st_size
    remote = remote_by_name[name]
    if remote.get('state') != 'uploaded':
      issues.append(f'{name}: state={remote.get("state")!r}')
    if remote.get('size') != local_size:
      issues.append(f'{name}: size={remote.get("size")!r}, expected={local_size}')
  if issues:
    raise RuntimeError('Draft Skia release asset verification failed:\n  ' + '\n  '.join(issues))


def stage_and_publish_release_artifacts(artifacts, version):
  """Stage a complete set in a draft, verify it, then make it public."""
  require_public_release_approval()
  artifacts = [Path(artifact) for artifact in artifacts]
  if not artifacts:
    raise RuntimeError('No Skia release artifacts were provided.')
  missing = [str(artifact) for artifact in artifacts if not artifact.is_file()]
  if missing:
    raise RuntimeError('Missing Skia release artifacts:\n  ' + '\n  '.join(missing))
  empty = [str(artifact) for artifact in artifacts if artifact.stat().st_size == 0]
  if empty:
    raise RuntimeError('Empty Skia release artifacts:\n  ' + '\n  '.join(empty))

  artifacts_by_name = {artifact.name: artifact for artifact in artifacts}
  if len(artifacts_by_name) != len(artifacts):
    raise RuntimeError('Skia release artifact names must be unique.')

  headers = common.github_headers()
  release_record = _get_or_create_draft_release(version, headers)
  if release_record.get('tag_name') != version:
    raise RuntimeError('GitHub release tag does not match the requested Skia version.')
  release_id = release_record['id']
  remote_assets = _list_release_assets(release_id, headers)

  if not release_record.get('draft', False):
    _validate_remote_assets(artifacts_by_name, remote_assets)
    print('Skia release is already public with the complete verified asset set.')
    return

  remote_by_name = {asset['name']: asset for asset in remote_assets}
  unexpected = sorted(set(remote_by_name) - set(artifacts_by_name))
  if unexpected:
    raise RuntimeError(
        'Draft Skia release contains unexpected assets; refusing to delete them:\n  '
        + '\n  '.join(unexpected)
    )

  upload_url = release_record['upload_url'].split('{', 1)[0]
  for name, artifact in sorted(artifacts_by_name.items()):
    existing = remote_by_name.get(name)
    if existing is not None:
      if existing.get('state') == 'uploaded' and existing.get('size') == artifact.stat().st_size:
        print('Keeping verified draft asset', name)
        continue
      _delete_release_asset(existing['id'], headers)
    _upload_release_asset(upload_url, artifact, headers)

  staged_assets = _list_release_assets(release_id, headers)
  _validate_remote_assets(artifacts_by_name, staged_assets)
  published = _publish_draft_release(release_id, headers)
  if published.get('draft', True):
    raise RuntimeError('GitHub did not publish the fully staged Skia release.')


def main():
  require_public_release_approval()
  raise RuntimeError(
      'Single-asset release is disabled. Use release_artifacts.py so the full '
      'matrix is staged and verified before publication.'
  )


if __name__ == '__main__':
  sys.exit(main())
