/*
 * Copyright 2018 Google LLC
 *
 * Use of this source code is governed by a BSD-style license that can be
 * found in the LICENSE file.
 */
#ifndef load_icu_DEFINED
#define load_icu_DEFINED

// The MSVC Windows build links ICU's stub data and loads icudtl.dat from disk at runtime, so it
// needs a real loader. The MinGW build instead compiles the data assembly straight into the
// binary, the way every non-Windows target does, which keeps a Kotlin/Native executable
// self-contained — so there is nothing to load.
#if defined(_WIN32) && !defined(__MINGW32__) && defined(SK_USING_THIRD_PARTY_ICU)
bool SkLoadICU();
#else
static inline bool SkLoadICU() { return true; }
#endif  // defined(_WIN32) && !defined(__MINGW32__) && defined(SK_USING_THIRD_PARTY_ICU)

#endif  // load_icu_DEFINED
