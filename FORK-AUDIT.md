# Skia fork audit

This document records the source and producer review for the MinGW-capable Skia fork. It is an
audit of the fork delta, not a claim that untouched upstream Skia code was rewritten or retested.

## Provenance and reviewed range

- Upstream source base: `95f46ce146df43b17ce28450a2efddf607633f41`
  (`m151-95f46ce146`, a shallow/grafted base in this checkout).
- Reviewed fork range: `95f46ce146..d8481a3cf`.
- Windows raster-pipeline ABI fix: `7753bd05b4aeeb1d5054aab155eb6de72092d654`.
- Deterministic producer/fail-closed release tooling: `4094b3be1`, `6bcb26f69`, and
  `d8481a3cf`.
- Partition Allocator pin: fork commit `b2b6b07755defa7c608615a5720d48ed1a5ea040`,
  based directly on upstream `b1d0141bcecfda2bfd108882d818fc5df70ae5c7`.

The review covered every changed hunk in these files:

- `BUILD.gn`, `gn/BUILDCONFIG.gn`, `gn/portable/BUILD.gn`, `gn/skia/BUILD.gn`,
  `gn/toolchain/BUILD.gn`, `modules/skcms/BUILD.gn`, `third_party/third_party.gni`, and
  `third_party/zlib/BUILD.gn`;
- `src/gpu/vk/vulkanmemoryallocator/BUILD.gn`, `src/opts/SkRasterPipeline_opts.h`,
  `src/ports/SkFontMgr_win_dw.cpp`, `src/ports/SkScalerContext_win_dw.cpp`,
  `src/xps/SkXPSDevice.cpp`, and `include/private/SkAttributes.h`;
- `third_party/icu/BUILD.gn`, `third_party/icu/SkLoadICU.h`, `DEPS`, and the pinned
  Partition Allocator fork delta;
- `.github/workflows/build_for_skiko.yml` and all changed/new files under
  `tools/skia_release`.

## Architecture findings

### MinGW target isolation

- `is_mingw` selects the GCC-like toolchain while retaining `target_os="win"`. MSVC discovery,
  `/`-style compiler flags, `.lib` names, and MSVC-only SDK configuration are guarded with
  `!is_mingw`; Windows source selection and Win32 APIs remain enabled.
- The producer supports only `x64` for MinGW and fails explicitly for any other architecture.
  Compiler, archive, and target-triple values are configuration inputs; there is no fixed checkout,
  SDK, user-home, or output path in the producer.
- MinGW uses `-femulated-tls` to match the Kotlin/Native runtime and embeds ICU data as generated
  C++ instead of depending on a separately deployed `icudtl.dat`.
- DirectWrite compatibility changes only compensate for mingw-w64 header spelling/constexpr
  differences. The XPS friend definition is standard linkage rather than an MSVC extension.
- The Partition Allocator fork omits MSVC/UCRT allocator-shim sources under `is_mingw` and avoids
  duplicate declspec-template tests under `__MINGW32__`. Skiko intentionally links
  `raw_ptr`, `allocator_core`, and `allocator_base`, not the global allocator override archive.

### CPU ABI correctness

- Windows x64 uses `__vectorcall` narrow raster-pipeline stages under both clang-cl and
  clang/MinGW. The previous System V wide-stage assumption corrupted non-trivial stage arguments
  under the Windows ABI.
- Causality was established before committing: the formerly crashing Blender/raster tests passed
  after the ABI change, and the complete Skiko MinGW native suite subsequently passed.

### GPU target boundaries

- MinGW builds OpenGL, Direct3D, and Vulkan; MSVC Windows builds Direct3D and Vulkan. These match
  the shared Skiko Windows GPU implementations.
- Apple Vulkan enablement from the earlier experimental commit was removed in `7c4e4f32f`; Apple
  targets remain Metal-only. Linux and Android keep their upstream-appropriate Vulkan settings.
- Vulkan Memory Allocator warning flags use GCC spelling only for MinGW. No Apple/MSVC configuration
  is changed by that guard.

### Producer output contract

- The release producer checks the exact 20 Windows x64 static inputs declared by Skiko. MinGW must
  emit `lib<name>.a`; MSVC must emit the four raw `libpng/libjpeg/libwebp/libwebp_sse41` spellings
  plus the unprefixed GN outputs. Empty files fail the build and all missing files are reported
  together.
- The root wrappers independently verify their consumer layouts after MSVC normalization; MinGW
  additionally verifies its generated `libd3d12.a` import archive.
- The x64 scope is intentional because the declared set contains the SSE4.1 WebP archive. It does
  not impose an x64 archive contract on the separate MSVC ARM64 workflow.
- `tools/skia_release/build.py` no longer rewrites tracked GN source. Delayed expansion is an
  explicit `gn/toolchain/BUILD.gn` input, and forced preparation failure plus real MinGW/MSVC builds
  leave that file unchanged.
- The shared FreeType policy is applied only on targets that actually declare the GN argument, so
  Windows builds are warning-free without changing Linux, Apple, Android, or Wasm semantics.

### Publication freeze and supply chain

- Ordinary workflow jobs have only `contents: read`. Their only output route is Actions artifacts
  with an explicit five-day retention.
- All eight third-party Actions are pinned to immutable commits verified against their official
  tag refs: checkout v4.4.0, upload-artifact v4.6.2, download-artifact v4.3.0, setup-python v5.6.0,
  setup-msbuild v1.3.3, msvc-dev-cmd v1.13.0, setup-ndk v1.6.0, and docker-run v3.
- Every checkout disables persisted Git credentials. The separate Skia test workflow now has an
  explicit read-only token and immutable action pins as well. Linux release containers use exact
  amd64/arm64 Ubuntu image-manifest digests, and generic Ubuntu runner labels are versioned.
- A durable GitHub Release requires manual dispatch, `should_release=true`, and the exact separate
  approval phrase. Only the approval-gated release job receives `contents: write`; the uploader
  rejects the same missing/incorrect approval before coordinate parsing or network access.
- Publication requires the exact 32-artifact workflow matrix. Every archive is opened, checked for
  duplicate/unsafe/encrypted/symlink entries, and fully decompressed for CRC verification before
  network access. Assets are uploaded sequentially to a draft release, then names, sizes,
  GitHub-provided SHA-256 digests, and `uploaded` states are re-read and compared before the release
  is made public. Upload or verification failure leaves a non-public draft.
- The release version must be `m<milestone>-<10-char-source-SHA>`; manual dispatch takes precedence
  over branch naming and both forms are validated. The draft and final release records must target
  the exact full source commit. API and upload requests use finite timeouts, and upload URLs are
  restricted to GitHub's HTTPS upload host.
- Retries keep matching uploaded assets, replace only stale same-name draft assets, reject unknown
  extras, and publish only after the complete set verifies. An already-public complete release is
  treated idempotently; an incomplete public release fails closed. Single-asset publication is
  disabled.
- No Maven, Maven Central, GitHub Packages, Pages, Docker push, signing, or hidden
  `continue-on-error` route exists in the reviewed Skia producer/workflow delta.

## Verification evidence

- Skia MinGW release regeneration/build: 170 affected targets rebuilt successfully after the GN
  toolchain change; final exact wrapper rerun completed with no work and no ineffective-argument
  warning.
- Skia MSVC release regeneration/build: 9 affected targets rebuilt successfully; final exact
  wrapper rerun completed with no work and no ineffective-argument warning.
- Both builds satisfied the raw 20-archive producer contract and left
  `gn/toolchain/BUILD.gn` byte-for-byte unchanged. The root MinGW wrapper also verified
  `libd3d12.a`; the MSVC wrapper verified all 20 normalized names.
- `tools.skia_release.build_test` and `tools.skia_release.release_test`: 33 tests passed with
  `ResourceWarning` promoted to an error. Coverage includes forced preparation failure,
  missing/empty archive aggregation, exact raw MSVC names, immutable Action pins, approval gates,
  the exact 32-artifact matrix, ZIP integrity/path/symlink rejection, source/target identity,
  remote digest mismatch, finite network timeouts, draft-only failure behavior, retry cleanup, and
  refusal to publish after incomplete verification.
- `actionlint` v1.7.7 passed on both Skia workflows; Python byte-compilation, YAML parsing,
  `git diff --check`, and mutable-Action/remote-route scans passed.
- Skiko `mingwX64Test` against this producer output: 286 tests in 57 suites, 0 failures, 0 errors,
  with 5 inherited skips. PNG/JPEG/WebP round trips, truncated/invalid inputs, setjmp/longjmp,
  backend selection, and the Windows emoji semantic oracle are covered in the Skiko audit.

## Host-bound coverage limits

- This Windows host cannot compile Apple targets. The current source audit confirms Apple Vulkan
  is absent and no MinGW guard leaks into Apple configuration; Apple compilation remains an Apple
  CI responsibility.
- The local MSVC wrapper covers the x64 libraries consumed by the tested JVM runtime. The pinned
  workflow retains its upstream ARM64 and Graphite/Dawn matrix, but those configurations were not
  rebuilt locally during this Windows x64 audit.
- Existing TODO/FIXME comments found in audited files are upstream comments outside fork-changed
  hunks; no fork-owned placeholder, ignored failure, silent target fallback, or test exclusion was
  added or retained in the reviewed delta.
