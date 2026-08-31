#! /usr/bin/env python3

import hashlib
import os
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release
import release_artifacts


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / '.github' / 'workflows' / 'build_for_skiko.yml'
TEST_WORKFLOW_PATH = REPO_ROOT / '.github' / 'workflows' / 'tests_for_skiko.yml'


def write_zip(path, contents=b'archive'):
  with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
    archive.writestr('include/SkiaFixture.h', contents)
  return path


class PublicReleaseApprovalTest(unittest.TestCase):
  def test_missing_approval_fails_closed(self):
    with mock.patch.dict(os.environ, {}, clear=True):
      with self.assertRaisesRegex(RuntimeError, 'release is frozen'):
        release.require_public_release_approval()

  def test_main_rejects_before_parsing_coordinates_or_touching_network(self):
    with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
        release.common,
        'version',
    ) as version:
      with self.assertRaisesRegex(RuntimeError, 'release is frozen'):
        release.main()
      version.assert_not_called()

  def test_incorrect_approval_fails_closed(self):
    with mock.patch.dict(
        os.environ,
        {'SKIA_PUBLIC_RELEASE_APPROVAL': 'true'},
        clear=True,
    ):
      with self.assertRaisesRegex(RuntimeError, 'release is frozen'):
        release.require_public_release_approval()

  def test_exact_approval_passes(self):
    with mock.patch.dict(
        os.environ,
        {'SKIA_PUBLIC_RELEASE_APPROVAL': release.PUBLIC_RELEASE_APPROVAL},
        clear=True,
    ):
      release.require_public_release_approval()


class WorkflowPublicationFreezeTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.workflow = WORKFLOW_PATH.read_text(encoding='utf-8')
    _, separator, cls.release_job = cls.workflow.partition('\n  release:\n')
    if not separator:
      raise AssertionError('release job not found')

  def test_default_token_is_read_only(self):
    self.assertIn('permissions:\n  contents: read\n', self.workflow)
    self.assertEqual(1, self.workflow.count('contents: write'))
    self.assertIn('    permissions:\n      contents: write\n', self.release_job)

  def test_all_actions_artifacts_expire_after_five_days(self):
    uploads = len(
        re.findall(r'uses: actions/upload-artifact@[0-9a-f]{40}', self.workflow)
    )
    self.assertEqual(6, uploads)
    self.assertEqual(uploads, self.workflow.count('retention-days: 5'))

  def test_every_third_party_action_is_pinned_to_an_immutable_sha(self):
    uses = re.findall(r'^\s*- uses: ([^\s#]+)', self.workflow, flags=re.MULTILINE)
    self.assertTrue(uses)
    for action in uses:
      self.assertRegex(action, r'^[^@]+@[0-9a-f]{40}$')

    expected_pins = {
        'actions/checkout': '11d5960a326750d5838078e36cf38b85af677262',
        'actions/upload-artifact': 'ea165f8d65b6e75b540449e92b4886f43607fa02',
        'actions/download-artifact': 'd3f86a106a0bac45b974a628896c90dbdf5c8093',
        'actions/setup-python': 'a26af69be951a213d495a4c3e4e4022e16d87065',
        'microsoft/setup-msbuild': 'ede762b26a2de8d110bb5a3db4d7e0e080c0e917',
        'ilammy/msvc-dev-cmd': '0b201ec74fa43914dc39ae48a89fd1d8cb592756',
        'nttld/setup-ndk': 'ed92fe6cadad69be94a966a7ee3271275e62f779',
        'addnab/docker-run-action': '4f65fabd2431ebc8d299f8e5a018d79a769ae185',
    }
    for action, sha in expected_pins.items():
      self.assertIn(f'{action}@{sha}', uses)

  def test_all_checkouts_drop_persisted_git_credentials(self):
    checkout_count = self.workflow.count('uses: actions/checkout@')
    self.assertEqual(8, checkout_count)
    self.assertEqual(checkout_count, self.workflow.count('persist-credentials: false'))

  def test_container_images_are_pinned_to_platform_digests(self):
    self.assertNotIn('ubuntu-latest', self.workflow)
    self.assertEqual(
        3,
        self.workflow.count(
            'container: ubuntu:20.04@sha256:'
        ),
    )
    self.assertIn('image: arm64v8/ubuntu:20.04@sha256:', self.workflow)
    self.assertNotRegex(self.workflow, r'(?m)^\s+(?:container|image): [^\n@]+:[^\n@]+$')

  def test_test_workflow_actions_are_pinned_and_checkout_is_credentialless(self):
    workflow = TEST_WORKFLOW_PATH.read_text(encoding='utf-8')
    self.assertIn('permissions:\n  contents: read\n', workflow)
    uses = re.findall(r'^\s*uses: ([^\s#]+)', workflow, flags=re.MULTILINE)
    self.assertTrue(uses)
    for action in uses:
      self.assertRegex(action, r'^[^@]+@[0-9a-f]{40}$')
    self.assertEqual(1, workflow.count('persist-credentials: false'))

  def test_only_approval_gated_job_can_invoke_release_uploader(self):
    self.assertNotIn('tools/skia_release/release_artifacts.py', self.workflow.split('\n  release:\n')[0])
    self.assertEqual(1, self.release_job.count('tools/skia_release/release_artifacts.py'))
    self.assertIn("release_approved == 'true'", self.release_job)
    self.assertIn('SKIA_PUBLIC_RELEASE_APPROVAL:', self.release_job)

  def test_workflow_requires_exact_manual_approval_phrase(self):
    self.assertIn('GITHUB_EVENT_NAME" != "workflow_dispatch', self.workflow)
    self.assertIn(
        'INPUT_RELEASE_APPROVAL" != "PUBLISH_PUBLIC_SKIA_RELEASE',
        self.workflow,
    )
    dispatch_check = self.workflow.index('if [[ "${GITHUB_EVENT_NAME}" == "workflow_dispatch"')
    branch_check = self.workflow.index('elif [[ "${GITHUB_REF_NAME}" == skiko-m*')
    self.assertLess(dispatch_check, branch_check)
    self.assertIn('^m[1-9][0-9]*$', self.workflow)


class ReleaseArtifactsTest(unittest.TestCase):
  VERSION = 'm151-deadbeef00'

  def _write_expected_matrix(self, artifact_dir, omitted=()):
    names = release_artifacts.expected_release_artifact_names(self.VERSION) - set(omitted)
    for name in names:
      write_zip(artifact_dir / name, name.encode('utf-8'))
    return names

  def test_expected_matrix_has_all_32_build_outputs(self):
    self.assertEqual(
        32,
        len(release_artifacts.expected_release_artifact_names(self.VERSION)),
    )

  def test_collects_and_sorts_complete_matrix(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      artifact_dir = Path(temp_dir)
      names = self._write_expected_matrix(artifact_dir)

      artifacts = release_artifacts.collect_release_artifacts(
          artifact_dir,
          self.VERSION,
      )
      self.assertEqual(sorted(names), [artifact.name for artifact in artifacts])

  def test_rejects_unexpected_artifact_before_upload(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      artifact_dir = Path(temp_dir)
      self._write_expected_matrix(artifact_dir)
      write_zip(artifact_dir / f'Skia-{self.VERSION}-windows-Release-x64-unsigned.zip')

      with self.assertRaisesRegex(RuntimeError, 'unexpected[\\s\\S]*unsigned.zip'):
        release_artifacts.collect_release_artifacts(artifact_dir, self.VERSION)

  def test_rejects_non_archive_and_nested_repository_entries(self):
    for condition in ('file', 'directory'):
      with self.subTest(condition=condition), tempfile.TemporaryDirectory() as temp_dir:
        artifact_dir = Path(temp_dir)
        self._write_expected_matrix(artifact_dir)
        unexpected = artifact_dir / 'unexpected'
        if condition == 'file':
          unexpected.write_text('unexpected\n', encoding='utf-8')
        else:
          unexpected.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'unexpected repository entry unexpected'):
          release_artifacts.collect_release_artifacts(artifact_dir, self.VERSION)

  def test_rejects_missing_matrix_artifact_before_upload(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      artifact_dir = Path(temp_dir)
      missing = f'Skia-{self.VERSION}-windows-Release-x64.zip'
      self._write_expected_matrix(artifact_dir, omitted=(missing,))

      with self.assertRaisesRegex(RuntimeError, f'missing {re.escape(missing)}'):
        release_artifacts.collect_release_artifacts(artifact_dir, self.VERSION)

  def test_rejects_corrupt_zip_before_upload(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      artifact_dir = Path(temp_dir)
      names = self._write_expected_matrix(artifact_dir)
      corrupt = artifact_dir / sorted(names)[0]
      data = bytearray(corrupt.read_bytes())
      data[len(data) // 2] ^= 0xFF
      corrupt.write_bytes(data)

      with self.assertRaisesRegex(RuntimeError, 'Invalid Skia release ZIP|CRC'):
        release_artifacts.collect_release_artifacts(artifact_dir, self.VERSION)

  def test_rejects_unsafe_and_symlink_zip_entries(self):
    for condition in ('traversal', 'symlink'):
      with self.subTest(condition=condition), tempfile.TemporaryDirectory() as temp_dir:
        artifact = Path(temp_dir) / 'Skia-unsafe.zip'
        with zipfile.ZipFile(artifact, 'w') as archive:
          if condition == 'traversal':
            archive.writestr('../escape.txt', b'escape')
          else:
            entry = zipfile.ZipInfo('include/link.h')
            entry.create_system = 3
            entry.external_attr = 0o120777 << 16
            archive.writestr(entry, b'../../outside')
        with self.assertRaisesRegex(RuntimeError, 'unsafe entry|contains a symlink'):
          release.validate_zip_artifact(artifact)


class ReleaseApiTest(unittest.TestCase):
  VERSION = 'm151-deadbeef00'
  REVISION = 'deadbeef00' + '1' * 30

  def _artifact(self, directory, name, contents=b'archive'):
    artifact = Path(directory) / name
    return write_zip(artifact, contents)

  def _approval(self):
    return mock.patch.dict(
        os.environ,
        {'SKIA_PUBLIC_RELEASE_APPROVAL': release.PUBLIC_RELEASE_APPROVAL},
        clear=True,
    )

  def test_non_404_lookup_failure_is_not_treated_as_missing_release(self):
    error = release.urllib.error.HTTPError(
        'https://api.github.test/releases/tags/m151-deadbeef00',
        500,
        'server error',
        None,
        None,
    )
    self.addCleanup(error.close)
    with mock.patch.object(
        release.common,
        'github_repo',
        return_value='archivesteak/skia',
    ), mock.patch.object(release, '_request_json', side_effect=error) as request:
      with self.assertRaises(release.urllib.error.HTTPError):
        release._find_release(self.VERSION, {})
      self.assertEqual(1, request.call_count)

  def test_new_release_is_created_as_draft(self):
    with mock.patch.object(
        release.common,
        'github_repo',
        return_value='archivesteak/skia',
    ), mock.patch.object(
        release.common,
        'current_revision',
        return_value=self.REVISION,
    ), mock.patch.object(
        release,
        '_request_json',
        return_value={'id': 7, 'draft': True},
    ) as request:
      release._create_draft_release(self.VERSION, {})
      self.assertTrue(request.call_args.kwargs['payload']['draft'])
      self.assertEqual(self.REVISION, request.call_args.kwargs['payload']['target_commitish'])

  def test_failed_upload_leaves_draft_unpublished(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      artifact = self._artifact(temp_dir, f'Skia-{self.VERSION}-windows-Release-x64.zip')
      draft = {
          'id': 7,
          'tag_name': self.VERSION,
          'target_commitish': self.REVISION,
          'draft': True,
          'upload_url': 'https://uploads.github.com/assets{?name,label}',
      }
      with self._approval(), mock.patch.object(
          release.common,
          'current_revision',
          return_value=self.REVISION,
      ), mock.patch.object(
          release.common,
          'github_headers',
          return_value={},
      ), mock.patch.object(
          release,
          '_get_or_create_draft_release',
          return_value=draft,
      ), mock.patch.object(
          release,
          '_list_release_assets',
          return_value=[],
      ), mock.patch.object(
          release,
          '_upload_release_asset',
          side_effect=RuntimeError('upload failed'),
      ), mock.patch.object(release, '_publish_draft_release') as publish:
        with self.assertRaisesRegex(RuntimeError, 'upload failed'):
          release.stage_and_publish_release_artifacts([artifact], self.VERSION)
        publish.assert_not_called()

  def test_retry_resumes_verified_assets_and_replaces_only_stale_asset(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      first = self._artifact(temp_dir, f'Skia-{self.VERSION}-linux-Release-x64.zip', b'first')
      second = self._artifact(temp_dir, f'Skia-{self.VERSION}-windows-Release-x64.zip', b'second')
      draft = {
          'id': 7,
          'tag_name': self.VERSION,
          'target_commitish': self.REVISION,
          'draft': True,
          'upload_url': 'https://uploads.github.com/assets{?name,label}',
      }
      initial_assets = [
          {
              'id': 11,
              'name': first.name,
              'state': 'uploaded',
              'size': first.stat().st_size,
              'digest': 'sha256:' + hashlib.sha256(first.read_bytes()).hexdigest(),
          },
          {'id': 12, 'name': second.name, 'state': 'starter', 'size': 0},
      ]
      verified_assets = [
          {
              'id': 11,
              'name': first.name,
              'state': 'uploaded',
              'size': first.stat().st_size,
              'digest': 'sha256:' + hashlib.sha256(first.read_bytes()).hexdigest(),
          },
          {
              'id': 13,
              'name': second.name,
              'state': 'uploaded',
              'size': second.stat().st_size,
              'digest': 'sha256:' + hashlib.sha256(second.read_bytes()).hexdigest(),
          },
      ]
      with self._approval(), mock.patch.object(
          release.common,
          'current_revision',
          return_value=self.REVISION,
      ), mock.patch.object(
          release.common,
          'github_headers',
          return_value={},
      ), mock.patch.object(
          release,
          '_get_or_create_draft_release',
          return_value=draft,
      ), mock.patch.object(
          release,
          '_list_release_assets',
          side_effect=(initial_assets, verified_assets),
      ), mock.patch.object(release, '_delete_release_asset') as delete, mock.patch.object(
          release,
          '_upload_release_asset',
      ) as upload, mock.patch.object(
          release,
          '_publish_draft_release',
          return_value={
              'draft': False,
              'tag_name': self.VERSION,
              'target_commitish': self.REVISION,
          },
      ) as publish:
        release.stage_and_publish_release_artifacts([first, second], self.VERSION)
        delete.assert_called_once_with(12, {})
        upload.assert_called_once_with('https://uploads.github.com/assets', second, {})
        publish.assert_called_once_with(7, {})

  def test_incomplete_post_upload_verification_never_publishes(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      artifact = self._artifact(temp_dir, f'Skia-{self.VERSION}-windows-Release-x64.zip')
      draft = {
          'id': 7,
          'tag_name': self.VERSION,
          'target_commitish': self.REVISION,
          'draft': True,
          'upload_url': 'https://uploads.github.com/assets{?name,label}',
      }
      with self._approval(), mock.patch.object(
          release.common,
          'current_revision',
          return_value=self.REVISION,
      ), mock.patch.object(
          release.common,
          'github_headers',
          return_value={},
      ), mock.patch.object(
          release,
          '_get_or_create_draft_release',
          return_value=draft,
      ), mock.patch.object(
          release,
          '_list_release_assets',
          side_effect=([], []),
      ), mock.patch.object(release, '_upload_release_asset'), mock.patch.object(
          release,
          '_publish_draft_release',
      ) as publish:
        with self.assertRaisesRegex(RuntimeError, 'missing'):
          release.stage_and_publish_release_artifacts([artifact], self.VERSION)
        publish.assert_not_called()

  def test_wrong_remote_digest_never_publishes(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      artifact = self._artifact(temp_dir, f'Skia-{self.VERSION}-windows-Release-x64.zip')
      draft = {
          'id': 7,
          'tag_name': self.VERSION,
          'target_commitish': self.REVISION,
          'draft': True,
          'upload_url': 'https://uploads.github.com/assets{?name,label}',
      }
      staged = [{
          'id': 11,
          'name': artifact.name,
          'state': 'uploaded',
          'size': artifact.stat().st_size,
          'digest': 'sha256:' + '0' * 64,
      }]
      with self._approval(), mock.patch.object(
          release.common,
          'current_revision',
          return_value=self.REVISION,
      ), mock.patch.object(
          release.common,
          'github_headers',
          return_value={},
      ), mock.patch.object(
          release,
          '_get_or_create_draft_release',
          return_value=draft,
      ), mock.patch.object(
          release,
          '_list_release_assets',
          side_effect=([], staged),
      ), mock.patch.object(release, '_upload_release_asset'), mock.patch.object(
          release,
          '_publish_draft_release',
      ) as publish:
        with self.assertRaisesRegex(RuntimeError, 'digest='):
          release.stage_and_publish_release_artifacts([artifact], self.VERSION)
        publish.assert_not_called()

  def test_release_target_mismatch_fails_before_asset_calls(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      artifact = self._artifact(temp_dir, f'Skia-{self.VERSION}-windows-Release-x64.zip')
      wrong_target = {
          'id': 7,
          'tag_name': self.VERSION,
          'target_commitish': '2' * 40,
          'draft': True,
          'upload_url': 'https://uploads.github.com/assets{?name,label}',
      }
      with self._approval(), mock.patch.object(
          release.common,
          'current_revision',
          return_value=self.REVISION,
      ), mock.patch.object(
          release.common,
          'github_headers',
          return_value={},
      ), mock.patch.object(
          release,
          '_get_or_create_draft_release',
          return_value=wrong_target,
      ), mock.patch.object(release, '_list_release_assets') as assets:
        with self.assertRaisesRegex(RuntimeError, 'target commit'):
          release.stage_and_publish_release_artifacts([artifact], self.VERSION)
        assets.assert_not_called()

  def test_invalid_release_version_fails_before_source_or_network_inspection(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      artifact = self._artifact(temp_dir, 'Skia-invalid.zip')
      with self._approval(), mock.patch.object(
          release.common,
          'current_revision',
      ) as revision, mock.patch.object(
          release.common,
          'github_headers',
      ) as headers:
        with self.assertRaisesRegex(RuntimeError, 'Invalid Skia release version'):
          release.stage_and_publish_release_artifacts(
              [artifact],
              'dev-deadbeef00',
          )
        revision.assert_not_called()
        headers.assert_not_called()

  def test_http_requests_have_finite_timeouts(self):
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = b'{}'
    with mock.patch.object(release.urllib.request, 'urlopen', return_value=response) as open_url:
      self.assertEqual({}, release._request_json('https://api.github.com/test', {}))
      self.assertEqual(release.API_TIMEOUT_SECONDS, open_url.call_args.kwargs['timeout'])

    with tempfile.TemporaryDirectory() as temp_dir:
      artifact = self._artifact(temp_dir, 'Skia-timeout.zip')
      response = mock.MagicMock()
      response.__enter__.return_value.read.return_value = b'{}'
      with mock.patch.object(
          release.urllib.request,
          'urlopen',
          return_value=response,
      ) as open_url:
        self.assertEqual(
            {},
            release._upload_release_asset('https://uploads.github.com/assets', artifact, {}),
        )
        self.assertEqual(release.UPLOAD_TIMEOUT_SECONDS, open_url.call_args.kwargs['timeout'])


if __name__ == '__main__':
  unittest.main()
