# Forgejo upstream security overlay

The complete Go scan exposed provider hostname patterns with unescaped dots and archive writes which could escape through entry names or symlinks. This overlay is anchored to the reviewed Forgejo v13.1.0 commit.

CopyCollector uses Go's os.Root for directory creation and file writes, verifies local entry and link paths, and preserves internal links. The relative local-action link path also rejects traversal. Provider patterns match complete, correctly escaped hostnames. Regressions cover traversal entries, malicious and existing outside symlinks, retained internal links and provider lookalikes.

Apply this overlay after native events and admission when preparing analysis or a runner build. Run the existing provider URL tests plus the three security regression groups. This source correction alone does not replace a live runner binary; release artifacts need their own build/provenance and controlled installation.

The common Windows/Linux/macOS build script now applies this overlay in both
native-events and guarded modes, tests it, and records its hashes. CI builds
all six platform targets in each mode; CodeQL analyzes the same guarded
source composition.
