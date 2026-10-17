# Version control — how this repo is organised

Written 2026-10-17 after a clean-up. Part 1 is plain language. Part 2 is the
exact rules every Claude session follows. Part 3 is what was cleaned up.

---

## Part 1 — In plain words

### Where the code lives

There are three kinds of copies. Only one of them is the truth.

| Copy | What it is | Is it production? |
|---|---|---|
| **GitHub `main`** | The master copy on github.com | **Yes.** Railway deploys every change pushed here to clinicos.store within ~2 minutes |
| **Your main folder** `Desktop/ClinicOS` | Your copy on this Mac, on `main` | Only after it is updated (`git pull`) |
| **Session folders** `ClinicOS/.claude/worktrees/<name>` | One per Claude session, each on its own branch `claude/<name>` | Never directly — work is shipped from here to GitHub `main` |

A **branch** is a label for one line of work. A **worktree** is a separate
folder showing one branch, so a Claude session can work without touching your
main folder.

### What went wrong before

- **Copies drifted apart.** A session folder was 96 changes behind production,
  your main folder was 14 behind GitHub, and nobody noticed, because nothing
  syncs on its own.
- **History got tangled.** Sessions kept merging old copies of `main` into their
  branches. That made it impossible to ship a branch cleanly, and once hid an
  old duplicate of the phone-layout change inside a branch.
- **Leftovers piled up.** Eight old branches, an abandoned session folder, and a
  forgotten "stash" (a parked set of edits) from July.
- **Each session folder carried stale copies of its settings**: Python tools
  pointing at the wrong folder, an old `.env`. Things that "worked" were
  sometimes running different code from what was being edited.

### How it works now

1. **GitHub `main` is the single source of truth**, and it is what is live.
2. Every session **starts from the latest `main`** and adds its work on top in a
   straight line, so shipping is one clean step.
3. Shipping happens **only when you ask**: full tests first, then a straight
   push to `main`, then a check of the live site and logs.
4. After shipping, **your main folder is updated**, so it matches production.
5. `git pull` now **refuses** to quietly mix diverged copies, and branches
   deleted on GitHub disappear locally too.
6. **Nothing unshipped is ever deleted.** Work that wasn't finished is kept on a
   branch named `archive/…`, backed up to GitHub.

### Archived work you may want to look at someday

| Branch | What it holds |
|---|---|
| `archive/2026-05-phone-lookup-trial-banner` | Unfinished May work: typing a phone number shows whether the patient already exists; a "trial upgrade" banner on the dashboard. Production has since gained its own returning-patient badge, so this may be superseded. |
| `archive/2026-07-stash-prescription-templates` | Early versions of the prescription pages, parked in July. Production has newer versions; ~90% of these lines already exist there. |

Neither is live. Ask a session to compare either with production if you want
something from it.

---

## Part 2 — Rules for every Claude session

**Truth.** `origin/main` = production (Railway service `Clincos`,
https://www.clinicos.store). Local `main` is only trustworthy right after
`git pull --ff-only`.

**Start of session.**
1. `git fetch origin`; `git rev-list --left-right --count origin/main...HEAD`.
2. Behind → `git merge --ff-only origin/main`. If it refuses and the branch
   holds nothing unshipped (`git diff origin/main HEAD` empty, nothing
   uncommitted) → `git reset --hard origin/main`. Never `git merge main` into a
   session branch.
3. Full suite green before changing code: `./venv/bin/python -m pytest tests/ -q`.

**Worktree traps.**
- `venv/bin/pip`, `venv/bin/uvicorn` have shebangs pointing at the main folder's
  Python. Always `./venv/bin/python -m pip|uvicorn|pytest`.
- The worktree `.env` is a stale copy; compare with the main folder's `.env`.
  Delete any `.env*.bak` immediately — `.gitignore` covers `.env` only.
- Preview server: launch config `clinicos-r2-worktree` (auto-reloads). The
  `clinicos` config runs the main folder, not the worktree. Read the startup
  line `document vault: … area dev/ — reachable` before trusting a browser test.

**Shipping** (only when asked).
1. Linear on top of `origin/main`: `git merge-base --is-ancestor origin/main HEAD`.
   If tangled: scratch worktree from `origin/main`, cherry-pick only your commits.
2. Full suite green on that exact tree → `git push origin HEAD:main`.
   Fast-forward only, never force.
3. `railway deployment list --service Clincos --json` until SUCCESS for your
   commit; `railway logs --service Clincos` must show
   `document vault: Cloudflare R2 (bucket medical-billing-bucket, area production top level) — reachable`
   and no Traceback.
4. `git -C /Users/apple/Desktop/ClinicOS pull --ff-only`.

**Data safety.**
- `.env` holds live keys; `tests/conftest.py` blanks Resend, Razorpay and R2
  for the whole suite. Keep it.
- Non-production environments use `dev/` in the R2 bucket
  (`storage_service.namespace()`); never build object keys another way.
- Resend's limit is ~100 emails/day — test workflows, not emails.

**Housekeeping.**
- Never delete `archive/*` without asking.
- Delete a branch only when `git merge-tree --write-tree origin/main <branch>`
  equals `git rev-parse origin/main^{tree}`. `git cherry` alone misses edits
  made inside merge commits.
- A worktree belonging to a live session (see the app's session list) is not
  removed, even if idle.

---

## Part 3 — Clean-up log, 2026-10-17

Every branch was checked by simulating a merge into production. Only branches
where that merge would change **nothing** were deleted.

| Item | Action |
|---|---|
| 5 old branches (`elastic-beaver`, `frosty-ride`, `gallant-vaughan`, `heuristic-wilbur`, `quirky-mirzakhani`) | Deleted — fully in production, no session uses them |
| `frosty-rosalind` folder + branch | Its session no longer existed. 241 lines of uncommitted work saved to `archive/2026-05-phone-lookup-trial-banner` (verified identical), then folder and branch removed |
| July stash | Saved to `archive/2026-07-stash-prescription-templates`, then dropped |
| `origin/claude/romantic-sanderson-91c6d5` on GitHub | Deleted — stale and fully in production |
| Both `archive/*` branches | Pushed to GitHub as a backup (scanned for keys first: none) |
| `production-issues` folder + branch | **Kept** — belongs to a live (idle) session. Its work is fully in production; archive that session in the app when you no longer need it |
| Your main folder | Updated from 3 behind to matching production; its Python environment was missing the new libraries, now installed |
| Repo settings | `pull.ff=only`, `fetch.prune=true` |
