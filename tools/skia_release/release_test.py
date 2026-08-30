#! /usr/bin/env python3

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release
import release_artifacts


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / '.github' / 'workflows' / 'build_for_skiko.yml'


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
    uploads = self.workflow.count('uses: actions/upload-artifact@v4')
    self.assertEqual(6, uploads)
    self.assertEqual(uploads, self.workflow.count('retention-days: 5'))

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


class ReleaseArtifactsTest(unittest.TestCase):
  def test_collects_and_sorts_valid_matrix_artifacts(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      artifact_dir = Path(temp_dir)
      names = [
          'Skia-m151-deadbeef00-android-Debug-arm64.zip',
          'Skia-m151-deadbeef00-windows-Release-x64.zip',
      ]
      for name in reversed(names):
        (artifact_dir / name).write_bytes(b'archive')

      artifacts = release_artifacts.collect_release_artifacts(
          artifact_dir,
          'm151-deadbeef00',
      )
      self.assertEqual(names, [artifact.name for artifact in artifacts])

  def test_rejects_unexpected_artifact_before_upload(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      artifact_dir = Path(temp_dir)
      (artifact_dir / 'Skia-m151-deadbeef00-windows-Release-x64.zip').write_bytes(b'archive')
      (artifact_dir / 'Skia-m151-deadbeef00-windows-Release-x64-unsigned.zip').write_bytes(
          b'archive'
      )

      with self.assertRaisesRegex(RuntimeError, 'Unexpected[\\s\\S]*unsigned.zip'):
        release_artifacts.collect_release_artifacts(artifact_dir, 'm151-deadbeef00')

  def test_duplicate_assets_fail_before_any_upload(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      artifact = Path(temp_dir) / 'Skia-m151-deadbeef00-windows-Release-x64.zip'
      artifact.write_bytes(b'archive')
      release_record = {
          'upload_url': 'https://uploads.github.test/assets{?name,label}',
          'assets': [{'name': artifact.name}],
      }
      with mock.patch.dict(
          os.environ,
          {'SKIA_PUBLIC_RELEASE_APPROVAL': release.PUBLIC_RELEASE_APPROVAL},
          clear=True,
      ), mock.patch.object(release.common, 'github_headers', return_value={}), mock.patch.object(
          release,
          '_get_or_create_release',
          return_value=release_record,
      ), mock.patch.object(release.urllib.request, 'urlopen') as urlopen:
        with self.assertRaisesRegex(RuntimeError, 'already exist'):
          release.upload_release_artifacts([artifact], 'm151-deadbeef00')
        urlopen.assert_not_called()


class ReleaseApiTest(unittest.TestCase):
  def test_non_404_lookup_failure_is_not_treated_as_missing_release(self):
    error = release.urllib.error.HTTPError(
        'https://api.github.test/releases/tags/m151-deadbeef00',
        500,
        'server error',
        None,
        None,
    )
    with mock.patch.object(
        release.common,
        'github_repo',
        return_value='archivesteak/skia',
    ), mock.patch.object(release.urllib.request, 'urlopen', side_effect=error) as urlopen:
      with self.assertRaises(release.urllib.error.HTTPError):
        release._get_or_create_release('m151-deadbeef00', {})
      self.assertEqual(1, urlopen.call_count)

  def test_404_lookup_creates_release_at_current_revision(self):
    missing = release.urllib.error.HTTPError(
        'https://api.github.test/releases/tags/m151-deadbeef00',
        404,
        'not found',
        None,
        None,
    )
    created = mock.Mock()
    created.read.return_value = b'{"upload_url":"https://uploads.github.test/assets{?name,label}"}'
    with mock.patch.object(
        release.common,
        'github_repo',
        return_value='archivesteak/skia',
    ), mock.patch.object(
        release.common,
        'current_revision',
        return_value='deadbeef00',
    ), mock.patch.object(
        release.urllib.request,
        'urlopen',
        side_effect=(missing, created),
    ) as urlopen:
      record = release._get_or_create_release('m151-deadbeef00', {})
      self.assertEqual(
          'https://uploads.github.test/assets{?name,label}',
          record['upload_url'],
      )
      self.assertEqual(2, urlopen.call_count)
      create_request = urlopen.call_args_list[1].args[0]
      self.assertIn(b'"target_commitish": "deadbeef00"', create_request.data)


if __name__ == '__main__':
  unittest.main()
