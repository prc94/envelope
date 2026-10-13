# Making a release candidate

The published runtime is `envelope.pyz`. The version comes from
`envelope/__init__.py`; Python package metadata reads the same value. Use a
prerelease version such as `0.1.0rc1` and an annotated tag such as `v0.1.0rc1`.

1. Update the version and add `docs/releases/<version>.md`. Keep installation
   instructions and known limits accurate; stable acceptance remains independent
   of publishing a release candidate.
2. Run `scripts/dev.sh all` with the Python 3.12 floor interpreter.
3. Build once with `python3.12 scripts/build.py`. The license is embedded in the
   zipapp. Avoid rebuilding after validating it: a new build can have different ZIP
   metadata and therefore a different checksum.
4. Run `python3.12 scripts/verify_release.py dist/envelope.pyz --expected-version
   <version>` on Linux. Copy the exact artifact and verifier to the Windows test VM
   and run them as its standard user. Select `--shell cmd`, `git-bash`, `pwsh` or
   `powershell` for the available Windows shells. The verifier uses isolated Python
   with site packages disabled and tests discovery, both protocol tool catalogs,
   commands, output retrieval, stopping and server-shutdown cleanup. Match the
   reported artifact SHA-256 across hosts.
5. Assemble the verified artifact with `python3.12 scripts/package_release.py`.
   This creates `dist/envelope-<version>/`, a ZIP and a ZIP checksum sidecar.
   The directory contains the zipapp, installation guide, release notes, license
   and a `SHA256SUMS` manifest. Check the manifest from inside that directory.
6. Record validation and the artifact digest in the release notes. If documents
   change after packaging, remove the generated bundle, checksum sidecar and
   bundle directory, then package again from the preserved `dist/envelope.pyz`.
   The packager refuses to overwrite existing output. Generated
   release files live under ignored `dist/`; only source, tooling and notes are
   committed.
7. Commit, create the annotated tag, and push the branch and tag to `forge` with
   ordinary fast-forward pushes. Create a Forgejo release with `prerelease: true`,
   targeting that tag. Attach the ZIP, its checksum sidecar, and the individual
   directory files. The release body comes from the checked-in release notes.
8. Download the published zipapp and ZIP to a fresh directory and compare their
   SHA-256 values with the verified local copies.

Publishing the release page and assets requires a Forgejo API token with repository
write access, or an authenticated web session. SSH Git access alone publishes the
tag and source but does not upload release assets. Keep API credentials outside the
repository and command output.
