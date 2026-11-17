Bundled text-to-speech engine for EmergencyAlertarr
====================================================

flite-x86_64 / flite-aarch64 are unmodified CMU Flite 2.2 (Carnegie Mellon
University, Language Technologies Institute), built as fully static Linux
binaries (musl libc, built with `zig cc`) so they run inside the Dispatcharr
container without installing anything.

Build configuration (not a source change): a voice list containing only
US English + the cmu_us_kal, cmu_us_kal16, cmu_us_rms and cmu_us_slt voices
(config/eas.lv), configured with --with-audio=none --disable-shared.

Source: Ubuntu archive flite_2.2.orig.tar.gz. License: see FLITE-COPYING
(BSD-style; free to use and redistribute with the copyright notice kept).
