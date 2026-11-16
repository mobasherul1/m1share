# M1share

**Offline LAN file sharing — no internet, no upload, no sign-up.**

M1share lets you share folders with devices on the same Wi-Fi / LAN. The sender picks a folder, M1share shows a local share link and a 6-letter share code, and receivers on the same network open the files and download them — Single file or All files. No ZIP.

Created by [Md. Mobasherul Hasan](https://bd.linkedin.com/in/mobasherul1).

Live site: https://m1share.netlify.app/

## What is in this repo right now

**UI only, for now.**

- `sender/index.html` — Sender / Companion page, v48 clean (STABLE design, 11 Nov 2026)
- `receiver/index.html` — Receiver page, v14 clean (STABLE design, 11 Nov 2026)
- `logos/` — M1share logo files
- `docs/design-notes-and-how-it-works.txt` — Ultra-detailed design notes and how every function works (666 lines)

These two pages are **design previews**. Open them in a browser to see the exact UI. Downloads in these previews are simulated — they are not the working file server yet.

The working Windows `.exe` source — Md.'s rebuild from zero — will be added here later, as a separate commit, when he sends the zip. That is when the repo will get a real release and a GitHub Actions build.

## How sharing works (when the exe is ready)

1. Sender opens M1share, picks a folder to share
2. M1share auto-detects the local IP and generates a 6-letter share code
3. Receiver on the same Wi-Fi opens the share link, enters the code if one is set, and sees Open files
4. Receiver downloads a Single file, or All files in order — main folder files first, then sub-folder files. No ZIP anywhere.
5. If there is no login code, the receiver page shows no login-code area at all — just Open files.

## Code signing policy

See [CODE-SIGNING-POLICY.md](CODE-SIGNING-POLICY.md).

Free code signing provided by SignPath.io, certificate by SignPath Foundation — *planned, once the exe source is in this repo, released, and built by GitHub Actions. No signed exe is available yet.*

## License

MIT — see [LICENSE](LICENSE).
