#! /usr/bin/env python3

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import common


SKIKO_WINDOWS_X64_STATIC_INPUTS = (
    'skia',
    'skia_ganesh_ext',
    'svg',
    'skparagraph',
    'skshaper',
    'skunicode_core',
    'skunicode_icu',
    'icu',
    'harfbuzz',
    'skresources',
    'png',
    'jpeg',
    'webp',
    'webp_sse41',
    'zlib',
    'expat',
    'd3d12allocator',
    'raw_ptr',
    'allocator_core',
    'allocator_base',
)

# These third-party GN targets deliberately retain their Unix-style `lib`
# prefix under MSVC. Skiko's archive extraction strips it for consumers.
MSVC_PREFIXED_STATIC_INPUTS = frozenset(('png', 'jpeg', 'webp', 'webp_sse41'))


def git_sync_with_retries(skia_dir, max_retries=3, backoff_seconds=5):
  attempt = 0
  while True:
    try:
      print("> Running tools/git-sync-deps (attempt {}/{})".format(attempt + 1, max_retries + 1))
      subprocess.check_call([sys.executable, "tools/git-sync-deps"], cwd=skia_dir)
      print("Success")
      return
    except subprocess.CalledProcessError as error:
      attempt += 1
      if attempt > max_retries:
        print("All {} retries failed. Giving up.".format(max_retries))
        raise
      wait = backoff_seconds * attempt
      print(f"Failed (exit {error.returncode}), retrying in {wait}s...")
      time.sleep(wait)


def prepare_skia_checkout(skia_dir):
  git_sync_with_retries(skia_dir)

  print("> Fetching ninja")
  subprocess.check_call([sys.executable, "bin/fetch-ninja"], cwd=skia_dir)


def verify_skiko_windows_x64_static_inputs(out, target):
  """Fail when a Windows x64 Skia build cannot satisfy Skiko's nativeInputs."""
  out = Path(out)
  missing = []
  for name in SKIKO_WINDOWS_X64_STATIC_INPUTS:
    if target == 'mingw':
      candidates = (out / f'lib{name}.a',)
    elif target == 'windows':
      prefix = 'lib' if name in MSVC_PREFIXED_STATIC_INPUTS else ''
      candidates = (out / f'{prefix}{name}.lib',)
    else:
      raise ValueError(f'Unsupported Windows Skia target: {target}')

    if not any(path.is_file() and path.stat().st_size > 0 for path in candidates):
      missing.append(candidates[0].name)

  if missing:
    raise RuntimeError(
        'Skiko Windows static input contract is incomplete in '
        + str(out)
        + ':\n  '
        + '\n  '.join(missing)
    )


def ninja_path(host):
  return os.path.join('third_party', 'ninja', 'ninja.exe' if host == 'windows' else 'ninja')


def main():
  skia_dir = common.skia_dir()
  os.chdir(skia_dir)
  target = common.target()
  prepare_skia_checkout(skia_dir)

  build_type = common.build_type()
  machine = common.machine()
  host = common.host()
  ndk = common.ndk()
  gpu_as_extension = common.gpu_as_extension()
  enable_ganesh = common.enable_ganesh()
  enable_graphite = common.enable_graphite()
  enable_graphite_dawn = common.enable_graphite_dawn()

  ninja = ninja_path(host)
  is_ios = target in ('ios', 'iosSim')
  is_tvos = target in ('tvos', 'tvosSim')
  is_ios_sim = target == 'iosSim'
  is_tvos_sim = target == 'tvosSim'
  is_macos = target == 'macos'

  if build_type == 'Debug':
    args = ['is_debug=true']
  else:
    args = ['is_official_build=true']

  args += [
      'target_cpu="' + machine + '"',
      'skia_use_system_expat=false',
      'skia_use_system_libjpeg_turbo=false',
      'skia_use_system_libpng=false',
      'skia_use_system_libwebp=false',
      'skia_use_system_zlib=false',
      'skia_use_system_harfbuzz=false',
      'skia_pdf_subset_harfbuzz=true',
      'skia_use_system_icu=false',
      'skia_enable_skottie=true',
      'extra_cflags=[]',
      'extra_cflags_cc=[]',
  ]

  # The DirectWrite Windows configurations do not load FreeType's GN file, so
  # this argument is undeclared there. Keep the upstream bundled-FreeType
  # policy on every target that consumes it without emitting a false warning
  # for either Windows ABI.
  if target not in ('windows', 'mingw'):
    args += ['skia_use_system_freetype2=false']

  if target == 'windows':
    args += ['extra_cflags+=["/clang:-fvisibility=default"]']
  else:
    args += ['extra_cflags+=["-fvisibility=default"]']

  if is_ios or is_tvos:
    args += [
        'skia_icu_data_filter="//third_party/externals/icu/filters/ios.json"',
        'skia_icu_data_filter_patch="//third_party/icu/skiko_ios/filter.patch"'
    ]

  if is_macos or is_ios or is_tvos:
    if is_macos:
      args += ['skia_use_fonthost_mac=true']
      if enable_graphite_dawn:
        args += ['dawn_enable_metal=true']
    args += ['extra_cflags_cc+=["-frtti"]']
    args += ['skia_use_metal=true']
    if is_ios:
      args += ['target_os="ios"']
      if is_ios_sim:
        args += ['ios_use_simulator=true']
        args += ['extra_cflags+=["-mios-simulator-version-min=12.0"]']
      else:
        args += ['ios_min_target="12.0"']
    elif is_tvos:
      args += ['target_os="tvos"']
      if is_tvos_sim:
        args += ['ios_use_simulator=true']
        args += ['extra_cflags+=["-mtvos-simulator-version-min=14", "-DSK_BUILD_FOR_TVOS"]']
      else:
        args += ['extra_cflags+=["-mtvos-version-min=14", "-DSK_BUILD_FOR_TVOS"]']
    elif machine == 'arm64':
      args += ['extra_cflags+=["-stdlib=libc++"]']
    else:
      args += ['extra_cflags+=["-stdlib=libc++", "-mmacosx-version-min=10.13"]']
  elif target == 'linux':
    args += ['skia_use_vulkan=true']
    if machine == 'arm64':
      args += [
          'skia_gl_standard="gles"',
          'skia_use_egl=true',
          'extra_cflags_cc+=["-fno-exceptions", "-fno-rtti", "-D_GLIBCXX_USE_CXX11_ABI=0", "-mno-outline-atomics"]',
          'cc="gcc-10"',
          'cxx="g++-10"',
      ]
    else:
      args += [
          'extra_cflags_cc+=["-fno-exceptions", "-fno-rtti","-D_GLIBCXX_USE_CXX11_ABI=0"]',
          'cc="gcc-10"',
          'cxx="g++-10"',
      ]
  elif target == 'windows':
    if enable_graphite_dawn:
      args += ['dawn_enable_d3d11=true', 'dawn_enable_d3d12=true']
    args += [
        'skia_use_direct3d=true',
        # Same three backends the MinGW build carries, so the shared GPU core has the same Skia
        # underneath it whichever toolchain built the library.
        'skia_use_vulkan=true',
        'extra_cflags+=["-DSK_FONT_HOST_USE_SYSTEM_SETTINGS"]',
    ]
    if host == 'windows':
      clang_path = shutil.which('clang-cl.exe')
      if not clang_path:
        raise Exception(
          "Please install LLVM from https://releases.llvm.org/, "
            "and make sure that clang-cl.exe is available in PATH"
        )
      args += [
          'clang_win="' + os.path.dirname(os.path.dirname(clang_path)) + '"',
          'is_trivial_abi=false',
      ]
  elif target == 'mingw':
    # Kotlin/Native mingwX64-compatible build: llvm-mingw (clang in GCC mode,
    # GNU ABI) instead of clang-cl/MSVC.
    #
    # All three GPU backends are built. Vulkan and Direct3D need no MSVC SDK:
    # the Vulkan headers and the two memory allocators are vendored under
    # third_party/externals, and mingw-w64 has shipped d3d12.h/dxgi1_6.h for
    # years. Neither backend is gated on !is_mingw in GN — only on
    # skia_enable_ganesh — so they build as they do for any other Windows host.
    if machine != 'x64':
      raise ValueError('The mingw target is supported only for x64 (Kotlin/Native mingwX64)')
    triple = 'x86_64-w64-mingw32'
    args += [
        'is_mingw=true',
        'target_os="win"',
        'skia_use_vulkan=true',
        'skia_use_direct3d=true',
        'cc="clang"',
        'cxx="clang++"',
        'ar="llvm-ar"',
        # mingw-w64 defaults to Win7 (0x0601); DirectWrite and partition_alloc
        # need Win10 APIs.
        'extra_cflags+=["--target=' + triple + '", "-D_WIN32_WINNT=0x0A00", "-DWINVER=0x0A00"]',
        # clang defaults to native TLS on Windows, but the MinGW libstdc++
        # (both llvm-mingw's and the Kotlin/Native sysroot's) is built with
        # emulated TLS, so e.g. std::__once_call only exists in its
        # __emutls_v.* form there. Match the runtime's TLS model.
        'extra_cflags+=["-femulated-tls"]',
        'extra_ldflags=["--target=' + triple + '"]',
    ]
  elif target == 'android':
    args += [
        'ndk="' + ndk + '"',
        'skia_use_vulkan=true',
    ]
  elif target == 'wasm':
    if enable_graphite_dawn:
      args += ['skia_use_webgpu=true']
    args += [
        'skia_use_dng_sdk=false',
        'skia_use_freetype=true',
        'skia_use_freetype_woff2=true',
        'skia_use_libjpeg_turbo_decode=true',
        'skia_use_libjpeg_turbo_encode=true',
        'skia_use_libpng_decode=true',
        'skia_use_libpng_encode=true',
        'skia_use_libwebp_decode=true',
        'skia_use_libwebp_encode=true',
        'skia_use_wuffs=true',
        'skia_use_lua=false',
        'skia_use_webgl=true',
        'skia_use_piex=false',
        'skia_use_system_libpng=false',
        'skia_use_system_libjpeg_turbo=false',
        'skia_use_system_libwebp=false',
        'skia_enable_tools=false',
        'skia_enable_fontmgr_custom_directory=false',
        'skia_enable_fontmgr_custom_embedded=true',
        'skia_enable_fontmgr_custom_empty=true',
        'skia_use_webgl=true',
        'skia_gl_standard="webgl"',
        'skia_use_gl=true',
        'skia_enable_svg=true',
        'skia_use_expat=true',
        'extra_cflags+=["-DSK_SUPPORT_GPU=1", "-DSK_GL", "-DSK_DISABLE_LEGACY_SHADERCONTEXT", "-sSUPPORT_LONGJMP=wasm"]',
        'extra_cflags_cc+=["-std=c++20"]',
    ]

  if gpu_as_extension:
    args += ['skia_gpu_as_extension=true']
  if not enable_ganesh:
    args += ['skia_enable_ganesh=false']
  if enable_graphite or enable_graphite_dawn:
    args += ['skia_enable_graphite=true']
  if enable_graphite_dawn:
    args += ['skia_use_dawn=true']

  out = os.path.join('out', build_type + '-' + target + '-' + machine)
  gn = 'gn.exe' if host == 'windows' else 'gn'
  gn_cmd = [os.path.join('bin', gn), 'gen', out, '--args=' + ' '.join(args)]
  subprocess.check_call(gn_cmd)
  ninja_targets = ['skia', 'modules']
  if gpu_as_extension:
    if enable_ganesh:
        ninja_targets.append('skia_ganesh_ext')
    if enable_graphite:
        ninja_targets.append('skia_graphite_ext')
    if enable_graphite_dawn:
        ninja_targets.append('skia_graphite_dawn_ext')

  subprocess.check_call([ninja, '-C', out] + ninja_targets)
  if machine == 'x64' and target in ('windows', 'mingw'):
    verify_skiko_windows_x64_static_inputs(out, target)
  return 0


if __name__ == '__main__':
  sys.exit(main())
