# Code signing policy

Free code signing provided by SignPath.io, certificate by SignPath Foundation

## Status

**v1.0.0 source is in this repo — no signed exe yet, no release yet, no SignPath application yet.**

The working Windows `.exe` source is now in this repo: `app.py` v1.0.0 (Python), `M1 Share.spec` (PyInstaller), `M1 Share.version`, `web/` sender + receiver pages, and `.github/workflows/build.yml` which builds the exe on GitHub-hosted `windows-latest` runners and smoke-tests it. The sender / receiver UI designs (sender v48, receiver v14, STABLE) are also in this repo.

No binaries are signed yet, and no signing request will be submitted until a v1.0.0 release is tagged and the project is approved by SignPath Foundation.

## What will be signed

- Only M1 Share's own Windows `.exe`, built from this public repo's source code.
- Only artifacts built by GitHub Actions on GitHub-hosted runners, from a tagged release of this repo.
- No locally-built binaries will ever be submitted for signing.
- No binaries from other projects will be signed with this subscription.

## Team roles

- **Authors / Committers:** Md. Mobasherul Hasan — trusted to modify source code in this repo.
- **Reviewers:** Md. Mobasherul Hasan — each change from a non-committer (e.g. a pull request) must be reviewed by a team member.
- **Approvers:** Md. Mobasherul Hasan — each signing request must be approved by a team member trusted by the entire team to decide if a certain release can be code signed.

All team members use multi-factor authentication for both SignPath and GitHub access.

## Privacy policy

This program will not transfer any information to other networked systems unless specifically requested by the user or the person installing or operating it.

M1share shares files only on the local network (LAN / Wi-Fi) between the sender's device and receivers on the same network. It does not upload files to the internet.

## Conditions

- The project uses the MIT License (OSI-approved), with no commercial dual-licensing, for all components.
- The project contains no proprietary, non open-source component.
- The project contains no malware or potentially unwanted programs, and no hacking tools.
- The project is actively maintained and will be released in the form that should be signed.
- The project's functionality is described on its download / release page and in this README.
