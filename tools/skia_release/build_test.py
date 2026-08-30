#! /usr/bin/env python3

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build


class PrepareSkiaCheckoutTest(unittest.TestCase):
  def test_fetch_failure_does_not_modify_toolchain(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      skia_dir = Path(temp_dir)
      toolchain = skia_dir / 'gn' / 'toolchain' / 'BUILD.gn'
      toolchain.parent.mkdir(parents=True)
      original = 'shell = "cmd.exe /v:on /c "\n'
      toolchain.write_text(original, encoding='utf-8')

      with mock.patch.object(build, 'git_sync_with_retries'), mock.patch.object(
          build.subprocess,
          'check_call',
          side_effect=subprocess.CalledProcessError(1, ['fetch-ninja']),
      ):
        with self.assertRaises(subprocess.CalledProcessError):
          build.prepare_skia_checkout(skia_dir)

      self.assertEqual(original, toolchain.read_text(encoding='utf-8'))


class VerifySkikoWindowsX64StaticInputsTest(unittest.TestCase):
  def _write_inputs(self, out, target):
    for name in build.SKIKO_WINDOWS_X64_STATIC_INPUTS:
      if target == 'mingw':
        path = out / f'lib{name}.a'
      else:
        prefix = 'lib' if name in build.MSVC_PREFIXED_STATIC_INPUTS else ''
        path = out / f'{prefix}{name}.lib'
      path.write_bytes(b'archive')

  def test_accepts_complete_mingw_layout(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      out = Path(temp_dir)
      self._write_inputs(out, 'mingw')
      build.verify_skiko_windows_x64_static_inputs(out, 'mingw')

  def test_accepts_exact_msvc_producer_spelling(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      out = Path(temp_dir)
      self._write_inputs(out, 'windows')
      build.verify_skiko_windows_x64_static_inputs(out, 'windows')

  def test_rejects_consumer_normalization_without_raw_msvc_output(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      out = Path(temp_dir)
      self._write_inputs(out, 'windows')
      (out / 'libpng.lib').unlink()
      (out / 'png.lib').write_bytes(b'archive')

      with self.assertRaisesRegex(RuntimeError, 'libpng.lib'):
        build.verify_skiko_windows_x64_static_inputs(out, 'windows')

  def test_reports_every_missing_or_empty_input(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      out = Path(temp_dir)
      self._write_inputs(out, 'mingw')
      (out / 'libskia.a').unlink()
      (out / 'libwebp.a').write_bytes(b'')

      with self.assertRaisesRegex(RuntimeError, 'libskia.a[\\s\\S]*libwebp.a'):
        build.verify_skiko_windows_x64_static_inputs(out, 'mingw')


if __name__ == '__main__':
  unittest.main()
