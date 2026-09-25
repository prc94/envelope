# Windows encoding fixtures (spike S6, 2026-09-25)

Real captured bytes from a Windows 10 21H2 (build 19044) VM, OEM/ANSI code page 437/1252
(en-US locale), used to verify `envelope/output/decode.py` against actual tool output
rather than synthetic byte strings. Captured via a raw `subprocess.run(..., capture_output=True)`
in Python — the same thing envelope itself does (a redirected pipe, not a console).

- `cmd_oem_no_chcp.bin` / `cmd_utf8_chcp65001.bin` — `cmd /d /c echo Größe 42 & echo zweite Zeile`,
  without and with a `chcp 65001` in the same cmd invocation. **Identical bytes, both CP437** —
  confirms the known limitation `launch.py`'s nested-cmd trick works around: a single cmd
  process does not re-decode text already in its own command line after `chcp`, regardless of
  redirection. `envelope` never emits this exact bug because it always uses the nested-cmd form
  (see D14); this fixture documents *why*.
- `powershell51_pipe_default.bin` / `pwsh7_pipe_default.bin` — `Write-Output "Größe 42"` with
  stdout redirected to a pipe, Windows PowerShell 5.1 and PowerShell 7, **no** envelope-side
  UTF-8 setup. Both CP437, matching cmd. Confirms envelope's `console_codepage` decode fallback
  is necessary for both PowerShells, not just legacy 5.1, and validates why S1's nested-cmd +
  `chcp 65001` launch mechanism is required to get UTF-8 out of either of them.
- `powershell51_outfile_unicode.bin` — `Write-Output "Größe 42" | Out-File -Encoding Unicode`,
  a real UTF-16LE+BOM file as Windows PowerShell 5.1 actually writes it.
- `reg_export_utf16.bin` — `reg export HKCU\Environment` to a real file (truncated to 200 bytes);
  Windows' own `.reg` export format, independently confirming real tools commonly emit
  UTF-16LE+BOM to files (not to redirected pipes — `cmd /c type` on a UTF-16 file was also
  tried and found to transcode to the console code page rather than pass bytes through, so a
  pipe-native UTF-16 sample from a real tool was not obtainable this way).

All are CP437-encoded ("ö" = `0x94`, "ß" = `0xe1`) except the two `_unicode`/`_utf16` files.
