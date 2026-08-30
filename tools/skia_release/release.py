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


def _get_or_create_release(version, headers):
  releases_url = 'https://api.github.com/repos/' + common.github_repo() + '/releases'

  try:
    response = urllib.request.urlopen(
        urllib.request.Request(releases_url + '/tags/' + version, headers=headers)
    ).read()
  except urllib.error.HTTPError as error:
    if error.code != 404:
      raise
    data = json.dumps({
        'tag_name': version,
        'name': version,
        'target_commitish': common.current_revision(),
    })
    response = urllib.request.urlopen(
        urllib.request.Request(releases_url, data=data.encode('utf-8'), headers=headers)
    ).read()

  return json.loads(response.decode('utf-8'))


def upload_release_artifacts(artifacts, version):
  require_public_release_approval()
  artifacts = [Path(artifact) for artifact in artifacts]
  if not artifacts:
    raise RuntimeError('No Skia release artifacts were provided.')
  missing = [str(artifact) for artifact in artifacts if not artifact.is_file()]
  if missing:
    raise RuntimeError('Missing Skia release artifacts:\n  ' + '\n  '.join(missing))

  headers = common.github_headers()
  release_record = _get_or_create_release(version, headers)
  existing_assets = {asset['name'] for asset in release_record.get('assets', [])}
  duplicates = [artifact.name for artifact in artifacts if artifact.name in existing_assets]
  if duplicates:
    raise RuntimeError('Skia release assets already exist:\n  ' + '\n  '.join(duplicates))

  upload_url = release_record['upload_url'].split('{', 1)[0]
  for artifact in artifacts:
    print('Uploading', artifact.name, 'to', upload_url)
    upload_headers = dict(headers)
    upload_headers['Content-Type'] = 'application/zip'
    upload_headers['Content-Length'] = str(artifact.stat().st_size)
    query = urllib.parse.urlencode({'name': artifact.name})
    with artifact.open('rb') as data:
      urllib.request.urlopen(
          urllib.request.Request(upload_url + '?' + query, data=data, headers=upload_headers)
      )


def main():
  require_public_release_approval()
  version = common.version()
  build_type = common.build_type()
  machine = common.machine()
  target = common.target()
  classifier = common.classifier()

  zip_name = 'Skia-' + version + '-' + target + '-' + build_type + '-' + machine + classifier + '.zip'
  zip_path = common.skia_dir() / zip_name
  upload_release_artifacts([zip_path], version)

  return 0


if __name__ == '__main__':
  sys.exit(main())
